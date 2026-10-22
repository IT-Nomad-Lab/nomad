"""Synthetic protocol fixtures. No live agent, model, MCP or service is used."""
from dataclasses import replace

from agent_execution.adapter import (
    Cancellation, PreparedExecution, ProcessIdentity, Recovery,
    require_agent, require_prepared,
)
from agent_execution.contracts import (
    AcceptanceCriterion, AgentSpec, ArtifactRef, Attempt, AttemptState,
    Boundary, Budget, CheckResult, Claim, ContractError, Decision, ExecutionEvent,
    ExecutionPolicy, ExecutionResult, Handoff, Privacy, Repository, ResultStatus,
    TaskSpec, Usage,
)

SHA = "a" * 40
ARTIFACT = ArtifactRef(ref="artifact:fixture", digest="b" * 64)
CLAIM = Claim(owner_id="scheduler-1", generation=7, lease_expires_at="2026-10-20T12:00:00Z")


def task():
    return TaskSpec(
        task_id="openproject:fixture", task_revision="r1", project_id="fixture",
        repository=Repository(identity="fixture", path="/tmp/fixture", base_commit=SHA),
        objective="Preserve the requested outcome", requirements=("Do not lose acceptance gates",),
        acceptance_criteria=(AcceptanceCriterion(id="G1", check="offline fixture", expected="pass"),),
        execution_policy=ExecutionPolicy(policy_revision="p1", privacy=Privacy.LOCAL_ONLY,
                                         tool_profile_ref="tools:offline", allowed_actions=("read",)),
        budget=Budget(wall_seconds=60, cost_policy_ref="budget:no-paid-calls"),
        required_capabilities=("repository_edit",),
    )


def agent(name="local"):
    return AgentSpec(
        agent_id=name, agent_type="coding_agent", provider="fixture", model_role="code",
        boundary=Boundary.LOCAL if name == "local" else Boundary.CLOUD,
        execution_mode="headless", availability="available", capabilities=("repository_edit",),
        validated_capabilities=("repository_edit",), validation_ref=ARTIFACT,
        privacy_compatibility=(Privacy.LOCAL_ONLY, Privacy.CLOUD_ALLOWED) if name == "local"
        else (Privacy.CLOUD_ALLOWED,),
    )


def attempt(name="local", state=AttemptState.RUNNING):
    return Attempt(execution_id="exec-1", attempt_id="attempt-1", attempt_number=1,
                   task_id="openproject:fixture", task_revision="r1", agent_id=name,
                   state=state, workspace="/tmp/fixture", base_commit=SHA,
                   context_ref=ARTIFACT, policy_revision="p1", claim=CLAIM)


def handoff():
    return Handoff(
        task_id="openproject:fixture", task_revision="r1", execution_id="exec-1",
        attempt_id="attempt-1", repository_identity="fixture", task_ref=ARTIFACT,
        current_state=AttemptState.HANDOFF_READY, base_commit=SHA, instruction_refs=(ARTIFACT,),
        completed=("Inspected requirements",), pending=("Implement change",),
        relevant_files=("module.py",), decisions=(Decision(decision="Preserve API", reason="Required"),),
        known_failures=("Implementation not started",), test_evidence=(), next_actions=("Implement change",),
        workspace_status="clean",
    )


class FakeAdapter:
    """Implements the protocol, not a claim of real vendor compatibility."""

    def __init__(self, name="local", outcome=ResultStatus.CANDIDATE_COMPLETE):
        self.name, self.outcome = name, outcome
        self.started = False
        self.terminated = False

    def describe(self):
        return agent(self.name)

    def prepare(self, spec, context):
        require_agent(spec, self.describe())
        return PreparedExecution(task_id=spec.task_id, task_revision=spec.task_revision,
                                 agent_id=self.name, policy_revision=spec.execution_policy.policy_revision,
                                 context_ref=context)

    def start(self, run, plan):
        require_prepared(run, plan)
        self.started = True
        return ProcessIdentity(pid=12345, start_time="fixture-start", wrapper_record=ARTIFACT)

    def events(self, run, cursor=0):
        for sequence, kind in enumerate(("execution.started", "execution.exited"), 1):
            if sequence > cursor:
                yield ExecutionEvent(execution_id=run.execution_id, attempt_id=run.attempt_id,
                                     generation=run.claim.generation, sequence=sequence, type=kind,
                                     timestamp="2026-10-20T12:00:00Z", payload={"fixture": True})

    def cancel(self, run, reason):
        return Cancellation(requested=True, termination_confirmed=self.terminated, reason=reason)

    def collect(self, run):
        if not self.terminated:
            raise ContractError("cannot collect while fixture still owns workspace")
        errors = ("fixture failure",) if self.outcome in (ResultStatus.FAILED, ResultStatus.UNSUPPORTED) else ()
        return ExecutionResult(execution_id=run.execution_id, attempt_id=run.attempt_id,
                               task_id=run.task_id, agent_id=self.name, generation=run.claim.generation,
                               status=self.outcome, workspace=run.workspace, base_commit=run.base_commit,
                               usage=Usage(), errors=errors, handoff_ref=ARTIFACT)

    def checkpoint(self, run):
        return replace(handoff(), execution_id=run.execution_id, attempt_id=run.attempt_id)

    def recover(self, run):
        if self.terminated:
            return Recovery(status="exited")
        if self.started:
            return Recovery(status="adopted", process=ProcessIdentity(
                pid=12345, start_time="fixture-start", wrapper_record=ARTIFACT))
        return Recovery(status="missing")
