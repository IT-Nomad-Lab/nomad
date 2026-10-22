"""ComfyUI under shared GPU admission (#1462): a fake ComfyUI, real kernel flocks on a tmp lock."""
import base64
import fcntl
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("NOMAD_DISPATCH_TOKEN", "test-token")   # before import: TOKEN is module-level

import comfy_gate  # noqa: E402
import dispatch  # noqa: E402
import gpu_admission as ga  # noqa: E402

PNG = b"\x89PNG fake"
GRAPH = {"1": {"class_type": "KSampler", "inputs": {}}}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += s


class FakeComfy:
    """Records every call with whether the GPU lock was free at that moment."""

    def __init__(self, paths, finish_after=2, busy_before=False, never_idle=False, vram=(0,),
                 pending=False, fail_prompt=None, bad_history=False):
        self.paths, self.calls = paths, []
        self.finish_after, self.polls = finish_after, 0
        self.busy_before, self.never_idle, self.vram = busy_before, never_idle, list(vram)
        self.pending, self.fail_prompt, self.bad_history = pending, fail_prompt, bad_history
        self.pid, self.stopped = None, False

    def _lock_free(self):
        try:
            os.close(ga.try_admit(self.paths["lock"], self.paths["state"]))
            return True
        except ga.Busy:
            return False

    def _log(self, what, body=None):
        self.calls.append((what, body, self._lock_free()))

    def _ours_active(self):
        return self.pid and not self.stopped and self.polls < self.finish_after

    def get(self, path, timeout=30):
        if path == "/queue":
            self._log("queue")
            if self.never_idle or (self.busy_before and not self.pid):
                return {"queue_running": [[0, "someone-else", {}, {}, []]], "queue_pending": []}
            if self._ours_active():
                state = "queue_pending" if self.pending else "queue_running"
                return {"queue_running": [], "queue_pending": [], state: [[1, self.pid, {}, {}, []]]}
            return {"queue_running": [], "queue_pending": []}
        if path.startswith("/history/"):
            self.polls += 1
            self._log("history")
            if self.bad_history:
                raise ValueError("not json")
            if self.polls < self.finish_after:
                return {}
            return {self.pid: {"outputs": {"9": {"images": [{"filename": "a.png", "subfolder": ""}]}},
                               "status": {"status_str": "success"}}}
        if path == "/system_stats":
            self._log("stats")
            mib = self.vram.pop(0) if len(self.vram) > 1 else self.vram[0]
            return {"devices": [{"torch_vram_total": mib * 2**20}]}
        raise AssertionError(path)

    def post(self, path, body, timeout=30):
        self._log(path, body)
        if path == "/prompt":
            if self.fail_prompt:
                raise self.fail_prompt
            self.pid = "p-1"
            return {"prompt_id": self.pid}
        if path in ("/interrupt", "/queue"):
            self.stopped = True
        return {}

    def raw(self, path, timeout=60):
        self._log("view", path)
        return PNG

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def paths(tmp_path):
    return {"lock": str(tmp_path / "gpu.lock"), "state": str(tmp_path / "gpu_guard_state.json")}


@pytest.fixture
def run(paths, monkeypatch):
    monkeypatch.setattr(comfy_gate, "SETTLE_SECONDS", 10)
    clock = Clock()

    def go(comfy, graph=GRAPH, timeout=5):
        return comfy_gate.generate(graph, timeout, comfy=comfy,
                                   admit=lambda: ga.try_admit(paths["lock"], paths["state"]),
                                   clock=clock, sleep=clock.sleep)
    return go


def lock_free(paths):
    try:
        os.close(ga.try_admit(paths["lock"], paths["state"]))
        return True
    except ga.Busy:
        return False


