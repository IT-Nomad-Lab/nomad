FROM python:3.11-slim

# espeak-ng: Piper phonemization. ffmpeg: audio decoding for Whisper (webm/ogg/wav).
RUN apt-get update && apt-get install -y --no-install-recommends \
        espeak-ng ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper voice: calm British male — fits the "Jarvis" register. Swap by changing PIPER_MODEL.
RUN mkdir -p /models && \
    base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium && \
    curl -fsSL -o /models/en_GB-alan-medium.onnx       "$base/en_GB-alan-medium.onnx" && \
    curl -fsSL -o /models/en_GB-alan-medium.onnx.json  "$base/en_GB-alan-medium.onnx.json"

# Bake the Whisper model into the image so the first request is fast.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8')"

# Bake the openWakeWord models (shared feature extractors + the 'hey_jarvis' keyword) — local,
# free, no account (the Picovoice/Porcupine replacement).
RUN python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"

COPY server.py .
ENV PIPER_MODEL=/models/en_GB-alan-medium.onnx \
    WHISPER_MODEL=base.en \
    WHISPER_DEVICE=cpu \
    WAKE_MODEL=hey_jarvis
EXPOSE 8200
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8200"]
