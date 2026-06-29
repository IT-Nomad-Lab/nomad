"""NOMAD Voice — local speech service (Piper TTS + faster-whisper STT).

Keeps NOMAD's voice fully on-device: no cloud speech APIs. The console proxies to this
service (which is bound to localhost), the browser only ever talks to the console.

  POST /tts  {text}              -> audio/wav   (Piper neural TTS)
  POST /stt  (file=<audio blob>) -> {text}      (faster-whisper transcription)
  GET  /health

STT runs on CPU int8 by default (reliable on the Blackwell GPU which still needs
CTranslate2/cuDNN compat shaken out); set WHISPER_DEVICE=cuda to try the GPU.
"""
import os
import subprocess
import tempfile

from fastapi import FastAPI, File, Request, UploadFile, WebSocket
from fastapi.responses import JSONResponse, Response

PIPER_MODEL = os.environ.get("PIPER_MODEL", "/models/en_GB-alan-medium.onnx")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_jarvis")          # openWakeWord pretrained keyword
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))

app = FastAPI(title="NOMAD Voice")
_whisper = None


def whisper():
    """Lazy-load the Whisper model (first call warms it; reused after)."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        ct = "int8" if WHISPER_DEVICE == "cpu" else "float16"
        _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=ct)
    return _whisper


@app.get("/health")
def health():
    return {"status": "ok", "tts": "piper", "voice": os.path.basename(PIPER_MODEL),
            "stt": WHISPER_MODEL, "device": WHISPER_DEVICE, "wake": WAKE_MODEL}


def _new_wake_model():
    """A fresh openWakeWord model (ONNX). Created per connection so each listener has clean audio
    state. Pretrained 'hey_jarvis' fits NOMAD's Jarvis identity — local, free, no account."""
    from openwakeword.model import Model
    return Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")


@app.websocket("/wake")
async def wake(websocket: WebSocket):
    """Always-on wake word. The browser streams raw 16 kHz mono int16 PCM; we run openWakeWord per
    1280-sample (80 ms) frame and send {"wake":true,"score":…} the moment 'hey jarvis' crosses the
    threshold (then a brief cooldown so one utterance fires once). Fully on-device."""
    await websocket.accept()
    import numpy as np
    try:
        model = _new_wake_model()
    except Exception as e:
        await websocket.send_json({"error": f"wake model unavailable: {str(e)[:120]}"})
        await websocket.close()
        return
    buf = bytearray()
    cooldown = 0
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data:
                continue
            buf += data
            while len(buf) >= 2560:                    # 1280 samples × 2 bytes
                frame = np.frombuffer(bytes(buf[:2560]), dtype=np.int16)
                del buf[:2560]
                if cooldown > 0:
                    cooldown -= 1
                    continue
                scores = model.predict(frame)
                score = max(scores.values()) if scores else 0.0
                if score >= WAKE_THRESHOLD:
                    await websocket.send_json({"wake": True, "score": round(float(score), 3)})
                    model.reset()
                    cooldown = 25                      # ~2 s before it can fire again
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


def _piper_tts(text: str) -> bytes:
    """Synthesize speech to WAV bytes via the Piper CLI (stable across API churn)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        out = tf.name
    try:
        subprocess.run(["piper", "-m", PIPER_MODEL, "-f", out],
                       input=text, text=True, capture_output=True, timeout=60, check=True)
        with open(out, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


@app.post("/tts")
async def tts(req: Request):
    body = await req.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    try:
        wav = _piper_tts(text[:1200])
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"piper failed: {(e.stderr or '')[:300]}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    return Response(content=wav, media_type="audio/wav")


@app.post("/stt")
async def stt(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return JSONResponse({"error": "empty audio"}, status_code=400)
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        segments, _ = whisper().transcribe(
            path, language="en", vad_filter=True,
            # bias toward NOMAD's vocabulary so the wake word/name transcribes correctly
            initial_prompt="Conversation with NOMAD, the AI assistant. Hey NOMAD.")
        text = "".join(s.text for s in segments).strip()
        return {"text": text}
    except Exception as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=500)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
