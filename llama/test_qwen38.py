"""llama/qwen38.sh start (#1428): idempotent, and one retry when the MTP draft fails to load.

systemctl, systemd-run, curl, journalctl, ip and sleep are stubs on PATH; nothing touches the GPU
or systemd. Run: python3 -m pytest llama/test_qwen38.py -q
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "qwen38.sh")
SERVER, BRIDGE = "nomad-llama-8240", "nomad-llama-8240-bridge"

# Each stub keeps its state as files in $ST. Server launches take their outcome, in order, from
# $ST/outcomes: ok (comes up healthy), draft (dies on the draft load), other (dies otherwise).
STUBS = {
    "systemctl": """
case " $* " in
  *" is-active "*) [ -e "$ST/${@: -1}.active" ] ;;
  *) exit 0 ;;
esac""",
    "systemd-run": """
unit=$(printf '%s\\n' "$@" | sed -n 's/^--unit=//p')
echo "$unit" >> "$ST/launches"
if [ "$unit" = nomad-llama-8240 ]; then
  n=$(grep -cx nomad-llama-8240 "$ST/launches")
  case $(sed -n "${n}p" "$ST/outcomes") in
    ok) touch "$ST/$unit.active" "$ST/healthy" ;;
    draft) echo "E srv    load_model: failed to load draft model, 'mtp.gguf'" >> "$ST/journal" ;;
    *) echo "E srv  llama_server: some other error" >> "$ST/journal" ;;
  esac
else
  touch "$ST/$unit.active"
fi""",
    "curl": """
case "$*" in
  */health*) [ -e "$ST/healthy" ] ;;
  */api/ps*) echo '{"models": []}' ;;
esac""",
    "journalctl": 'cat "$ST/journal" 2>/dev/null; true',
    "ip": "echo 'default via 192.0.2.1 dev eth0'",
    "sleep": "true",
}


@pytest.fixture
def run(tmp_path):
    bin_dir, st = tmp_path / "bin", tmp_path / "st"
    bin_dir.mkdir()
    st.mkdir()
    for name, body in STUBS.items():
        p = bin_dir / name
        p.write_text("#!/usr/bin/env bash\n" + body + "\n")
        p.chmod(0o755)

    def _run(outcomes=(), up=()):
        (st / "outcomes").write_text("".join(o + "\n" for o in outcomes))
        for unit in up:
            (st / f"{unit}.active").touch()
        if SERVER in up:
            (st / "healthy").touch()
        env = {**os.environ, "ST": str(st), "PATH": f"{bin_dir}:/usr/bin:/bin",
               "NOMAD_GPU_LOCK": str(tmp_path / "gpu.lock"),
               "NOMAD_GPU_GUARD_STATE": str(tmp_path / "no_such_guard_state")}
        r = subprocess.run(["bash", SCRIPT, "start"], capture_output=True, text=True, env=env, timeout=30)
        launches = (st / "launches").read_text().split() if (st / "launches").exists() else []
        return r, launches

    return _run


pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="no bash")


def test_script_parses():
    r = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_context_and_kv_cache_defaults():
    # No dry-run mode: pin the launcher text without running a server (#1505).
    from pathlib import Path
    script = Path(SCRIPT).read_text()
    assert 'CTX=${QWEN38_CTX:-65536}' in script
    assert '-c "$CTX" -ctk q8_0 -ctv q8_0' in script


def test_start_when_already_up_returns_0_and_launches_nothing(run):
    r, launches = run(up=(SERVER, BRIDGE))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "already up" in r.stdout
    assert launches == []


def test_start_with_the_server_up_only_adds_the_bridge(run):
    r, launches = run(up=(SERVER,))
    assert r.returncode == 0, r.stdout + r.stderr
    assert launches == [BRIDGE]


def test_start_from_cold_launches_server_and_bridge(run):
    r, launches = run(outcomes=("ok",))
    assert r.returncode == 0, r.stdout + r.stderr
    assert launches == [SERVER, BRIDGE]


def test_a_draft_load_failure_is_retried_once(run):
    r, launches = run(outcomes=("draft", "ok"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "retrying once" in r.stdout
    assert launches == [SERVER, BRIDGE, SERVER]


def test_a_second_draft_failure_gives_up(run):
    r, launches = run(outcomes=("draft", "draft", "ok"))
    assert r.returncode == 1
    assert launches.count(SERVER) == 2


def test_any_other_load_failure_is_not_retried(run):
    r, launches = run(outcomes=("other", "ok"))
    assert r.returncode == 1
    assert launches.count(SERVER) == 1


# --- shared GPU admission (#1462): qwen38.sh and qwen3vl.sh take /tmp/nomad-gpu.lock ----------
# Same stubs idea, plus an Ollama that has a model loaded and a log of every state-changing call.
# The lock is a temp file (NOMAD_GPU_LOCK) held from the test with flock; nothing real is touched.
import fcntl

GPU_STUBS = {
    "systemctl": """
