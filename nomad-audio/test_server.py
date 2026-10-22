"""nomad-audio request handling with the models mocked: no torch, acestep or GPU needed (#151).

Run: uv run --no-project --with pytest --with fastapi --with httpx python -m pytest -q nomad-audio/test_server.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import types

import pytest
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))


def load_server(monkeypatch, tmp_path, version="1.5"):
    monkeypatch.setenv("NOMAD_AUDIO_DIR", str(tmp_path))
    monkeypatch.setenv("ACE_STEP_VERSION", version)
    # Never the live /tmp/nomad-gpu.lock or the media worker's guard state.
    monkeypatch.setenv("NOMAD_GPU_LOCK", str(tmp_path / "gpu.lock"))
    monkeypatch.setenv("NOMAD_GPU_GUARD_STATE", str(tmp_path / "gpu_guard_state.json"))
    monkeypatch.setenv("NOMAD_GPU_LEASE_FILE", str(tmp_path / "gpu_lease.json"))
    spec = importlib.util.spec_from_file_location("nomad_audio_server", os.path.join(HERE, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def srv(monkeypatch, tmp_path):
    mod = load_server(monkeypatch, tmp_path)
    calls = {}

    def fake_music(**kw):
        calls["music"] = kw
        with open(kw["out"], "wb") as f:
            f.write(b"RIFFmusic")
        return kw["out"]

    def fake_sfx(out, prompt, negative, duration_s, steps, seed):
        calls["sfx"] = dict(prompt=prompt, negative=negative, duration_s=duration_s, steps=steps, seed=seed)
        with open(out, "wb") as f:
            f.write(b"RIFFsfx")
        return 44100

    monkeypatch.setattr(mod, "_render_music", fake_music)
    monkeypatch.setattr(mod, "_render_sfx", fake_sfx)
    return mod, TestClient(mod.app), calls


# /music

def test_music_needs_a_prompt(srv):
    _, c, calls = srv
    r = c.post("/music", json={"prompt": "  "})
    assert r.status_code == 400 and "prompt" in r.json()["error"]
    assert "music" not in calls


@pytest.mark.parametrize("body", [
    {"duration": "long"}, {"steps": "many"}, {"guidance_scale": [1]}, {"seed": "lucky"},
    {"duration": 0}, {"duration": 381}, {"engine": "nope"},
    {"engine": "ace-step", "duration": 241},
])
def test_music_bad_parameters_are_a_400_and_render_nothing(srv, body):
    _, c, calls = srv
    r = c.post("/music", json={"prompt": "lofi", **body})
    assert r.status_code == 400, r.text
    assert "music" not in calls


def test_music_returns_a_wav_and_passes_parsed_parameters(srv):
    _, c, calls = srv
    r = c.post("/music", json={"engine": "ace-step", "prompt": " lofi, 90 bpm ", "lyrics": "[instrumental]",
                               "duration": "12.5", "steps": "8", "guidance_scale": 7, "seed": "42"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["x-model"] == "ace-step-1.5"
    assert r.content == b"RIFFmusic"
    kw = calls["music"]
    assert (kw["prompt"], kw["lyrics"], kw["duration"], kw["steps"], kw["guidance"], kw["seed"]) == \
        ("lofi, 90 bpm", "[instrumental]", 12.5, 8, 7.0, 42)


def test_music_leaves_unset_knobs_to_the_backend_defaults(srv):
    mod, c, calls = srv
    assert c.post("/music", json={"prompt": "lofi", "engine": "ace-step"}).status_code == 200
    kw = calls["music"]
    assert kw["duration"] == mod.DEF_DURATION
    assert kw["steps"] is None and kw["guidance"] is None and kw["seed"] is None


def test_music_render_failure_is_a_500_with_the_reason(srv, monkeypatch):
    mod, c, _ = srv

    def boom(**_kw):
        raise RuntimeError("installed acestep has no ['caption']")
    monkeypatch.setattr(mod, "_render_music", boom)
    r = c.post("/music", json={"prompt": "lofi", "engine": "ace-step"})
    assert r.status_code == 500 and "caption" in r.json()["error"]


# /music engine sa3 (#1343)

def test_music_defaults_to_sa3_and_allows_380_seconds(srv):
    mod, c, calls = srv
    r = c.post("/music", json={"prompt": "lofi", "duration": 380, "seed": 5})
    assert r.status_code == 200
    assert calls["music"]["engine"] == "sa3" and calls["music"]["seed"] == 5
    assert r.headers["x-model"] == "sa3-medium" and r.headers["x-sample-rate"] == "44100"
    assert r.headers["x-seed"] == "5"


def test_music_sa3_without_seed_picks_one_and_reports_it(srv):
    _, c, calls = srv
    r = c.post("/music", json={"prompt": "lofi"})
    assert r.headers["x-seed"] == str(calls["music"]["seed"]) and calls["music"]["seed"] is not None


def test_music_ace_step_stays_selectable(srv):
    _, c, calls = srv
    r = c.post("/music", json={"prompt": "lofi", "engine": "ace-step"})
    assert r.status_code == 200 and calls["music"]["engine"] == "ace-step"
    assert r.headers["x-model"] == "ace-step-1.5"


def test_render_sa3_runs_the_worker_in_its_venv(monkeypatch, tmp_path):
    mod = load_server(monkeypatch, tmp_path)
    py = tmp_path / "python"
    py.write_text("")
    monkeypatch.setattr(mod, "SA3_PYTHON", str(py))
    monkeypatch.setattr(mod, "_free_mib", lambda: 20000)
    seen = {}

    def fake_run(cmd, input, **kw):
        seen.update(cmd=cmd, req=json.loads(input), timeout=kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, 'noise\nRESULT {"seconds": 20.0, "sample_rate": 44100}\n', "")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    r = mod._render_sa3(out="/x/o.wav", prompt="p", duration=20.0, seed=42)
    assert r["sample_rate"] == 44100 and r["seed"] == 42
    assert mod._render_music(engine="sa3", out="/x/o.wav", prompt="p", lyrics="", duration=20.0,
                             steps=None, guidance=None, seed=42) == "/x/o.wav"
    assert seen["cmd"] == [str(py), "-I", mod.SA3_WORKER]
    assert seen["req"] == {"model": "medium", "out": "/x/o.wav", "prompt": "p",
                           "negative_prompt": "poor quality, vocals", "duration": 20.0, "seed": 42}


def test_render_sa3_needs_the_venv_and_free_vram(monkeypatch, tmp_path):
    mod = load_server(monkeypatch, tmp_path)
    kw = dict(engine="sa3", out="o", prompt="p", lyrics="", duration=5.0, steps=None, guidance=None, seed=1)
    monkeypatch.setattr(mod, "SA3_PYTHON", str(tmp_path / "missing"))
    with pytest.raises(mod.SongUnavailable):
        mod._render_music(**kw)
    (tmp_path / "py").write_text("")
    monkeypatch.setattr(mod, "SA3_PYTHON", str(tmp_path / "py"))
    monkeypatch.setattr(mod, "_free_mib", lambda: 3000)
    with pytest.raises(mod.SongUnavailable):
        mod._render_music(**kw)


def test_music_sa3_unavailable_is_a_503(srv, monkeypatch):
    mod, c, _ = srv

    def no(**_kw):
        raise mod.SongUnavailable("not installed")
    monkeypatch.setattr(mod, "_render_music", no)
    assert c.post("/music", json={"prompt": "lofi"}).status_code == 503


# /sfx

def test_sfx_needs_a_prompt(srv):
    _, c, calls = srv
    assert c.post("/sfx", json={}).status_code == 400
    assert "sfx" not in calls


@pytest.mark.parametrize("body", [
    {"duration_s": 0}, {"duration_s": 47.5}, {"duration_s": "x"}, {"steps": "x"}, {"seed": "x"},
])
def test_sfx_bad_parameters_are_a_400_and_render_nothing(srv, body):
    _, c, calls = srv
    r = c.post("/sfx", json={"prompt": "door creak", **body})
    assert r.status_code == 400, r.text
    assert "sfx" not in calls


def test_sfx_returns_a_wav_with_seed_and_defaults(srv):
    mod, c, calls = srv
    r = c.post("/sfx", json={"prompt": "door creak", "duration_s": 47, "seed": 7})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["x-seed"] == "7" and r.headers["x-sample-rate"] == "44100"
    assert r.headers["x-model"] == mod.SAO_MODEL
    assert calls["sfx"] == dict(prompt="door creak", negative="Low quality.", duration_s=47.0,
                                steps=mod.SAO_STEPS, seed=7)


def test_sfx_without_a_seed_picks_one_and_reports_it(srv):
    _, c, calls = srv
    r = c.post("/sfx", json={"prompt": "rain"})
    assert r.status_code == 200
    assert r.headers["x-seed"] == str(calls["sfx"]["seed"])


def test_sfx_gated_model_error_says_how_to_get_access(srv, monkeypatch):
    mod, c, _ = srv

    def gated(*_a):
        raise OSError("403 Client Error: access to model stabilityai/stable-audio-open-1.0 is restricted")
    monkeypatch.setattr(mod, "_render_sfx", gated)
    r = c.post("/sfx", json={"prompt": "rain"})
    assert r.status_code == 500
    assert "accept the licence" in r.json()["error"]


# residency, /health, config

@pytest.fixture
def fake_torch(monkeypatch):
    t = types.SimpleNamespace(cuda=types.SimpleNamespace(empty_cache=lambda: None,
                                                         is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", t)
    return t


def test_only_one_model_is_resident(srv, fake_torch):
    mod, _, _ = srv
    mod._ace15, mod._sao = object(), None
    mod._make_room("sfx")
    assert mod._ace15 is None
    mod._sao = object()
    mod._make_room("music")
    assert mod._sao is None


def test_health_reports_versions_without_loading_a_model(srv):
    mod, c, _ = srv
    h = c.get("/health").json()
    assert h["ace_step_version"] == "1.5" and h["model"] == mod.ACESTEP15_CONFIG
    assert h["loaded"] is False and h["sfx"]["loaded"] is False and h["sfx"]["max_duration_s"] == 47


def test_rollback_version_is_selectable(monkeypatch, tmp_path):
    mod = load_server(monkeypatch, tmp_path, version="1")
    assert mod.ACE_STEP_VERSION == "1"
    assert TestClient(mod.app).get("/health").json()["model"] == "ace-step-v1-3.5B"


def test_unknown_version_refuses_to_start(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        load_server(monkeypatch, tmp_path, version="2")


def test_fit_refuses_a_missing_required_field_and_drops_unknown_optionals(srv):
    mod, _, _ = srv

    def target(caption, duration, seed=None):
        pass
    assert mod._fit(target, {"caption": "x", "duration": 5}, {"seed": 1, "thinking": True}) == \
        {"caption": "x", "duration": 5, "seed": 1}
    with pytest.raises(RuntimeError, match="lyrics"):
        mod._fit(target, {"caption": "x", "lyrics": ""}, {})


# /song (HeartMuLa subprocess, #1419)

@pytest.fixture
def song(srv, monkeypatch, tmp_path):
    """/song with the engine 'installed' and the subprocess faked; `state` sets free VRAM."""
    mod, c, calls = srv
    py = tmp_path / "hm" / "python"
    ck = tmp_path / "hm" / "ckpt"
    for part in mod._CKPT_PARTS:
        os.makedirs(ck / part, exist_ok=True)
    py.write_text("")
    monkeypatch.setattr(mod, "HEARTMULA_PYTHON", str(py))
    monkeypatch.setattr(mod, "HEARTMULA_CKPT", str(ck))
    state = {"free": 23900, "rc": 0}
    monkeypatch.setattr(mod, "_free_mib", lambda: state["free"])

    def fake_run(cmd, input, **kw):
        req = json.loads(input)
        calls.setdefault("song", []).append(dict(req, cmd=cmd, timeout=kw["timeout"]))
        if state["rc"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nCUDA out of memory")
        with open(req["out"], "wb") as f:
            f.write(b"RIFFsong")
        res = {"out": req["out"], "seconds": 124.32, "sample_rate": 48000, "channels": 2}
        return types.SimpleNamespace(returncode=0, stdout="noise\nRESULT " + json.dumps(res), stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return mod, c, calls, state


LYR = "[Verse]\nCoffee on the desk\n[Chorus]\nWe build it"


@pytest.mark.parametrize("body,field", [
    ({"lyrics": LYR}, "tags"), ({"tags": "pop"}, "lyrics"), ({"tags": "pop", "lyrics": "  "}, "lyrics"),
])
def test_song_needs_tags_and_lyrics(song, body, field):
    _, c, calls, _ = song
    r = c.post("/song", json=body)
    assert r.status_code == 400 and field in r.json()["error"]
    assert "song" not in calls


@pytest.mark.parametrize("extra", [
    {"max_seconds": 5}, {"max_seconds": 241}, {"max_seconds": "long"}, {"seed": "x"},
    {"cfg_scale": 0}, {"temperature": 3}, {"topk": 0}, {"lyrics": "x" * 5001},
])
def test_song_bad_parameters_are_a_400_and_run_nothing(song, extra):
    _, c, calls, _ = song
    r = c.post("/song", json={"tags": "pop", "lyrics": LYR, **extra})
    assert r.status_code == 400, r.text
    assert "song" not in calls


def test_song_full_card_runs_full_load_and_reports_seed_and_length(song):
    mod, c, calls, _ = song
    r = c.post("/song", json={"tags": " Latin pop, male vocal ", "lyrics": LYR, "seed": 1416,
                              "max_seconds": 180})
    assert r.status_code == 200, r.text
    assert r.content == b"RIFFsong" and r.headers["content-type"] == "audio/wav"
    h = r.headers
    assert (h["x-seed"], h["x-duration-seconds"], h["x-sample-rate"], h["x-song-mode"], h["x-model"]) == \
        ("1416", "124.32", "48000", "full", "heartmula-oss-3b")
    (req,) = calls["song"]
    assert req["cmd"] == [mod.HEARTMULA_PYTHON, "-I", mod.SONG_WORKER]
    assert (req["tags"], req["lyrics"], req["seed"], req["lazy"]) == ("Latin pop, male vocal", LYR, 1416, False)
    assert (req["max_seconds"], req["cfg_scale"], req["temperature"], req["topk"]) == (180, 3.0, 1.0, 50)
    assert req["timeout"] == 3 * 180 + 120


def test_song_with_less_free_vram_runs_lazy(song):
    _, c, calls, state = song
    state["free"] = 15000
    r = c.post("/song", json={"tags": "pop", "lyrics": LYR})
    assert r.status_code == 200 and r.headers["x-song-mode"] == "lazy"
    assert calls["song"][0]["lazy"] is True and r.headers["x-seed"] == str(calls["song"][0]["seed"])


@pytest.mark.parametrize("free", [8000, None])
def test_song_on_a_full_gpu_is_a_503_and_starts_nothing(song, free):
    _, c, calls, state = song
    state["free"] = free
    r = c.post("/song", json={"tags": "pop", "lyrics": LYR})
    assert r.status_code == 503
    assert "song" not in calls
    if free:
        assert "gpu_guard" in r.json()["error"]


def test_song_drops_resident_models_first(song, fake_torch):
    mod, c, _, _ = song
    mod._ace15, mod._sao = object(), object()
    assert c.post("/song", json={"tags": "pop", "lyrics": LYR}).status_code == 200
    assert mod._ace15 is None and mod._sao is None


def test_song_not_installed_is_a_503(song, monkeypatch, tmp_path):
    mod, c, calls, _ = song
    monkeypatch.setattr(mod, "HEARTMULA_CKPT", str(tmp_path / "nowhere"))
    r = c.post("/song", json={"tags": "pop", "lyrics": LYR})
    assert r.status_code == 503 and "install.sh heartmula" in r.json()["error"]
    assert "song" not in calls
    assert c.get("/health").json()["song"]["installed"] is False


def test_song_worker_failure_is_a_500_with_the_tail(song):
    _, c, _, state = song
    state["rc"] = 1
    r = c.post("/song", json={"tags": "pop", "lyrics": LYR})
    assert r.status_code == 500 and "out of memory" in r.json()["error"]


def test_song_mode_can_be_forced(song, monkeypatch):
    mod, _, _, _ = song
    monkeypatch.setattr(mod, "HEARTMULA_MODE", "lazy")
    assert mod._song_mode(24000) == "lazy"
    monkeypatch.setattr(mod, "HEARTMULA_MODE", "full")
    with pytest.raises(mod.SongUnavailable):
        mod._song_mode(20000)


def test_music_and_sfx_still_work_after_a_song(song):
    _, c, calls, _ = song
    assert c.post("/song", json={"tags": "pop", "lyrics": LYR}).status_code == 200
    assert c.post("/music", json={"prompt": "lofi"}).status_code == 200
    assert c.post("/sfx", json={"prompt": "rain"}).status_code == 200
    assert "music" in calls and "sfx" in calls


def test_health_reports_the_song_engine(song):
    _, c, _, _ = song
    s = c.get("/health").json()["song"]
    assert s["installed"] is True and s["model"] == "heartmula-oss-3b" and s["full_mib"] == 23500


def test_free_mib_falls_back_to_the_wsl_nvidia_smi(monkeypatch, tmp_path):
    # #1426: under systemd --user, nvidia-smi is not on PATH; the WSL driver copy is.
    mod = load_server(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or types.SimpleNamespace(stdout="21051\n"))
    assert mod._free_mib() == 21051
    assert seen[0][0] == "/usr/lib/wsl/lib/nvidia-smi"


# GPU admission (#1462): own admission, the guard's lease, or a 503 that loads nothing

HOLDER = """
import fcntl, json, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
if sys.argv[2] != "-":
    with open(os.open(sys.argv[2], os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"token": sys.argv[3], "pid": os.getpid()}, f)
