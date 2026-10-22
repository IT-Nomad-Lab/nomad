"""Independent commit verification using a controller-owned gate registry.

Gate argv and gate implementations are trusted configuration, never task CHECK text.
No merge, finalization, tracker update or service deployment happens here. Dirty candidates
require explicit finalization before this verifier can accept them.
"""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import shutil
import stat
import fcntl

from .adapter import require_result
from .contracts import CheckResult, ContractError, ResultStatus
from .native import _git, _group_members
from .tool_authority import PROTECTED


SYSTEM_CA_BUNDLE = '/etc/ssl/certs/ca-certificates.crt'


class VerificationUncertain(ContractError):
    """Check termination is unproven; preserve reservation and private runtime evidence."""


class VerificationFailed(ContractError):
    def __init__(self, message, result, evidence):
        super().__init__(message)
        self.result, self.evidence = result, evidence


@dataclass(frozen=True)
class Gate:
    gate_id: str
    argv: tuple[str, ...]
    timeout_seconds: int = 60
    read_roots: tuple[str, ...] = ()

    def __post_init__(self):
        if (not self.gate_id or type(self.argv) is not tuple or not self.argv
                or not all(type(arg) is str and arg and '\x00' not in arg for arg in self.argv)
                or not Path(self.argv[0]).is_absolute()
                or type(self.timeout_seconds) is not int or self.timeout_seconds <= 0):
            raise ContractError('gate requires explicit id, absolute executable and bounded timeout')
        if type(self.read_roots) is not tuple or any(type(root) is not str or not Path(root).is_absolute()
                                                    or Path(root) == Path('/') for root in self.read_roots):
            raise ContractError('gate read roots must be explicit absolute paths, never the filesystem root')


