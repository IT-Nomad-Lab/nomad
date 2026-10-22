"""Shared GPU admission (#1462) against real kernel flocks held by a separate process. No GPU used."""
import importlib.util
import json
import os
import subprocess
import sys
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("gpu_admission", os.path.join(HERE, "gpu_admission.py"))
ga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ga)

TOKEN = "t" * 32
HOLDER = textwrap.dedent("""
    import fcntl, json, os, sys
    lock, lease, token = sys.argv[1:4]
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if lease != "-":
        with open(os.open(lease, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"token": token, "pid": os.getpid()}, f)
    print("held", flush=True)
    sys.stdin.read()
""")


@pytest.fixture
def paths(tmp_path):
    return {"lock": str(tmp_path / "gpu.lock"), "state": str(tmp_path / "gpu_guard_state.json"),
            "lease": str(tmp_path / "gpu_lease.json")}


@pytest.fixture
def holder(paths):
    procs = []

    def hold(lease=True, token=TOKEN):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, paths["lock"], paths["lease"] if lease else "-", token],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "held"
        procs.append(p)
        return p

    yield hold
    for p in procs:
        p.stdin.close()
        p.wait(timeout=10)


def admit(paths):
    return ga.try_admit(paths["lock"], paths["state"])


def request(paths, token, proc_locks="/proc/locks"):
    return ga.request_admission(token, paths["lock"], paths["state"], paths["lease"], proc_locks)


def test_free_lock_admits_and_closing_releases(paths):
    fd = admit(paths)
    with pytest.raises(ga.Busy):
        admit(paths)              # a second open file description is refused, even in-process
    os.close(fd)
    os.close(admit(paths))


def test_contention_and_pending_restore_refuse(paths, holder):
    p = holder(lease=False)
    with pytest.raises(ga.Busy, match="holds admission"):
        admit(paths)
    p.stdin.close()
    p.wait(timeout=10)
    open(paths["state"], "w").close()
    with pytest.raises(ga.Busy, match="restore is pending"):
        admit(paths)
    os.unlink(paths["state"])
    os.close(admit(paths))


@pytest.mark.parametrize("make", ["symlink", "hardlink", "group_writable"])
def test_unsafe_lock_files_are_refused(paths, tmp_path, make):
    target = tmp_path / "real"
    target.write_text("")
    target.chmod(0o600)
    if make == "symlink":
        os.symlink(target, paths["lock"])
    elif make == "hardlink":
        os.link(target, paths["lock"])
    else:
        target.chmod(0o620)
        os.rename(target, paths["lock"])
    with pytest.raises((ga.Unsafe, OSError)):
        admit(paths)


def test_wait_admit_is_bounded_and_holds_nothing_while_waiting(paths, holder):
    p = holder(lease=False)
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    with pytest.raises(ga.Busy):
        ga.wait_admit(3, poll=1, lock=paths["lock"], state=paths["state"], clock=lambda: now[0], sleep=sleep)
    assert sleeps == [1, 1, 1]

    def release_then_sleep(seconds):
        p.stdin.close()
        p.wait(timeout=10)
        sleeps.append(seconds)

    fd = ga.wait_admit(30, poll=1, lock=paths["lock"], state=paths["state"], clock=lambda: 0.0,
                       sleep=release_then_sleep)
    os.close(fd)


def test_wait_admit_keeps_waiting_while_a_restore_is_pending(paths):
    open(paths["state"], "w").close()
    attempts = []

    def sleep(_):
        attempts.append(1)
        if len(attempts) == 2:
            os.unlink(paths["state"])

    fd = ga.wait_admit(30, poll=1, lock=paths["lock"], state=paths["state"], clock=lambda: 0.0, sleep=sleep)
    assert len(attempts) == 2
    os.close(fd)


def test_guard_lease_passes_only_with_its_token_while_it_holds_the_lock(paths, holder):
    p = holder()
    with request(paths, TOKEN) as how:
        assert how == "lease"
    for bad in (None, "x" * 32, TOKEN[:-1] + "u", "short"):
        with pytest.raises(ga.Busy), request(paths, bad):
            pass
    p.stdin.close()
    p.wait(timeout=10)
    with request(paths, TOKEN) as how:      # holder gone: the lock is free, so this is our own admission
        assert how == "own"
        with pytest.raises(ga.Busy):
            admit(paths)


def test_stale_lease_is_refused_when_another_process_holds_the_lock(paths, holder):
    p = holder()
    lease = json.load(open(paths["lease"]))
    p.stdin.close()
    p.wait(timeout=10)
    holder(lease=False)                     # someone else now holds the lock; the old lease file remains
    assert json.load(open(paths["lease"])) == lease
    with pytest.raises(ga.Busy), request(paths, TOKEN):
        pass


def test_lease_file_must_be_private_and_well_formed(paths, holder):
    holder()
    os.chmod(paths["lease"], 0o644)
    assert ga.lease_holds(TOKEN, paths["lock"], paths["lease"])
    os.chmod(paths["lease"], 0o620)
    assert not ga.lease_holds(TOKEN, paths["lock"], paths["lease"])
    os.chmod(paths["lease"], 0o600)
    for body in ("{}", "[]", "not json", json.dumps({"token": TOKEN, "pid": "1"}),
                 json.dumps({"token": TOKEN, "pid": os.getpid(), "pad": "x" * 5000})):
        with open(paths["lease"], "w") as f:
            f.write(body)
        assert not ga.lease_holds(TOKEN, paths["lock"], paths["lease"]), body


def test_lease_pid_must_appear_as_the_holder_in_proc_locks(paths, holder, tmp_path):
    holder()
    fake = tmp_path / "locks"
    fake.write_text("")
    assert not ga.lease_holds(TOKEN, paths["lock"], paths["lease"], str(fake))
    real = open("/proc/locks").read()
    pid = json.load(open(paths["lease"]))["pid"]
    blocked_only = "\n".join(line.replace(": FLOCK", ": -> FLOCK", 1) for line in real.splitlines()
                             if f" {pid} " in line)
    fake.write_text(blocked_only + "\n")
    assert not ga.lease_holds(TOKEN, paths["lock"], paths["lease"], str(fake))


def test_own_admission_is_released_when_the_block_ends_or_raises(paths):
    with request(paths, None) as how:
        assert how == "own"
    os.close(admit(paths))
    with pytest.raises(RuntimeError), request(paths, TOKEN):
        raise RuntimeError("render failed")
    os.close(admit(paths))
