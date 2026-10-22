"""Controller-side authority, independent of model prompts and native permission syntax.

Registry entries are trusted, reviewed configuration describing enforced native profiles.
This module does not itself install a sandbox or authorize an unmediated MCP connection.
Every tool gateway must call authorize_effect before dispatch, and must reconcile uncertain
external outcomes rather than retrying them. Approval records come from an authenticated
operator channel; model text cannot establish an actor's identity.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import time

from .adapter import require_agent
from .contracts import AttemptState, Boundary, Contract, ContractError


@dataclass(frozen=True, kw_only=True)
class ToolProfile(Contract):
    ref: str
    policy_revision: str
    agent_ids: tuple[str, ...]
    tools: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    approval_actions: tuple[str, ...]
    max_wall_seconds: int
    cost_policy_refs: tuple[str, ...] = ()
    max_cost: float | None = None
    local_route_verified: bool = False

    def validate(self):
        if self.max_wall_seconds <= 0 or (self.max_cost is not None and self.max_cost < 0):
            raise ContractError('invalid profile budget')
        if set(self.allowed_actions) & set(self.approval_actions):
            raise ContractError('profile action scopes overlap')
        for values in (self.agent_ids, self.tools, self.allowed_actions, self.approval_actions,
                       self.cost_policy_refs):
            if len(values) != len(set(values)) or '*' in values:
                raise ContractError('profile scopes must be unique explicit identifiers')


@dataclass(frozen=True, kw_only=True)
class ActionScope(Contract):
    task_id: str
    task_revision: str
    policy_revision: str
    tool: str
    action: str
    target: str
    content_digest: str

    def validate(self):
        from .contracts import ArtifactRef
        ArtifactRef(ref='action-content', digest=self.content_digest)

    @property
    def digest(self):
        return hashlib.sha256(self.to_json().encode()).hexdigest()


def action_scope(task, *, tool, action, target, arguments):
    # Strict JSON validation rejects objects, non-string keys and non-finite floats.
    from .contracts import _json_copy
    payload = json.dumps(_json_copy(arguments), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return ActionScope(task_id=task.task_id, task_revision=task.task_revision,
                       policy_revision=task.execution_policy.policy_revision, tool=tool, action=action,
                       target=target, content_digest=hashlib.sha256(payload.encode()).hexdigest())


@dataclass(frozen=True, kw_only=True)
class OperatorApproval(Contract):
    approval_id: str
    actor_id: str
    scope: ActionScope
    expires_at: str
    max_uses: int = 1

    def validate(self):
        from .contracts import _utc
        _utc(self.expires_at)
        if self.max_uses <= 0:
            raise ContractError('invalid approval use limit')


class EffectivePolicy:
    """Fresh authority lookup is required on each lifecycle step and tool dispatch."""
    def __init__(self, *, current_task, profiles, operators, store, clock=time.time):
        if not callable(current_task):
            raise ContractError('current task authority is required')
        self.current_task, self.operators, self.store, self.clock = current_task, frozenset(operators), store, clock
        self.profiles = {p.ref: p for p in profiles}
        if len(self.profiles) != len(profiles):
            raise ContractError('ambiguous tool profile registry')

    def __call__(self, task, agent, phase):
        if phase not in ('prepare', 'recover', 'poll', 'finalize', 'integrate', 'verify', 'effect'):
            raise ContractError('unknown policy lifecycle phase')
        current = self.current_task(task.task_id)
        if current is None or current.to_json() != task.to_json():
            raise ContractError('task requirements or policy changed; reconciliation required')
        require_agent(task, agent)
        policy = task.execution_policy
        profile = self.profiles.get(policy.tool_profile_ref)
        if profile is None or profile.policy_revision != policy.policy_revision or agent.agent_id not in profile.agent_ids:
            raise ContractError('no approved native tool profile for this agent/policy')
        if not set(profile.tools) <= set(agent.supported_tools):
            raise ContractError('native tool support has not been established')
        if not set(policy.allowed_actions) <= set(profile.allowed_actions):
            raise ContractError('task requests actions beyond enforced profile')
        if not set(policy.approval_actions) <= set(profile.approval_actions):
            raise ContractError('task requests unsupported approval effects')
        if task.budget.wall_seconds > profile.max_wall_seconds:
            raise ContractError('wall budget exceeds approved profile')
        if task.budget.cost_limit is None:
            if task.budget.cost_policy_ref not in profile.cost_policy_refs:
                raise ContractError('unknown cost policy; cannot assume free execution')
        elif profile.max_cost is None or task.budget.cost_limit > profile.max_cost:
            raise ContractError('cost budget cannot be enforced by this profile')
        if agent.boundary == Boundary.LOCAL and not profile.local_route_verified:
            raise ContractError('local route isolation/fallback has not been verified')
        return profile

    def register_approval(self, approval, *, authenticated_actor):
        if authenticated_actor != approval.actor_id or authenticated_actor not in self.operators:
            raise ContractError('approval actor is not an authenticated operator')
        scope = approval.scope
        task = self.current_task(scope.task_id)
        if task is None or (task.task_revision, task.execution_policy.policy_revision) != (
                scope.task_revision, scope.policy_revision):
            raise ContractError('approval references obsolete task/policy')
        if scope.action not in task.execution_policy.approval_actions:
            raise ContractError('approval cannot widen task action scope')
        until = datetime.fromisoformat(approval.expires_at.replace('Z', '+00:00')).timestamp()
        if until <= self.clock():
            raise ContractError('approval expired')
        self.store.register_approval(approval.approval_id, task_id=scope.task_id,
                                     scope_digest=scope.digest, max_uses=approval.max_uses, expires_at=until)

    def reconcile_effect(self, effect_id, *, outcome, authenticated_actor):
        if authenticated_actor not in self.operators:
            raise ContractError('only an authenticated operator can reconcile an external effect')
        self.store.reconcile_external_effect(effect_id, actor=authenticated_actor, outcome=outcome)

    def authorize_effect(self, task, agent, run, scope, *, approval_id=None):
        profile = self(task, agent, 'effect')
        if (run.task_id, run.task_revision, run.agent_id, run.policy_revision) != (
                task.task_id, task.task_revision, agent.agent_id, task.execution_policy.policy_revision):
            raise ContractError('attempt does not match current authority')
        if (scope.task_id, scope.task_revision, scope.policy_revision) != (
                task.task_id, task.task_revision, task.execution_policy.policy_revision):
            raise ContractError('action scope does not match current authority')
        if scope.tool not in profile.tools:
            raise ContractError('tool/MCP endpoint not authorized')
        current = self.store.heartbeat(run.attempt_id, run.claim.owner_id, run.claim.generation)
        if current.state != AttemptState.RUNNING:
            raise ContractError('attempt is not running; tool dispatch refused')
        if scope.action in task.execution_policy.allowed_actions:
            return
        if scope.action not in task.execution_policy.approval_actions or approval_id is None:
            raise ContractError('action requires explicit scoped operator approval')
        # Consume before dispatch. A crash or uncertain response consumes the use too;
        # retrying an external effect requires explicit reconciliation/new authority.
        self.store.consume_approval(approval_id, run.attempt_id, run.claim.owner_id,
                                    run.claim.generation, scope_digest=scope.digest)
