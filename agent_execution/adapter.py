"""Adapter interface and preflight helpers, independent of provider implementations.

This protocol does not execute commands. Eligibility here is only the agent-capability
part of policy; callers must also enforce tools, budgets, resources and context access.
"""
from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from .contracts import (
    AgentSpec, ArtifactRef, Attempt, Boundary, Contract, ContractError,
    ExecutionEvent, ExecutionResult, Handoff, Privacy, TaskSpec,
)


@dataclass(frozen=True, kw_only=True)
class PreparedExecution(Contract):
    task_id: str
    task_revision: str
    agent_id: str
    policy_revision: str
    context_ref: ArtifactRef


@dataclass(frozen=True, kw_only=True)
class ProcessIdentity(Contract):
    pid: int
    start_time: str
    wrapper_record: ArtifactRef
    native_session: str | None = None

    def validate(self):
        if self.pid <= 0:
            raise ContractError("process PID must be positive")


@dataclass(frozen=True, kw_only=True)
class Cancellation(Contract):
    requested: bool
    termination_confirmed: bool
    reason: str

    @property
    def workspace_releasable(self):
        return self.termination_confirmed


@dataclass(frozen=True, kw_only=True)
class Recovery(Contract):
    status: str
    process: ProcessIdentity | None = None

    def validate(self):
        if self.status not in ("adopted", "exited", "uncertain", "missing"):
            raise ContractError("unknown recovery status")
        if self.status == "adopted" and self.process is None:
            raise ContractError("adoption requires process identity")


@runtime_checkable
class AgentAdapter(Protocol):
    def describe(self) -> AgentSpec: ...
    def prepare(self, task: TaskSpec, context: ArtifactRef) -> PreparedExecution: ...
    def start(self, attempt: Attempt, plan: PreparedExecution) -> ProcessIdentity: ...
    def events(self, attempt: Attempt, cursor: int = 0) -> Iterable[ExecutionEvent]: ...
    def cancel(self, attempt: Attempt, reason: str) -> Cancellation: ...
    def collect(self, attempt: Attempt) -> ExecutionResult: ...
    def checkpoint(self, attempt: Attempt) -> Handoff: ...
    def recover(self, attempt: Attempt) -> Recovery: ...


def require_agent(task: TaskSpec, agent: AgentSpec):
    """Refuse an ineligible backend, including fallback; does not grant tool authority."""
    if agent.availability != "available" or agent.execution_mode != "headless":
        raise ContractError("agent unavailable for headless execution")
    if task.execution_policy.privacy not in agent.privacy_compatibility:
        raise ContractError("agent privacy compatibility not established")
    if task.execution_policy.privacy == Privacy.LOCAL_ONLY and agent.boundary != Boundary.LOCAL:
        raise ContractError("local-only task cannot use cloud agent")
    if not set(task.required_capabilities) <= set(agent.validated_capabilities):
        raise ContractError("required capabilities lack validation")


def require_prepared(attempt: Attempt, plan: PreparedExecution):
    """Prevent a prepared context/policy/agent plan being applied to another attempt scope."""
    for field in ("task_id", "task_revision", "agent_id", "policy_revision", "context_ref"):
        if getattr(attempt, field) != getattr(plan, field):
            raise ContractError(f"prepared plan mismatch: {field}")


def require_result(attempt: Attempt, result: ExecutionResult):
    """Check correlation before accepting a candidate; caller still verifies live ownership."""
    for field in ("execution_id", "attempt_id", "task_id", "agent_id", "workspace", "base_commit"):
        if getattr(attempt, field) != getattr(result, field):
            raise ContractError(f"result mismatch: {field}")
    if attempt.claim is None or result.generation != attempt.claim.generation:
        raise ContractError("result has stale or missing ownership generation")
