"""trellis_gen GPU admission (#1462) with torch, trellis2, xformers and o_voxel stubbed: no GPU needed.

Run: uv run --no-project --with pytest python -m pytest -q nomad-3d/test_trellis_admission.py
"""
import fcntl
import importlib.util
import os
import sys
import threading
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


class Fakes:
    """The CUDA stack trellis_gen imports. Every GPU touch records whether admission was held."""

    def __init__(self, lock_free, fail_run=False):
        self.events, self.lock_free, self.fail_run = [], lock_free, fail_run

    def touch(self, what):
        self.events.append((what, self.lock_free()))

    def modules(self):
        f = self
        torch = types.ModuleType("torch")
        torch.cuda = types.SimpleNamespace(
            get_device_capability=lambda: f.touch("capability") or (12, 0),
            reset_peak_memory_stats=lambda: None,
            max_memory_allocated=lambda: 3 * 2**30,
            empty_cache=lambda: f.touch("empty_cache"))
        pil, image = types.ModuleType("PIL"), types.ModuleType("PIL.Image")
        image.open = lambda p: str(p)
        pil.Image = image

        class Mesh:
            vertices = faces = attrs = coords = layout = None
            voxel_size = 0.01

            def simplify(self, n):
                pass

        class Pipe:
            @classmethod
            def from_pretrained(cls, path):
                f.touch("from_pretrained")
                return cls()

            def cuda(self):
                f.touch("cuda")

            def run(self, img, seed, pipeline_type):
                f.touch("run")
                if f.fail_run:
                    raise RuntimeError("CUDA out of memory")
                return [Mesh()]

        class Glb:
            def export(self, path):
                open(path, "wb").write(b"glTF")

        pipelines = types.ModuleType("trellis2.pipelines")
        pipelines.Trellis2ImageTo3DPipeline = Pipe
        trellis2 = types.ModuleType("trellis2")
        trellis2.pipelines = pipelines
        o_voxel = types.ModuleType("o_voxel")
        o_voxel.postprocess = types.SimpleNamespace(to_glb=lambda **k: f.touch("to_glb") or Glb())
        dispatch = types.ModuleType("xformers.ops.fmha.dispatch")
        dispatch._set_use_fa3 = lambda on: f.touch(f"fa3={on}")
        mods = {"torch": torch, "PIL": pil, "PIL.Image": image, "trellis2": trellis2,
                "trellis2.pipelines": pipelines, "o_voxel": o_voxel,
                "xformers.ops.fmha.dispatch": dispatch}
        for name in ("xformers", "xformers.ops", "xformers.ops.fmha"):
            mods[name] = types.ModuleType(name)
        mods["xformers.ops.fmha"].dispatch = dispatch
        return mods

    def names(self):
        return [e[0] for e in self.events]


@pytest.fixture
def paths(tmp_path, monkeypatch):
    p = {"lock": str(tmp_path / "gpu.lock"), "state": str(tmp_path / "gpu_guard_state.json")}
    monkeypatch.setenv("NOMAD_GPU_LOCK", p["lock"])
    monkeypatch.setenv("NOMAD_GPU_GUARD_STATE", p["state"])
    return p


def _load_module():
    spec = importlib.util.spec_from_file_location("trellis_gen_under_test",
                                                  os.path.join(HERE, "trellis_gen.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def trellis(paths, monkeypatch, tmp_path, capsys):
    """Returns run(*extra_args, fail_run=False) -> (exit code, Fakes, stderr)."""
    mod = _load_module()
    img = tmp_path / "chair.png"
    img.write_bytes(b"png")
    out = tmp_path / "out"

    def lock_free():
        try:
            os.close(mod.gpu_admission.try_admit())
            return True
        except mod.gpu_admission.Busy:
            return False

    def run(*extra, fail_run=False):
        fakes = Fakes(lock_free, fail_run)
        for name, m in fakes.modules().items():
            monkeypatch.setitem(sys.modules, name, m)
        monkeypatch.setattr(sys, "argv", ["trellis_gen.py", str(img), "--out-dir", str(out), *extra])
        rc = mod.main()
        return rc, fakes, capsys.readouterr().err

    run.out, run.lock_free = out, lock_free
    return run


@pytest.fixture
def other_holder(paths):
    fd = os.open(paths["lock"], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    holder = types.SimpleNamespace(fd=fd, released=False)

    def release():
        if not holder.released:
            holder.released = True
            os.close(fd)
    holder.release = release
    yield holder
    release()


def test_the_whole_batch_runs_admitted_and_frees_vram_before_release(trellis):
    rc, f, _ = trellis()
    assert rc == 0 and (trellis.out / "chair.glb").read_bytes() == b"glTF"
    assert f.names() == ["capability", "fa3=False", "from_pretrained", "cuda", "run", "to_glb",
                         "empty_cache"]
    assert all(not free for _, free in f.events)        # every CUDA touch held admission
    assert trellis.lock_free()


def test_busy_gpu_exits_75_without_touching_cuda(trellis, other_holder):
    rc, f, err = trellis()
    assert rc == 75 and f.events == [] and "holds admission" in err
    assert not (trellis.out / "chair.glb").exists()


def test_pending_restore_exits_75_without_touching_cuda(trellis, paths):
    open(paths["state"], "w").close()
    rc, f, err = trellis()
    assert rc == 75 and f.events == [] and "restore is pending" in err


def test_wait_admits_once_the_holder_lets_go(trellis, other_holder):
    threading.Timer(0.3, other_holder.release).start()
    rc, f, _ = trellis("--wait", "10")
    assert rc == 0 and "run" in f.names() and trellis.lock_free()


def test_wait_is_bounded(trellis, other_holder):
    rc, f, err = trellis("--wait", "0.5")
    assert rc == 75 and f.events == [] and "holds admission" in err


def test_a_failure_mid_batch_frees_vram_while_still_admitted(paths, monkeypatch, tmp_path):
    mod = _load_module()

    def lock_free():
        try:
            os.close(mod.gpu_admission.try_admit())
            return True
        except mod.gpu_admission.Busy:
            return False

    fakes = Fakes(lock_free, fail_run=True)
    for name, m in fakes.modules().items():
        monkeypatch.setitem(sys.modules, name, m)
    (tmp_path / "a.png").write_bytes(b"png")
    monkeypatch.setattr(sys, "argv", ["trellis_gen.py", str(tmp_path / "a.png"), "--out-dir",
                                      str(tmp_path / "o")])
    with pytest.raises(RuntimeError):
        mod.main()
    assert fakes.events[-1] == ("empty_cache", False) and lock_free()


def test_importing_the_script_touches_no_cuda(paths, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)     # any import of torch would raise
    mod = _load_module()
    assert hasattr(mod, "main")
