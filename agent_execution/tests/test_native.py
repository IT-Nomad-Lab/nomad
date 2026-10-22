"""Real fake subprocesses in disposable Git worktrees, with portable context and SQLite claims."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_execution.adapter import AgentAdapter
from agent_execution.context import ArtifactStore, build_context
from agent_execution.contracts import AttemptState, ContractError, Privacy, ResultStatus
from agent_execution.native import NativeAdapter, NativeProfile
from agent_execution.store import AttemptStore
from agent_execution.tests.fixtures import agent, task


FAKE = r'''
import json, os, signal, subprocess, sys, time
backend = os.environ['FIXTURE_BACKEND']
mode = os.environ.get('FIXTURE_MODE', 'success')
prompt = sys.stdin.read() if backend == 'codex' else sys.argv[sys.argv.index('-p') + 1]
with open(os.environ['FIXTURE_CAPTURE'], 'w') as stream:
    json.dump({'prompt': prompt, 'api_key_inherited': 'OPENAI_API_KEY' in os.environ,
               'argv': sys.argv[1:], 'codex_home': os.environ.get('CODEX_HOME')}, stream)
if mode == 'hang':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True: time.sleep(0.1)
if mode == 'escape':
    escaped = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(600)'],
        start_new_session=True)
    with open(os.environ['FIXTURE_CAPTURE'], 'w') as stream:
        json.dump({'escaped_pid': escaped.pid}, stream)
if mode == 'write':
    with open('module.py', 'w') as stream: stream.write('VALUE = 1\n')
if mode == 'tool':
    settings = json.load(open(sys.argv[sys.argv.index('--settings') + 1]))
    hook = settings['hooks']['PreToolUse'][0]['hooks'][0]
    verdicts = []
    for path in ('README', '../private'):
        outcome = subprocess.run([hook['command'], *hook['args']],
            input=json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': path}}),
            capture_output=True, text=True, check=True)
        verdicts.append(json.loads(outcome.stdout)['hookSpecificOutput']['permissionDecision'])
    with open(os.environ['FIXTURE_CAPTURE'], 'w') as stream:
        json.dump({'verdicts': verdicts, 'settings': settings, 'argv': sys.argv[1:]}, stream)
if mode == 'missing': sys.exit(0)
if backend == 'codex':
    print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture-thread'}), flush=True)
    print(json.dumps({'type': 'turn.failed' if mode == 'failure' else 'turn.completed',
                      'usage': {'input_tokens': 12, 'output_tokens': 4}}), flush=True)
else:
    print(json.dumps({'type': 'result', 'is_error': mode == 'failure',
                      'result': 'fixture done', 'total_cost_usd': 0.42}), flush=True)
sys.exit(1 if mode == 'failure' else 0)
'''

# A stand-in for the installed Codex CLI as CodexTrustedEnforcement drives it: `mcp list --json`
# and the app-server `hooks/list` method. A hook is trusted only when config.toml pins its current
# hash, as Codex 0.163.0 does; FAKE_CODEX_MODE injects each way the hook set can go wrong.
FAKE_CODEX = r'''
import hashlib, json, os, re, sys
mode = os.environ.get('FAKE_CODEX_MODE', 'ok')
home = os.environ['CODEX_HOME']
hooks_path = os.path.join(home, 'hooks.json')

def tamper():
    os.chmod(hooks_path, 0o600)
    with open(hooks_path, 'a') as stream: stream.write(' ')

def listing():
    config = open(os.path.join(home, 'config.toml')).read()
    if os.environ.get('FAKE_CODEX_SEEN'):
        with open(os.environ['FAKE_CODEX_SEEN'], 'a') as stream: stream.write(config)
    trusted = dict(re.findall(r'\[hooks\.state\.("[^"]*")\]\ntrusted_hash = ("[^"]*")', config))
    if trusted and mode == 'tampered':
        tamper()
    data = open(hooks_path, 'rb').read()
    key = hooks_path + ':pre_tool_use:0:0'
    current = 'sha256:' + hashlib.sha256(data).hexdigest()
    pinned = trusted.get(json.dumps(key))
    status = 'untrusted' if pinned is None or mode == 'untrusted' else (
        'trusted' if json.loads(pinned) == current else 'modified')
    hooks = [{'key': key, 'currentHash': current, 'trustStatus': status, 'source': 'user',
              'isManaged': mode == 'managed', 'pluginId': None}]
    if mode == 'extra':
        hooks.append({'key': '/elsewhere/hooks.json:pre_tool_use:0:0', 'currentHash': 'sha256:00',
                      'trustStatus': 'trusted', 'source': 'user', 'isManaged': False, 'pluginId': None})
    if mode == 'absent' and trusted:
        hooks = []
    return hooks

if sys.argv[1:] == ['mcp', 'list', '--json']:
    if mode == 'tamper-early':
        tamper()
    print(json.dumps([{'name': 'docs'}]))
elif sys.argv[1:] == ['app-server']:
    for line in sys.stdin:
        message = json.loads(line)
        if message.get('id') == 1:
            print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}), flush=True)
        elif message.get('id') == 2:
            if mode == 'error':
                print(json.dumps({'jsonrpc': '2.0', 'id': 2, 'error': {'code': -1, 'message': 'no'}}), flush=True)
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': 2, 'result': {'data': [
                    {'cwd': message['params']['cwds'][0], 'hooks': listing()}]}}), flush=True)
else:
    sys.exit(2)
'''


class NativeExecution(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'README').write_text('fixture\n')
        self.git('add', 'README')
        self.git('commit', '-qm', 'fixture')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.worktree = self.root / 'worktree'
        self.git('worktree', 'add', '-qb', 'feat/fixture', str(self.worktree), 'main')
        self.script = self.root / 'fake_agent.py'
        self.script.write_text(FAKE)
        self.artifacts = ArtifactStore(self.root / 'artifacts')
        self.store = AttemptStore(self.root / 'attempts.db')
        self.addCleanup(self.store.close)
        self.instructions = (self.artifacts.put(b'Approved instructions: preserve all acceptance criteria'),)
        self.adapters = []
        self.addCleanup(self.stop_all)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True, stderr=subprocess.DEVNULL)

    def stop_all(self):
        for adapter, run in self.adapters:
            try:
                adapter.cancel(run, 'fixture cleanup')
            except ContractError:
                pass

    def prepare(self, backend='local', mode='success', wall_seconds=60):
        spec = task()
        privacy = Privacy.LOCAL_ONLY if backend == 'local' else Privacy.CLOUD_ALLOWED
        spec = replace(spec, repository=replace(spec.repository, path=str(self.repo), base_commit=self.base),
                       execution_policy=replace(spec.execution_policy, privacy=privacy),
                       budget=replace(spec.budget, wall_seconds=wall_seconds))
        context = build_context(self.artifacts, spec, self.instructions)
        profile = NativeProfile(backend=backend, agent=agent(backend), command_prefix=(sys.executable, str(self.script)),
                                settings_args=('--settings', 'fixture-only'), policy_revision='p1',
                                tool_profile_ref='tools:offline', environment={
                                    'FIXTURE_BACKEND': backend, 'FIXTURE_MODE': mode,
                                    'FIXTURE_CAPTURE': str(self.root / 'captured.json')})
        adapter = NativeAdapter(profile, self.artifacts, self.store, self.root / 'runtime', kill_grace=0.05)
        plan = adapter.prepare(spec, context)
        run = self.store.claim(spec, context, agent_id=profile.agent.agent_id,
                               workspace=str(self.worktree), owner_id='scheduler')
        self.adapters.append((adapter, run))
        return spec, adapter, plan, run

    def wait(self, adapter, run):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if adapter.recover(run).status == 'exited':
                return
            time.sleep(0.02)
        self.fail('fake process did not exit')

    def test_recovery_rereads_terminal_record_after_group_disappears(self):
        _, adapter, plan, run = self.prepare('codex')
        adapter.start(run, plan)
        self.wait(adapter, run)
        terminal = adapter._identity(run)
        initial = {key: value for key, value in terminal.items() if key != 'exit_code'}
        with patch.object(adapter, '_identity', side_effect=(initial, terminal)), \
                patch('agent_execution.native._group_members', return_value=[]), \
                patch('agent_execution.native.process_start', return_value=None):
            self.assertEqual(adapter.recover(run).status, 'exited')

    def test_wall_budget_terminates_stubborn_agent_without_controller_polling(self):
        _, adapter, plan, run = self.prepare('codex', 'hang', wall_seconds=1)
        adapter.start(run, plan)
        time.sleep(1.4)
        self.wait(adapter, run)
        identity = adapter._identity(run)
        self.assertEqual(identity['exit_code'], 124)
        self.assertTrue(identity['wall_budget_exceeded'])
        self.assertEqual(adapter.collect(run).status, ResultStatus.FAILED)

    def test_wall_budget_reaps_escaped_orphan_after_cli_success(self):
        _, adapter, plan, run = self.prepare('codex', 'escape', wall_seconds=1)
        adapter.start(run, plan)
        self.wait(adapter, run)
        self.assertEqual(adapter._identity(run)['exit_code'], 124)
        self.assertEqual(adapter.collect(run).status, ResultStatus.FAILED)

    def test_codex_consumes_full_context_and_normalizes_unknown_cost(self):
        # Each backend gets an independent fixture/store/worktree to avoid implicit retry state.
        spec, adapter, plan, run = self.prepare('codex')
        self.assertIsInstance(adapter, AgentAdapter)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-not-a-key'}):
            identity = adapter.start(run, plan)
        self.wait(adapter, run)
        capture = json.loads((self.root / 'captured.json').read_text())
        self.assertFalse(capture['api_key_inherited'])
        self.assertIn('Do not lose acceptance gates', capture['prompt'])
        self.assertIn('Approved instructions', capture['prompt'])
        self.assertEqual(self.store.get(run.attempt_id).process_pid, identity.pid)
        self.assertTrue(self.artifacts.read(identity.wrapper_record))
        result = adapter.collect(run)
        self.assertEqual(result.status, ResultStatus.CANDIDATE_COMPLETE)
        self.assertEqual(result.usage.tokens, 16)
        self.assertIsNone(result.usage.cost_amount)

    def codex_enforced(self, mode='ok', **options):
        from agent_execution.enforcement import CodexTrustedEnforcement
        _, adapter, plan, run = self.prepare('codex')
        cli = self.root / 'fake_codex'
        cli.write_text(f'#!{sys.executable}\n' + FAKE_CODEX)
        cli.chmod(0o700)
        adapter.profile = replace(adapter.profile, settings_args=(),
                                  environment={**adapter.profile.environment, 'FAKE_CODEX_MODE': mode})
        options.setdefault('runtime_root', self.root / 'codex-homes')
        adapter.enforcement = CodexTrustedEnforcement(
            str(cli), options.pop('runtime_root'), ('/usr',), lambda run: str(self.root / 'authority.sock'),
            base_config='model = "fixture"\n', **options)
        return adapter, plan, run

    def test_cleanup_warning_survives_recovery_recollection_and_wire_roundtrip(self):
        from agent_execution.contracts import ExecutionResult
        auth = self.root / 'auth.json'
        auth.write_text('{"fixture": "seed"}')
        adapter, plan, run = self.codex_enforced(auth_source=str(auth))
        adapter.start(run, plan)
        home = Path(adapter.enforcement.environment(run)['CODEX_HOME'])
        (home / 'auth.json').write_text('{"fixture": "refreshed"}')
        self.wait(adapter, run)
        result = adapter.collect(run)
        self.assertEqual(result.cleanup_outcome, {'auth': 'changed_not_persisted'})
        self.assertEqual(result.warnings, (
            'Codex refreshed its login during the run; ~/.codex was not updated. '
            'If the next Codex run fails to authenticate, run `codex login`.',))
        self.assertEqual(auth.read_text(), '{"fixture": "seed"}')
        self.assertFalse(home.exists())
        adapter.enforcement.homes.clear()
        self.assertEqual(adapter.recover(run).status, 'exited')
        again = adapter.collect(run)
        self.assertEqual(again.cleanup_outcome, result.cleanup_outcome)
        self.assertEqual(again.warnings, result.warnings)
        self.assertEqual(ExecutionResult.from_dict(result.to_dict()).warnings, result.warnings)
        self.assertNotIn('refreshed"}', json.dumps(result.to_dict()))

    def test_cleanup_without_writes_has_no_warning(self):
        adapter, plan, run = self.codex_enforced()
        adapter.start(run, plan)
        self.wait(adapter, run)
        result = adapter.collect(run)
        self.assertEqual(result.cleanup_outcome, {'auth': 'unchanged'})
        self.assertEqual(result.warnings, ())

    def assert_not_launched(self, run, homes=None):
        self.assertFalse((self.root / 'captured.json').exists())
        self.assertIsNone(self.store.get(run.attempt_id).process_pid)
        self.assertEqual(list((homes or self.root / 'codex-homes').glob('*')), [])

    def test_codex_adapter_factory_builds_the_governed_adapter(self):
        from agent_execution.codex import codex_adapter_factory
        from agent_execution.tool_authority import authority_socket_path
        spec, _, _, run = self.prepare('codex')
        # Install-prefix layout: the CLI's prefix (cli/) becomes a sandbox read root.
        cli = self.root / 'cli' / 'bin' / 'fake_codex'
        cli.parent.mkdir(parents=True)
        cli.write_text(f'#!{sys.executable}\n' + FAKE_CODEX)
        cli.chmod(0o700)
        auth = self.root / 'fixture-auth.json'
        auth.write_text('{"fixture": "not a credential"}')
        controller_root = self.root / 'execution'
        controller_root.mkdir(mode=0o700)
        adapter = codex_adapter_factory(policy_revision='p1', tool_profile_ref='tools:offline', codex=str(cli),
                                        auth_source=str(auth), base_config='model = "fixture"\n',
                                        runtime=self.root / 'run')(
            agent('codex'), self.artifacts, self.store, controller_root)
        self.assertEqual((adapter.profile.backend, adapter.profile.command_prefix, adapter.profile.settings_args),
                         ('codex', (str(cli),), ()))
        # The fake CLI answers only enforcement's listing calls; the attempt itself runs the fake agent.
        adapter.profile = replace(adapter.profile, command_prefix=(sys.executable, str(self.script)), environment={
            'FAKE_CODEX_MODE': 'ok', 'FIXTURE_BACKEND': 'codex', 'FIXTURE_MODE': 'success',
            'FIXTURE_CAPTURE': str(self.root / 'captured.json')})
        plan = adapter.prepare(spec, build_context(self.artifacts, spec, self.instructions))
        self.adapters.append((adapter, run))
        adapter.start(run, plan)
        home = Path(adapter.enforcement.environment(run)['CODEX_HOME'])
        self.assertEqual(home.parent, controller_root / 'codex-homes')
        self.assertIn(str(authority_socket_path(controller_root, run.attempt_id, self.root / 'run')),
                      (home / 'hooks.json').read_text())
        self.assertEqual((home / 'auth.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue((home / 'config.toml').read_text().startswith('model = "fixture"\n'))
        self.assertIs(adapter.enforcement.auth_writeback, False)
        self.wait(adapter, run)
        self.assertEqual(json.loads((self.root / 'captured.json').read_text())['codex_home'], str(home))
        self.assertFalse(home.exists())
        self.assertEqual(auth.read_text(), '{"fixture": "not a credential"}')

    def test_codex_adapter_factory_needs_the_installed_cli(self):
        from agent_execution.codex import codex_adapter_factory, codex_runtime_reads
        with patch('agent_execution.codex.shutil.which', return_value=None), \
                self.assertRaisesRegex(ContractError, 'Codex CLI not found'):
            codex_adapter_factory(policy_revision='p1', tool_profile_ref='tools:offline')
        self.assertEqual(codex_runtime_reads('/home/linuxbrew/.linuxbrew/bin/codex'),
                         ('/usr', '/home/linuxbrew/.linuxbrew'))

    def test_codex_launches_with_a_verified_trusted_private_home(self):
        adapter, plan, run = self.codex_enforced()
        adapter.start(run, plan)
        home = Path(adapter.enforcement.environment(run)['CODEX_HOME'])
        self.wait(adapter, run)
        capture = json.loads((self.root / 'captured.json').read_text())
        self.assertEqual(capture['codex_home'], str(home))
        self.assertEqual(home.parent, self.root / 'codex-homes')
        self.assertFalse(home.is_relative_to(self.worktree))
        argv = capture['argv']
        for override in ('features.unified_exec_tty=false', 'mcp_servers.docs.enabled=false',
                         'approval_policy="never"'):
            self.assertIn(override, argv)
        self.assertEqual(argv[:2], ['exec', '--json'])
        self.assertEqual(adapter.collect(run).status, ResultStatus.CANDIDATE_COMPLETE)
        # Proven exit removed the home and its credentials.
        self.assertFalse(home.exists())
        self.assertEqual(adapter.recover(run).status, 'exited')

    def test_trusted_home_records_trust_for_its_own_hook_only(self):
        from agent_execution.enforcement import codex_hooks_list
        adapter, plan, run = self.codex_enforced()
        environment = {'PATH': os.environ['PATH'], 'FAKE_CODEX_MODE': 'ok'}
        adapter.enforcement(run, environment)
        home = Path(adapter.enforcement.environment(run)['CODEX_HOME'])
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)
        for name in ('hooks.json', 'config.toml'):
            self.assertEqual((home / name).stat().st_mode & 0o777, 0o400)
        config = (home / 'config.toml').read_text()
        self.assertTrue(config.startswith('model = "fixture"\n'))
        self.assertEqual(config.count('trusted_hash'), 1)
        listed = codex_hooks_list(adapter.enforcement.codex, {**environment, 'CODEX_HOME': str(home)}, run.workspace)
        self.assertEqual([hook['trustStatus'] for hook in listed], ['trusted'])
        adapter.enforcement.release(run)
        self.assertFalse(home.exists())

    def test_codex_refuses_every_unverified_hook_set_before_launch(self):
        for mode, match in (('tampered', 'not trusted'), ('tamper-early', 'hooks.json changed'),
                            ('extra', 'does not match'), ('untrusted', 'not trusted'),
                            ('absent', 'does not match'), ('managed', 'managed or plugin'),
                            ('error', 'cannot list Codex hooks')):
            with self.subTest(mode=mode):
                self.setUp()
                adapter, plan, run = self.codex_enforced(mode)
                seen = self.root / 'seen-config.toml'
                adapter.profile = replace(adapter.profile, environment={**adapter.profile.environment,
                                                                        'FAKE_CODEX_SEEN': str(seen)})
                with self.assertRaisesRegex(ContractError, match):
                    adapter.start(run, plan)
                self.assert_not_launched(run)
                # Trust is only ever recorded for the attempt's own hooks.json.
                self.assertNotIn('/elsewhere', seen.read_text() if seen.exists() else '')
                with self.assertRaisesRegex(ContractError, 'not prepared'):
                    adapter.enforcement.environment(run)

    def test_codex_refuses_a_home_inside_the_workspace(self):
        adapter, plan, run = self.codex_enforced(runtime_root=self.worktree / '.codex-homes')
        with self.assertRaisesRegex(ContractError, 'outside every sandbox read and write root'):
            adapter.start(run, plan)
        self.assertFalse((self.worktree / '.codex-homes').exists())
        self.assertFalse((self.root / 'captured.json').exists())

    def test_codex_refuses_a_home_under_a_runtime_read_root(self):
        from agent_execution.enforcement import CodexTrustedEnforcement
        _, _, run = self.codex_enforced()
        home = self.root / 'codex-homes' / __import__('hashlib').sha256(run.attempt_id.encode()).hexdigest()
        for reads in ((str(self.root),), (str(home / 'inner'),)):
            with self.subTest(reads=reads):
                enforcement = CodexTrustedEnforcement('/bin/false', self.root / 'codex-homes', reads,
                                                      lambda run: 'sock')
                with self.assertRaisesRegex(ContractError, 'outside every sandbox read and write root'):
                    enforcement(run, {})

    def test_codex_refuses_an_existing_home(self):
        adapter, plan, run = self.codex_enforced()
        stale = self.root / 'codex-homes' / __import__('hashlib').sha256(run.attempt_id.encode()).hexdigest()
        stale.parent.mkdir(mode=0o700)
        stale.mkdir(mode=0o700)
        with self.assertRaisesRegex(ContractError, 'already exists'):
            adapter.start(run, plan)
        self.assertTrue(stale.exists())
        self.assertFalse((self.root / 'captured.json').exists())

    def test_adapter_refuses_codex_enforcement_without_a_verified_home(self):
        from agent_execution.enforcement import CodexEnforcement
        adapter, plan, run = self.codex_enforced()
        trusted = adapter.enforcement

        class Wrapped:
            def __init__(self, settings=None, environment=None):
                self.settings, self.extra = settings, environment

            def __call__(self, run, environment):
                settings = trusted(run, environment)
                return settings if self.settings is None else self.settings(settings)

            def environment(self, run):
                home = trusted.environment(run)
                return home if self.extra is None else self.extra(home)

        (self.worktree / 'sub').mkdir()
        outside = self.root / 'outside-home'
        outside.mkdir()
        (self.root / 'linked-home').symlink_to(outside)
        without_tty = lambda args: tuple(arg for arg in args if arg != 'features.unified_exec_tty=false')
        cases = (
            (CodexEnforcement(('/usr',), lambda run: ()), 'verified trusted CODEX_HOME'),
            (Wrapped(settings=without_tty), 'disable unified_exec_tty'),
            (Wrapped(environment=lambda home: {}), 'exactly CODEX_HOME'),
            (Wrapped(environment=lambda home: {**home, 'OPENAI_API_KEY': 'fixture-not-a-key'}), 'exactly CODEX_HOME'),
            (Wrapped(environment=lambda home: {'CODEX_HOME': str(self.worktree)}), 'outside the workspace'),
            (Wrapped(environment=lambda home: {'CODEX_HOME': str(self.worktree / 'sub')}), 'outside the workspace'),
            (Wrapped(environment=lambda home: {'CODEX_HOME': str(self.worktree / '.git')}), 'outside the workspace'),
            (Wrapped(environment=lambda home: {'CODEX_HOME': str(self.root)}), 'outside the workspace'),
            (Wrapped(environment=lambda home: {'CODEX_HOME': 'relative'}), 'outside the workspace'),
            (Wrapped(environment=lambda home: {'CODEX_HOME': str(self.root / 'linked-home')}), 'outside the workspace'),
            (Wrapped(environment=lambda home: {'CODEX_HOME': str(self.root / 'missing-home')}), 'outside the workspace'),
        )
        for enforcement, match in cases:
            with self.subTest(match=match):
                adapter.enforcement = enforcement
                with self.assertRaisesRegex(ContractError, match):
                    adapter.start(run, plan)
                self.assertFalse((self.root / 'captured.json').exists())
                self.assertIsNone(self.store.get(run.attempt_id).process_pid)
                trusted.homes.pop(run.attempt_id, None)
                trusted.release(run)

    def test_enforced_codex_profile_cannot_add_its_own_arguments(self):
        adapter, plan, run = self.codex_enforced()
        adapter.profile = replace(adapter.profile, cap_args=('--add-dir', '/'))
        with self.assertRaisesRegex(ContractError, 'cannot inherit'):
            adapter.start(run, plan)
        self.assert_not_launched(run)

    def test_claude_result_preserves_notional_cost_without_claiming_billed_cost(self):
        _, adapter, plan, run = self.prepare('claude')
        adapter.start(run, plan)
        self.wait(adapter, run)
        result = adapter.collect(run)
        self.assertEqual(result.status, ResultStatus.CANDIDATE_COMPLETE)
        self.assertEqual(result.usage.cost_amount, 0.42)
        self.assertEqual(result.usage.cost_basis, 'native-reported-notional')

    def test_local_harness_gets_complete_context_and_can_capture_dirty_candidate(self):
        _, adapter, plan, run = self.prepare('local', 'write')
        adapter.start(run, plan)
        self.wait(adapter, run)
        result = adapter.collect(run)
        self.assertEqual(result.changes, ('module.py',))
        self.assertIsNone(result.result_commit)
        self.assertIsNotNone(result.diff_ref)
        checkpoint = adapter.checkpoint(run)
        self.assertEqual(checkpoint.workspace_status, 'explicit-diff')
        self.assertIn('module.py', checkpoint.relevant_files)
        self.assertTrue(self.artifacts.read(checkpoint.diff_ref))

    def test_native_failure_and_missing_terminal_event_are_not_success(self):
        _, adapter, plan, run = self.prepare('codex', 'failure')
        adapter.start(run, plan)
        self.wait(adapter, run)
        self.assertEqual(adapter.collect(run).status, ResultStatus.FAILED)

    def test_missing_native_terminal_event_refuses_candidate_success(self):
        _, adapter, plan, run = self.prepare('claude', 'missing')
        adapter.start(run, plan)
        self.wait(adapter, run)
        self.assertIn('missing native terminal event', adapter.collect(run).errors)

    def test_wrapper_record_recovers_start_before_database_process_registration(self):
        spec, adapter, plan, run = self.prepare()
        with patch.object(self.store, 'record_process', side_effect=RuntimeError('simulated controller crash')):
            with self.assertRaises(RuntimeError):
                adapter.start(run, plan)
        self.wait(adapter, run)
        self.assertIsNone(self.store.get(run.attempt_id).process_pid)
        recovered = NativeAdapter(adapter.profile, self.artifacts, self.store, self.root / 'runtime')
        recovered.prepare(spec, run.context_ref)
        state = recovered.recover(run)
        self.assertEqual(state.status, 'exited')
        self.assertIsNotNone(state.process)
        self.assertEqual(recovered.collect(run).status, ResultStatus.CANDIDATE_COMPLETE)
        self.store.record_process(run.attempt_id, 'scheduler', run.claim.generation,
                                  pid=state.process.pid, start_time=state.process.start_time)
        self.assertEqual(self.store.get(run.attempt_id).state, AttemptState.RUNNING)

    def test_group_cancellation_handles_agent_ignoring_sigterm(self):
        _, adapter, plan, run = self.prepare(mode='hang')
        adapter.start(run, plan)
        deadline = time.monotonic() + 2
        while not (self.root / 'captured.json').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        result = adapter.cancel(run, 'operator')
        self.assertTrue(result.requested)
        self.assertTrue(result.termination_confirmed)
        self.assertEqual(adapter.recover(run).status, 'exited')

    def test_changed_native_identity_is_uncertain_and_not_signalled(self):
        _, adapter, plan, run = self.prepare()
        adapter.start(run, plan)
        self.wait(adapter, run)
        _, record, _, _ = adapter._paths(run)
        identity = json.loads(record.read_text())
        identity['generation'] += 1
        record.write_text(json.dumps(identity))
        with self.assertRaises(ContractError):
            adapter.recover(run)

    def test_cancellation_reaps_descendant_that_escaped_the_process_group(self):
        _, adapter, plan, run = self.prepare(mode='escape')
        adapter.start(run, plan)
        deadline = time.monotonic() + 2
        escaped = None
        while time.monotonic() < deadline:
            capture = self.root / 'captured.json'
            if capture.exists():
                try:
                    escaped = json.loads(capture.read_text()).get('escaped_pid')
                except ValueError:
                    pass
            if escaped:
                break
            time.sleep(0.02)
        self.assertIsNotNone(escaped)
        with self.assertRaises(ContractError):
            adapter.collect(run)
        cancelled = adapter.cancel(run, 'operator')
        self.assertTrue(cancelled.termination_confirmed)
        from agent_execution.native import _family
        self.assertNotIn(escaped, _family(escaped))

    def test_command_and_environment_profiles_cannot_bypass_safety(self):
        _, adapter, _, _ = self.prepare()
        with self.assertRaises(ContractError):
            replace(adapter.profile, settings_args=('--dangerously-bypass-approvals-and-sandbox',))
        with self.assertRaises(ContractError):
            replace(adapter.profile, backend='codex')


class NativeProfileSafety(unittest.TestCase):
    def profile(self, **changes):
        values = dict(backend='codex', agent=agent('codex'), command_prefix=('codex',),
                      settings_args=(), policy_revision='p1', tool_profile_ref='tools:offline')
        values.update(changes)
        return NativeProfile(**values)

    def test_bypass_spellings_refused_in_every_launch_argument_source(self):
        unsafe = (
            '--dangerously-bypass-hook-trust', '--dangerously-bypass-hook-trust=true',
            '--permission-mode=bypassPermissions', '--dangerously-skip-permissions=true',
            '--allow-dangerously-skip-permissions=true', '--sandbox=danger-full-access',
            'sandbox_mode="danger-full-access"', "permissions.defaultMode='bypassPermissions'",
            '--ignore-rules=true', '--bare=true', '--safe-mode=true',
        )
        for field in ('command_prefix', 'settings_args', 'cap_args'):
            for argument in unsafe:
                with self.subTest(field=field, argument=argument), self.assertRaises(ContractError):
                    self.profile(**{field: (argument,)})

    def test_reviewed_profile_does_not_retain_mutable_launch_inputs(self):
        prefix, settings, caps = ['codex'], ['--sandbox', 'workspace-write'], ['--ephemeral']
        environment = {'LANG': 'C'}
        profile = self.profile(command_prefix=prefix, settings_args=settings,
                               cap_args=caps, environment=environment)
        prefix.append('--dangerously-bypass-hook-trust')
        settings[:] = ['--sandbox=danger-full-access']
        caps.append('--ignore-rules')
        environment['OPENAI_API_KEY'] = 'fixture-only'
        self.assertEqual(profile.command_prefix, ('codex',))
        self.assertEqual(profile.settings_args, ('--sandbox', 'workspace-write'))
        self.assertEqual(profile.cap_args, ('--ephemeral',))
        self.assertEqual(dict(profile.environment), {'LANG': 'C'})
        with self.assertRaises(TypeError):
            profile.environment['OPENAI_API_KEY'] = 'fixture-only'
        self.assertEqual(replace(profile, policy_revision='p2').policy_revision, 'p2')

    def test_invalid_argument_and_environment_types_refused(self):
        for changes in ({'settings_args': '--ephemeral'}, {'cap_args': (None,)},
                        {'command_prefix': ()}, {'environment': {'LANG': None}},
                        {'environment': [('LANG', 'C')]}):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.profile(**changes)


if __name__ == '__main__':
    unittest.main()
