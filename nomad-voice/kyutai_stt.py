"""KyutaiSTTService — Pipecat STT that transcribes each utterance via the nomad-stt GPU sidecar
(Kyutai streaming STT over WebSocket).

A drop-in for WhisperSTTService: same SegmentedSTTService seam — the pipeline VAD buffers a speech
segment, run_stt() transcribes it. Barge-in still comes from the pipeline VAD; this only makes the
transcript faster and more accurate than CPU Whisper. Fail-soft: if the sidecar is unreachable,
run_stt yields an ErrorFrame and the turn is simply missed, so keep NOMAD_STT_ENGINE=whisper as the
default until the sidecar is confirmed up.
"""
import asyncio
import json

import numpy as np
import websockets
from pipecat.frames.frames import ErrorFrame, TranscriptionFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601


def _resample_i16(audio: bytes, sr_in: int, sr_out: int) -> bytes:
    """Linear-interpolate 16-bit mono PCM from sr_in to sr_out (16k→24k for Kyutai's Mimi codec).
    Linear is plenty for speech STT; Mimi is robust to it."""
    if sr_in == sr_out or not audio:
        return audio
    x = np.frombuffer(audio, dtype=np.int16).astype(np.float32)
    if x.size < 2:
        return audio
    n_out = max(1, int(round(x.size * sr_out / sr_in)))
    fp = np.linspace(0.0, x.size - 1, n_out, dtype=np.float32)
    y = np.interp(fp, np.arange(x.size, dtype=np.float32), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


class KyutaiSTTService(SegmentedSTTService):
    def __init__(self, *, url="ws://127.0.0.1:8212/stt", target_sample_rate=24000,
                 language=Language.EN, **kwargs):
        super().__init__(**kwargs)
        self._url = url
        self._target_sr = target_sample_rate
        self._language = language

    @property
    def wants_wav_segments(self) -> bool:
        return False   # we want raw 16-bit PCM, not a WAV container

    async def run_stt(self, audio: bytes):
        pcm = _resample_i16(audio, self.sample_rate, self._target_sr)
        pieces = []
        try:
            async with websockets.connect(self._url, max_size=None) as ws:
                chunk = int(self._target_sr * 0.04) * 2   # ~40 ms of int16 per send
                for i in range(0, len(pcm), chunk):
                    await ws.send(pcm[i:i + chunk])
                await ws.send(json.dumps({"type": "flush"}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if msg.get("text"):
                        pieces.append(msg["text"])
                    if msg.get("type") == "flushed" or msg.get("error"):
                        break
        except Exception as e:   # noqa: BLE001 — a dead sidecar must not crash the voice pipeline
            yield ErrorFrame(f"nomad-stt unreachable: {str(e)[:160]}")
            return
        text = "".join(pieces).strip()
        if text:
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), self._language)
