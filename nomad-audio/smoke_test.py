"""Smoke test for a running nomad-audio: /health, /music and /sfx each return what they should.

  ~/.local/share/nomad-audio/ace-step-1.5/.venv/bin/python nomad-audio/smoke_test.py [--base http://127.0.0.1:8220]

Hits the live service on the GPU, so the first run also downloads and loads both models. Exits
non-zero on the first failure. Checks that each audio endpoint returns a non-empty WAV that
soundfile can open and that is roughly the length asked for.
"""
import argparse
import io
import sys

import requests
import soundfile as sf

TIMEOUT = 1200   # first call per model includes the download and load


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def wav(r, what, want_s):
    check(r.status_code == 200, f"{what} → {r.status_code} {r.text[:200] if r.status_code != 200 else ''}")
    check(r.headers.get("content-type", "").startswith("audio/"), f"{what} content-type is audio")
    check(len(r.content) > 1024, f"{what} body is {len(r.content)} bytes")
    info = sf.info(io.BytesIO(r.content))
    check(info.frames > 0, f"{what} WAV has {info.frames} frames at {info.samplerate} Hz")
    check(abs(info.duration - want_s) <= max(1.0, want_s * 0.25),
          f"{what} lasts {info.duration:.1f}s (asked {want_s}s)")
    print(f"     path {r.headers.get('x-audio-path')}  model {r.headers.get('x-model')}  "
          f"{r.headers.get('x-generation-seconds')}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8220")
    ap.add_argument("--expect-version", default=None, help="fail unless /health reports this")
    a = ap.parse_args()
    base = a.base.rstrip("/")

    h = requests.get(f"{base}/health", timeout=10)
    check(h.status_code == 200, f"/health → {h.status_code}")
    hj = h.json()
    print(f"     {hj}")
    check(hj.get("ace_step_version") in ("1", "1.5"), "/health reports ace_step_version")
    if a.expect_version:
        check(hj["ace_step_version"] == a.expect_version, f"running ACE-Step {a.expect_version}")

    r = requests.post(f"{base}/music", json={"prompt": "lofi hip hop, mellow piano, 80 bpm",
                                             "lyrics": "[instrumental]", "duration": 10,
                                             "seed": 7}, timeout=TIMEOUT)
    wav(r, "/music", 10)

    r = requests.post(f"{base}/sfx", json={"prompt": "a wooden door creaks open",
                                           "duration_s": 3, "seed": 7}, timeout=TIMEOUT)
    wav(r, "/sfx", 3)
    check(r.headers.get("x-seed") == "7", "/sfx echoes the seed")

    r = requests.post(f"{base}/sfx", json={"prompt": "x", "duration_s": 999}, timeout=10)
    check(r.status_code == 400, "/sfx refuses a duration over the cap")
    print("all checks passed")


if __name__ == "__main__":
    main()
