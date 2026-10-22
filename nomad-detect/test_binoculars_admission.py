"""nomad-detect GPU admission (#1462) with torch, transformers and the model pair stubbed: no GPU needed.

Run: uv run --no-project --with pytest --with fastapi --with httpx --with uvicorn python -m pytest -q nomad-detect/test_binoculars_admission.py
"""
import fcntl
import os
import sys
import threading
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _fake_torch(emptied):
    torch = types.ModuleType("torch")
    torch.bfloat16, torch.float32, torch.Tensor = "bf16", "f32", object
    torch.no_grad = lambda: (lambda f: f)
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: emptied.append(1))
    nn, functional = types.ModuleType("torch.nn"), types.ModuleType("torch.nn.functional")
    torch.nn, nn.functional = nn, functional
    transformers = types.ModuleType("transformers")
    transformers.AutoModelForCausalLM = transformers.AutoTokenizer = object
    return {"torch": torch, "torch.nn": nn, "torch.nn.functional": functional, "transformers": transformers}


@pytest.fixture
def detect(monkeypatch, tmp_path):
    """Returns a loader: detect(device) imports binoculars and server fresh on that device."""
    monkeypatch.setenv("NOMAD_GPU_LOCK", str(tmp_path / "gpu.lock"))
    monkeypatch.setenv("NOMAD_GPU_GUARD_STATE", str(tmp_path / "gpu_guard_state.json"))
    monkeypatch.syspath_prepend(HERE)
    emptied = []
    for name, mod in _fake_torch(emptied).items():
        monkeypatch.setitem(sys.modules, name, mod)

    def load(device):
        monkeypatch.setenv("NOMAD_DETECT_DEVICE", device)
        for name in ("binoculars", "server", "calibrate", "check", "evaluate", "external", "style"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        import binoculars
        import server
        ns = types.SimpleNamespace(bino=binoculars, server=server, emptied=emptied, built=[], scored=[])

        class FakePair:
            pair = "fake|pair"

            def __init__(self):
                ns.built.append(1)

            def score(self, text):
                ns.scored.append(lock_is_free(binoculars))
                if text == "boom":
                    raise RuntimeError("cuda error")
                return {"ok": True, "score": 0.95, "band": "human", "thresholds": binoculars.thresholds()}

        monkeypatch.setattr(binoculars, "Binoculars", FakePair)
        return ns

    yield load
    for name in ("binoculars", "server", "calibrate", "check", "evaluate", "external", "style"):
        sys.modules.pop(name, None)


def lock_is_free(bino):
    try:
        os.close(bino.gpu_admission.try_admit())
        return True
    except bino.gpu_admission.Busy:
        return False


@pytest.fixture
def other_holder(tmp_path):
    fd = os.open(tmp_path / "gpu.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield
    os.close(fd)


TEXT = "A draft long enough to score. " * 10


def test_cuda_scores_while_admitted_then_drops_the_models_and_releases(detect):
    d = detect("cuda")
    out = d.server.detect(d.server.DetectIn(text=TEXT))
    assert out["binoculars"]["ok"] and d.scored == [False]
    assert d.bino._engine is None and d.emptied == [1] and not d.bino._admitted
    assert lock_is_free(d.bino)
    d.server.detect(d.server.DetectIn(text=TEXT))
    assert d.built == [1, 1]                     # reloads per admitted block, never resident


def test_cuda_busy_gpu_fails_open_without_loading(detect, other_holder):
    d = detect("cuda")
    out = d.server.detect(d.server.DetectIn(text=TEXT))
    assert out["ok"] and not out["binoculars"]["ok"] and "holds admission" in out["binoculars"]["error"]
    assert "findings" in out["style"] and d.built == [] and d.scored == []


def test_cuda_pending_restore_fails_open_without_loading(detect, tmp_path):
    d = detect("cuda")
    (tmp_path / "gpu_guard_state.json").write_text("{}")
    out = d.server.detect(d.server.DetectIn(text=TEXT))
    assert not out["binoculars"]["ok"] and "restore" in out["binoculars"]["error"] and d.built == []


def test_cuda_engine_outside_admission_refuses_to_load(detect):
    d = detect("cuda")
    with pytest.raises(RuntimeError, match="admitted"):
        d.bino.engine()
    assert d.built == []


def test_cuda_failure_inside_the_block_still_drops_and_releases(detect):
    d = detect("cuda")
    out = d.server.detect(d.server.DetectIn(text="boom"))
    assert out["binoculars"] == {"ok": False, "error": "cuda error"}
    assert d.bino._engine is None and not d.bino._admitted and lock_is_free(d.bino)
    with pytest.raises(RuntimeError, match="admitted"):
        d.bino.engine()


def test_cuda_concurrent_requests_queue_in_process_instead_of_failing(detect):
    d = detect("cuda")
    inside, release, results = threading.Event(), threading.Event(), {}

    def first():
        with d.bino.admitted():
            inside.set()
            release.wait(10)
        results["first"] = "done"

    t1 = threading.Thread(target=first)
    t1.start()
    assert inside.wait(10)
    t2 = threading.Thread(target=lambda: results.setdefault(
        "second", d.server.detect(d.server.DetectIn(text=TEXT))["binoculars"]))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive() and "second" not in results    # waiting on the in-process lock, not refused
    release.set()
    t1.join(10)
    t2.join(10)
    assert results["second"]["ok"] and lock_is_free(d.bino)


def test_cuda_calibration_holds_admission_for_the_whole_batch(detect, monkeypatch, tmp_path):
    d = detect("cuda")
    import calibrate
    monkeypatch.setattr(calibrate, "THRESHOLDS", tmp_path / "thresholds.json")
    samples = [{"text": TEXT, "label": lab} for lab in ("human", "ai", "human", "ai")]
    calibrate.fit(samples, save=False)
    assert d.built == [1] and d.scored == [False] * 4
    assert d.bino._engine is None and lock_is_free(d.bino)


def test_cuda_check_cli_holds_admission(detect, monkeypatch, tmp_path, capsys):
    d = detect("cuda")
    import check
    draft = tmp_path / "draft.md"
    draft.write_text(TEXT)
    monkeypatch.setattr(sys, "argv", ["check.py", str(draft), "--json"])
    with pytest.raises(SystemExit):
        check.main()
    assert d.scored == [False] and d.bino._engine is None and lock_is_free(d.bino)


def test_cpu_takes_no_admission_and_keeps_the_models(detect, other_holder):
    d = detect("cpu")
    out = d.server.detect(d.server.DetectIn(text=TEXT))
    assert out["binoculars"]["ok"] and d.built == [1]
    assert d.bino._engine is not None and d.emptied == []
    d.bino.engine()
    assert d.built == [1]


def test_cuda_evaluation_holds_admission_for_the_whole_run(detect, monkeypatch, tmp_path, capsys):
    d = detect("cuda")
    import evaluate
    for label, name in (("human", "me_1.txt"), ("ai", "gpt_1.txt"), ("ai", "opus_1.txt")):
        (tmp_path / label).mkdir(exist_ok=True)
        (tmp_path / label / name).write_text(TEXT)
    monkeypatch.setattr(evaluate, "SAMPLES", tmp_path)
    evaluate.main()
    assert "pair: fake|pair" in capsys.readouterr().out
    assert d.built == [1] and d.scored == [False] * 3
    assert d.bino._engine is None and lock_is_free(d.bino)
