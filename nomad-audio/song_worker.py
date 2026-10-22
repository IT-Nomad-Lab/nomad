"""One HeartMuLa song, run by server.py's /song as a subprocess in the HeartMuLa venv (#1419).

heartlib pins transformers, torchtune and pyarrow versions that clash with ACE-Step's venv, so it
runs here, one song per process: the weights load, the song is made, the process exits and the
GPU is free again.

  stdin:  one JSON object {ckpt, out, tags, lyrics, max_seconds, seed, cfg_scale, temperature,
          topk, lazy}
  stdout: the last line is `RESULT {json}` with the out path, length, sample rate and timings.
          heartlib's own prints go to stderr.

`lazy` loads HeartMuLa and HeartCodec one at a time (12.8 GiB peak, about 2.3x slower) instead of
both at once (23.3 GiB). The same seed usually gives the same song, but not always (#1427).
"""
import contextlib
import json
import os
import sys
import tempfile
import time


def _patch_torchaudio_save():
    """heartlib saves with torchaudio.save, which in torchaudio 2.10 needs torchcodec (no build
    for the host's FFmpeg 8). Write the WAV with soundfile instead: [channels, frames] in."""
    import soundfile as sf
    import torch
    import torchaudio

    def _save(uri, src, sample_rate=48000, **_kw):
        sf.write(str(uri), src.to(torch.float32).cpu().numpy().T, int(sample_rate), subtype="PCM_24")

    torchaudio.save = _save


def run(req: dict) -> dict:
    import soundfile as sf
    import torch
    from heartlib import HeartMuLaGenPipeline

    _patch_torchaudio_save()
    cuda = torch.device("cuda")
    t0 = time.time()
    pipe = HeartMuLaGenPipeline.from_pretrained(
        req["ckpt"], device={"mula": cuda, "codec": cuda},
        dtype={"mula": torch.bfloat16, "codec": torch.float32},
        version="3B", lazy_load=bool(req["lazy"]))
    load_s = time.time() - t0

    # heartlib reads `tags` and `lyrics` from a file when the string is an existing path. Always
    # hand it a file, so request text is never taken for a path.
    with tempfile.TemporaryDirectory() as tmp:
        inputs = {}
        for key in ("tags", "lyrics"):
            path = os.path.join(tmp, f"{key}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(req[key])
            inputs[key] = path
        torch.manual_seed(req["seed"])
        torch.cuda.manual_seed_all(req["seed"])
        torch.cuda.reset_peak_memory_stats()
        t1 = time.time()
        with torch.no_grad():
            pipe(inputs, max_audio_length_ms=int(req["max_seconds"] * 1000), save_path=req["out"],
                 topk=req["topk"], temperature=req["temperature"], cfg_scale=req["cfg_scale"])
        gen_s = time.time() - t1

    info = sf.info(req["out"])
    return {"out": req["out"], "seconds": round(info.frames / info.samplerate, 2),
            "sample_rate": info.samplerate, "channels": info.channels,
            "load_s": round(load_s, 1), "gen_s": round(gen_s, 1),
            "peak_mib": round(torch.cuda.max_memory_allocated() / 2**20)}


def main():
    req = json.loads(sys.stdin.read())
    with contextlib.redirect_stdout(sys.stderr):   # heartlib prints progress; keep stdout for RESULT
        result = run(req)
    print("RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
