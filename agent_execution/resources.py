"""Single-host GPU admission compatible with existing command-wrapper flock users.

Trusted controller configuration supplies paths. No model loading, eviction, service restart,
or recovery is performed here. Consumers must all participate in the same lock protocol;
this primitive alone cannot coordinate unmodified media/inference callers.
"""
import fcntl
import os
from pathlib import Path
import stat
import time

from .contracts import ContractError


GPU_LOCK = Path('/tmp/nomad-gpu.lock')


class GpuLease:
    def __init__(self, *, lock_path=GPU_LOCK, recovery_paths=(), wait_seconds=0):
        if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 60:
            raise ContractError('GPU admission wait must be bounded to 0..60 seconds')
        self.path = Path(lock_path)
        self.recovery_paths = tuple(Path(p) for p in recovery_paths)
        self.wait_seconds, self.fd = wait_seconds, None

    def __enter__(self):
        if self.fd is not None:
            raise ContractError('GPU lease already entered')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise ContractError('GPU lock must be a regular singly linked file owned by controller')
            if info.st_mode & 0o022:
                raise ContractError('GPU lock is writable by another user')
            deadline = time.monotonic() + self.wait_seconds
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise ContractError('GPU occupied; task must wait') from exc
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            current = self.path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ContractError('GPU lock path changed during admission')
            # Check under the admission lock, not before it. A failed restoration must
            # block the next owner even after the previous holder exited.
            for pending in self.recovery_paths:
                try:
                    pending.lstat()
                except FileNotFoundError:
                    continue
                raise ContractError('GPU restoration pending; explicit recovery required')
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def detach(self):
        """Close this reference without unlocking descriptors inherited by the supervisor."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *_):
        # flock belongs to the open file description. LOCK_UN would also unlock an
        # inherited duplicate while native children still run. Last close releases it.
        self.detach()


def local_gpu_admission(*, media_state_path, lock_path=GPU_LOCK, wait_seconds=0):
    """Controller resource_for callback with an explicit media restoration interlock."""
    from .contracts import Boundary
    from contextlib import nullcontext
    def admit(task, agent):
        if agent.boundary != Boundary.LOCAL:
            return nullcontext()
        return GpuLease(lock_path=lock_path, recovery_paths=(media_state_path,), wait_seconds=wait_seconds)
    return admit
