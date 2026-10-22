"""Private native launch wrapper: record attempt/process identity before exec.

Invoked by native.py via an absolute script path. Configuration arrives on a private pipe,
is never persisted, and is not derived from repository instructions. No hooks are bypassed.
"""
import json
import ctypes
import os
from pathlib import Path
import subprocess
import signal
import sys
import time


def process_start(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def descendants(root):
    """Snapshot identities of live descendants, including adopted setsid children."""
    rows = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z':
                rows[int(path.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned = {root}
    while True:
        added = {pid for pid, (parent, _) in rows.items() if parent in owned}
        if added <= owned:
            return {pid: rows[pid][1] for pid in owned if pid != root and pid in rows}
        owned |= added


def supervise(child, wall_seconds, grace_seconds):
    deadline = time.monotonic() + wall_seconds
    timed_out = False
    child_status = None
    while True:
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            sig = signal.SIGTERM if now < deadline + grace_seconds else signal.SIGKILL
            for descendant, started in descendants(os.getpid()).items():
                if process_start(descendant) == started:
                    try:
                        os.kill(descendant, sig)
                    except ProcessLookupError:
                        pass
        # Reap every orphan, not just the CLI. WNOHANG keeps the wall deadline active
        # while an escaped descendant outlives its original parent.
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            child.returncode = child_status
            return (124 if timed_out else child_status), timed_out
        if pid:
            if pid == child.pid:
                child_status = os.waitstatus_to_exitcode(status)
            continue
        time.sleep(0.02)


def main():
    record_path = Path(sys.argv[1])
    request = json.load(sys.stdin)
    wall_seconds, grace_seconds = request['wall_seconds'], request['termination_grace_seconds']
    if type(wall_seconds) is not int or wall_seconds <= 0:
        raise RuntimeError('supervisor requires a positive wall-time budget')
    if type(grace_seconds) not in (int, float) or not 0 <= grace_seconds <= 10:
        raise RuntimeError('invalid supervisor termination grace')
    # Keep orphaned/setsid descendants attached to this supervisor. A successful exit record
    # is written only after every child has exited, not merely after the immediate CLI exits.
    if not sys.platform.startswith('linux') or ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('native supervisor requires Linux child-subreaper support')
    signal.signal(signal.SIGTERM, lambda *_: None)
    record = {"schema_version": 1, "attempt_id": request['attempt_id'],
              "generation": request['generation'], "pid": os.getpid(),
              "start_time": process_start(os.getpid())}
    if record['start_time'] is None:
        raise RuntimeError('cannot establish process identity')
    fd = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    stdin = request.get('stdin_path')
    fd = os.open(stdin if stdin else os.devnull, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        child = subprocess.Popen(request['command'], env=request['environment'], stdin=fd)
    finally:
        os.close(fd)
    record['exit_code'], record['wall_budget_exceeded'] = supervise(child, wall_seconds, grace_seconds)
    temporary = record_path.with_name('.exit-' + os.urandom(16).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, record_path)
    return record['exit_code']


if __name__ == '__main__':
    sys.exit(main())
