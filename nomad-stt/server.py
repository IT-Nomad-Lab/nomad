"""nomad-stt — Kyutai STT on the GPU, streamed over WebSocket.

A GPU sidecar for the voice pipeline. It loads a Kyutai STT model (delayed-streams-modeling,
PyTorch) once and streams transcription: send it 24 kHz mono PCM, get text pieces back the instant
they emit. This is the low-latency, barge-in-friendly ear that replaces CPU Whisper — the LLM brain
and Piper TTS in nomad-voice stay exactly as they are.

Runs host-native on the RTX GPU (torch cu128, Blackwell sm_120 verified). The voice service reaches
it at ws://127.0.0.1:8212/stt.

  WS /stt   binary in = 24 kHz mono int16 PCM (any chunk size)
            text in  = {"type":"flush"} drain trailing text · {"type":"reset"} new utterance
            out      = {"text": "<piece>"} as pieces emit · {"type":"flushed"} after a flush
  GET /health
"""
import asyncio
import json
import os
import sys

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from moshi.models import LMGen, loaders

HF_REPO = os.environ.get("KYUTAI_STT_REPO", "kyutai/stt-1b-en_fr")   # 1B, ~0.5s delay, en/fr
DEVICE = os.environ.get("KYUTAI_DEVICE", "cuda")
HOST = os.environ.get("NOMAD_STT_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOMAD_STT_PORT", "8212"))   # 8200 voice · 8210 scraper · 8212 stt
_SKIP = (0, 3)   # pad / special text tokens — never spoken


class StreamingSTT:
    """One loaded model, one streaming state (batch 1). Fine for a personal assistant: one speaker
    at a time. A new connection resets the state rather than running concurrent streams."""

    def __init__(self, repo=HF_REPO, device=DEVICE, dtype=torch.bfloat16):
        self.device = device
        ci = loaders.CheckpointInfo.from_hf_repo(repo)
        self.mimi = ci.get_mimi(device=device)
        self.tokenizer = ci.get_text_tokenizer()
        self.lm = ci.get_moshi(device=device, dtype=dtype)
        self.frame_size = int(self.mimi.sample_rate / self.mimi.frame_rate)   # 1920 samples = 80 ms
        self.sample_rate = int(self.mimi.sample_rate)                         # 24000
        self.audio_delay = float(ci.stt_config.get("audio_delay_seconds", 0.5))
        self.lm_gen = LMGen(self.lm, cfg_coef=1.0, condition_tensors={}, **ci.lm_gen_config)
        self.mimi.streaming_forever(1)
        self.lm_gen.streaming_forever(1)
        self._first = True

    def reset(self):
        self.mimi.reset_streaming()
        self.lm_gen.reset_streaming()
        self._first = True

    @torch.no_grad()
    def push(self, frame_f32: np.ndarray) -> str:
        """Feed exactly one frame (frame_size float32 samples, mono, 24 kHz). Returns the text piece
        emitted this step, or "" (most steps emit nothing; text lags audio by ~audio_delay)."""
        chunk = torch.from_numpy(frame_f32).to(self.device).view(1, 1, -1)
        codes = self.mimi.encode(chunk)
        if self._first:
            # feed the first slice twice so the transformer sees it (else it's masked by init tokens)
            self.lm_gen.step(codes)
            self._first = False
        tokens = self.lm_gen.step(codes)
        if tokens is None:
            return ""
        tid = int(tokens[0, 0].item())
        if tid in _SKIP:
            return ""
        return self.tokenizer.id_to_piece(tid).replace("▁", " ")

    @torch.no_grad()
    def flush(self) -> str:
        """Drain trailing text by feeding audio_delay worth of silence (call when the speaker stops)."""
        out, sil = [], np.zeros(self.frame_size, dtype=np.float32)
        for _ in range(int(self.audio_delay * self.sample_rate / self.frame_size) + 2):
            t = self.push(sil)
            if t:
                out.append(t)
        return "".join(out)


stt: StreamingSTT | None = None
_lock = asyncio.Lock()   # one active stream at a time
app = FastAPI(title="nomad-stt")


@app.get("/health")
async def health():
    return {"status": "ok" if stt else "loading", "service": "nomad-stt", "repo": HF_REPO,
            "device": DEVICE, "sample_rate": stt.sample_rate if stt else None,
            "frame_size": stt.frame_size if stt else None}


def _to_frames(buf: bytearray, frame_bytes: int):
    """Pop as many whole int16 frames as buffered; return (frames_float32, leftover advanced)."""
    frames = []
    while len(buf) >= frame_bytes:
        raw = bytes(buf[:frame_bytes]); del buf[:frame_bytes]
        frames.append(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)
    return frames


@app.websocket("/stt")
async def stt_ws(ws: WebSocket):
    await ws.accept()
    if stt is None:
        await ws.send_json({"error": "model still loading"}); await ws.close(); return
    if _lock.locked():
        await ws.send_json({"error": "busy — another stream is active"}); await ws.close(); return
    async with _lock:
        stt.reset()
        buf = bytearray()
        frame_bytes = stt.frame_size * 2   # int16
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    buf += msg["bytes"]
                    for frame in _to_frames(buf, frame_bytes):
                        piece = await asyncio.to_thread(stt.push, frame)
                        if piece:
                            await ws.send_json({"text": piece})
                elif msg.get("text"):
                    ctrl = json.loads(msg["text"])
                    if ctrl.get("type") == "flush":
                        tail = await asyncio.to_thread(stt.flush)
                        if tail:
                            await ws.send_json({"text": tail})
                        await ws.send_json({"type": "flushed"})
                    elif ctrl.get("type") == "reset":
                        buf.clear(); stt.reset()
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001 — never let one stream kill the service
            try:
                await ws.send_json({"error": str(e)[:200]})
            except Exception:
                pass


def _load():
    global stt
    print(f"[nomad-stt] loading {HF_REPO} on {DEVICE} …", flush=True)
    stt = StreamingSTT()
    # warm up: the first real utterance must not pay cold-kernel cost (136ms/frame cold vs ~18ms
    # warm on this GPU). Feed silence through the loop, then reset so state is clean.
    sil = np.zeros(stt.frame_size, dtype=np.float32)
    for _ in range(30):
        stt.push(sil)
    if torch.cuda.is_available():
        torch.cuda.synchronize()
    stt.reset()
    print(f"[nomad-stt] ready · {stt.sample_rate} Hz · {stt.frame_size}-sample frames "
          f"· {stt.audio_delay}s delay · warmed", flush=True)


@app.on_event("startup")
async def _startup():
    await asyncio.to_thread(_load)


# ── standalone smoke test: transcribe a wav through the streaming loop (no server) ──
def _smoke(path: str):
    import sphn
    s = StreamingSTT()
    pcm, _ = sphn.read(path, sample_rate=s.sample_rate)   # (channels, samples), float32
    mono = pcm[0]
    pieces = []
    for i in range(0, len(mono) - s.frame_size + 1, s.frame_size):
        t = s.push(np.ascontiguousarray(mono[i:i + s.frame_size]))
        if t:
            pieces.append(t)
    pieces.append(s.flush())
    print("TRANSCRIPT:", "".join(pieces).strip())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        with torch.no_grad():
            _smoke(sys.argv[2])
    else:
        uvicorn.run(app, host=HOST, port=PORT)