case " $* " in
  *" is-active "*) [ -e "$ST/${@: -1}.active" ] ;;
  *" stop "*) echo "stop $*" >> "$ST/changes"; exit 0 ;;
  *) exit 0 ;;
esac""",
    "systemd-run": """
unit=$(printf '%s\\n' "$@" | sed -n 's/^--unit=//p')
echo "launch $unit" >> "$ST/changes"
[ -e /proc/self/fd/9 ] && echo "$unit" >> "$ST/inherited_lock_fd"
touch "$ST/$unit.active"
case $unit in nomad-llama-8240|nomad-llama-8241) touch "$ST/healthy" ;; esac""",
    "curl": """
case "$*" in
  */health*) [ -e "$ST/healthy" ] ;;
  */api/ps*) echo '{"models": [{"name": "private"}]}' ;;
  */api/generate*) echo "unload $*" >> "$ST/changes" ;;
esac""",
    "journalctl": "true",
    "ip": "echo 'default via 192.0.2.1 dev eth0'",
    "sleep": "true",
}
SCRIPTS = {"qwen38": os.path.join(HERE, "qwen38.sh"), "qwen3vl": os.path.join(HERE, "qwen3vl.sh")}


@pytest.fixture
def gpu(tmp_path):
    bin_dir, st = tmp_path / "bin", tmp_path / "st"
    bin_dir.mkdir()
    st.mkdir()
    for name, body in GPU_STUBS.items():
        p = bin_dir / name
        p.write_text("#!/usr/bin/env bash\n" + body + "\n")
        p.chmod(0o755)
    lock = tmp_path / "gpu.lock"
    held = []

    def _run(script, cmd, hold=False, guard_state=False, lock_mode=None, symlink=False):
        if hold:
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(fd)
        if lock_mode is not None:
            lock.touch()
            lock.chmod(lock_mode)
        if symlink:
            (tmp_path / "real.lock").touch()
            lock.symlink_to(tmp_path / "real.lock")
        state = tmp_path / "guard_state.json"
        if guard_state:
            state.write_text("{}")
        env = {**os.environ, "ST": str(st), "PATH": f"{bin_dir}:/usr/bin:/bin", "NOMAD_GPU_LOCK": str(lock),
               "NOMAD_GPU_GUARD_STATE": str(state)}
        r = subprocess.run(["bash", SCRIPTS[script], cmd], capture_output=True, text=True, env=env, timeout=30)
        read = lambda n: (st / n).read_text().splitlines() if (st / n).exists() else []
        return r, read("changes"), read("inherited_lock_fd")

    yield _run
    for fd in held:
        os.close(fd)


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("cmd", ["start", "stop"])
def test_g1_held_lock_exits_75_and_changes_nothing(gpu, script, cmd):
    r, changes, _ = gpu(script, cmd, hold=True)
    assert r.returncode == 75, r.stdout + r.stderr
    assert "GPU busy" in r.stderr
    assert changes == []          # no ollama unload, no launch, no stop


@pytest.mark.parametrize("script", SCRIPTS)
def test_g1_pending_gpu_guard_restore_blocks_start(gpu, script):
    r, changes, _ = gpu(script, "start", guard_state=True)
    assert r.returncode == 75, r.stdout + r.stderr
    assert "restore is pending" in r.stderr
    assert changes == []


@pytest.mark.parametrize("script", SCRIPTS)
def test_pending_restore_does_not_block_stop(gpu, script):
    r, changes, _ = gpu(script, "stop", guard_state=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("stop ") for c in changes)


@pytest.mark.parametrize("script", SCRIPTS)
def test_g2_free_lock_starts_as_before_and_server_does_not_hold_the_lock_fd(gpu, script):
    r, changes, inherited = gpu(script, "start")
    assert r.returncode == 0, r.stdout + r.stderr
    assert changes[0].startswith("unload ")                   # ollama unloaded first, as before
    assert [c for c in changes if c.startswith("launch ")] == [
        f"launch {u}" for u in (("nomad-llama-8240", "nomad-llama-8240-bridge") if script == "qwen38"
                                else ("nomad-llama-8241", "nomad-llama-8241-bridge"))]
    assert inherited == []                                    # neither launch inherited fd 9


@pytest.mark.parametrize("script", SCRIPTS)
def test_g2_free_lock_stop_stops(gpu, script):
    r, changes, _ = gpu(script, "stop")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "stopped" in r.stdout
    assert len(changes) == 1 and changes[0].startswith("stop ")


@pytest.mark.parametrize("script", SCRIPTS)
def test_lock_is_released_when_the_script_exits(gpu, script, tmp_path):
    gpu(script, "start")
    fd = os.open(tmp_path / "gpu.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)        # would raise if a server kept it
    finally:
        os.close(fd)


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("case", ["symlink", "writable"])
def test_unsafe_lock_file_is_refused_without_changes(gpu, script, case):
    r, changes, _ = gpu(script, "start", symlink=case == "symlink", lock_mode=0o666 if case == "writable" else None)
    assert r.returncode == 1, r.stdout + r.stderr
    assert changes == []
