"""V17: the default branch moves during verification. The candidate the integration policy requires
(one containing the current tip) is reconciled by a controller-made merge and verified again."""
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch

from agent_execution.contracts import AttemptState as S, ContractError
from agent_execution.tests.fixtures import agent, task
from agent_execution.policy import EffectivePolicy
from agent_execution.integration import DefaultBranchIntegration, IntegrationConflict, merge_message
from agent_execution.tests.test_controller_finalization import AUTHOR, Harness


class IntegrationHarness(Harness):
    def setUp(self):
        super().setUp()
        self.integration = DefaultBranchIntegration(AUTHOR, branch='main', max_rounds=3,
                                                    commit_date='2026-01-02T00:00:00Z')
        self.controller.close()
        self.controller = self.make(integration=self.integration)

    def advance_main(self, name, content='upstream\n'):
        f = self.fixture
        (f.repo / name).write_text(content)
        f.git('add', name)
        f.git('commit', '-qm', 'upstream ' + name)
        return f.git('rev-parse', 'HEAD').strip()

    def parents(self, rev='HEAD'):
        return self.git('rev-list', '--parents', '-n', '1', rev).split()[1:]

    def payload(self):
        (effect,) = self.fixture.store.pending_effects()
        return json.loads(effect['payload'])

    def counting(self, verifier, on_call=None):
        calls = []
        real = verifier._verify

        def wrapped(*args, **kwargs):
            outcome = real(*args, **kwargs)
            calls.append(args[2].result_commit)
            if on_call is not None:
                on_call(len(calls))
            return outcome

        patcher = patch.object(verifier, '_verify', side_effect=wrapped)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def finalized_only(self, run):
        """Leave a finalized candidate recorded and VERIFYING by refusing the integrate phase."""
        self.advance_main('first.txt')
        self.authorize.side_effect = lambda task, agent, phase: (
            (_ for _ in ()).throw(ContractError('revoked')) if phase == 'integrate' else None)
        with self.assertRaisesRegex(ContractError, 'policy re-check failed'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.authorize.side_effect = None
        return self.fixture.store.result(run.attempt_id).result_commit


class DefaultBranchMoves(IntegrationHarness):
    def test_unmoved_default_branch_verifies_the_finalized_commit_without_a_merge(self):
        run = self.candidate()
        done = self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(done.state, S.SUCCEEDED)
        self.assertEqual(self.parents(), [self.fixture.base])
        self.assertEqual(self.payload()['default_branch_tip'], self.fixture.base)
        self.assertNotIn('integrate', [c.args[2] for c in self.authorize.call_args_list])

    def test_moved_before_verification_the_merge_is_what_gets_verified(self):
        run = self.candidate()
        tip = self.advance_main('upstream.txt')
        verifier = self.verifier()
        calls = self.counting(verifier)
        done = self.controller.verify(run.attempt_id, verifier)
        self.assertEqual(done.state, S.SUCCEEDED)
        head = self.git('rev-parse', 'HEAD')
        finalized, merged_tip = self.parents()
        self.assertEqual(merged_tip, tip)
        self.assertEqual(self.parents(finalized), [self.fixture.base])
        self.assertEqual(calls, [head])
        self.assertEqual(self.git('show', 'HEAD:upstream.txt'), 'upstream')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('branch', '--show-current'), 'feat/fixture')
        self.assertEqual(self.fixture.git('rev-parse', 'main').strip(), tip)
        stored = self.fixture.store.result(run.attempt_id)
        self.assertEqual((stored.result_commit, stored.changes), (head, ('module.py',)))
        self.assertEqual((self.payload()['commit'], self.payload()['default_branch_tip']), (head, tip))
        phases = [c.args[2] for c in self.authorize.call_args_list]
        self.assertLess(phases.index('finalize'), phases.index('integrate'))
        self.assertEqual(phases[-1], 'verify')

    def test_moved_during_verification_reconciles_and_reverifies(self):
        run = self.candidate()
        tips = []
        verifier = self.verifier()
        calls = self.counting(verifier, on_call=lambda n: tips.append(self.advance_main('during.txt')) if n == 1 else None)
        done = self.controller.verify(run.attempt_id, verifier)
        self.assertEqual(done.state, S.SUCCEEDED)
        head = self.git('rev-parse', 'HEAD')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], head)
        self.assertEqual(self.parents(), [calls[0], tips[0]])
        self.assertEqual(self.fixture.store.result(run.attempt_id).result_commit, head)
        self.assertEqual(self.payload()['default_branch_tip'], tips[0])
        self.assertIsNone(self.fixture.store.verification_claim(run.attempt_id))

    def test_a_tip_that_keeps_moving_leaves_the_attempt_awaiting_verification(self):
        self.controller.close()
        self.controller = self.make(integration=replace_rounds(self.integration, 2))
        run = self.candidate()
        verifier = self.verifier()
        calls = self.counting(verifier, on_call=lambda n: self.advance_main(f'move{n}.txt'))
        with self.assertRaisesRegex(ContractError, 'kept moving'):
            self.controller.verify(run.attempt_id, verifier)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertIsNone(self.fixture.store.verification_claim(run.attempt_id))
        self.assertEqual(self.fixture.store.pending_effects(), [])
        done = self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(done.state, S.SUCCEEDED)
        self.assertEqual(self.git('show', 'HEAD:move2.txt'), 'upstream')

    def test_a_conflicting_default_branch_blocks_without_touching_the_worktree(self):
        run = self.candidate()
        tip = self.advance_main('module.py', 'VALUE = 2\n')
        with self.assertRaises(IntegrationConflict):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.BLOCKED)
        self.assertEqual(self.parents(), [self.fixture.base])
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('show', 'HEAD:module.py'), 'VALUE = 1')
        self.assertEqual(self.payload(), {'status': 'Blocked', 'attempt_id': run.attempt_id,
                                          'reason': 'default branch conflicts with the candidate',
                                          'default_branch_tip': tip})

    def test_policy_revoked_at_integration_merges_nothing(self):
        run = self.candidate()
        finalized = self.finalized_only(run)
        self.assertEqual(self.git('rev-parse', 'HEAD'), finalized)
        self.assertEqual(self.parents(), [self.fixture.base])
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertEqual(self.fixture.store.pending_effects(), [])
        self.assertIsNone(self.fixture.store.verification_claim(run.attempt_id))

    def test_unrelated_local_edits_are_preserved_and_refuse_the_merge(self):
        run = self.candidate()
        finalized = self.finalized_only(run)
        (self.fixture.worktree / 'notes.txt').write_text('operator notes\n')
        with self.assertRaisesRegex(ContractError, 'not clean'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual((self.fixture.worktree / 'notes.txt').read_text(), 'operator notes\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), finalized)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)

    def test_crash_after_the_merge_recovers_the_same_merge_on_retry(self):
        run = self.candidate()
        tip = self.advance_main('upstream.txt')
        with patch.object(self.fixture.store, 'record_integrated', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.controller.verify(run.attempt_id, self.verifier())
        merged = self.git('rev-parse', 'HEAD')
        finalized = self.fixture.store.result(run.attempt_id).result_commit
        self.assertEqual(self.parents(), [finalized, tip])
        done = self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(done.state, S.SUCCEEDED)
        self.assertEqual(self.git('rev-parse', 'HEAD'), merged)
        self.assertEqual(self.fixture.store.result(run.attempt_id).result_commit, merged)

    def test_a_foreign_head_is_not_adopted(self):
        run = self.candidate()
        finalized = self.finalized_only(run)
        self.git('commit', '-q', '--allow-empty', '-m', 'someone else')
        with self.assertRaisesRegex(ContractError, 'not the recorded candidate'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(self.parents(), [finalized])
        self.assertEqual(self.fixture.store.result(run.attempt_id).result_commit, finalized)

    def test_a_merge_with_a_forged_message_is_not_adopted(self):
        run = self.candidate()
        finalized = self.finalized_only(run)
        tip = self.fixture.git('rev-parse', 'main').strip()
        self.git('merge', '-q', '--no-ff', '-m', 'not ours', tip)
        with self.assertRaisesRegex(ContractError, 'not the recorded candidate'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(self.fixture.store.result(run.attempt_id).result_commit, finalized)

    def test_a_merge_on_another_first_parent_is_not_adopted(self):
        run = self.candidate()
        finalized = self.finalized_only(run)
        tip = self.fixture.git('rev-parse', 'main').strip()
        self.git('commit', '-q', '--allow-empty', '-m', 'someone else')
        candidate = self.fixture.store.result(run.attempt_id)
        self.git('merge', '-q', '--no-ff', '-m', merge_message(candidate, tip), tip)
        with self.assertRaisesRegex(ContractError, 'not the recorded candidate'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(self.fixture.store.result(run.attempt_id).result_commit, finalized)

    def test_a_worktree_off_its_feature_branch_is_not_merged(self):
        run = self.candidate()
        finalized = self.finalized_only(run)
        self.git('checkout', '-q', '-b', 'other')
        with self.assertRaisesRegex(ContractError, 'feature branch'):
            self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(self.git('rev-parse', 'HEAD'), finalized)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)

    def test_unproven_agent_termination_merges_nothing(self):
        run = self.candidate()
        finalized = self.finalized_only(run)
        with patch.object(self.adapter, 'recover', return_value=Mock(status='adopted')):
            with self.assertRaisesRegex(ContractError, 'termination is not proven'):
                self.controller.verify(run.attempt_id, self.verifier())
        self.assertEqual(self.git('rev-parse', 'HEAD'), finalized)
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)

    def test_a_moved_tip_with_unproven_check_termination_keeps_the_claim(self):
        run = self.candidate()
        verifier = self.verifier()
        self.counting(verifier, on_call=lambda n: self.advance_main('during.txt'))
        with patch.object(verifier, 'reconcile', return_value=False):
            with self.assertRaisesRegex(ContractError, 'termination remains uncertain'):
                self.controller.verify(run.attempt_id, verifier)
        self.assertIsNotNone(self.fixture.store.verification_claim(run.attempt_id))
        self.assertEqual(self.fixture.store.get(run.attempt_id).state, S.VERIFYING)
        self.assertEqual(self.fixture.store.pending_effects(), [])

    def test_a_verifier_that_cannot_prove_its_checks_stopped_is_refused_first(self):
        run = self.candidate()
        tip = self.advance_main('upstream.txt')
        with self.assertRaisesRegex(ContractError, 'prove its checks stopped'):
            self.controller.verify(run.attempt_id, Mock(spec=['__call__', 'max_seconds']))
        self.assert_untouched(run)
        self.assertEqual(self.fixture.git('rev-parse', 'main').strip(), tip)


def replace_rounds(integration, rounds):
    return DefaultBranchIntegration(integration.author, branch=integration.branch, max_rounds=rounds,
                                    commit_date=integration.commit_date)


class StoreIntegrationFence(IntegrationHarness):
    def setUp(self):
        super().setUp()
        self.run_ = self.candidate()
        self.finalized = self.finalized_only(self.run_)
        self.stored = self.fixture.store.result(self.run_.attempt_id)
        self.good = replace(self.stored, result_commit=self.fixture.base)

    def owner(self):
        current = self.fixture.store.get(self.run_.attempt_id)
        return self.run_.attempt_id, self.controller.owner_id, current.claim.generation

    def test_only_the_same_candidate_under_a_new_commit_is_accepted(self):
        store = self.fixture.store
        for previous, bad in ((self.fixture.base, self.good), (self.finalized, self.stored),
                              (self.finalized, replace(self.good, changes=('other.py',))),
                              (self.finalized, replace(self.good, errors=('x',))),
                              (self.finalized, replace(self.good, workspace='/elsewhere'))):
            with self.subTest(previous=previous, bad=bad), self.assertRaises(ContractError):
                store.record_integrated(*self.owner(), previous, bad)
        self.assertEqual(store.result(self.run_.attempt_id).result_commit, self.finalized)
        self.assertEqual(store.record_integrated(*self.owner(), self.finalized, self.good), self.good)

    def test_stale_owner_held_claim_and_left_verification_are_refused(self):
        store = self.fixture.store
        attempt_id, owner, generation = self.owner()
        with self.assertRaises(ContractError):
            store.record_integrated(attempt_id, owner, generation + 1, self.finalized, self.good)
        with self.assertRaises(ContractError):
            store.record_integrated(attempt_id, 'someone-else', generation, self.finalized, self.good)
        token = store.begin_verification(attempt_id, owner, generation)
        with self.assertRaisesRegex(ContractError, 'verifier claim'):
            store.record_integrated(attempt_id, owner, generation, self.finalized, self.good)
        store.reconcile_verification(attempt_id, owner, generation, token=token, termination_confirmed=True)
        store.request_cancellation(attempt_id, owner, generation)
        with self.assertRaisesRegex(ContractError, 'not awaiting verification'):
            store.record_integrated(attempt_id, owner, generation, self.finalized, self.good)
        self.assertEqual(store.result(attempt_id).result_commit, self.finalized)


class Configuration(unittest.TestCase):
    def test_integration_arguments_are_validated(self):
        for kwargs in ({'author': ('', 'x@y')}, {'author': 'name'}, {'branch': 'feat/x'}, {'branch': '-x'},
                       {'branch': 'a..b'}, {'branch': ''}, {'max_rounds': 0}, {'max_rounds': True},
                       {'branch': {}}, {'branch': {'relative': 'main'}}, {'branch': {'/r': 'feat/x'}},
                       {'branch': {'/r': 'main', '/s': ''}}, {'branch': ['main']}):
            arguments = {'author': AUTHOR, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                DefaultBranchIntegration(arguments.pop('author'), **arguments)

    def test_a_mapping_reads_each_repositorys_own_default_branch(self):
        import subprocess, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tips = {}
            for name, branch in (('a', 'master'), ('b', 'main')):
                repo = Path(tmp) / name
                subprocess.run(['git', 'init', '-q', '-b', branch, str(repo)], check=True)
                subprocess.run(['git', '-C', str(repo), '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit',
                                '-q', '--allow-empty', '-m', name], check=True)
                tips[str(repo)] = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                                                 capture_output=True, text=True).stdout.strip()
            integration = DefaultBranchIntegration(AUTHOR, branch={str(Path(tmp) / 'a'): 'master',
                                                                   str(Path(tmp) / 'b'): 'main'})
            for path, tip in tips.items():
                at = replace(task(), repository=replace(task().repository, path=path))
                self.assertEqual(integration.tip(at), tip)
            stranger = replace(task(), repository=replace(task().repository, path=str(Path(tmp) / 'c')))
            with self.assertRaisesRegex(ContractError, 'no default branch configured'):
                integration.tip(stranger)

    def test_merge_refuses_dirty_candidates_unproven_termination_and_a_false_policy(self):
        integration = DefaultBranchIntegration(AUTHOR)
        candidate = Mock(result_commit='a' * 40, diff_ref=None, workspace='/nonexistent')
        with self.assertRaisesRegex(ContractError, 'committed candidate'):
            integration.merge(Mock(result_commit=None, diff_ref=object()), 'b' * 40,
                              terminated=lambda: True, policy_check=lambda: True)
        with self.assertRaisesRegex(ContractError, 'committed candidate'):
            integration.merge(Mock(result_commit=None, diff_ref=None), 'b' * 40,
                              terminated=lambda: True, policy_check=lambda: True)
        with self.assertRaisesRegex(ContractError, 'committed candidate'):
            integration.merge(Mock(result_commit='a' * 40, diff_ref=object()), 'b' * 40,
                              terminated=lambda: True, policy_check=lambda: True)
        with self.assertRaisesRegex(ContractError, 'termination is not proven'):
            integration.merge(candidate, 'b' * 40, terminated=lambda: 'yes', policy_check=lambda: True)
        with self.assertRaisesRegex(ContractError, 'no longer permits'):
            integration.merge(candidate, 'b' * 40, terminated=lambda: True, policy_check=lambda: 1)

    def test_merge_message_names_the_attempt_and_tip(self):
        result = Mock(task_id='t', attempt_id='a', generation=3)
        self.assertEqual(merge_message(result, 'f' * 40).splitlines()[2:],
                         ['Execution-Attempt: a', 'Execution-Generation: 3', 'Integrates: ' + 'f' * 40])


class IntegratePhase(unittest.TestCase):
    def test_effective_policy_knows_the_integrate_phase(self):
        current = Mock(return_value=None)
        policy = EffectivePolicy(current_task=current, profiles=(), operators=(), store=None)
        with self.assertRaisesRegex(ContractError, 'changed; reconciliation required'):
            policy(task(), agent('codex'), 'integrate')
        current.assert_called_once()


class ControllerConfiguration(IntegrationHarness):
    def test_the_controller_refuses_an_incomplete_integration_policy(self):
        self.controller.close()
        for bad in (Mock(spec=['tip', 'contains', 'max_rounds'], max_rounds=1),
                    Mock(spec=['tip', 'contains', 'merge'], max_rounds=0)):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                self.make(integration=bad)


if __name__ == '__main__':
    unittest.main()