print("held", flush=True)
sys.stdin.read()
"""
LEASE = "lease-" + "a" * 26


@pytest.fixture
def gpu_holder(tmp_path):
    procs = []

    def hold(lease=True):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, str(tmp_path / "gpu.lock"),
                              str(tmp_path / "gpu_lease.json") if lease else "-", LEASE],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "held"
        procs.append(p)
        return p

    yield hold
    for p in procs:
        p.stdin.close()
        p.wait(timeout=10)


@pytest.mark.parametrize("path,body", [("/music", {"prompt": "lofi"}), ("/sfx", {"prompt": "door"}),
                                       ("/song", {"tags": "pop", "lyrics": "[Verse] la"})])
def test_busy_gpu_is_a_503_that_renders_nothing(srv, gpu_holder, monkeypatch, path, body):
    mod, c, calls = srv
    monkeypatch.setattr(mod, "_render_song", lambda *a: calls.setdefault("song", a))
    gpu_holder()
    r = c.post(path, json=body)
    assert r.status_code == 503 and r.headers["Retry-After"] == "30"
    assert "busy" in r.json()["error"].lower()
    r = c.post(path, json=body, headers={"X-Nomad-Gpu-Lease": "lease-" + "b" * 26})
    assert r.status_code == 503
    assert calls == {}


def test_pending_guard_restore_is_a_503(srv, tmp_path):
    _, c, calls = srv
    (tmp_path / "gpu_guard_state.json").write_text("{}")
    r = c.post("/music", json={"prompt": "lofi"})
    assert r.status_code == 503 and "restore" in r.json()["error"]
    assert calls == {}


def test_guard_lease_renders_and_keeps_the_model_for_the_guarded_job(srv, gpu_holder, monkeypatch):
    mod, c, calls = srv
    sentinel = object()

    def fake_sfx(out, *a):
        mod._sao = sentinel
        open(out, "wb").write(b"RIFFsfx")
        calls["sfx"] = True
        return 44100

    monkeypatch.setattr(mod, "_render_sfx", fake_sfx)
    gpu_holder()
    r = c.post("/sfx", json={"prompt": "door"}, headers={"X-Nomad-Gpu-Lease": LEASE})
    assert r.status_code == 200 and calls == {"sfx": True}
    assert mod._sao is sentinel


def test_own_admission_is_held_during_the_render_and_drops_models_before_release(srv, tmp_path, monkeypatch):
    mod, c, calls = srv
    seen = {}

    def fake_music(**kw):
        try:
            os.close(mod.gpu_admission.try_admit())
            seen["admitted_elsewhere"] = True
        except mod.gpu_admission.Busy:
            seen["admitted_elsewhere"] = False
        mod._pipe, mod._ace15, mod._sao = object(), object(), object()
        open(kw["out"], "wb").write(b"RIFFmusic")
        return kw["out"]

    monkeypatch.setattr(mod, "_render_music", fake_music)
    r = c.post("/music", json={"prompt": "lofi"})
    assert r.status_code == 200 and seen == {"admitted_elsewhere": False}
    assert (mod._pipe, mod._ace15, mod._sao) == (None, None, None)
    os.close(mod.gpu_admission.try_admit())   # released afterwards


def test_failed_render_still_drops_models_and_releases(srv, monkeypatch):
    mod, c, _ = srv

    def boom(**kw):
        mod._pipe = object()
        raise RuntimeError("cuda oom")

    monkeypatch.setattr(mod, "_render_music", boom)
    r = c.post("/music", json={"prompt": "lofi"})
    assert r.status_code == 500 and mod._pipe is None
    os.close(mod.gpu_admission.try_admit())
