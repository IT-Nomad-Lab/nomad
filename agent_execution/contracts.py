"""Version-one wire contracts shared by controllers and native agent adapters.

No provider dependencies, filesystem reads, configuration loading or process execution.
Validation is deliberately strict: unknown fields, coercion of booleans/numbers, nonfinite
usage and unsupported versions are refused. Validation is not authorization or a database lock.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import PurePosixPath
import re
import types
from typing import Union, get_args, get_origin, get_type_hints


class ContractError(ValueError):
    """The supplied data cannot safely represent this contract."""


class Privacy(str, Enum):
    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED = "cloud_allowed"


class Boundary(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class AttemptState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    HANDOFF_READY = "handoff_ready"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"


class ResultStatus(str, Enum):
    CANDIDATE_COMPLETE = "candidate_complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNSUPPORTED = "unsupported"


def _json_copy(value):
    """Reject non-JSON Python objects and copy nested payloads without lossy coercion."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_json_copy(v) for v in value]
    if type(value) is dict and all(type(k) is str for k in value):
        return {k: _json_copy(v) for k, v in value.items()}
    raise ContractError("payload must contain finite JSON values and string keys")


def _decode(value, annotation, name):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        for option in args:
            try:
                return _decode(value, option, name)
            except ContractError:
                pass
        raise ContractError(f"{name}: invalid optional/union value")
    if annotation is type(None):
        if value is None:
            return None
    elif origin is tuple:
        if type(value) not in (list, tuple):
            raise ContractError(f"{name}: expected array")
        return tuple(_decode(v, args[0], name) for v in value)
    elif annotation is dict:
        if type(value) is dict:
            return _json_copy(value)
    elif isinstance(annotation, type) and issubclass(annotation, Contract):
        if type(value) is annotation:
            # Revalidate nested objects; an event payload may have been changed by its caller.
            return annotation.from_dict(value.to_dict())
        if type(value) is dict:
            return annotation.from_dict(value)
    elif isinstance(annotation, type) and issubclass(annotation, Enum):
        if type(value) is annotation:
            return value
        if type(value) is str:
            try:
                return annotation(value)
            except ValueError:
                pass
    elif annotation is float:
        if type(value) in (int, float):
            try:
                if math.isfinite(value):
                    return float(value)
            except OverflowError:
                pass
    elif annotation in (str, int, bool):
        if type(value) is annotation:
            if annotation is str and (not value.strip() or "\x00" in value):
                raise ContractError(f"{name}: string must not be blank or contain NUL")
            return value
    raise ContractError(f"{name}: unexpected value/type")


def _encode(value):
    if isinstance(value, Contract):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return _json_copy(value)
    return value


@dataclass(frozen=True, kw_only=True)
class Contract:
    schema_version: int = 1

    def __post_init__(self):
        hints = get_type_hints(type(self))
        for field in fields(self):
            object.__setattr__(self, field.name,
                               _decode(getattr(self, field.name), hints[field.name], field.name))
        if self.schema_version != 1:
            raise ContractError("unsupported schema_version")
        self.validate()

    def validate(self):
        """Subclass semantic validation, after strict field decoding."""

    def to_dict(self):
        return {f.name: _encode(getattr(self, f.name)) for f in fields(self)}

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)

    @classmethod
    def from_dict(cls, data):
        if type(data) is not dict:
            raise ContractError(f"{cls.__name__}: expected object")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ContractError(f"{cls.__name__}: unknown fields: {sorted(unknown)}")
        if "schema_version" not in data:
            raise ContractError("schema_version is required on wire objects")
        try:
            return cls(**data)
        except TypeError as exc:
            raise ContractError(f"{cls.__name__}: missing required fields") from exc

    @classmethod
    def from_json(cls, text):
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ContractError(f"duplicate JSON field: {key}")
                result[key] = value
            return result
        try:
            return cls.from_dict(json.loads(text, object_pairs_hook=unique_pairs))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError("invalid JSON object") from exc


def _positive(value, name):
    if value <= 0:
        raise ContractError(f"{name}: must be positive")


def _nonnegative(value, name):
    if value is not None and value < 0:
        raise ContractError(f"{name}: must not be negative")


def _unique(values, name):
    if len(set(values)) != len(values):
        raise ContractError(f"{name}: duplicate values")


def _commit(value):
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ContractError("commit must be a full lowercase Git object ID")


