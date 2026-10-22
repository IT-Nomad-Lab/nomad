"""Native adapters consuming portable contracts and durable attempt identities.

Profiles are trusted controller inputs, never task/repository-provided command strings.
Activation requires reviewed policy/tool settings and resource admission outside this class.
Only fake executables are used by the accompanying characterization tests.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import MappingProxyType

from .adapter import Cancellation, PreparedExecution, ProcessIdentity, Recovery, require_agent, require_prepared
from .context import ArtifactStore, content_ref, load_context, render_context
from .contracts import (
    AgentSpec, AttemptState, Boundary, ContractError, ExecutionEvent, ExecutionResult, Handoff,
    ResultStatus, Usage,
)
from .launcher import process_start
from .tool_authority import CANDIDATE_PROTECTED, is_protected


@dataclass(frozen=True)
class NativeProfile:
    backend: str
    agent: AgentSpec
    command_prefix: tuple[str, ...]
    settings_args: tuple[str, ...]
    cap_args: tuple[str, ...] = ()
    environment: dict = field(default_factory=dict, repr=False)
    # A profile must be separately reviewed before the controller exposes real task context.
    policy_revision: str = ''
    tool_profile_ref: str = ''

    def __post_init__(self):
        # Frozen dataclasses do not freeze caller-owned lists or dictionaries. Copy
        # the reviewed launch inputs before validating so they cannot change later.
        for name in ('command_prefix', 'settings_args', 'cap_args'):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(type(value) is not str for value in values):
                raise ContractError('native arguments must be string sequences')
            object.__setattr__(self, name, tuple(values))
        if not isinstance(self.environment, dict) and not isinstance(self.environment, MappingProxyType):
            raise ContractError('native environment must be a string mapping')
        if any(type(key) is not str or type(value) is not str for key, value in self.environment.items()):
            raise ContractError('native environment must be a string mapping')
        object.__setattr__(self, 'environment', MappingProxyType(dict(self.environment)))
        if self.backend not in ('claude', 'codex', 'local') or not self.command_prefix:
            raise ContractError('unsupported native backend/profile')
        if not self.policy_revision or not self.tool_profile_ref:
            raise ContractError('native profile requires explicit policy and tool scope')
        expected_boundary = Boundary.LOCAL if self.backend == 'local' else Boundary.CLOUD
        if self.agent.boundary != expected_boundary:
            raise ContractError('native backend and declared execution boundary disagree')
        if self.backend != 'local' and any(self.environment.get(key) for key in (
            'OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'
        )):
            raise ContractError('cloud adapters use existing subscription authentication, not injected API keys')
        # Values can occur in --flag=value and TOML -c key="value" forms.
        # Check the whole token, rather than only exact standalone arguments.
        if any(any(forbidden in arg for forbidden in (
            'dangerously-bypass', 'dangerously-skip-permissions',
            'bypassPermissions', 'danger-full-access')) or arg.split('=', 1)[0] in (
            '--ignore-rules', '--dangerously-skip-permissions', '--allow-dangerously-skip-permissions',
            '--bare', '--safe-mode')
               for arg in (*self.command_prefix, *self.settings_args, *self.cap_args)):
            raise ContractError('native profile must not bypass safety enforcement')


def _git(workspace, *args):
    result = subprocess.run(['git', '-C', workspace, *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ContractError('workspace Git inspection failed')
    return result.stdout


def _group_members(group):
    members = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] != 'Z':
                members.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return members


def _family(root):
    rows = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            values = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            rows[int(entry.name)] = (int(values[1]), values[19], values[0])
        except (OSError, ValueError, IndexError):
            pass
    owned = {root}
    while True:
        added = {pid for pid, (parent, _, state) in rows.items() if parent in owned and state != 'Z'}
        if added <= owned:
            break
        owned |= added
    return {pid: rows[pid] for pid in owned if pid in rows and rows[pid][2] != 'Z'}


class NativeAdapter:
    def __init__(self, profile: NativeProfile, artifacts: ArtifactStore, attempts,
                 runtime_root: Path, *, kill_grace=2.0, enforcement=None):
        self.profile, self.artifacts, self.attempts = profile, artifacts, attempts
        self.runtime_root = Path(runtime_root)
        self.kill_grace = kill_grace
        self.tasks = {}
        self.processes = {}
        self.reservation_fds = {}
        self.enforcement = enforcement

    def describe(self):
        return self.profile.agent

    def prepare(self, task, context):
        require_agent(task, self.describe())
        if (task.execution_policy.policy_revision != self.profile.policy_revision or
                task.execution_policy.tool_profile_ref != self.profile.tool_profile_ref):
            raise ContractError('native profile policy/tool scope mismatch')
        load_context(self.artifacts, context, expected_task=task)
        self.tasks[context.digest] = task
        return PreparedExecution(task_id=task.task_id, task_revision=task.task_revision,
                                 agent_id=self.describe().agent_id,
                                 policy_revision=task.execution_policy.policy_revision, context_ref=context)

    def _paths(self, attempt):
        directory = self.runtime_root / hashlib.sha256(attempt.attempt_id.encode()).hexdigest()
        return directory, directory / 'process.json', directory / 'stdout.jsonl', directory / 'stderr.log'

    def _context(self, attempt):
        task = self.tasks.get(attempt.context_ref.digest)
        if task is None:
            raise ContractError('prepare current task/context before start or recovery')
        return load_context(self.artifacts, attempt.context_ref, expected_task=task)

    def command(self, message, settings_args=None):
        profile = self.profile
        settings = profile.settings_args if settings_args is None else settings_args
        if profile.backend == 'codex':
            return [*profile.command_prefix, 'exec', '--json', *settings, *profile.cap_args, '-']
        return [*profile.command_prefix, '-p', message, *settings, *profile.cap_args,
                '--verbose', '--output-format', 'stream-json']

    def start(self, attempt, plan):
        require_prepared(attempt, plan)
        context = self._context(attempt)
        if attempt.base_commit != context.task.repository.base_commit or attempt.state != AttemptState.CLAIMED:
            raise ContractError('native start requires a matching claimed base')
        if attempt.workspace == context.task.repository.path or Path(attempt.workspace).resolve() != Path(attempt.workspace):
            raise ContractError('native execution requires its own canonical task worktree')
        branch = _git(attempt.workspace, 'branch', '--show-current').decode().strip()
        if not branch.startswith(('feat/', 'fix/')):
            raise ContractError('native execution requires a task feature branch')
        _git(attempt.workspace, 'merge-base', '--is-ancestor', attempt.base_commit, 'HEAD')
        expected_common = _git(context.task.repository.path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        actual_common = _git(attempt.workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
        if expected_common != actual_common:
            raise ContractError('workspace belongs to another repository')
        self.attempts.heartbeat(attempt.attempt_id, attempt.claim.owner_id, attempt.claim.generation)
        message = render_context(context, max_bytes=64 * 1024)
        directory, record, output, errors = self._paths(attempt)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if record.exists() or output.exists() or errors.exists():
            raise ContractError('attempt launch already has runtime evidence; reconcile it first')
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'USER', 'SHELL')
                       if key in os.environ}
        environment.update(self.profile.environment)
        stdin_path = None
        if self.profile.backend == 'codex':
            prompt_ref = self.artifacts.put(message.encode())
            stdin_path = str(self.artifacts.root / prompt_ref.digest)
        settings = None
        if self.enforcement is not None:
            if self.profile.settings_args or self.profile.cap_args:
                raise ContractError('enforced native profile cannot inherit additional permission/cap arguments')
            if self.profile.backend == 'codex':
                settings, home = self._codex_enforcement(attempt, environment)
                environment['CODEX_HOME'] = home
            else:
                settings = self.enforcement(attempt)
        request = {'attempt_id': attempt.attempt_id, 'generation': attempt.claim.generation,
                   'command': self.command(message, settings), 'environment': environment, 'stdin_path': stdin_path,
                   'wall_seconds': context.task.budget.wall_seconds,
                   'termination_grace_seconds': self.kill_grace}
        with open(output, 'xb') as out, open(errors, 'xb') as err:
            os.chmod(output, 0o600)
            os.chmod(errors, 0o600)
            proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('launcher.py')), str(record)],
                                    cwd=attempt.workspace, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    start_new_session=True, pass_fds=self.reservation_fds.get(attempt.attempt_id, ()))
        self.processes[attempt.attempt_id] = proc
        try:
            started = process_start(proc.pid)
            if started is None:
                raise ContractError('cannot establish launched process identity')
            proc.stdin.write(json.dumps(request).encode())
            proc.stdin.close()
            until = time.monotonic() + 2.0
            while not record.exists() and time.monotonic() < until:
                if proc.poll() is not None:
                    break
                time.sleep(0.01)
            identity = self._identity(attempt)
            if identity is None or identity['pid'] != proc.pid or identity['start_time'] != started:
                raise ContractError('launch wrapper identity was not established')
            self.attempts.record_process(attempt.attempt_id, attempt.claim.owner_id, attempt.claim.generation,
                                         pid=proc.pid, start_time=started)
        except BaseException:
            # Keep the reservation on failure. Recovery must reconcile even this startup window.
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            raise
        return ProcessIdentity(pid=proc.pid, start_time=started,
                               wrapper_record=self.artifacts.put(json.dumps(identity, sort_keys=True).encode()))

    def _codex_enforcement(self, attempt, environment):
        """Codex launches only with a trusted private CODEX_HOME from the enforcement's environment
        hook (#1486). The enforcement re-verifies its hook set as its last step before returning."""
        hook = getattr(self.enforcement, 'environment', None)
        if not callable(hook):
            raise ContractError('Codex enforcement must supply a verified trusted CODEX_HOME')
        settings = tuple(self.enforcement(attempt, dict(environment)))
        if not any(settings[index:index + 2] == ('-c', 'features.unified_exec_tty=false')
                   for index in range(len(settings) - 1)):
            raise ContractError('Codex enforcement must disable unified_exec_tty')
        extra = hook(attempt)
        if not isinstance(extra, dict) or set(extra) != {'CODEX_HOME'} or type(extra['CODEX_HOME']) is not str:
            raise ContractError('Codex enforcement environment must be exactly CODEX_HOME')
        home, workspace = Path(extra['CODEX_HOME']), Path(attempt.workspace)
        if (home.resolve() != home or not home.is_dir()
                or home.is_relative_to(workspace) or workspace.is_relative_to(home)):
            raise ContractError('Codex trusted home must be a canonical directory outside the workspace')
        return settings, str(home)

    def _identity(self, attempt):
        _, record, _, _ = self._paths(attempt)
        try:
            identity = json.loads(record.read_text())
        except (OSError, ValueError):
            return None
        if (identity.get('attempt_id') != attempt.attempt_id or identity.get('generation') != attempt.claim.generation
                or type(identity.get('pid')) is not int or not isinstance(identity.get('start_time'), str)):
            raise ContractError('native launch identity does not match attempt')
        return identity

    def recover(self, attempt):
        identity = self._identity(attempt)
        if identity is None:
            # Missing launch evidence is uncertain, not proof that no subprocess was created.
            return Recovery(status='uncertain')
        current = process_start(identity['pid'])
        if current is not None and current != identity['start_time']:
            return Recovery(status='uncertain')
        proc = self.processes.get(attempt.attempt_id)
        if proc is not None:
            proc.poll()
        process = ProcessIdentity(pid=identity['pid'], start_time=identity['start_time'],
                                  wrapper_record=self.artifacts.put(json.dumps(identity, sort_keys=True).encode()))
        if _group_members(identity['pid']):
            return Recovery(status='adopted', process=process)
        # The supervisor may publish its terminal record between our first read and
        # observing the empty process group. Re-read after that observation so a normal
        # exit is not mistaken for a bookkeeping gap. Missing evidence stays uncertain.
        terminal = self._identity(attempt)
        if terminal is not None and (terminal['pid'], terminal['start_time']) == (
                identity['pid'], identity['start_time']):
            identity = terminal
            process = ProcessIdentity(pid=identity['pid'], start_time=identity['start_time'],
                                      wrapper_record=self.artifacts.put(json.dumps(identity, sort_keys=True).encode()))
        if 'exit_code' in identity or identity.get('controller_termination_confirmed') is True:
            if proc is not None:
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    return Recovery(status='adopted', process=process)
            # Only proven termination may remove per-attempt credentials (the Codex trusted home).
            release = getattr(self.enforcement, 'release', None)
            if callable(release) and 'cleanup_outcome' not in identity:
                outcome = release(attempt)
                if isinstance(outcome, dict):
                    identity['cleanup_outcome'] = dict(outcome)
                    _, record, _, _ = self._paths(attempt)
                    temporary = record.with_name('.cleanup-' + os.urandom(16).hex())
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(identity, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, record)
            return Recovery(status='exited', process=process)
        return Recovery(status='uncertain', process=process)

    def cancel(self, attempt, reason):
        state = self.recover(attempt)
        if state.status == 'uncertain':
            return Cancellation(requested=False, termination_confirmed=False, reason='uncertain process identity')
        if state.status == 'exited':
            return Cancellation(requested=False, termination_confirmed=True, reason=reason)
        identity = state.process
        for sig, grace in ((signal.SIGTERM, self.kill_grace),):
            current = process_start(identity.pid)
            if current is not None and current != identity.start_time:
                return Cancellation(requested=True, termination_confirmed=False, reason='process identity changed')
            try:
                os.killpg(identity.pid, sig)
            except ProcessLookupError:
                break
            until = time.monotonic() + grace
            while time.monotonic() < until:
                proc = self.processes.get(attempt.attempt_id)
                if proc is not None:
                    proc.poll()
                if not _group_members(identity.pid):
                    break
                time.sleep(0.02)
            if not _group_members(identity.pid):
                break
        if self.recover(attempt).status == 'exited':
            return Cancellation(requested=True, termination_confirmed=True, reason=reason)
        # Freeze the supervisor and its full descendant tree before escalation. Process-group
        # signals alone miss children that called setsid. Only signal identities we established.
        if process_start(identity.pid) != identity.start_time:
            return Cancellation(requested=True, termination_confirmed=False, reason='cannot prove descendant ownership')
        os.kill(identity.pid, signal.SIGSTOP)
        frozen = {}
        stable = False
        for _ in range(20):
            current = _family(identity.pid)
            for pid, (_, started, _) in current.items():
                if process_start(pid) == started:
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
            if set(current) == set(frozen) and all(row[2] in ('T', 't') for row in current.values()):
                frozen, stable = current, True
                break
            frozen = current
            time.sleep(0.01)
        if not stable:
            return Cancellation(requested=True, termination_confirmed=False, reason='descendant tree not stable; workspace quarantined')
        for pid in [pid for pid in frozen if pid != identity.pid] + [identity.pid]:
            if process_start(pid) == frozen[pid][1]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        until = time.monotonic() + 2.0
        while time.monotonic() < until:
            proc = self.processes.get(attempt.attempt_id)
            if proc is not None:
                proc.poll()
            if not _group_members(identity.pid) and not any(
                pid in _family(pid) and process_start(pid) == row[1] for pid, row in frozen.items()
            ):
                break
            time.sleep(0.02)
        survivors = {pid for pid, row in frozen.items() if pid in _family(pid) and process_start(pid) == row[1]}
        confirmed = not survivors and not _group_members(identity.pid)
        if confirmed:
            _, record, _, _ = self._paths(attempt)
            data = self._identity(attempt)
            data['controller_termination_confirmed'] = True
            temporary = record.with_name('.terminated-' + os.urandom(16).hex())
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, record)
        return Cancellation(requested=True, termination_confirmed=confirmed, reason=reason)

    def _native_events(self, attempt):
        _, _, output, _ = self._paths(attempt)
        if not output.exists():
            return []
        if output.stat().st_size > 8 * 1024 * 1024:
            raise ContractError('native event log exceeds collection budget')
        events = []
        with output.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        return events

    def events(self, attempt, cursor=0):
        for sequence, event in enumerate(self._native_events(attempt), 1):
            if sequence <= cursor:
                continue
            kind = event.get('type', 'unknown')
            event_type = 'execution.exited' if kind in ('result', 'turn.completed', 'turn.failed', 'error') else 'progress'
            yield ExecutionEvent(execution_id=attempt.execution_id, attempt_id=attempt.attempt_id,
                                 generation=attempt.claim.generation, sequence=sequence, type=event_type,
                                 timestamp=datetime.now(timezone.utc).isoformat(), payload={'native_type': str(kind)})

    def _snapshot(self, attempt):
        tracked = _git(attempt.workspace, 'diff', '--name-only', '-z', 'HEAD').decode().split('\0')
        untracked = _git(attempt.workspace, 'ls-files', '--others', '--exclude-standard', '-z').decode().split('\0')
        changes = tuple(sorted(set(path for path in (*tracked, *untracked) if path)))
        head = _git(attempt.workspace, 'rev-parse', 'HEAD').decode().strip()
        if not changes:
            return changes, head, None
        for name in changes:
            if is_protected(name, extra=CANDIDATE_PROTECTED):
                # Bound before escaping so hostile filenames cannot forge log lines or grow errors.
                raise ContractError(json.dumps({'reason': 'protected_candidate_path',
                                                'path': name[:160]}, ensure_ascii=True))
        patch = _git(attempt.workspace, 'diff', '--binary', 'HEAD')
        extra = {}
        for name in untracked:
            if not name:
                continue
            path = Path(attempt.workspace) / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > self.artifacts.max_bytes:
                raise ContractError('unsafe or oversized untracked candidate')
            if name.startswith('.') or any(part.startswith('.') for part in Path(name).parts):
                raise ContractError('hidden untracked files require explicit controller review')
            extra[name] = base64.b64encode(path.read_bytes()).decode()
        snapshot = json.dumps({'base_commit': head, 'tracked_diff_b64': base64.b64encode(patch).decode(),
                               'untracked': extra}, sort_keys=True).encode()
        return changes, None, self.artifacts.put(snapshot)

    def collect(self, attempt):
        if self.recover(attempt).status != 'exited':
            raise ContractError('native process group must exit before collection')
        events = self._native_events(attempt)
        errors, terminal, usage = [], None, Usage()
        for event in events:
            if self.profile.backend == 'codex':
                if event.get('type') == 'turn.completed':
                    terminal = event
                    native = event.get('usage') or {}
                    values = (native.get('input_tokens'), native.get('output_tokens'))
                    if all(type(value) is int and value >= 0 for value in values):
                        usage = Usage(tokens=sum(values))
                elif event.get('type') in ('turn.failed', 'error'):
                    errors.append('native turn failed')
            elif event.get('type') == 'result':
                terminal = event
                if event.get('is_error'):
                    errors.append('native turn failed')
                cost = event.get('total_cost_usd')
                if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                    usage = Usage(cost_amount=cost, cost_measurement='estimated', cost_basis='native-reported-notional')
        if terminal is None:
            errors.append('missing native terminal event')
        identity = self._identity(attempt)
        if identity is None or identity.get('exit_code') != 0:
            errors.append('native process exited unsuccessfully')
        outcome = identity.get('cleanup_outcome') if identity else None
        warnings = []
        if outcome:
            if outcome['auth'] == 'changed_not_persisted':
                warnings.append('Codex refreshed its login during the run; ~/.codex was not updated. '
                                'If the next Codex run fails to authenticate, run `codex login`.')
        changes, commit, diff = self._snapshot(attempt)
        return ExecutionResult(execution_id=attempt.execution_id, attempt_id=attempt.attempt_id,
                               task_id=attempt.task_id, agent_id=attempt.agent_id, generation=attempt.claim.generation,
                               status=ResultStatus.FAILED if errors else ResultStatus.CANDIDATE_COMPLETE,
                               workspace=attempt.workspace, base_commit=attempt.base_commit, usage=usage,
                               changes=changes, result_commit=commit, diff_ref=diff, errors=tuple(errors),
                               cleanup_outcome=outcome, warnings=tuple(warnings))

    def checkpoint(self, attempt):
        context = self._context(attempt)
        result = self.collect(attempt)
        prior = context.handoff
        return Handoff(task_id=attempt.task_id, task_revision=attempt.task_revision,
                       execution_id=attempt.execution_id, attempt_id=attempt.attempt_id,
                       repository_identity=context.task.repository.identity, task_ref=context.package.task_ref,
                       current_state=AttemptState.HANDOFF_READY, base_commit=attempt.base_commit,
                       instruction_refs=context.package.instruction_refs,
                       completed=('Native turn exited; candidate still requires independent verification',),
                       pending=prior.pending if prior else ('Verify candidate against all acceptance criteria',),
                       relevant_files=result.changes, decisions=prior.decisions if prior else (),
                       known_failures=result.errors, test_evidence=(),
                       next_actions=('Run independent verification and review remaining task work',),
                       workspace_status='explicit-diff' if result.diff_ref else 'committed',
                       result_commit=result.result_commit, diff_ref=result.diff_ref)
