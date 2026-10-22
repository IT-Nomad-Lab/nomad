"""Authority and durable approval negatives; no native sandbox claims."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from agent_execution.contracts import AttemptState, ContractError
from agent_execution.policy import EffectivePolicy, OperatorApproval, ToolProfile, action_scope
from agent_execution.store import AttemptStore
from agent_execution.tests.fixtures import ARTIFACT, agent, task


class PolicyAuthority(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = AttemptStore(Path(tmp.name) / 'attempts.db', clock=lambda: 1000)
        self.addCleanup(self.store.close)
        self.task = replace(task(), execution_policy=replace(task().execution_policy,
                            approval_actions=('publish',)))
        self.agent = replace(agent(), supported_tools=('Read', 'mcp:tracker:comment'))
        self.profile = ToolProfile(ref='tools:offline', policy_revision='p1', agent_ids=('local',),
                                   tools=self.agent.supported_tools, allowed_actions=('read',),
                                   approval_actions=('publish',), max_wall_seconds=60,
                                   cost_policy_refs=('budget:no-paid-calls',), local_route_verified=True)
        self.current = self.task
        self.policy = self.make_policy()
        self.run = self.store.claim(self.task, ARTIFACT, agent_id='local', workspace=str(Path(tmp.name) / 'wt'),
                                    owner_id='scheduler')
        self.run = self.store.record_process(self.run.attempt_id, 'scheduler', self.run.claim.generation,
                                             pid=12345, start_time='fixture')

    def make_policy(self, profile=None):
        return EffectivePolicy(current_task=lambda _: self.current, profiles=(profile or self.profile,),
                               operators=('operator',), store=self.store, clock=lambda: 1000)

    def scope(self, **kwargs):
        return action_scope(self.task, tool='mcp:tracker:comment', action='publish', target='task:123',
                            arguments={'body': kwargs.get('body', 'approved content')})

    def approval(self, **kwargs):
        return OperatorApproval(approval_id='approval:1', actor_id=kwargs.get('actor', 'operator'),
                                scope=self.scope(), expires_at=kwargs.get('expiry', '1970-01-01T00:30:00Z'))

    def test_current_task_required_and_unchanged_revision_cannot_hide_requirement_edit(self):
        self.policy(self.task, self.agent, 'prepare')
        self.current = replace(self.task, objective='different requirement, same revision')
        with self.assertRaises(ContractError):
            self.policy(self.task, self.agent, 'prepare')
        self.current = None
        with self.assertRaises(ContractError):
            self.policy(self.task, self.agent, 'recover')

    def test_local_only_cannot_fall_back_to_cloud(self):
        for backend in ('claude', 'codex'):
            with self.subTest(backend=backend), self.assertRaises(ContractError):
                self.policy(self.task, agent(backend), 'prepare')

    def test_unproven_local_route_unknown_budget_and_unenforced_scope_refused(self):
        for profile in (replace(self.profile, local_route_verified=False),
                        replace(self.profile, cost_policy_refs=()),
                        replace(self.profile, allowed_actions=()),
                        replace(self.profile, tools=('mcp:unknown',)),
                        replace(self.profile, max_wall_seconds=59),
                        replace(self.profile, policy_revision='obsolete')):
            with self.subTest(profile=profile), self.assertRaises(ContractError):
                self.make_policy(profile)(self.task, self.agent, 'prepare')

    def test_unauthorized_mcp_is_refused_even_for_otherwise_allowed_action(self):
        scope = action_scope(self.task, tool='mcp:unknown', action='read', target='data', arguments={})
        with self.assertRaises(ContractError):
            self.policy.authorize_effect(self.task, self.agent, self.run, scope)

    def test_operator_actor_expiry_and_revision_checked_before_registration(self):
        for approval, actor in ((self.approval(), 'model-claimed-operator'),
                                (self.approval(actor='stranger'), 'stranger'),
                                (self.approval(expiry='1970-01-01T00:10:00Z'), 'operator'),
                                (replace(self.approval(), scope=replace(self.scope(), task_revision='old')), 'operator')):
            with self.subTest(approval=approval), self.assertRaises(ContractError):
                self.policy.register_approval(approval, authenticated_actor=actor)

    def test_exact_target_content_and_tool_bound_and_use_survives_restart(self):
        approval = self.approval()
        self.policy.register_approval(approval, authenticated_actor='operator')
        for scope in (self.scope(body='changed content'), replace(self.scope(), target='task:456'),
                      replace(self.scope(), tool='Read')):
            with self.subTest(scope=scope), self.assertRaises(ContractError):
                self.policy.authorize_effect(self.task, self.agent, self.run, scope, approval_id=approval.approval_id)
        self.policy.authorize_effect(self.task, self.agent, self.run, self.scope(), approval_id=approval.approval_id)
        path = self.store.path
        self.store.close()
        self.store = AttemptStore(path, clock=lambda: 1000)
        self.addCleanup(self.store.close)
        self.policy = self.make_policy()
        with self.assertRaises(ContractError):
            self.policy.authorize_effect(self.task, self.agent, self.run, self.scope(), approval_id=approval.approval_id)

    def test_scope_json_rejects_nonfinite_or_nonstring_keys(self):
        for arguments in ({1: 'value'}, {'amount': float('nan')}, {'nested': object()}):
            with self.assertRaises(ContractError):
                action_scope(self.task, tool='Read', action='read', target='file', arguments=arguments)

    def test_allowed_read_still_checks_live_ownership(self):
        scope = action_scope(self.task, tool='Read', action='read', target='file', arguments={})
        self.policy.authorize_effect(self.task, self.agent, self.run, scope)
        with self.assertRaises(ContractError):
            self.policy.authorize_effect(self.task, self.agent,
                                          replace(self.run, claim=replace(self.run.claim, generation=999)), scope)

    def test_cancellation_revokes_dispatch_even_with_unconsumed_approval(self):
        approval = self.approval()
        self.policy.register_approval(approval, authenticated_actor='operator')
        self.store.advance(self.run.attempt_id, 'scheduler', self.run.claim.generation, AttemptState.CANCELLING)
        with self.assertRaises(ContractError):
            self.policy.authorize_effect(self.task, self.agent, self.run, self.scope(),
                                          approval_id=approval.approval_id)


if __name__ == '__main__':
    unittest.main()
