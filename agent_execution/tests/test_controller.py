"""Control-plane composition tests: policy before context, restart, verification and outbox."""
from dataclasses import replace
import sys
import json
import time
import unittest
import threading
import fcntl
import os
from pathlib import Path
from unittest.mock import Mock, patch

from agent_execution.context import content_ref
from agent_execution.adapter import Cancellation
from agent_execution.contracts import AttemptState as S, CheckResult, ContractError, Privacy, ResultStatus
from agent_execution.controller import ExecutionController
from agent_execution.native import NativeAdapter, NativeProfile
from agent_execution.resources import GpuLease, local_gpu_admission
from agent_execution.verification import CommitVerifier, Gate, VerificationUncertain
from agent_execution.enforcement import ClaudeEnforcement
from agent_execution.policy import EffectivePolicy, ToolProfile
from agent_execution.tool_authority import AuthorityServer, ToolAuthority
from agent_execution.tests import test_native as native_fixtures
from agent_execution.tests.fixtures import agent, task


class ControllerIntegration(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeExecution('test_codex_consumes_full_context_and_normalizes_unknown_cost')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.task = replace(task(), repository=replace(task().repository, path=str(f.repo), base_commit=f.base),
                            execution_policy=replace(task().execution_policy, privacy=Privacy.CLOUD_ALLOWED))
        self.profile = NativeProfile(backend='codex', agent=agent('codex'),
                                     command_prefix=(sys.executable, str(f.script)), settings_args=(),
                                     policy_revision='p1', tool_profile_ref='tools:offline',
                                     environment={'FIXTURE_BACKEND': 'codex', 'FIXTURE_MODE': 'success',
                                                  'FIXTURE_CAPTURE': str(f.root / 'captured.json')})
        self.adapter = NativeAdapter(self.profile, f.artifacts, f.store, f.root / 'runtime')
        self.authorize = Mock()
        self.controller = ExecutionController(f.store, f.artifacts, {'codex': self.adapter}, authorize=self.authorize)
        self.addCleanup(self.controller.close)

    def start(self):
        run = self.controller.start(self.task, 'codex', self.fixture.instructions, workspace=str(self.fixture.worktree))
        self.fixture.adapters.append((self.adapter, run))
        return run

    def wait(self, run):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.controller.poll(run.attempt_id)
            if result is not None:
                return result
            time.sleep(0.02)
        self.fail('controller fixture did not finish')

    def verifier(self, task, run, candidate):
        evidence = self.fixture.artifacts.put(b'independent fixture verifier evidence')
        return replace(candidate, result_commit=self.fixture.base,
                       test_results=(CheckResult(gate_id='G1', status='passed', evidence=evidence),)), evidence

    def test_policy_refusal_happens_before_context_read_or_native_launch(self):
        self.authorize.side_effect = ContractError('privacy/tool scope refused')
        with patch.object(self.fixture.artifacts, 'read') as read, patch.object(self.adapter, 'start') as start:
            with self.assertRaises(ContractError):
                self.start()
        read.assert_not_called()
        start.assert_not_called()

    def test_single_controller_lock_refuses_second_live_instance(self):
        with self.assertRaises(ContractError):
            ExecutionController(self.fixture.store, self.fixture.artifacts, {'codex': self.adapter}, authorize=Mock())

    def test_complete_flow_runs_verifier_then_retries_tracker_without_rerunning_agent(self):
        run = self.start()
        candidate = self.wait(run)
        self.assertEqual(candidate.status, ResultStatus.CANDIDATE_COMPLETE)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        done = self.controller.verify(run.attempt_id, self.verifier)
        self.assertEqual(done.state, S.SUCCEEDED)
        deliver = Mock(side_effect=RuntimeError('tracker unavailable'))
        self.controller.deliver_outbox(deliver)
        self.assertEqual(len(self.fixture.store.pending_effects()), 1)
        deliver.side_effect = None
        self.controller.deliver_outbox(deliver)
        self.controller.deliver_outbox(deliver)
        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(self.fixture.store.pending_effects(), [])
        self.assertEqual(self.controller.poll(run.attempt_id).result_commit, self.fixture.base)
        self.assertEqual(len(self.adapter.processes), 1)

    def test_failed_verification_blocks_without_done_tracker_effect(self):
        run = self.start()
        self.wait(run)
        verifier = Mock(side_effect=ContractError('required outcome not met'))
        with self.assertRaises(ContractError):
            self.controller.verify(run.attempt_id, verifier)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.BLOCKED)
        pending = self.fixture.store.pending_effects()
        self.assertEqual(len(pending), 1)
        self.assertIn('Blocked', pending[0]['payload'])

    def test_controller_restart_recovers_prepared_process_record_without_new_launch(self):
        with patch.object(self.fixture.store, 'record_process', side_effect=RuntimeError('controller crash')):
            with self.assertRaises(RuntimeError):
                self.start()
        attempt_id = next(iter(self.controller.active))
        old = self.fixture.store.get(attempt_id)
        self.fixture.adapters.append((self.adapter, old))
        self.fixture.wait(self.adapter, old)
        self.controller.close()
        recovered_adapter = NativeAdapter(self.profile, self.fixture.artifacts, self.fixture.store,
                                           self.fixture.root / 'runtime')
        self.controller = ExecutionController(self.fixture.store, self.fixture.artifacts,
                                               {'codex': recovered_adapter}, authorize=self.authorize)
        self.addCleanup(self.controller.close)
        recovered = self.controller.recover(self.task, 'codex', attempt_id)
        self.assertIsNotNone(recovered.process_pid)
        self.assertEqual(self.controller.poll(attempt_id).status, ResultStatus.CANDIDATE_COMPLETE)
        self.controller.verify(attempt_id, self.verifier)
        self.assertEqual(recovered_adapter.processes, {})

    def test_uncertain_cancellation_persists_intent_and_reserves_workspace(self):
        run = self.start()
        uncertain = Cancellation(requested=True, termination_confirmed=False, reason='uncertain fixture')
        with patch.object(self.adapter, 'cancel', return_value=uncertain), patch.object(self.adapter, 'collect') as collect:
            self.assertFalse(self.controller.cancel(run.attempt_id).termination_confirmed)
            self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.CANCELLING)
            self.assertIsNone(self.controller.poll(run.attempt_id))
            collect.assert_not_called()
            with self.assertRaises(ContractError):
                self.start()
            self.assertEqual(self.fixture.store.pending_effects(), [])
        self.assertTrue(self.controller.cancel(run.attempt_id).termination_confirmed)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.CANCELLED)
        self.assertEqual(len(self.fixture.store.pending_effects()), 1)
        self.assertTrue(self.controller.cancel(run.attempt_id).termination_confirmed)
        self.assertEqual(len(self.fixture.store.pending_effects()), 1)

    def test_crash_during_cancellation_does_not_forget_requested_state(self):
        run = self.start()
        with patch.object(self.adapter, 'cancel', side_effect=RuntimeError('signal boundary crash')):
            with self.assertRaises(RuntimeError):
                self.controller.cancel(run.attempt_id)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.CANCELLING)
        self.controller.close()
        self.controller = ExecutionController(self.fixture.store, self.fixture.artifacts,
                                               {'codex': self.adapter}, authorize=self.authorize)
        self.addCleanup(self.controller.close)
        self.controller.recover(self.task, 'codex', run.attempt_id)
        self.controller.poll(run.attempt_id)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.CANCELLED)
        self.assertEqual(len(self.adapter.processes), 1)

    def test_live_local_supervisor_retains_gpu_admission_across_controller_restart(self):
        self.controller.close()
        lock_path = self.fixture.root / 'gpu.lock'
        profile = replace(self.profile, backend='local', agent=agent(),
                          environment=dict(self.profile.environment, FIXTURE_BACKEND='local', FIXTURE_MODE='hang'))
        local = NativeAdapter(profile, self.fixture.artifacts, self.fixture.store, self.fixture.root / 'local-runtime')
        self.controller = ExecutionController(
            self.fixture.store, self.fixture.artifacts, {'local': local}, authorize=self.authorize,
            resource_for=local_gpu_admission(media_state_path=self.fixture.root / 'media-pending', lock_path=lock_path))
        self.addCleanup(self.controller.close)
        run = self.controller.start(self.task, 'local', self.fixture.instructions, workspace=str(self.fixture.worktree))
        self.fixture.adapters.append((local, run))
        self.controller.close()
        with self.assertRaises(ContractError):
            with GpuLease(lock_path=lock_path):
                self.fail('native supervisor lost GPU ownership')
        recovered = NativeAdapter(profile, self.fixture.artifacts, self.fixture.store, self.fixture.root / 'local-runtime')
        self.controller = ExecutionController(self.fixture.store, self.fixture.artifacts,
                                               {'local': recovered}, authorize=self.authorize)
        self.addCleanup(self.controller.close)
        self.controller.recover(self.task, 'local', run.attempt_id)
        self.assertTrue(self.controller.cancel(run.attempt_id).termination_confirmed)
        with GpuLease(lock_path=lock_path):
            pass

    def gate_verifier(self, code, *, timeout=2, gate_id='G1'):
        return CommitVerifier(self.fixture.artifacts, self.fixture.root / 'verification',
                              (Gate(gate_id, ('/usr/bin/python3', '-c', code), timeout),))

    def test_independent_gate_runs_and_pins_exact_commit_before_done(self):
        run = self.start()
        self.wait(run)
        verifier = self.gate_verifier("from pathlib import Path; assert Path('README').read_text() == 'fixture\\n'")
        done = self.controller.verify(run.attempt_id, verifier)
        self.assertEqual(done.state, S.SUCCEEDED)
        result = self.fixture.store.result(run.attempt_id)
        self.assertEqual(result.result_commit, self.fixture.base)
        self.assertEqual(result.test_results[0].status, 'passed')
        record = self.fixture.artifacts.read(result.test_results[0].evidence)
        self.assertIn(self.fixture.base.encode(), record)

    def test_failed_independent_gate_cannot_emit_done(self):
        run = self.start()
        self.wait(run)
        with self.assertRaisesRegex(ContractError, 'independent gate failed'):
            self.controller.verify(run.attempt_id, self.gate_verifier('raise SystemExit(9)'))
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.BLOCKED)
        self.assertNotIn('Done', self.fixture.store.pending_effects()[0]['payload'])
        result = self.fixture.store.result(run.attempt_id)
        self.assertEqual(result.status, ResultStatus.FAILED)
        self.assertEqual(result.test_results[0].status, 'failed')
        self.assertTrue(self.fixture.artifacts.read(result.test_results[0].evidence))
        self.assertIn('verification', self.fixture.store.pending_effects()[0]['payload'])

    def test_gate_mutating_candidate_is_refused(self):
        run = self.start()
        self.wait(run)
        with self.assertRaisesRegex(ContractError, 'worktree is dirty'):
            self.controller.verify(run.attempt_id, self.gate_verifier("from pathlib import Path; Path('README').write_text('changed')"))
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.BLOCKED)
        self.assertEqual((self.fixture.worktree / 'README').read_text(), 'changed')

    def test_missing_trusted_gate_registry_refuses_execution(self):
        run = self.start()
        self.wait(run)
        verifier = self.gate_verifier('raise AssertionError("must not run")', gate_id='unapproved')
        with patch.object(verifier, '_run') as execute, self.assertRaises(ContractError):
            self.controller.verify(run.attempt_id, verifier)
        execute.assert_not_called()

    def test_policy_is_rechecked_after_verification_before_success(self):
        run = self.start()
        self.wait(run)
        def changed_policy(task, run, candidate):
            result = self.verifier(task, run, candidate)
            self.authorize.side_effect = ContractError('task changed during verification')
            return result
        with self.assertRaisesRegex(ContractError, 'task changed'):
            self.controller.verify(run.attempt_id, changed_policy)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.BLOCKED)

    def test_uncertain_check_termination_keeps_workspace_reserved(self):
        run = self.start()
        self.wait(run)
        with self.assertRaises(VerificationUncertain):
            self.controller.verify(run.attempt_id, Mock(side_effect=VerificationUncertain('check still live')))
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        another = Mock()
        with self.assertRaisesRegex(ContractError, 'unreconciled claim'):
            self.controller.verify(run.attempt_id, another)
        another.assert_not_called()
        with self.assertRaises(ContractError):
            self.start()
        self.assertEqual(self.fixture.store.pending_effects(), [])

    def test_independent_gate_timeout_reaps_escaped_descendants(self):
        run = self.start()
        self.wait(run)
        code = "import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)"
        with self.assertRaisesRegex(ContractError, 'independent gate failed'):
            self.controller.verify(run.attempt_id, self.gate_verifier(code, timeout=1))
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.BLOCKED)

    def test_stopped_verifier_reconciles_after_restart_then_runs_checks_again(self):
        run = self.start()
        self.wait(run)
        verifier = self.gate_verifier('pass')
        with patch.object(verifier, '_manifest', side_effect=VerificationUncertain('controller interrupted')):
            with self.assertRaises(VerificationUncertain):
                self.controller.verify(run.attempt_id, verifier)
        token = self.fixture.store.verification_claim(run.attempt_id)
        self.controller.close()
        self.controller = ExecutionController(self.fixture.store, self.fixture.artifacts,
                                               {'codex': self.adapter}, authorize=self.authorize)
        self.addCleanup(self.controller.close)
        self.controller.recover(self.task, 'codex', run.attempt_id)
        restored = self.controller.reconcile_verification(run.attempt_id, verifier)
        self.assertEqual(restored.state, S.VERIFYING)
        self.assertIsNone(self.fixture.store.verification_claim(run.attempt_id))
        self.assertTrue((verifier.runtime_root / run.attempt_id / token / 'claim.json').exists())
        self.assertEqual(self.fixture.store.pending_effects(), [])
        self.assertEqual(self.controller.verify(run.attempt_id, verifier).state, S.SUCCEEDED)
        self.assertEqual(len(list((verifier.runtime_root / run.attempt_id).iterdir())), 2)

    def test_live_verifier_blocks_reconciliation_and_cancellation(self):
        run = self.start()
        candidate = self.wait(run)
        run = self.fixture.store.get(run.attempt_id)
        token = self.fixture.store.begin_verification(run.attempt_id, self.controller.owner_id, run.claim.generation)
        # The immediate gate exits while a setsid descendant retains the sandbox.
        verifier = self.gate_verifier("import subprocess,sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(1)'], start_new_session=True)")
        outcome = []
        def execute():
            try:
                outcome.append(verifier.run_claim(self.task, run, candidate, token))
            except BaseException as exc:
                outcome.append(exc)
        thread = threading.Thread(target=execute)
        thread.start()
        self.addCleanup(thread.join, 5)
        directory = verifier.runtime_root / run.attempt_id / token
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not list(directory.glob('*/process.json')):
            time.sleep(0.01)
        self.assertTrue(list(directory.glob('*/process.json')))
        with self.assertRaises(VerificationUncertain):
            self.controller.reconcile_verification(run.attempt_id, verifier)
        with self.assertRaisesRegex(ContractError, 'reconcile verifier'):
            self.controller.cancel(run.attempt_id)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertEqual(self.fixture.store.verification_claim(run.attempt_id), token)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        # Even terminal records cannot override an actively held runner lock.
        with (directory / 'claim.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(VerificationUncertain):
                self.controller.reconcile_verification(run.attempt_id, verifier)
        self.controller.reconcile_verification(run.attempt_id, verifier)
        self.assertTrue(self.controller.cancel(run.attempt_id).termination_confirmed)

    def test_supervisor_keeps_verifier_claim_locked_after_runner_crash(self):
        import subprocess
        run = self.start()
        candidate = self.wait(run)
        run = self.fixture.store.get(run.attempt_id)
        token = self.fixture.store.begin_verification(run.attempt_id, self.controller.owner_id, run.claim.generation)
        verifier = self.gate_verifier('import time; time.sleep(1)')
        original = subprocess.Popen
        pid = os.fork()
        if pid == 0:
            def launch(*args, **kwargs):
                proc = original(*args, **kwargs)
                if args[0][1].endswith('/launcher.py'):
                    proc.wait = lambda **_: os._exit(0)
                return proc
            try:
                with patch('agent_execution.verification.subprocess.Popen', side_effect=launch):
                    verifier.run_claim(self.task, run, candidate, token)
            except BaseException:
                os._exit(2)
            os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        with self.assertRaises(VerificationUncertain):
            self.controller.reconcile_verification(run.attempt_id, verifier)
        deadline = time.monotonic() + 5
        while True:
            try:
                self.controller.reconcile_verification(run.attempt_id, verifier)
                break
            except VerificationUncertain:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertEqual(self.fixture.store.pending_effects(), [])

    def test_verifier_claim_without_launch_journal_stays_quarantined(self):
        run = self.start()
        self.wait(run)
        self.fixture.store.begin_verification(run.attempt_id, self.controller.owner_id, run.claim.generation)
        with self.assertRaises(VerificationUncertain):
            self.controller.reconcile_verification(run.attempt_id, self.gate_verifier('pass'))
        with self.assertRaisesRegex(ContractError, 'reconcile verifier'):
            self.controller.cancel(run.attempt_id)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertEqual(self.fixture.store.pending_effects(), [])

    def test_revoked_policy_allows_verifier_reconciliation_and_safe_cancellation(self):
        run = self.start()
        self.wait(run)
        verifier = self.gate_verifier('pass')
        with patch.object(verifier, '_manifest', side_effect=VerificationUncertain('interrupted')):
            with self.assertRaises(VerificationUncertain):
                self.controller.verify(run.attempt_id, verifier)
        self.authorize.side_effect = ContractError('policy revoked')
        self.controller.reconcile_verification(run.attempt_id, verifier)
        with self.assertRaisesRegex(ContractError, 'policy revoked'):
            self.controller.verify(run.attempt_id, verifier)
        self.assertTrue(self.controller.cancel(run.attempt_id).termination_confirmed)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.CANCELLED)
        self.assertNotIn('Done', self.fixture.store.pending_effects()[0]['payload'])

    def test_recovery_refuses_missing_exit_record_and_foreign_claim_scope(self):
        run = self.start()
        candidate = self.wait(run)
        run = self.fixture.store.get(run.attempt_id)
        token = self.fixture.store.begin_verification(run.attempt_id, self.controller.owner_id, run.claim.generation)
        verifier = self.gate_verifier('pass')
        verifier.run_claim(self.task, run, candidate, token)
        directory = verifier.runtime_root / run.attempt_id / token
        record = next(directory.glob('*/process.json'))
        data = record.read_bytes()
        record.unlink()
        with self.assertRaises(VerificationUncertain):
            self.controller.reconcile_verification(run.attempt_id, verifier)
        record.write_text('[]')
        record.chmod(0o600)
        with self.assertRaises(VerificationUncertain):
            self.controller.reconcile_verification(run.attempt_id, verifier)
        record.write_bytes(data)
        record.chmod(0o600)
        scope = directory / 'claim.json'
        contents = json.loads(scope.read_text())
        contents['generation'] += 1
        scope.write_text(json.dumps(contents))
        with self.assertRaises(VerificationUncertain):
            self.controller.reconcile_verification(run.attempt_id, verifier)
        self.assertEqual(self.fixture.store.verification_claim(run.attempt_id), token)

    def test_native_launch_receives_scoped_settings_and_uses_real_hook_authority(self):
        self.controller.close()
        socket_for = lambda run: self.fixture.root / 'authority' / (run.attempt_id + '.sock')
        spec = replace(agent(), supported_tools=('Read',))
        tool_profile = ToolProfile(ref='tools:offline', policy_revision='p1', agent_ids=('local',),
                                    tools=('Read',), allowed_actions=('read',), approval_actions=(),
                                    max_wall_seconds=60, cost_policy_refs=('budget:no-paid-calls',),
                                    local_route_verified=True)
        policy = EffectivePolicy(current_task=lambda _: self.task, profiles=(tool_profile,),
                                   operators=('operator',), store=self.fixture.store)
        profile = replace(self.profile, backend='local', agent=spec,
                          environment=dict(self.profile.environment, FIXTURE_BACKEND='local', FIXTURE_MODE='tool'))
        enforced = NativeAdapter(profile, self.fixture.artifacts, self.fixture.store,
                                  self.fixture.root / 'enforced-runtime',
                                  enforcement=ClaudeEnforcement(self.fixture.root / 'permissions', socket_for))
        def authority_for(task, agent, run):
            return AuthorityServer(socket_for(run), ToolAuthority(policy, task, agent, run, sandbox_verified=True))
        self.controller = ExecutionController(self.fixture.store, self.fixture.artifacts,
                                               {'local': enforced}, authorize=policy,
                                               tool_authority_for=authority_for,
                                               resource_for=local_gpu_admission(
                                                   media_state_path=self.fixture.root / 'media-pending',
                                                   lock_path=self.fixture.root / 'gpu.lock'))
        self.addCleanup(self.controller.close)
        run = self.controller.start(self.task, 'local', self.fixture.instructions, workspace=str(self.fixture.worktree))
        self.fixture.adapters.append((enforced, run))
        result = self.wait(run)
        self.assertEqual(result.status, ResultStatus.CANDIDATE_COMPLETE)
        captured = json.loads((self.fixture.root / 'captured.json').read_text())
        self.assertEqual(captured['verdicts'], ['allow', 'deny'])
        self.assertIn('--restricted', captured['argv'])
        settings = captured['settings']
        self.assertTrue(settings['sandbox']['failIfUnavailable'])
        self.assertFalse(settings['sandbox']['allowUnsandboxedCommands'])
        self.assertEqual(settings['sandbox']['network']['allowedDomains'], [])
        self.assertIn(str(self.fixture.worktree / '**' / '.env*'), settings['sandbox']['filesystem']['denyRead'])
        self.assertFalse(socket_for(run).exists())

    def test_policy_revocation_cancels_owned_native_writer(self):
        self.adapter.profile = replace(self.profile, environment=dict(self.profile.environment, FIXTURE_MODE='hang'))
        run = self.start()
        self.authorize.side_effect = ContractError('current task policy revoked')
        with self.assertRaisesRegex(ContractError, 'policy revoked'):
            self.controller.poll(run.attempt_id)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.CANCELLED)
        self.assertEqual(self.adapter.recover(run).status, 'exited')

    def test_expired_lease_still_allows_proven_owner_to_cancel(self):
        self.adapter.profile = replace(self.profile, environment=dict(self.profile.environment, FIXTURE_MODE='hang'))
        run = self.start()
        self.fixture.store.clock = lambda: time.time() + 1000
        cancelled = self.controller.cancel(run.attempt_id)
        self.assertTrue(cancelled.termination_confirmed)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.CANCELLED)

    def test_independent_check_cannot_read_operator_files_or_workspace_secrets(self):
        outside = self.fixture.root / 'operator-secret'
        outside.write_text('disposable-secret')
        excludes = self.fixture.root / 'git-excludes'
        excludes.write_text('.env\n')
        self.fixture.git('config', 'core.excludesFile', str(excludes))
        (self.fixture.worktree / '.env').write_text('workspace-disposable-secret')
        run = self.start()
        self.wait(run)
        code = ("from pathlib import Path; "
                f"assert not Path({str(outside)!r}).exists(); "
                "assert Path('.env').read_bytes() == b''; "
                "assert not Path('/home/jdelimam/.aws').exists()")
        done = self.controller.verify(run.attempt_id, self.gate_verifier(code))
        self.assertEqual(done.state, S.SUCCEEDED)
        self.assertEqual(outside.read_text(), 'disposable-secret')
        self.assertEqual((self.fixture.worktree / '.env').read_text(), 'workspace-disposable-secret')


    @unittest.skipUnless(Path('/etc/ssl/certs/ca-certificates.crt').is_file(), 'no system CA bundle')
    def test_independent_check_trusts_the_system_ca_store_not_inherited_or_workspace_pems(self):
        excludes = self.fixture.root / 'git-excludes'
        excludes.write_text('bundle.pem\n')
        self.fixture.git('config', 'core.excludesFile', str(excludes))
        (self.fixture.worktree / 'bundle.pem').write_text('workspace-disposable-pem')
        run = self.start()
        self.wait(run)
        code = ("import os, ssl; from pathlib import Path; "
                "assert os.environ['SSL_CERT_FILE'] == '/etc/ssl/certs/ca-certificates.crt'; "
                "assert ssl.create_default_context().cert_store_stats()['x509_ca'] > 0; "
                "assert not Path('/etc/ssl/private').exists(); "
                "assert Path('bundle.pem').read_bytes() == b''")
        with patch.dict(os.environ, {'SSL_CERT_FILE': str(self.fixture.root / 'inherited.pem')}):
            done = self.controller.verify(run.attempt_id, self.gate_verifier(code))
        self.assertEqual(done.state, S.SUCCEEDED)

if __name__ == '__main__':
    unittest.main()
