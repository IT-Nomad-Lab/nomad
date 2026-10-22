"""One Stable Audio 3 music clip, run by server.py's /music (engine "sa3") as a subprocess in the
SA3 venv (#1343).

SA3 needs Python 3.10, torch 2.7.0+cu128 and flash-attn, which clash with ACE-Step's venv, so it
runs here, one clip per process: the weights load, the clip is made, the process exits and the GPU
is free again. Output is 44.1 kHz stereo; resampling to 48 kHz is the caller's job.

  stdin:  one JSON object {model, out, prompt, negative_prompt, duration, seed}
  stdout: the last line is `RESULT {json}` with the out path, length, sample rate and timings.
          The library's own prints go to stderr.
"""
import contextlib
import json
import sys
import time


def run(req: dict) -> dict:
    import torch
    from stable_audio_3 import StableAudioModel

    t0 = time.time()
    model = StableAudioModel.from_pretrained(req["model"], device="cuda")
    load_s = time.time() - t0
    torch.cuda.reset_peak_memory_stats()
    t1 = time.time()
    audio = model.generate(prompt=req["prompt"], negative_prompt=req["negative_prompt"],
                           duration=req["duration"], seed=req["seed"])
    torch.cuda.synchronize()
    gen_s = time.time() - t1
    sr = int(model.model.sample_rate)
    wav = audio[0].detach().to(torch.float32).cpu()          # [channels, frames]
    try:
        import soundfile as sf
        sf.write(req["out"], wav.numpy().T, sr, subtype="PCM_16")
    except ImportError:
        import torchaudio
        torchaudio.save(req["out"], wav, sr)
    return {"out": req["out"], "seconds": round(wav.shape[-1] / sr, 2), "sample_rate": sr,
            "channels": int(wav.shape[0]), "load_s": round(load_s, 1), "gen_s": round(gen_s, 1),
            "peak_mib": round(torch.cuda.max_memory_allocated() / 2**20)}


def main():
    req = json.loads(sys.stdin.read())
    with contextlib.redirect_stdout(sys.stderr):
        result = run(req)
    print("RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