@pytest.fixture
def other_holder(paths):
    fd = os.open(paths["lock"], os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield
    os.close(fd)


def test_generates_while_admitted_then_frees_comfy_before_release(run, paths):
    c = FakeComfy(paths)
    out = run(c)
    assert out["ok"] and base64.b64decode(out["png_b64"]) == PNG and out["prompt_id"] == "p-1"
    assert all(not free for _, _, free in c.calls)            # every ComfyUI call ran admitted
    assert ("/free", {"unload_models": True, "free_memory": True}, False) in c.calls
    assert c.names().index("/free") > c.names().index("view")
    assert "/interrupt" not in c.names() and lock_free(paths)


@pytest.mark.parametrize("why", ["holder", "restore"])
def test_busy_gpu_refuses_without_touching_comfy(run, paths, why, request):
    if why == "holder":
        request.getfixturevalue("other_holder")
    else:
        open(paths["state"], "w").close()
    c = FakeComfy(paths)
    out = run(c)
    assert out["ok"] is False and out["busy"] is True and c.calls == []


def test_work_already_in_comfy_is_refused_and_left_alone(run, paths):
    c = FakeComfy(paths, busy_before=True)
    out = run(c)
    assert out["busy"] is True and "outside" in out["error"]
    assert c.names() == ["queue"] and lock_free(paths)        # no prompt, no interrupt, no /free


@pytest.mark.parametrize("pending,stop", [(False, "/interrupt"), (True, "/queue")])
def test_timeout_stops_only_our_prompt_then_frees_and_releases(run, paths, pending, stop):
    c = FakeComfy(paths, finish_after=10**6, pending=pending)
    out = run(c, timeout=3)
    assert out["ok"] is False and "did not finish within 3s" in out["error"]
    body = {"prompt_id": "p-1"} if stop == "/interrupt" else {"delete": ["p-1"]}
    assert (stop, body, False) in c.calls
    assert {"/interrupt", "/queue"} - {stop} & set(c.names()) == set()
    assert c.names().index("/free") > c.names().index(stop)
    assert lock_free(paths)


def test_a_bad_answer_mid_run_still_settles_and_releases(run, paths):
    c = FakeComfy(paths, bad_history=True)
    out = run(c)
    assert out["ok"] is False and "ValueError" in out["error"]
    assert "/interrupt" in c.names() and "/free" in c.names() and lock_free(paths)


def test_comfy_unreachable_at_queue_time_releases_without_settling(run, paths):
    c = FakeComfy(paths, fail_prompt=urllib.error.URLError("refused"))
    out = run(c)
    assert out["ok"] is False and "unreachable" in out["error"]
    assert "/free" not in c.names() and lock_free(paths)


def test_settle_is_bounded_and_reported_when_comfy_never_goes_idle(run, paths):
    c = FakeComfy(paths, finish_after=10**6)
    orig = c.get

    def get(path, timeout=30):
        if path == "/queue" and c.pid:
            c._log("queue")
            return {"queue_running": [[1, "p-1", {}, {}, []]], "queue_pending": []}
        return orig(path, timeout)

    c.get = get
    out = run(c, timeout=2)
    assert any("still busy" in n for n in out["settle"]) and lock_free(paths)
    assert "/free" in c.names()


def test_free_wait_is_bounded_and_reported(run, paths):
    c = FakeComfy(paths, vram=(9000,))
    out = run(c)
    assert out["ok"] and any("MiB after /free" in n for n in out["settle"]) and lock_free(paths)


def test_free_waits_for_vram_to_drop(run, paths):
    c = FakeComfy(paths, vram=(9000, 9000, 0))
    out = run(c)
    assert out["ok"] and "settle" not in out
    assert c.names().count("stats") == 3


@pytest.mark.parametrize("graph,timeout", [(None, 5), ({}, 5), ([1], 5), (GRAPH, "soon")])
def test_bad_requests_take_no_admission(run, paths, graph, timeout, other_holder):
    c = FakeComfy(paths)
    out = run(c, graph=graph, timeout=timeout)
    assert out["ok"] is False and "busy" not in out and c.calls == []


def test_concurrent_generations_queue_in_process_instead_of_failing(paths, monkeypatch):
    monkeypatch.setattr(comfy_gate, "SETTLE_SECONDS", 10)
    inside, release = threading.Event(), threading.Event()
    admit = lambda: ga.try_admit(paths["lock"], paths["state"])

    class Slow(FakeComfy):
        def post(self, path, body, timeout=30):
            if path == "/prompt":
                inside.set()
                release.wait(10)
            return super().post(path, body, timeout)

    results = {}
    t1 = threading.Thread(target=lambda: results.setdefault(
        "a", comfy_gate.generate(GRAPH, 5, comfy=Slow(paths), admit=admit, sleep=lambda s: None)))
    t1.start()
    assert inside.wait(10)
    t2 = threading.Thread(target=lambda: results.setdefault(
        "b", comfy_gate.generate(GRAPH, 5, comfy=FakeComfy(paths), admit=admit, sleep=lambda s: None)))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()
    release.set()
    t1.join(10)
    t2.join(10)
    assert results["a"]["ok"] and results["b"]["ok"] and lock_free(paths)


def test_dispatcher_route_needs_the_token_and_runs_the_gate(monkeypatch):
    seen = []
    monkeypatch.setattr(comfy_gate, "generate", lambda graph, timeout: seen.append((graph, timeout))
                        or {"ok": True, "png_b64": "eA=="})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dispatch.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/comfy/generate"
    body = json.dumps({"graph": GRAPH, "timeout": 42}).encode()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=10)
        assert e.value.code == 401 and seen == []
        req = urllib.request.Request(url, data=body, headers={"X-Nomad-Token": dispatch.TOKEN})
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert json.load(resp) == {"ok": True, "png_b64": "eA=="}
        assert seen == [(GRAPH, 42)]
    finally:
        srv.shutdown()
        srv.server_close()
