"""Shared GPU admission for host-native GPU services (#1462). Stdlib only; load it by path.

Same protocol as agent_execution/resources.py GpuLease, llama/gpu-lock.sh, the media gpu_guard and
content-transcribe: flock LOCK_EX|LOCK_NB on /tmp/nomad-gpu.lock, a regular single-link file owned
by this user and not group/other writable, still the same inode after the flock. While the
gpu_guard state file exists a restore is unfinished, and no new owner may load anything.

Three ways in:
- try_admit: per-request, never waits. Raises Busy on contention or a pending restore.
- wait_admit: bounded wait, for a resident tenant loading at startup. The guard starts tenants
  while it still holds admission and never waits for them, so refusing outright would kill every
  tenant it restores. Nothing is held while waiting, so no lock-order cycle is possible.
- request_admission: try_admit, or, when the lock is busy, a lease from the gpu_guard holding it.
  The guard writes {"token", "pid"} to the lease file while it holds admission and passes the token
  to the caller it made room for (header X-Nomad-Gpu-Lease). The token alone is not enough: the
  lease pid must hold the flock on the lock's inode right now (/proc/locks), so a lease left by
  a dead guard is refused. This coordinates cooperating same-user services; it is not a boundary
  against a hostile process of the same user, which could take the lock itself.

Env: NOMAD_GPU_LOCK, NOMAD_GPU_GUARD_STATE, NOMAD_GPU_LEASE_FILE.
"""
import contextlib
import fcntl
import hmac
import json
import os
import stat
import time

_STATE_DIR = os.path.expanduser("~/.local/state/content-media-worker")
LOCK = os.environ.get("NOMAD_GPU_LOCK", "/tmp/nomad-gpu.lock")
GUARD_STATE = os.environ.get("NOMAD_GPU_GUARD_STATE", os.path.join(_STATE_DIR, "gpu_guard_state.json"))
LEASE_FILE = os.environ.get("NOMAD_GPU_LEASE_FILE", os.path.join(_STATE_DIR, "gpu_lease.json"))
LEASE_HEADER = "X-Nomad-Gpu-Lease"
MAX_LEASE_BYTES = 4096


class Busy(RuntimeError):
    """Another workload holds admission, or a gpu_guard restore is pending."""


class Unsafe(RuntimeError):
    """The lock or lease file is not one this user can trust."""


def _check(fd, what):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
            or info.st_mode & 0o022):
        raise Unsafe(f"{what} must be a regular single-link file owned by this user, not group/other writable")
    return info


def _open_lock(lock):
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    try:
        return fd, _check(fd, "GPU lock")
    except BaseException:
        os.close(fd)
        raise


def _restore_pending(state):
    try:
        os.lstat(state)
    except FileNotFoundError:
        return False
    return True


def try_admit(lock=None, state=None):
    """Hold admission without waiting. Returns the fd; closing it releases admission."""
    lock, state = lock or LOCK, state or GUARD_STATE
    fd, info = _open_lock(lock)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("GPU busy: another GPU workload holds admission") from exc
        current = os.stat(lock, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise Unsafe("GPU lock path changed during admission")
        # Checked under the lock: the guard removes its state before it unlocks after a good restore.
        if _restore_pending(state):
            raise Busy("GPU busy: a gpu_guard restore is pending; run 'gpu_guard recover' first")
        return fd
    except BaseException:
        os.close(fd)
        raise


def wait_admit(timeout, poll=1.0, lock=None, state=None, clock=time.monotonic, sleep=time.sleep):
    """try_admit, retried until `timeout` seconds pass. Holds nothing between attempts."""
    deadline = clock() + timeout
    while True:
        try:
            return try_admit(lock, state)
        except Busy:
            if clock() >= deadline:
                raise
        sleep(poll)


def _flock_holders(info, proc_locks):
    """Pids holding an exclusive flock on this inode, from /proc/locks."""
    dev = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    holders = set()
    with open(proc_locks) as f:
        for line in f:
            fields = line.split()
            if len(fields) < 6 or fields[1] == "->":   # "->" marks a blocked waiter
                continue
            if fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"] and fields[5] == dev:
                holders.add(int(fields[4]))
    return holders


def lease_holds(token, lock=None, lease=None, proc_locks="/proc/locks"):
    """True when `token` is the live lease of the process holding the GPU lock right now."""
    lock, lease = lock or LOCK, lease or LEASE_FILE
    if type(token) is not str or not 16 <= len(token) <= 256:
        return False
    try:
        fd = os.open(lease, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        try:
            _check(fd, "GPU lease")
        except Unsafe:
            return False
        raw = os.read(fd, MAX_LEASE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_LEASE_BYTES:
        return False
    try:
        held = json.loads(raw)
        expected, pid = held["token"], held["pid"]
    except (ValueError, TypeError, KeyError):
        return False
    if type(expected) is not str or type(pid) is not int or not hmac.compare_digest(expected, token):
        return False
    try:
        info = os.stat(lock, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and pid in _flock_holders(info, proc_locks)


@contextlib.contextmanager
def request_admission(token=None, lock=None, state=None, lease=None, proc_locks="/proc/locks"):
    """Yield "own" while this process holds admission, or "lease" under the guard that holds it."""
    try:
        fd = try_admit(lock, state)
    except Busy:
        if token is None or not lease_holds(token, lock, lease, proc_locks):
            raise
        yield "lease"
        return
    try:
        yield "own"
    finally:
        os.close(fd)
