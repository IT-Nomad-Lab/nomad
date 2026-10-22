"""ComfyUI under shared GPU admission (#1462): POST /comfy/generate on the dispatcher.

The v2 engine builds the text-to-image graph but runs in a container as root, so it can neither
see nor safely open the host's /tmp/nomad-gpu.lock. The dispatcher runs on the host as the lock's
owner, so it takes admission for the engine:

  1. admission, without waiting (dispatcher/gpu_admission.try_admit). A gpu_guard job, another
     admitted workload or a pending restore refuses with busy. One generation per dispatcher at a
     time; a second request queues in-process, bounded by its own timeout.
  2. ComfyUI must be idle. Work already there came from outside admission (the web UI, a script),
     so this refuses instead of queueing behind it.
  3. queue the graph, wait for it, read the first image.
  4. always, while still admitted: if our prompt is still queued or running, delete it and
     interrupt it (by prompt_id, never someone else's), wait for ComfyUI to go idle, then /free and
     wait for its VRAM to drop. Only then release.

ComfyUI down counts as idle and empty, the same as gpu_guard. Callers that bypass this (the
ComfyUI web UI, scripts posting to :8188) are still not coordinated.
"""
import base64
import contextlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import gpu_admission

COMFY_URL = os.environ.get("NOMAD_COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800
SETTLE_SECONDS = float(os.environ.get("NOMAD_COMFY_SETTLE_SECONDS", "120"))
FREE_WAIT_SECONDS = 30
FREED_MIB = 1024

_serial = threading.Lock()


class Comfy:
    """The few ComfyUI calls the gate makes. Tests swap in a fake."""

    def __init__(self, url=COMFY_URL):
        self.url = url

    def _open(self, path, body=None, timeout=30):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def get(self, path, timeout=30):
        return json.loads(self._open(path, timeout=timeout) or b"{}")

    def post(self, path, body, timeout=30):
        raw = self._open(path, body, timeout)
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def raw(self, path, timeout=60):
        return self._open(path, timeout=timeout)


def _where(queue, pid):
    """'running', 'pending' or None for prompt `pid` in a /queue answer."""
    for state in ("running", "pending"):
        if any(isinstance(item, list) and len(item) > 1 and item[1] == pid
               for item in queue.get(f"queue_{state}") or []):
            return state
    return None


def _idle(comfy):
    try:
        q = comfy.get("/queue", 15)
    except (OSError, ValueError):     # down: nothing runs there
        return True
    return not (q.get("queue_running") or q.get("queue_pending"))


def _vram_mib(comfy):
    try:
        return int(comfy.get("/system_stats", 15)["devices"][0].get("torch_vram_total", 0)) // 2**20
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return 0


def _settle(comfy, pid, clock, sleep):
    """Stop our prompt if it is still there, wait for idle, then empty ComfyUI. Never raises."""
    notes = []
    with contextlib.suppress(OSError, ValueError):
        if pid:
            where = _where(comfy.get("/queue", 15), pid)
            if where == "pending":
                comfy.post("/queue", {"delete": [pid]})
            elif where == "running":
                comfy.post("/interrupt", {"prompt_id": pid})
    deadline = clock() + SETTLE_SECONDS
    while not _idle(comfy):
        if clock() >= deadline:
            notes.append(f"ComfyUI still busy after {SETTLE_SECONDS:.0f} s; released anyway")
            break
        sleep(2)
    with contextlib.suppress(OSError, ValueError):
        comfy.post("/free", {"unload_models": True, "free_memory": True})
    deadline = clock() + FREE_WAIT_SECONDS
    while _vram_mib(comfy) > FREED_MIB:
        if clock() >= deadline:
            notes.append(f"ComfyUI still holds {_vram_mib(comfy)} MiB after /free")
            break
        sleep(1)
    return notes


def _run(comfy, graph, timeout, clock, sleep, queued):
    if not _idle(comfy):
        return {"ok": False, "busy": True, "error": "ComfyUI is already running work from outside "
                                                     "GPU admission; not queueing behind it"}
    try:
        pid = comfy.post("/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex})["prompt_id"]
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"ComfyUI refused the graph: HTTP {e.code}"}
    except (OSError, KeyError, TypeError) as e:
        return {"ok": False, "error": f"ComfyUI unreachable or no prompt_id: {type(e).__name__}: {e}"}
    queued.append(pid)
    deadline = clock() + timeout
    while clock() < deadline:
        h = comfy.get(f"/history/{urllib.parse.quote(pid, safe='')}")
        if pid in h:
            outputs = h[pid].get("outputs") or {}
            for node in outputs.values():
                for img in node.get("images") or []:
                    q = urllib.parse.urlencode({"filename": img["filename"],
                                                "subfolder": img.get("subfolder", ""),
                                                "type": img.get("type", "output")})
                    return {"ok": True, "prompt_id": pid,
                            "png_b64": base64.b64encode(comfy.raw(f"/view?{q}")).decode()}
            status = h[pid].get("status") or {}
            return {"ok": False, "error": f"ComfyUI finished with no image output "
                                          f"(status {status.get('status_str', '?')}): "
                                          f"{json.dumps(outputs)[:200]}"}
        sleep(1.0)
    return {"ok": False, "error": f"ComfyUI did not finish within {timeout:.0f}s"}


def generate(graph, timeout=None, comfy=None, admit=None, clock=time.monotonic, sleep=time.sleep):
    """Run one ComfyUI API-format graph under admission. Returns {ok, png_b64, prompt_id} or
    {ok: False, error, busy?}. Never leaves our prompt or ComfyUI's models behind on release."""
    if not isinstance(graph, dict) or not graph:
        return {"ok": False, "error": "graph must be a non-empty ComfyUI API-format object"}
    try:
        timeout = min(max(float(timeout or DEFAULT_TIMEOUT), 1.0), MAX_TIMEOUT)
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout must be a number of seconds"}
    comfy = comfy or Comfy()
    admit = admit or gpu_admission.try_admit
    if not _serial.acquire(timeout=timeout):
        return {"ok": False, "busy": True, "error": "another ComfyUI generation is still running"}
    try:
        try:
            fd = admit()
        except gpu_admission.Busy as e:
            return {"ok": False, "busy": True, "error": str(e)}
        queued = []
        result = {"ok": False, "error": "generation did not complete"}
        try:
            result = _run(comfy, graph, timeout, clock, sleep, queued)
        except Exception as e:                       # noqa: BLE001  a bad answer must still settle
            result = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            try:
                if queued:
                    notes = _settle(comfy, queued[0], clock, sleep)
                    if notes:
                        result["settle"] = notes
            finally:
                os.close(fd)
        return result
    finally:
        _serial.release()