def _utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("timestamp must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError("timestamp must include UTC offset")


@dataclass(frozen=True, kw_only=True)
class ArtifactRef(Contract):
    ref: str
    digest: str

    def validate(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ContractError("artifact digest must be lowercase SHA-256")


@dataclass(frozen=True, kw_only=True)
class Repository(Contract):
    identity: str
    path: str
    base_commit: str

    def validate(self):
        if not PurePosixPath(self.path).is_absolute() or ".." in PurePosixPath(self.path).parts:
            raise ContractError("repository path must be absolute without traversal")
        _commit(self.base_commit)


@dataclass(frozen=True, kw_only=True)
class AcceptanceCriterion(Contract):
    id: str
    check: str
    expected: str


@dataclass(frozen=True, kw_only=True)
class Budget(Contract):
    wall_seconds: int
    cost_limit: float | None = None
    cost_policy_ref: str | None = None

    def validate(self):
        _positive(self.wall_seconds, "wall_seconds")
        _nonnegative(self.cost_limit, "cost_limit")
        if self.cost_limit is None and self.cost_policy_ref is None:
            raise ContractError("null cost_limit requires explicit cost_policy_ref")


@dataclass(frozen=True, kw_only=True)
class ExecutionPolicy(Contract):
    policy_revision: str
    privacy: Privacy
    tool_profile_ref: str
    allowed_actions: tuple[str, ...] = ()
    approval_actions: tuple[str, ...] = ()
    max_attempts: int = 1

    def validate(self):
        _positive(self.max_attempts, "max_attempts")
        _unique(self.allowed_actions, "allowed_actions")
        _unique(self.approval_actions, "approval_actions")
        if set(self.allowed_actions) & set(self.approval_actions):
            raise ContractError("action cannot be both allowed and approval-required")


@dataclass(frozen=True, kw_only=True)
class TaskSpec(Contract):
    task_id: str
    task_revision: str
    project_id: str
    repository: Repository
    objective: str
    requirements: tuple[str, ...]
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    execution_policy: ExecutionPolicy
    budget: Budget
    dependencies: tuple[str, ...] = ()
    priority: str = "normal"
    required_capabilities: tuple[str, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()

    def validate(self):
        if not self.requirements or not self.acceptance_criteria:
            raise ContractError("requirements and acceptance criteria must not be empty")
        _unique(tuple(c.id for c in self.acceptance_criteria), "acceptance criteria IDs")
        _unique(self.dependencies, "dependencies")
        _unique(self.required_capabilities, "required_capabilities")
        if self.task_id in self.dependencies:
            raise ContractError("task cannot depend on itself")
        if self.priority not in ("low", "normal", "high", "urgent"):
            raise ContractError("unknown priority")


@dataclass(frozen=True, kw_only=True)
class AgentSpec(Contract):
    agent_id: str
    agent_type: str
    provider: str
    model_role: str
    boundary: Boundary
    execution_mode: str
    availability: str
    capabilities: tuple[str, ...] = ()
    validated_capabilities: tuple[str, ...] = ()
    supported_tools: tuple[str, ...] = ()
    privacy_compatibility: tuple[Privacy, ...] = ()
    resolved_model: str | None = None
    context_limit: int | None = None
    validation_ref: ArtifactRef | None = None
    estimated_cost: float | None = None

    def validate(self):
        if self.agent_type != "coding_agent":
            raise ContractError("an inference server is not a coding agent")
        if self.availability not in ("available", "unavailable", "unknown"):
            raise ContractError("unknown availability")
        for name in ("capabilities", "validated_capabilities", "supported_tools", "privacy_compatibility"):
            _unique(getattr(self, name), name)
        if not set(self.validated_capabilities) <= set(self.capabilities):
            raise ContractError("validated capabilities must be declared")
        if self.validated_capabilities and self.validation_ref is None:
            raise ContractError("validated capabilities require evidence")
        if self.boundary == Boundary.CLOUD and Privacy.LOCAL_ONLY in self.privacy_compatibility:
            raise ContractError("cloud agent cannot declare local-only compatibility")
        if self.context_limit is not None:
            _positive(self.context_limit, "context_limit")
        _nonnegative(self.estimated_cost, "estimated_cost")


@dataclass(frozen=True, kw_only=True)
class Claim(Contract):
    owner_id: str
    generation: int
    lease_expires_at: str

    def validate(self):
        _positive(self.generation, "generation")
        _utc(self.lease_expires_at)


@dataclass(frozen=True, kw_only=True)
class Attempt(Contract):
    execution_id: str
    attempt_id: str
    attempt_number: int
    task_id: str
    task_revision: str
    agent_id: str
    state: AttemptState
    workspace: str
    base_commit: str
    context_ref: ArtifactRef
    policy_revision: str
    claim: Claim | None = None
    process_pid: int | None = None
    process_start: str | None = None
    retry_of: str | None = None

    def validate(self):
        _positive(self.attempt_number, "attempt_number")
        _commit(self.base_commit)
        if not PurePosixPath(self.workspace).is_absolute() or ".." in PurePosixPath(self.workspace).parts:
            raise ContractError("workspace must be absolute without traversal")
        if self.state not in (AttemptState.QUEUED, AttemptState.BLOCKED, AttemptState.CANCELLED) and self.claim is None:
            raise ContractError("claimed execution state requires owner identity")
        if (self.process_pid is None) != (self.process_start is None):
            raise ContractError("process identity requires both PID and start time")
        if self.process_pid is not None:
            _positive(self.process_pid, "process_pid")
        if self.retry_of == self.attempt_id:
            raise ContractError("attempt cannot retry itself")


@dataclass(frozen=True, kw_only=True)
class Usage(Contract):
    wall_seconds: float | None = None
    tokens: int | None = None
    vram_peak_mib: int | None = None
    cost_amount: float | None = None
    cost_measurement: str = "unknown"
    cost_basis: str | None = None
    currency: str = "USD"

    def validate(self):
        for name in ("wall_seconds", "tokens", "vram_peak_mib", "cost_amount"):
            _nonnegative(getattr(self, name), name)
        if self.cost_measurement not in ("unknown", "measured", "estimated"):
            raise ContractError("unknown cost measurement")
        if self.cost_measurement == "unknown" and self.cost_amount is not None:
            raise ContractError("unknown cost must remain null")
        if self.cost_measurement != "unknown" and (self.cost_amount is None or self.cost_basis is None):
            raise ContractError("known cost requires amount and provenance")


@dataclass(frozen=True, kw_only=True)
class CheckResult(Contract):
    gate_id: str
    status: str
    evidence: ArtifactRef

    def validate(self):
        if self.status not in ("passed", "failed", "not_run"):
            raise ContractError("unknown check status")


@dataclass(frozen=True, kw_only=True)
class ExecutionResult(Contract):
    execution_id: str
    attempt_id: str
    task_id: str
    agent_id: str
    generation: int
    status: ResultStatus
    workspace: str
    base_commit: str
    usage: Usage
    result_commit: str | None = None
    diff_ref: ArtifactRef | None = None
    changes: tuple[str, ...] = ()
    test_results: tuple[CheckResult, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    errors: tuple[str, ...] = ()
    handoff_ref: ArtifactRef | None = None
    cleanup_outcome: dict | None = None
    warnings: tuple[str, ...] = ()

    def validate(self):
        if self.cleanup_outcome is not None:
            outcome = self.cleanup_outcome
            if (set(outcome) != {'auth'}
                    or outcome['auth'] not in ('unchanged', 'changed_not_persisted', 'persisted', 'conflict')):
                raise ContractError('invalid cleanup outcome')
        _positive(self.generation, "generation")
        _commit(self.base_commit)
        if not PurePosixPath(self.workspace).is_absolute() or ".." in PurePosixPath(self.workspace).parts:
            raise ContractError("result workspace must be absolute without traversal")
        if self.result_commit is not None:
            _commit(self.result_commit)
        if self.changes and self.result_commit is None and self.diff_ref is None:
            raise ContractError("changes require commit or diff evidence")
        for path in self.changes:
            if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
                raise ContractError("changed paths must be repository-relative without traversal")
        if self.status in (ResultStatus.FAILED, ResultStatus.UNSUPPORTED) and not self.errors:
            raise ContractError("failed/unsupported results require explanation")


@dataclass(frozen=True, kw_only=True)
class Decision(Contract):
    decision: str
    reason: str


@dataclass(frozen=True, kw_only=True)
class Handoff(Contract):
    task_id: str
    task_revision: str
    execution_id: str
    attempt_id: str
    repository_identity: str
    task_ref: ArtifactRef
    current_state: AttemptState
    base_commit: str
    instruction_refs: tuple[ArtifactRef, ...]
    completed: tuple[str, ...]
    pending: tuple[str, ...]
    relevant_files: tuple[str, ...]
    decisions: tuple[Decision, ...]
    known_failures: tuple[str, ...]
    test_evidence: tuple[CheckResult, ...]
    next_actions: tuple[str, ...]
    workspace_status: str
    result_commit: str | None = None
    diff_ref: ArtifactRef | None = None
    native_session_hint: str | None = None

    def validate(self):
        _commit(self.base_commit)
        if self.result_commit is not None:
            _commit(self.result_commit)
        if not self.instruction_refs:
            raise ContractError("handoff requires instruction provenance")
        if self.workspace_status not in ("clean", "committed", "explicit-diff"):
            raise ContractError("unknown workspace status")
        if self.workspace_status == "explicit-diff" and self.diff_ref is None:
            raise ContractError("dirty handoff requires explicit diff")
        if self.workspace_status == "committed" and self.result_commit is None:
            raise ContractError("committed handoff requires result commit")
        if self.pending and not self.next_actions:
            raise ContractError("pending work requires next actions")


@dataclass(frozen=True, kw_only=True)
class ExecutionEvent(Contract):
    execution_id: str
    attempt_id: str
    generation: int
    sequence: int
    type: str
    timestamp: str
    payload: dict

    def validate(self):
        _positive(self.generation, "generation")
        _positive(self.sequence, "sequence")
        _utc(self.timestamp)
        if self.type not in {
            "execution.started", "progress", "tool.started", "tool.finished",
            "approval.requested", "approval.decided", "checkpoint.created", "execution.exited",
            "verification.finished", "handoff.accepted", "ownership.rejected", "tracker.sync_failed",
        }:
            raise ContractError("unsupported event type")
