"""Transactional single-host attempt authority, distinct from OpenProject task authority.

SQLite transactions serialize independent scheduler connections. A lease never releases a
workspace. A trusted controller must reconcile process termination before transfer. No agent
or external effect is executed here; outbox delivery belongs to a separately authorized caller.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .adapter import require_result
from .contracts import ArtifactRef, Attempt, AttemptState as S, Claim, ContractError, ExecutionResult, ResultStatus, TaskSpec
from .lifecycle import transition
from .rollback import store_blockers


SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
 execution_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_revision TEXT NOT NULL,
 task_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(task_id, task_revision));
CREATE TABLE IF NOT EXISTS heads (task_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
 attempt_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(execution_id),
 task_id TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL,
 owner_id TEXT NOT NULL, lease_until REAL NOT NULL, data TEXT NOT NULL,
 terminated INTEGER NOT NULL DEFAULT 0, result TEXT);
CREATE TABLE IF NOT EXISTS reservations (
 workspace TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
 attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id));
CREATE TABLE IF NOT EXISTS approvals (
 approval_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, scope_digest TEXT NOT NULL,
 max_uses INTEGER NOT NULL, uses INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS approval_uses (
 approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
 attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), PRIMARY KEY(approval_id, attempt_id));
CREATE TABLE IF NOT EXISTS outbox (
 effect_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL,
 delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS verification (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verification_claims (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), token TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS external_effects (
 effect_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
 tool TEXT NOT NULL, scope_digest TEXT NOT NULL, approval_id TEXT, state TEXT NOT NULL,
 started_at REAL NOT NULL, outcome TEXT, reconciled_by TEXT);
CREATE TABLE IF NOT EXISTS schema_rollbacks (
 rolled_back_at REAL NOT NULL, from_version INTEGER NOT NULL, to_version INTEGER NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_control (
 id INTEGER PRIMARY KEY CHECK(id=1), disabled INTEGER NOT NULL, actor TEXT NOT NULL,
 reason TEXT NOT NULL, changed_at REAL NOT NULL);
"""
# Added in place like dispatch_control: a writer that ignores them only ever retries, and
# TrackerDelivery never overwrites a conflicting remote record.
SCHEMA_VERSION = 3
OLDEST_ROLLBACK_VERSION = 1
OUTBOX_COLUMNS = (("conflict", "conflict INTEGER NOT NULL DEFAULT 0"),
                  ("next_attempt_at", "next_attempt_at REAL NOT NULL DEFAULT 0"))


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class AttemptStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ContractError("attempt database must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.lock = threading.RLock()
        existed = self.path.exists()
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        if not existed:
            os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(SCHEMA_VERSION + 1):
                raise ContractError("unsupported attempt database schema")
            if version == 0 and self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise ContractError("refusing an unversioned existing database")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    self.db.execute(statement)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbox)")}
            for column, ddl in OUTBOX_COLUMNS:
                if column not in columns:
                    self.db.execute(f"ALTER TABLE outbox ADD COLUMN {ddl}")
            # 3 adds external_effects; older writers refuse it, so none can complete past an uncertain effect.
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _get(self, attempt_id):
        row = self.db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise ContractError("unknown attempt")
        return row, Attempt.from_json(row["data"])

    def get(self, attempt_id):
        with self.lock:
            return self._get(attempt_id)[1]

    def result(self, attempt_id):
        with self.lock:
            row, _ = self._get(attempt_id)
            return ExecutionResult.from_json(row['result']) if row['result'] else None

    def adopt_process(self, attempt_id, owner_id, generation, *, pid, start_time, lease_seconds=60):
        """Recovery under the controller's exclusive instance lock, after native identity proof."""
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ContractError('invalid recovery lease')
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation, allow_expired=True)
            if attempt.process_pid is not None and (attempt.process_pid != pid or attempt.process_start != start_time):
                raise ContractError('recovered process identity disagrees with persistent record')
            until = self.clock() + lease_seconds
            restored = replace(attempt, process_pid=pid, process_start=start_time,
                               claim=replace(attempt.claim, lease_expires_at=_iso(until)))
            if restored.state == S.CLAIMED:
                restored = transition(restored, S.RUNNING, owner_id=owner_id, generation=generation)
            self._save(restored)
            self.db.execute('UPDATE attempts SET lease_until=? WHERE attempt_id=?', (until, attempt_id))
            return restored

    def _current(self, attempt_id, owner_id, generation, *, allow_expired=False):
        row, attempt = self._get(attempt_id)
        head = self.db.execute("SELECT generation FROM heads WHERE task_id=?", (attempt.task_id,)).fetchone()
        if type(generation) is not int or row["owner_id"] != owner_id or row["generation"] != generation:
            raise ContractError("stale or foreign ownership")
        if head is None or head[0] != generation:
            raise ContractError("stale task generation")
        reserved = self.db.execute("SELECT attempt_id FROM reservations WHERE task_id=?",
                                   (attempt.task_id,)).fetchone()
        if reserved is None or reserved[0] != attempt_id:
            raise ContractError("attempt no longer owns a workspace")
        if not allow_expired and row["lease_until"] <= self.clock():
            raise ContractError("lease expired; process reconciliation required")
        return row, attempt

    def _save(self, attempt, *, terminated=None, result=None):
        self.db.execute("UPDATE attempts SET state=?, data=? WHERE attempt_id=?",
                        (attempt.state.value, attempt.to_json(), attempt.attempt_id))
        if terminated is not None:
            self.db.execute("UPDATE attempts SET terminated=? WHERE attempt_id=?",
                            (int(terminated), attempt.attempt_id))
        if result is not None:
            self.db.execute("UPDATE attempts SET result=? WHERE attempt_id=?", (result.to_json(), attempt.attempt_id))

    def claim(self, task: TaskSpec, context_ref: ArtifactRef, *, agent_id: str, workspace: str,
              owner_id: str, lease_seconds: int = 60, retry_authorized: bool = False) -> Attempt:
        if type(lease_seconds) is not int or lease_seconds <= 0 or type(retry_authorized) is not bool:
            raise ContractError("invalid claim lease/retry policy")
        canonical = str(Path(workspace).resolve())
        if canonical != workspace:
            raise ContractError("workspace must be a canonical absolute path")
        with self._transaction():
            if self._dispatch_disabled():
                raise ContractError("new dispatch is disabled for rollback")
            if self.db.execute("SELECT 1 FROM reservations WHERE task_id=? OR workspace=?",
                               (task.task_id, workspace)).fetchone():
                raise ContractError("task or workspace still reserved; expiry does not release it")
            for reserved in self.db.execute("SELECT workspace FROM reservations"):
                current, other = Path(workspace), Path(reserved[0])
                if current.is_relative_to(other) or other.is_relative_to(current):
                    raise ContractError("workspace overlaps another active reservation")
            execution = self.db.execute("SELECT * FROM executions WHERE task_id=? AND task_revision=?",
                                        (task.task_id, task.task_revision)).fetchone()
            if execution:
                if execution["task_json"] != task.to_json():
                    raise ContractError("task changed without revision change")
                if not retry_authorized or execution["attempts"] >= task.execution_policy.max_attempts:
                    raise ContractError("retry not authorized or attempt limit exhausted")
                if self.db.execute("SELECT 1 FROM attempts WHERE execution_id=? AND state=?",
                                   (execution["execution_id"], S.SUCCEEDED.value)).fetchone():
                    raise ContractError("execution already succeeded")
                execution_id, number = execution["execution_id"], execution["attempts"] + 1
            else:
                execution_id, number = str(uuid.uuid4()), 1
                self.db.execute("INSERT INTO executions(execution_id,task_id,task_revision,task_json) VALUES(?,?,?,?)",
                                (execution_id, task.task_id, task.task_revision, task.to_json()))
            head = self.db.execute("SELECT generation FROM heads WHERE task_id=?", (task.task_id,)).fetchone()
            generation = (head[0] if head else 0) + 1
            self.db.execute("INSERT INTO heads VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET generation=excluded.generation",
                            (task.task_id, generation))
            until = self.clock() + lease_seconds
            attempt = Attempt(execution_id=execution_id, attempt_id=str(uuid.uuid4()), attempt_number=number,
                              task_id=task.task_id, task_revision=task.task_revision, agent_id=agent_id,
                              state=S.CLAIMED, workspace=workspace, base_commit=task.repository.base_commit,
                              context_ref=context_ref, policy_revision=task.execution_policy.policy_revision,
                              claim=Claim(owner_id=owner_id, generation=generation, lease_expires_at=_iso(until)))
            self.db.execute("INSERT INTO attempts(attempt_id,execution_id,task_id,state,generation,owner_id,lease_until,data) VALUES(?,?,?,?,?,?,?,?)",
                            (attempt.attempt_id, execution_id, task.task_id, attempt.state.value, generation,
                             owner_id, until, attempt.to_json()))
            self.db.execute("INSERT INTO reservations VALUES(?,?,?)", (workspace, task.task_id, attempt.attempt_id))
            self.db.execute("UPDATE executions SET attempts=? WHERE execution_id=?", (number, execution_id))
            return attempt

    def heartbeat(self, attempt_id, owner_id, generation, *, lease_seconds=60):
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ContractError("invalid lease duration")
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            until = self.clock() + lease_seconds
            updated = replace(attempt, claim=replace(attempt.claim, lease_expires_at=_iso(until)))
            self._save(updated)
            self.db.execute("UPDATE attempts SET lease_until=? WHERE attempt_id=?", (until, attempt_id))
            return updated

    def record_process(self, attempt_id, owner_id, generation, *, pid, start_time):
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            if attempt.process_pid is not None:
                raise ContractError("process already registered")
            updated = replace(attempt, process_pid=pid, process_start=start_time)
            updated = transition(updated, S.RUNNING, owner_id=owner_id, generation=generation)
            self._save(updated)
            return updated

    def advance(self, attempt_id, owner_id, generation, target, *, termination_confirmed=False,
                verification_passed=False, approval_confirmed=False, checkpoint_accepted=False):
        with self._transaction():
            if target == S.SUCCEEDED:
                raise ContractError("use complete to atomically record verified success and tracker effect")
            row, attempt = self._current(attempt_id, owner_id, generation)
            terminated = bool(row["terminated"]) or termination_confirmed
            updated = transition(attempt, target, owner_id=owner_id, generation=generation,
                                 termination_confirmed=terminated, verification_passed=verification_passed,
                                 approval_confirmed=approval_confirmed, checkpoint_accepted=checkpoint_accepted)
            self._save(updated, terminated=terminated)
            if updated.state in (S.CANCELLED, S.BLOCKED, S.HANDOFF_READY, S.SUCCEEDED) and terminated:
                self.db.execute("DELETE FROM reservations WHERE attempt_id=?", (attempt_id,))
            return updated

    def accept_result(self, attempt_id, owner_id, generation, result: ExecutionResult):
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            require_result(attempt, result)
            if attempt.state not in (S.RUNNING, S.VERIFYING):
                raise ContractError("attempt cannot accept a result in this state")
            self._save(attempt, result=result)

    def reconcile_termination(self, attempt_id, owner_id, generation, *, termination_confirmed):
        """Controller recovery entry point. Caller must verify actual process identity/termination."""
        if termination_confirmed is not True:
            raise ContractError("uncertain process cannot relinquish workspace")
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation, allow_expired=True)
            if self.verification_claim(attempt_id) is not None:
                raise ContractError('reconcile verifier termination before releasing workspace')
            if attempt.state in (S.SUCCEEDED, S.CANCELLED, S.HANDOFF_READY):
                raise ContractError("cannot rewrite terminal history")
            blocked = replace(attempt, state=S.BLOCKED)
            self._save(blocked, terminated=True)
            self.db.execute("DELETE FROM reservations WHERE attempt_id=?", (attempt_id,))
            return blocked

    def block(self, attempt_id, owner_id, generation, *, termination_confirmed, tracker_payload,
              result=None, verification_ref=None):
        if termination_confirmed is not True:
            raise ContractError('uncertain process must remain reserved')
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            if attempt.state not in (S.RUNNING, S.CLAIMED, S.RECOVERING, S.VERIFYING, S.CANCELLING, S.AWAITING_APPROVAL):
                raise ContractError('cannot block terminal history')
            if result is not None:
                require_result(attempt, result)
                if result.status != ResultStatus.FAILED:
                    raise ContractError('blocking result must preserve failure')
            if verification_ref is not None:
                if not isinstance(verification_ref, ArtifactRef):
                    raise ContractError('verification evidence must be an artifact reference')
                self.db.execute('INSERT INTO verification VALUES(?,?)', (attempt_id, verification_ref.to_json()))
            blocked = replace(attempt, state=S.BLOCKED)
            self._save(blocked, terminated=True, result=result)
            self._enqueue_effect(f'blocked:{attempt_id}', attempt.task_id, tracker_payload)
            self.db.execute('DELETE FROM reservations WHERE attempt_id=?', (attempt_id,))
            self.db.execute('DELETE FROM verification_claims WHERE attempt_id=?', (attempt_id,))
            return blocked

    def record_finalized(self, attempt_id, owner_id, generation, committed):
        """Replace a dirty candidate with its finalized commit; nothing else may change."""
        with self._transaction():
            row, attempt = self._current(attempt_id, owner_id, generation)
            if attempt.state != S.VERIFYING:
                raise ContractError('attempt is not awaiting verification')
            if self.verification_claim(attempt_id) is not None:
                raise ContractError('cannot replace a candidate under an unreconciled verifier claim')
            stored = ExecutionResult.from_json(row['result']) if row['result'] else None
            if stored is None or stored.result_commit is not None or stored.diff_ref is None:
                raise ContractError('only a stored dirty candidate can be finalized')
            if not isinstance(committed, ExecutionResult) or committed.result_commit is None \
                    or committed.diff_ref is not None \
                    or replace(committed, result_commit=None, diff_ref=stored.diff_ref) != stored:
                raise ContractError('finalized candidate differs from the stored candidate')
            require_result(attempt, committed)
            self._save(attempt, result=committed)
            return committed

    def record_integrated(self, attempt_id, owner_id, generation, previous_commit, integrated):
        """Move a committed candidate to its default-branch merge; nothing else may change."""
        with self._transaction():
            row, attempt = self._current(attempt_id, owner_id, generation)
            if attempt.state != S.VERIFYING:
                raise ContractError('attempt is not awaiting verification')
            if self.verification_claim(attempt_id) is not None:
                raise ContractError('cannot replace a candidate under an unreconciled verifier claim')
            stored = ExecutionResult.from_json(row['result']) if row['result'] else None
            if stored is None or stored.diff_ref is not None or stored.result_commit is None \
                    or stored.result_commit != previous_commit:
                raise ContractError('only the stored committed candidate can be integrated')
            if not isinstance(integrated, ExecutionResult) or integrated.result_commit in (None, previous_commit) \
                    or replace(integrated, result_commit=previous_commit) != stored:
                raise ContractError('integrated candidate differs from the stored candidate')
            require_result(attempt, integrated)
            self._save(attempt, result=integrated)
            return integrated

    def begin_verification(self, attempt_id, owner_id, generation):
        """Fence check runners across controller crashes; uncertainty never frees this claim."""
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            if attempt.state != S.VERIFYING:
                raise ContractError('attempt is not awaiting verification')
            if self.db.execute('SELECT 1 FROM verification_claims WHERE attempt_id=?', (attempt_id,)).fetchone():
                raise ContractError('verification already has an unreconciled claim')
            token = str(uuid.uuid4())
            self.db.execute('INSERT INTO verification_claims VALUES(?,?)', (attempt_id, token))
            return token

    def verification_claim(self, attempt_id):
        row = self.db.execute('SELECT token FROM verification_claims WHERE attempt_id=?', (attempt_id,)).fetchone()
        return row['token'] if row else None

    def reconcile_verification(self, attempt_id, owner_id, generation, *, token, termination_confirmed):
        """Clear only the proven stopped verifier; keep the candidate and workspace reserved."""
        if termination_confirmed is not True:
            raise ContractError('verifier termination remains uncertain')
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            if attempt.state != S.VERIFYING or self.verification_claim(attempt_id) != token or not token:
                raise ContractError('verification reconciliation scope mismatch')
            self.db.execute('DELETE FROM verification_claims WHERE attempt_id=? AND token=?', (attempt_id, token))
            return attempt

    def request_cancellation(self, attempt_id, owner_id, generation):
        """Expiry forbids execution, but cannot prevent cancelling an owned process."""
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation, allow_expired=True)
            if self.verification_claim(attempt_id) is not None:
                raise ContractError('reconcile verifier termination before cancellation')
            if attempt.state == S.CANCELLING:
                return attempt
            cancelling = transition(attempt, S.CANCELLING, owner_id=owner_id, generation=generation)
            self._save(cancelling)
            return cancelling

    def finish_cancellation(self, attempt_id, owner_id, generation, *, termination_confirmed, tracker_payload):
        """Commit confirmed cancellation and its tracker effect in the same transaction."""
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            if self.verification_claim(attempt_id) is not None:
                raise ContractError('reconcile verifier termination before cancellation')
            cancelled = transition(attempt, S.CANCELLED, owner_id=owner_id, generation=generation,
                                   termination_confirmed=termination_confirmed)
            self._save(cancelled, terminated=True)
            self._enqueue_effect(f'cancelled:{attempt_id}', attempt.task_id, tracker_payload)
            self.db.execute('DELETE FROM reservations WHERE attempt_id=?', (attempt_id,))
            return cancelled

    def register_approval(self, approval_id, *, task_id, scope_digest, max_uses, expires_at):
        if type(max_uses) is not int or max_uses <= 0 or expires_at <= self.clock():
            raise ContractError("invalid approval limits")
        # Approval actor/scope authority is validated by policy before registration.
        ArtifactRef(ref="scope", digest=scope_digest)
        with self._transaction():
            existing = self.db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            data = (approval_id, task_id, scope_digest, max_uses, expires_at)
            if existing:
                actual = tuple(existing[k] for k in ("approval_id", "task_id", "scope_digest", "max_uses", "expires_at"))
                if actual != data:
                    raise ContractError("approval identity already bound to another scope")
                return
            self.db.execute("INSERT INTO approvals(approval_id,task_id,scope_digest,max_uses,expires_at) VALUES(?,?,?,?,?)", data)

    def consume_approval(self, approval_id, attempt_id, owner_id, generation, *, scope_digest):
        with self._transaction():
            _, attempt = self._current(attempt_id, owner_id, generation)
            row = self.db.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None or row["task_id"] != attempt.task_id or row["scope_digest"] != scope_digest:
                raise ContractError("approval scope mismatch")
            if row["expires_at"] <= self.clock() or row["uses"] >= row["max_uses"]:
                raise ContractError("approval expired or consumed")
            if self.db.execute("SELECT 1 FROM approval_uses WHERE approval_id=? AND attempt_id=?",
                               (approval_id, attempt_id)).fetchone():
                raise ContractError("approval already consumed for this attempt")
            self.db.execute("INSERT INTO approval_uses VALUES(?,?)", (approval_id, attempt_id))
            self.db.execute("UPDATE approvals SET uses=uses+1 WHERE approval_id=?", (approval_id,))

    def _unreconciled_effects(self, attempt_id):
        # 'dispatching' left behind by a crash is as uncertain as a failed handler.
        return [dict(row) for row in self.db.execute(
            "SELECT effect_id, attempt_id, tool, state FROM external_effects WHERE attempt_id=? AND state!='done' "
            "AND outcome IS NULL ORDER BY started_at, effect_id", (attempt_id,))]

    def unreconciled_effects(self, attempt_id):
        with self.lock:
            return self._unreconciled_effects(attempt_id)

    def begin_external_effect(self, attempt_id, owner_id, generation, *, tool, scope_digest, approval_id=None):
        """Durable intent before a handler runs, so a crash mid-dispatch stays visible as uncertain."""
        with self._transaction():
            self._current(attempt_id, owner_id, generation)
            if any(effect['state'] == 'uncertain' for effect in self._unreconciled_effects(attempt_id)):
                raise ContractError("unreconciled uncertain external effect blocks further effects")
            effect_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO external_effects(effect_id,attempt_id,tool,scope_digest,approval_id,state,"
                            "started_at) VALUES(?,?,?,?,?,'dispatching',?)",
                            (effect_id, attempt_id, tool, scope_digest, approval_id, self.clock()))
            return effect_id

    def finish_external_effect(self, effect_id, *, certain):
        # Not fenced by generation: the effect already happened and must be recorded regardless.
        with self._transaction():
            updated = self.db.execute("UPDATE external_effects SET state=? WHERE effect_id=? AND state='dispatching'",
                                      ('done' if certain is True else 'uncertain', effect_id)).rowcount
            if updated != 1:
                raise ContractError("unknown or already finished external effect")

    def reconcile_external_effect(self, effect_id, *, actor, outcome):
        """Operator states what happened remotely; nothing is retried or compensated here."""
        if outcome not in ('applied', 'not_applied') or type(actor) is not str or not actor:
            raise ContractError("invalid external effect reconciliation")
        with self._transaction():
            updated = self.db.execute("UPDATE external_effects SET outcome=?, reconciled_by=? WHERE effect_id=? "
                                      "AND state!='done' AND outcome IS NULL", (outcome, actor, effect_id)).rowcount
            if updated != 1:
                raise ContractError("external effect is not awaiting reconciliation")

    def enqueue_effect(self, effect_id, task_id, payload):
        with self._transaction():
            self._enqueue_effect(effect_id, task_id, payload)

    def _enqueue_effect(self, effect_id, task_id, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        existing = self.db.execute("SELECT task_id,payload FROM outbox WHERE effect_id=?", (effect_id,)).fetchone()
        if existing:
            if tuple(existing) != (task_id, encoded):
                raise ContractError("outbox effect identity reused with changed content")
            return
        self.db.execute("INSERT INTO outbox(effect_id,task_id,payload) VALUES(?,?,?)", (effect_id, task_id, encoded))

    def complete(self, attempt_id, owner_id, generation, result, *, verification_ref, tracker_payload):
        """Trusted verifier commits candidate evidence, success and outbox in one transaction."""
        if not isinstance(verification_ref, ArtifactRef):
            raise ContractError("independent verification evidence required")
        with self._transaction():
            row, attempt = self._current(attempt_id, owner_id, generation)
            require_result(attempt, result)
            if result.status != ResultStatus.CANDIDATE_COMPLETE or result.result_commit is None:
                raise ContractError("verified completion requires an exact candidate commit")
            execution = self.db.execute("SELECT task_json FROM executions WHERE execution_id=?",
                                        (attempt.execution_id,)).fetchone()
            task = TaskSpec.from_json(execution[0])
            if self._unreconciled_effects(attempt_id):
                raise ContractError("unreconciled external effect blocks completion")
            passed = {check.gate_id for check in result.test_results if check.status == "passed"}
            if not {check.id for check in task.acceptance_criteria} <= passed:
                raise ContractError("required verification gate missing or failed")
            if any(check.status != "passed" for check in result.test_results):
                raise ContractError("candidate has failed or unrun verification")
            done = transition(attempt, S.SUCCEEDED, owner_id=owner_id, generation=generation,
                              termination_confirmed=bool(row["terminated"]), verification_passed=True)
            self._save(done, result=result)
            self.db.execute("INSERT INTO verification VALUES(?,?)", (attempt_id, verification_ref.to_json()))
            self.db.execute('DELETE FROM verification_claims WHERE attempt_id=?', (attempt_id,))
            self._enqueue_effect(f"complete:{attempt_id}", attempt.task_id, tracker_payload)
            self.db.execute("DELETE FROM reservations WHERE attempt_id=?", (attempt_id,))
            return done

    def pending_effects(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM outbox WHERE delivered=0 ORDER BY rowid")]

    def conflicted_effects(self):
        """Effects whose ID exists remotely with another payload; they wait for an operator."""
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM outbox WHERE delivered=0 AND conflict=1 ORDER BY rowid")]

    def note_delivery(self, effect_id, *, error=None, conflict=False, retry_at=0):
        if conflict and error is None:
            raise ContractError("a delivery conflict needs an error")
        if type(retry_at) not in (int, float) or retry_at < 0 or (retry_at and error is None):
            raise ContractError("retry backoff applies only to a failed delivery")
        with self._transaction():
            row = self.db.execute("SELECT delivered FROM outbox WHERE effect_id=?", (effect_id,)).fetchone()
            if row is None:
                raise ContractError("unknown outbox effect")
            if not row[0]:
                self.db.execute("UPDATE outbox SET delivered=?,attempts=attempts+1,last_error=?,conflict=?,"
                                "next_attempt_at=? WHERE effect_id=?",
                                (int(error is None), error, int(conflict), retry_at, effect_id))

    # Staged rollback (#1463 RB01-RB03): a durable dispatch gate checked inside claim's
    # transaction, and a read-only inventory of everything that still holds a workspace.
    def _dispatch_disabled(self):
        row = self.db.execute('SELECT disabled FROM dispatch_control WHERE id=1').fetchone()
        return bool(row and row['disabled'])

    def dispatch_disabled(self):
        with self.lock:
            return self._dispatch_disabled()

    def set_dispatch(self, *, enabled, actor, reason):
        if type(enabled) is not bool or not isinstance(actor, str) or not actor or not isinstance(reason, str) or not reason:
            raise ContractError('dispatch change needs an actor and a reason')
        with self._transaction():
            self.db.execute('INSERT INTO dispatch_control VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                            'disabled=excluded.disabled, actor=excluded.actor, reason=excluded.reason, '
                            'changed_at=excluded.changed_at', (int(not enabled), actor, reason, self.clock()))

    def rollback_inventory(self):
        """Writers not proven stopped, held reservations, live verifier claims, undelivered and uncertain effects."""
        with self.lock:
            unterminated = [dict(attempt_id=row['attempt_id'], task_id=row['task_id'], state=row['state'])
                            for row in self.db.execute('SELECT attempt_id, task_id, state FROM attempts '
                                                       'WHERE terminated=0 ORDER BY attempt_id')]
            reservations = [dict(row) for row in self.db.execute('SELECT workspace, task_id, attempt_id FROM '
                                                                 'reservations ORDER BY workspace')]
            verifiers = [row['attempt_id'] for row in self.db.execute('SELECT attempt_id FROM verification_claims '
                                                                      'ORDER BY attempt_id')]
            effects = [row['effect_id'] for row in self.db.execute('SELECT effect_id FROM outbox WHERE delivered=0 '
                                                                   'ORDER BY effect_id')]
            uncertain = [dict(row) for row in self.db.execute(
                "SELECT effect_id, attempt_id, tool, state FROM external_effects WHERE state!='done' "
                "AND outcome IS NULL ORDER BY started_at, effect_id")]
            return {'dispatch_disabled': self._dispatch_disabled(), 'unterminated': unterminated,
                    'reservations': reservations, 'verifiers': verifiers, 'pending_effects': effects,
                    'uncertain_effects': uncertain}

    def rollback_schema(self, target_version, *, actor, reason):
        """Lower the schema version for an older writer only while no hybrid writer can act.

        The blockers are re-read inside the write transaction, so no claim, verifier or effect
        can start between the check and the change. Nothing is dropped; reopening with this
        code migrates forward again."""
        if type(target_version) is not int or not OLDEST_ROLLBACK_VERSION <= target_version < SCHEMA_VERSION:
            raise ContractError("unsupported schema rollback target")
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
            raise ContractError("schema rollback requires an actor and a reason")
        with self._transaction():
            current = self.db.execute("PRAGMA user_version").fetchone()[0]
            if current != SCHEMA_VERSION:
                raise ContractError("schema was already rolled back; reopen to migrate forward first")
            found = store_blockers(self.rollback_inventory())
            if found:
                raise ContractError("schema rollback refused: " + "; ".join(found))
            self.db.execute("INSERT INTO schema_rollbacks VALUES(?,?,?,?,?)",
                            (self.clock(), current, target_version, actor, reason))
            self.db.execute(f"PRAGMA user_version={target_version}")
        return target_version
