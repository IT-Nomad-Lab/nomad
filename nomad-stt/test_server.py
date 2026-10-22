"""nomad-stt GPU admission (#1462) with torch, moshi and the model stubbed: no GPU or weights needed.

Run: uv run --no-project --with pytest --with fastapi --with numpy python -m pytest -q nomad-stt/test_server.py
"""
import fcntl
import importlib.util
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def stt(monkeypatch, tmp_path):
    monkeypatch.setenv("NOMAD_GPU_LOCK", str(tmp_path / "gpu.lock"))
    monkeypatch.setenv("NOMAD_GPU_GUARD_STATE", str(tmp_path / "gpu_guard_state.json"))
    monkeypatch.setenv("NOMAD_STT_ADMISSION_WAIT_SECONDS", "0")
    torch = types.ModuleType("torch")
    torch.bfloat16 = "bf16"
    torch.no_grad = lambda: (lambda f: f)
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, synchronize=lambda: None)
    moshi, models = types.ModuleType("moshi"), types.ModuleType("moshi.models")
    models.LMGen, models.loaders = object, types.SimpleNamespace()
    for name, mod in {"torch": torch, "moshi": moshi, "moshi.models": models,
                      "uvicorn": types.ModuleType("uvicorn")}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("nomad_stt_server", os.path.join(HERE, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def lock_is_free(mod):
    try:
        os.close(mod.gpu_admission.try_admit())
        return True
    except mod.gpu_admission.Busy:
        return False


def test_load_holds_admission_while_loading_and_releases_once_warm(stt, monkeypatch):
    seen = []
    monkeypatch.setattr(stt, "_load_admitted", lambda: seen.append(lock_is_free(stt)))
    stt._load()
    assert seen == [False] and lock_is_free(stt)


def test_load_never_happens_while_another_workload_holds_the_gpu(stt, monkeypatch, tmp_path):
    loaded = []
    monkeypatch.setattr(stt, "_load_admitted", lambda: loaded.append(1))
    fd = os.open(tmp_path / "gpu.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(stt.gpu_admission.Busy):
            stt._load()
    finally:
        os.close(fd)
    (tmp_path / "gpu_guard_state.json").write_text("{}")
    with pytest.raises(stt.gpu_admission.Busy, match="restore"):
        stt._load()
    assert loaded == []


def test_load_waits_for_the_guard_to_finish_its_restore(stt, monkeypatch, tmp_path):
    fd = os.open(tmp_path / "gpu.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    waits = []

    def guard_finishes(seconds):
        waits.append(seconds)
        os.close(fd)

    real = stt.gpu_admission.wait_admit
    monkeypatch.setattr(stt.gpu_admission, "wait_admit", lambda timeout: real(5, sleep=guard_finishes))
    loaded = []
    monkeypatch.setattr(stt, "_load_admitted", lambda: loaded.append(lock_is_free(stt)))
    stt._load()
    assert waits == [1.0] and loaded == [False] and lock_is_free(stt)


def test_failed_load_releases_admission(stt, monkeypatch):
    def boom():
        raise RuntimeError("cuda init failed")

    monkeypatch.setattr(stt, "_load_admitted", boom)
    with pytest.raises(RuntimeError):
        stt._load()
    assert lock_is_free(stt)


def test_cpu_device_takes_no_admission(stt, monkeypatch, tmp_path):
    monkeypatch.setattr(stt, "DEVICE", "cpu")
    fd = os.open(tmp_path / "gpu.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    loaded = []
    monkeypatch.setattr(stt, "_load_admitted", lambda: loaded.append(1))
    try:
        stt._load()
    finally:
        os.close(fd)
    assert loaded == [1]
