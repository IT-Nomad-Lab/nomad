"""Turn downloaded media into things a model can read: keyframes and a transcript.

  frames      ffmpeg, evenly spaced across the clip plus one at 0.3 s (the hook frame, which is
              what a scroller actually sees). 4-12 per video depending on length, 720 px wide.
  transcript  faster-whisper on the CPU (int8). Multilingual with language auto-detect, which is
              why this does not reuse nomad-voice /stt: that one is pinned to English and primed
              with "Hey NOMAD". CPU on purpose: the 5090 is usually full (ComfyUI + detect + stt).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

FFMPEG_DIR = os.environ.get("NOMAD_SOCIAL_FFMPEG_DIR", "/home/linuxbrew/.linuxbrew/bin")
FFMPEG = os.path.join(FFMPEG_DIR, "ffmpeg")
FFPROBE = os.path.join(FFMPEG_DIR, "ffprobe")
WHISPER_MODEL = os.environ.get("NOMAD_SOCIAL_WHISPER", "small")
WHISPER_DEVICE = os.environ.get("NOMAD_SOCIAL_WHISPER_DEVICE", "cpu")
MAX_FRAMES = int(os.environ.get("NOMAD_SOCIAL_MAX_FRAMES", "12"))      # per video
MAX_IMAGES = int(os.environ.get("NOMAD_SOCIAL_MAX_IMAGES", "20"))      # per item, all slides

_whisper = None
_wlock = threading.Lock()


def probe(path: str) -> dict:
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format",
                              "-show_streams", path], capture_output=True, text=True, timeout=60)
        d = json.loads(out.stdout or "{}")
    except Exception:
        return {}
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {"duration_s": float(d.get("format", {}).get("duration") or 0),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "width": v.get("width"), "height": v.get("height")}


def frames(video: str, out_dir: Path, duration: float, cap: int = MAX_FRAMES) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = max(min(4, cap), min(cap, round(duration / 2.5))) if duration else min(4, cap)
    span = max(duration - 0.5, 0.1)
    times = sorted({0.3, *[round(span * (i + 1) / n, 2) for i in range(n - 1)]})
    got = []
    for i, t in enumerate(times):
        p = out_dir / f"{Path(video).stem}_f{i:02d}_{t:06.2f}s.jpg"
        r = subprocess.run([FFMPEG, "-y", "-v", "error", "-ss", str(t), "-i", video,
                            "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "3", str(p)],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and p.exists():
            got.append({"t": t, "path": str(p)})
    return got


def _model():
    global _whisper
    with _wlock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE,
                                    compute_type="int8" if WHISPER_DEVICE == "cpu" else "float16")
    return _whisper


def transcribe(video: str) -> dict:
    """{language, probability, text, segments:[{start,end,text}]}. Empty text = no speech."""
    segs, info = _model().transcribe(video, vad_filter=True, beam_size=5)
    segments = [{"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()}
                for s in segs]
    return {"language": info.language, "probability": round(info.language_probability, 3),
            "text": " ".join(s["text"] for s in segments).strip(), "segments": segments}


def process(item) -> dict:
    """Frames + transcript for every video in the item; images pass through. Written into the
    item dir and returned as {visuals:[{path, label}], transcripts:[...], videos:[probe...]}."""
    visuals, transcripts, videos = [], [], []
    # split the per-item image budget across slides so a 3-video carousel shows every slide,
    # not 12 frames of the first one and nothing of the rest
    n_img = sum(1 for m in item.media if m["type"] == "image")
    n_vid = len(item.media) - n_img
    cap = max(3, min(MAX_FRAMES, (MAX_IMAGES - n_img) // max(1, n_vid)))
    for m in item.media:
        if m["type"] == "image":
            visuals.append({"path": m["path"], "label": f"slide {m['index'] + 1} (image)"})
            continue
        info = probe(m["path"])
        videos.append({"index": m["index"], **info})
        for f in frames(m["path"], item.dir / "frames", info.get("duration_s", 0), cap):
            visuals.append({"path": f["path"],
                            "label": f"slide {m['index'] + 1} video @ {f['t']:.1f}s"})
        if info.get("has_audio"):
            try:
                tr = transcribe(m["path"])
                tr["index"] = m["index"]
                transcripts.append(tr)
            except Exception as e:
                transcripts.append({"index": m["index"], "error": str(e)[:300], "text": ""})
    if transcripts:
        lines = []
        for tr in transcripts:
            lines.append(f"# media {tr['index']}  lang={tr.get('language')}")
            for s in tr.get("segments", []):
                lines.append(f"[{s['start']:6.1f}-{s['end']:6.1f}] {s['text']}")
        (item.dir / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")
    return {"visuals": visuals, "transcripts": transcripts, "videos": videos}