class CommitVerifier:
    def __init__(self, artifacts, runtime_root, gates):
        self.artifacts, self.runtime_root = artifacts, Path(runtime_root)
        self.gates = tuple(gates)
        self.processes = {}
        if len({gate.gate_id for gate in self.gates}) != len(self.gates):
            raise ContractError('duplicate verification gate')

    @property
    def max_seconds(self):
        return sum(gate.timeout_seconds + 5 for gate in self.gates) + 60

    def _claim_directory(self, run, token):
        if str(uuid.UUID(token)) != token or str(uuid.UUID(run.attempt_id)) != run.attempt_id:
            raise ContractError('invalid verifier claim identity')
        directory = self.runtime_root / run.attempt_id / token
        if directory.resolve() != directory:
            raise ContractError('verifier runtime must be canonical')
        return directory

    @staticmethod
    def _read_record(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 65536):
                raise ContractError('unsafe verifier recovery record')
            record = json.loads(stream.read(65537))
            if type(record) is not dict:
                raise ContractError('verifier recovery record must be an object')
            return record

    def run_claim(self, task, run, candidate, token):
        """Journal before launching; supervisors inherit the claim lock across controller exit."""
        directory = self._claim_directory(run, token)
        directory.mkdir(parents=True, mode=0o700)
        fd = os.open(directory / 'claim.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            scope = {'attempt_id': run.attempt_id, 'generation': run.claim.generation,
                     'token': token, 'workspace': run.workspace, 'result_commit': candidate.result_commit}
            record_fd = os.open(directory / 'claim.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(record_fd, 'w') as stream:
                json.dump(scope, stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                return self._verify(task, run, candidate, runtime_root=directory, reservation_fd=fd)
            except Exception:
                # An unexpected pipe/log/artifact error must not free check writers.
                # Closing our descriptor leaves any live supervisor holding the lock.
                os.close(fd)
                fd = None
                self.reconcile(run, candidate, token)
                raise
        finally:
            if fd is not None:
                os.close(fd)

    def reconcile(self, run, candidate, token):
        """Prove a journaled check run stopped. Never infer termination from lease expiry."""
        directory = self._claim_directory(run, token)
        try:
            fd = os.open(directory / 'claim.lock', os.O_RDWR | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise ContractError('unsafe verifier recovery lock')
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                expected = {'attempt_id': run.attempt_id, 'generation': run.claim.generation,
                            'token': token, 'workspace': run.workspace, 'result_commit': candidate.result_commit}
                if self._read_record(directory / 'claim.json') != expected:
                    raise ContractError('verifier recovery scope mismatch')
                entries = list(directory.iterdir())
                if len(entries) > 10000:
                    raise ContractError('verifier recovery scan exceeds limit')
                for entry in entries:
                    if entry.name in ('claim.json', 'claim.lock'):
                        continue
                    if not stat.S_ISDIR(entry.lstat().st_mode) or str(uuid.UUID(entry.name)) != entry.name:
                        raise ContractError('unexpected verifier runtime entry')
                    data = self._read_record(entry / 'process.json')
                    if (data.get('schema_version') != 1 or data.get('attempt_id') != entry.name
                            or data.get('generation') != 1 or type(data.get('pid')) is not int
                            or data['pid'] <= 0 or not isinstance(data.get('start_time'), str)
                            or type(data.get('exit_code')) is not int or _group_members(data['pid'])):
                        raise ContractError('verifier termination not proven')
                    proc = self.processes.get(entry)
                    if proc is not None:
                        proc.wait(timeout=0)
                        self.processes.pop(entry)
                # The inherited lock and terminal subreaper records jointly prove no
                # gate can still spawn or write, including escaped process groups.
                return True
            finally:
                os.close(fd)
        except (OSError, ValueError, ContractError, subprocess.TimeoutExpired) as exc:
            raise VerificationUncertain('verifier reconciliation refused; preserve runtime evidence') from exc

    def _pin(self, task, run, candidate):
        require_result(run, candidate)
        if candidate.status != ResultStatus.CANDIDATE_COMPLETE or candidate.result_commit is None or candidate.diff_ref:
            raise ContractError('verification requires a committed candidate')
        if (run.task_id, run.task_revision, run.policy_revision, run.base_commit) != (
                task.task_id, task.task_revision, task.execution_policy.policy_revision, task.repository.base_commit):
            raise ContractError('verification task scope mismatch')
        workspace = Path(run.workspace)
        if workspace.resolve() != workspace or workspace == Path(task.repository.path):
            raise ContractError('verification requires an isolated canonical worktree')
        expected = _git(task.repository.path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        actual = _git(run.workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        if actual != expected:
            raise ContractError('candidate belongs to another repository')
        branch = _git(run.workspace, 'branch', '--show-current').decode().strip()
        if not branch.startswith(('feat/', 'fix/')):
            raise ContractError('verification requires a feature branch')
        head = _git(run.workspace, 'rev-parse', 'HEAD').decode().strip()
        if head != candidate.result_commit or _git(run.workspace, 'status', '--porcelain', '--untracked-files=all'):
            raise ContractError('candidate commit changed or worktree is dirty')
        _git(run.workspace, 'merge-base', '--is-ancestor', run.base_commit, head)
        return head

    def _run(self, gate, run, *, runtime_root=None, reservation_fd=None):
        # Each check has its own subreaper, so even setsid orphans remain supervised
        # and bounded. A controller timeout never implies that those writers stopped.
        identity = str(uuid.uuid4())
        directory = (runtime_root if runtime_root is not None else self.runtime_root / run.attempt_id) / identity
        directory.mkdir(parents=True, mode=0o700)
        record = directory / 'process.json'
        output = directory / 'output.log'
        home = directory / 'home'
        home.mkdir(mode=0o700)
        environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
        environment.update(HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
        if Path(SYSTEM_CA_BUNDLE).is_file():
            # Workspace *.pem (certifi's bundle too) are masked; point TLS at the system store,
            # never at an inherited path.
            environment.update(SSL_CERT_FILE=SYSTEM_CA_BUNDLE, SSL_CERT_DIR=str(Path(SYSTEM_CA_BUNDLE).parent))
        command, inner_request = self._sandbox(gate, run, directory, home, identity, environment)
        request = {'attempt_id': identity, 'generation': 1, 'command': command,
                   'environment': environment, 'stdin_path': str(inner_request),
                   'wall_seconds': gate.timeout_seconds + 2, 'termination_grace_seconds': 0.2}
        with output.open('xb') as stream:
            os.chmod(output, 0o600)
            proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('launcher.py')), str(record)],
                                    cwd=run.workspace, stdin=subprocess.PIPE, stdout=stream,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    pass_fds=() if reservation_fd is None else (reservation_fd,))
        self.processes[directory] = proc
        proc.stdin.write(json.dumps(request).encode())
        proc.stdin.close()
        try:
            proc.wait(timeout=gate.timeout_seconds + 5)
        except subprocess.TimeoutExpired as exc:
            raise VerificationUncertain('check supervisor still live; evidence at ' + str(directory)) from exc
        self.processes.pop(directory)
        try:
            data = json.loads(record.read_text())
        except (OSError, ValueError) as exc:
            raise VerificationUncertain('check exit evidence missing: ' + str(directory)) from exc
        if (data.get('attempt_id'), data.get('generation'), data.get('pid')) != (identity, 1, proc.pid) or 'exit_code' not in data:
            raise VerificationUncertain('check termination not proven: ' + str(directory))
        # Oversized output refuses acceptance; a short diagnostic and full byte count
        # remain in the bounded manifest, while private raw output stays on disk.
        size = output.stat().st_size
        log_ref = None
        if size <= self.artifacts.max_bytes:
            log_ref = self.artifacts.put(output.read_bytes())
        passed = data['exit_code'] == 0 and proc.returncode == 0 and log_ref is not None
        evidence = self.artifacts.put(json.dumps({
            'gate_id': gate.gate_id, 'argv': gate.argv, 'result_commit': _git(run.workspace, 'rev-parse', 'HEAD').decode().strip(),
            'exit_code': data['exit_code'], 'wall_budget_exceeded': data.get('wall_budget_exceeded', False),
            'output_bytes': size, 'output': log_ref.to_dict() if log_ref else None,
            'supervisor': data,
        }, sort_keys=True).encode())
        return CheckResult(gate_id=gate.gate_id, status='passed' if passed else 'failed', evidence=evidence)

    def _sandbox(self, gate, run, directory, home, identity, environment):
        binary = shutil.which('bwrap')
        if binary is None:
            raise ContractError('independent checks require bubblewrap; no unsandboxed fallback')
        # Public CA certificates only: /etc/ssl/private is never mounted by default.
        roots = ['/usr', '/bin', '/lib', '/lib64', '/etc/ld.so.cache', '/etc/alternatives',
                 '/etc/passwd', '/etc/group', '/etc/ssl/certs', '/etc/ssl/openssl.cnf', *gate.read_roots]
        command = [binary, '--unshare-all', '--die-with-parent', '--new-session']
        for root in dict.fromkeys(roots):
            path = Path(root)
            if path.exists():
                command += ['--ro-bind', str(path), str(path)]
        command += ['--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp',
                    '--bind', run.workspace, run.workspace, '--bind', str(home), str(home)]
        empty_file, empty_directory = directory / 'empty-file', directory / 'empty-directory'
        empty_file.write_bytes(b'')
        empty_directory.mkdir(mode=0o700)
        scanned = 0
        protected = PROTECTED
        for parent, folders, files in os.walk(run.workspace, followlinks=False):
            for name in [*folders, *files]:
                scanned += 1
                if scanned > 100000:
                    raise ContractError('verification workspace exceeds bounded safety scan')
                path = Path(parent) / name
                info = path.lstat()
                secret = name.startswith('.env') or name.endswith(('.pem', '.key')) or name in ('id_rsa', 'id_ed25519')
                if not secret and name not in protected and not stat.S_ISSOCK(info.st_mode):
                    continue
                is_directory = stat.S_ISDIR(info.st_mode)
                public_guard = name in ('AGENTS.md', 'CLAUDE.md', 'Nomad.md', 'secret_scan.py', '.git')
                source = path if public_guard and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else (
                    empty_directory if is_directory else empty_file)
                command += ['--ro-bind', str(source), str(path)]
                if name in folders:
                    folders.remove(name)
        # Keep a subreaper inside the PID namespace too. Otherwise a successful
        # immediate check could exit and have namespace teardown kill unfinished
        # descendants while reporting success for incomplete asynchronous work.
        helper = home / 'supervisor.py'
        helper.touch(mode=0o600)
        command += ['--ro-bind', str(Path(__file__).with_name('launcher.py').resolve()), str(helper)]
        inner_request = directory / 'inner-request.json'
        fd = os.open(inner_request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'attempt_id': identity, 'generation': 1, 'command': gate.argv,
                       'environment': environment, 'stdin_path': None,
                       'wall_seconds': gate.timeout_seconds, 'termination_grace_seconds': 0.2}, stream)
        return ([*command, '--chdir', run.workspace, '--', '/usr/bin/python3', str(helper),
                 str(home / 'process.json')], inner_request)

    def __call__(self, task, run, candidate):
        return self._verify(task, run, candidate)

    def _verify(self, task, run, candidate, *, runtime_root=None, reservation_fd=None):
        if {gate.gate_id for gate in self.gates} != {criterion.id for criterion in task.acceptance_criteria}:
            raise ContractError('trusted gate registry must cover exactly the required criteria')
        commit = self._pin(task, run, candidate)
        checks = []
        for gate in self.gates:
            checks.append(self._run(gate, run, runtime_root=runtime_root, reservation_fd=reservation_fd))
            self._pin(task, run, candidate)
            if checks[-1].status != 'passed':
                evidence = self._manifest(task, run, commit, checks)
                failed = replace(candidate, status=ResultStatus.FAILED, test_results=tuple(checks),
                                 errors=(*candidate.errors, 'independent gate failed: ' + gate.gate_id),
                                 artifacts=(*candidate.artifacts, evidence))
                raise VerificationFailed('independent gate failed: ' + gate.gate_id, failed, evidence)
        evidence = self._manifest(task, run, commit, checks)
        return replace(candidate, test_results=tuple(checks)), evidence

    def _manifest(self, task, run, commit, checks):
        evidence = self.artifacts.put(json.dumps({
            'task_id': task.task_id, 'task_revision': task.task_revision, 'attempt_id': run.attempt_id,
            'generation': run.claim.generation, 'result_commit': commit,
            'checks': [check.to_dict() for check in checks],
        }, sort_keys=True).encode())
        return evidence
