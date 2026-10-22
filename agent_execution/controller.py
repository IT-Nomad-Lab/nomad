"""Single-host control-plane integration of context, native adapters and durable attempts.

Policy, resource admission and independent verification are explicit trusted collaborators.
No default permits a task; no tracker callback or service starts on import. One controller
instance lock is held across the entire lifecycle, including recovery after restart.
"""
from contextlib import nullcontext
import fcntl
import json
import os
from pathlib import Path

from .context import build_context
from .adapter import Cancellation
from .contracts import AttemptState as S, Boundary, ContractError, ResultStatus
from .delivery import TrackerDelivery, exponential_backoff
from .integration import IntegrationConflict
from .verification import VerificationFailed, VerificationUncertain


class ExecutionController:
    def __init__(self, attempts, artifacts, adapters, *, authorize, owner_id='nomad-hybrid-controller',
                 resource_for=None, tool_authority_for=None, finalizer=None, finalization_scope=None,
                 integration=None):
        if not callable(authorize):
            raise ContractError('controller requires an effective-policy authorizer')
        self.attempts, self.artifacts, self.adapters = attempts, artifacts, dict(adapters)
        self.authorize, self.owner_id, self.resource_for = authorize, owner_id, resource_for
        if tool_authority_for is not None and not callable(tool_authority_for):
            raise ContractError('tool authority factory must be callable')
        self.tool_authority_for, self.authorities = tool_authority_for, {}
        if (finalizer is None) != (finalization_scope is None) or (
                finalization_scope is not None and not callable(finalization_scope)):
            raise ContractError('finalization needs both a finalizer and a trusted scope source')
        self.finalizer, self.finalization_scope = finalizer, finalization_scope
        if integration is not None and (
                not all(callable(getattr(integration, name, None)) for name in ('tip', 'contains', 'merge'))
                or type(getattr(integration, 'max_rounds', None)) is not int or integration.max_rounds < 1):
            raise ContractError('integration policy must read the tip, test containment and merge')
        self.integration = integration
        self.resources, self.active = {}, {}
        lock_path = attempts.path.with_suffix('.controller.lock')
        self.lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise ContractError('another controller instance owns this execution store') from exc

    def close(self):
        for attempt_id in list(self.authorities):
            self._close_authority(attempt_id)
        # Live native supervisors retain inherited resource FDs. Closing the controller cannot
        # silently release an inference reservation while the supervised workload still runs.
        for attempt_id in list(self.resources):
            resource = self.resources[attempt_id]
            if getattr(resource, 'fd', None) is not None and attempt_id in self.active:
                _, backend = self.active[attempt_id]
                run = self.attempts.get(attempt_id)
                if self._adapter(backend).recover(run).status != 'exited':
                    detach = getattr(resource, 'detach', None)
                    if detach is None:
                        raise ContractError('cannot release a live resource without safe descriptor transfer')
                    detach()
                    self.resources.pop(attempt_id)
                    continue
            self._release_resource(attempt_id)
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None

    def _check_open(self):
        if self.lock_fd is None:
            raise ContractError('controller is closed')

    def _release_resource(self, attempt_id):
        resource = self.resources.pop(attempt_id, None)
        if resource is not None:
            resource.__exit__(None, None, None)

    def _open_authority(self, task, backend, run):
        if self.tool_authority_for is not None:
            authority = self.tool_authority_for(task, self._adapter(backend).describe(), run)
            authority.__enter__()
            self.authorities[run.attempt_id] = authority

    def _close_authority(self, attempt_id):
        authority = self.authorities.pop(attempt_id, None)
        if authority is not None:
            authority.__exit__(None, None, None)

    def _adapter(self, name):
        try:
            return self.adapters[name]
        except KeyError as exc:
            raise ContractError('requested adapter is not registered') from exc

    def start(self, task, backend, instruction_refs, *, workspace, handoff=None, retry_authorized=False):
        self._check_open()
        adapter = self._adapter(backend)
        self.authorize(task, adapter.describe(), 'prepare')
        if adapter.describe().boundary == Boundary.LOCAL and self.resource_for is None:
            raise ContractError('local execution requires explicit resource admission')
        resource = self.resource_for(task, adapter.describe()) if self.resource_for else nullcontext()
        resource.__enter__()
        try:
            context = build_context(self.artifacts, task, instruction_refs, handoff=handoff)
            plan = adapter.prepare(task, context)
            run = self.attempts.claim(task, context, agent_id=adapter.describe().agent_id,
                                      workspace=workspace, owner_id=self.owner_id,
                                      retry_authorized=retry_authorized)
        except BaseException:
            resource.__exit__(None, None, None)
            raise
        self.resources[run.attempt_id] = resource
        self.active[run.attempt_id] = (task, backend)
        fd = getattr(resource, 'fd', None)
        if fd is not None:
            adapter.reservation_fds[run.attempt_id] = (fd,)
        try:
            self._open_authority(task, backend, run)
            adapter.start(run, plan)
        except BaseException:
            # Keep process/workspace/resource evidence for explicit recovery. Do not reset the claim.
            raise
        return self.attempts.get(run.attempt_id)

    def recover(self, task, backend, attempt_id):
        self._check_open()
        run = self.attempts.get(attempt_id)
        adapter = self._adapter(backend)
        if run.claim.owner_id != self.owner_id or run.agent_id != adapter.describe().agent_id:
            raise ContractError('controller/adapter ownership mismatch')
        self.authorize(task, adapter.describe(), 'recover')
        adapter.prepare(task, run.context_ref)
        state = adapter.recover(run)
        if state.status not in ('adopted', 'exited') or state.process is None:
            raise ContractError('process ownership uncertain; workspace remains quarantined')
        restored = self.attempts.adopt_process(attempt_id, self.owner_id, run.claim.generation,
                                              pid=state.process.pid, start_time=state.process.start_time)
        self.active[attempt_id] = (task, backend)
        self._open_authority(task, backend, restored)
        return restored

    def poll(self, attempt_id):
        self._check_open()
        task, backend = self.active[attempt_id]
        adapter = self._adapter(backend)
        run = self.attempts.get(attempt_id)
        try:
            self.authorize(task, adapter.describe(), 'poll')
        except Exception:
            if run.state in (S.CLAIMED, S.RUNNING, S.RECOVERING, S.AWAITING_APPROVAL, S.CANCELLING):
                self.cancel(attempt_id, 'current task/policy authority refused execution')
            raise
        if run.state in (S.BLOCKED, S.CANCELLED, S.SUCCEEDED, S.HANDOFF_READY):
            return self.attempts.result(attempt_id)
        run = self.attempts.heartbeat(attempt_id, self.owner_id, run.claim.generation)
        if run.state == S.VERIFYING:
            return self.attempts.result(attempt_id)
        if run.state == S.CANCELLING:
            self.cancel(attempt_id, 'pending cancellation reconciliation')
            return None
        state = adapter.recover(run)
        if state.status == 'adopted':
            return None
        if state.status != 'exited':
            raise ContractError('process exit unconfirmed; workspace remains reserved')
        self._close_authority(attempt_id)
        result = adapter.collect(run)
        self.attempts.accept_result(attempt_id, self.owner_id, run.claim.generation, result)
        if result.status == ResultStatus.CANDIDATE_COMPLETE:
            self.attempts.advance(attempt_id, self.owner_id, run.claim.generation, S.VERIFYING,
                                  termination_confirmed=True)
        else:
            self.attempts.block(attempt_id, self.owner_id, run.claim.generation, termination_confirmed=True,
                                tracker_payload={'status': 'Blocked', 'attempt_id': attempt_id,
                                                 'reason': 'native execution failed'})
        self._release_resource(attempt_id)
        return result

    def verify(self, attempt_id, verifier):
        self._check_open()
        task, backend = self.active[attempt_id]
        run = self.attempts.get(attempt_id)
        if run.state != S.VERIFYING:
            raise ContractError('candidate is not awaiting independent verification')
        self.authorize(task, self._adapter(backend).describe(), 'verify')
        run = self.attempts.heartbeat(attempt_id, self.owner_id, run.claim.generation)
        candidate = self.attempts.result(attempt_id)
        if candidate is None:
            raise ContractError('missing candidate evidence')
        if self.integration is not None and not callable(getattr(verifier, 'reconcile', None)):
            raise ContractError('integration requires a verifier that can prove its checks stopped')
        if candidate.result_commit is None and candidate.diff_ref is not None:
            candidate = self._finalize(task, backend, run, candidate)
        for _ in range(self.integration.max_rounds if self.integration is not None else 1):
            tip = None
            if self.integration is not None:
                tip = self.integration.tip(task)
                if not self.integration.contains(run.workspace, tip, candidate.result_commit):
                    candidate = self._integrate(task, backend, run, candidate, tip)
            duration = getattr(verifier, 'max_seconds', 60)
            if type(duration) is not int or duration <= 0:
                duration = 60
            run = self.attempts.heartbeat(attempt_id, self.owner_id, run.claim.generation,
                                          lease_seconds=max(60, duration + 30))
            token = self.attempts.begin_verification(attempt_id, self.owner_id, run.claim.generation)
            # Verifier executes approved checks and pins the exact commit.
            try:
                claimed_runner = getattr(type(verifier), 'run_claim', None)
                result, evidence = (claimed_runner(verifier, task, run, candidate, token)
                                    if callable(claimed_runner) else verifier(task, run, candidate))
                self.authorize(task, self._adapter(backend).describe(), 'verify')
            except VerificationFailed as exc:
                self.attempts.block(attempt_id, self.owner_id, run.claim.generation, termination_confirmed=True,
                                    result=exc.result, verification_ref=exc.evidence,
                                    tracker_payload={'status': 'Blocked', 'attempt_id': attempt_id,
                                                     'reason': 'independent gate failed',
                                                     'verification': exc.evidence.to_dict()})
                raise
            except VerificationUncertain:
                # Native agent exit does not prove that verifier descendants have stopped.
                # Preserve the workspace and check runtime evidence for reconciliation.
                raise
            except Exception as exc:
                self.attempts.block(attempt_id, self.owner_id, run.claim.generation, termination_confirmed=True,
                                    tracker_payload={'status': 'Blocked', 'attempt_id': attempt_id,
                                                     'reason': 'verification failed: ' + type(exc).__name__})
                raise
            payload = {'status': 'Done', 'attempt_id': attempt_id, 'commit': result.result_commit,
                       'verification': evidence.to_dict()}
            if tip is not None:
                # The default branch may have moved while the gates ran. Evidence for an older tip
                # does not cover the candidate the integration policy now requires.
                current = self.integration.tip(task)
                if not self.integration.contains(run.workspace, current, result.result_commit):
                    if verifier.reconcile(run, candidate, token) is not True:
                        raise VerificationUncertain('verifier termination remains uncertain')
                    self.attempts.reconcile_verification(attempt_id, self.owner_id, run.claim.generation,
                                                         token=token, termination_confirmed=True)
                    continue
                payload['default_branch_tip'] = current
            return self.attempts.complete(attempt_id, self.owner_id, run.claim.generation, result,
                                          verification_ref=evidence, tracker_payload=payload)
        raise ContractError('default branch kept moving; candidate left awaiting verification')

    def _integrate(self, task, backend, run, candidate, tip):
        """Merge the current default-branch tip into a committed candidate before its gates run.
        A conflict blocks the attempt for rework; any other refusal leaves it VERIFYING."""
        adapter = self._adapter(backend)

        def policy_check():
            self.authorize(task, adapter.describe(), 'integrate')
            return True

        try:
            merged = self.integration.merge(candidate, tip, terminated=lambda: adapter.recover(run).status == 'exited',
                                            policy_check=policy_check)
        except IntegrationConflict:
            self.attempts.block(run.attempt_id, self.owner_id, run.claim.generation, termination_confirmed=True,
                                tracker_payload={'status': 'Blocked', 'attempt_id': run.attempt_id,
                                                 'reason': 'default branch conflicts with the candidate',
                                                 'default_branch_tip': tip})
            raise
        return self.attempts.record_integrated(run.attempt_id, self.owner_id, run.claim.generation,
                                               candidate.result_commit, merged)

    def _finalize(self, task, backend, run, candidate):
        """Commit a permitted dirty candidate before any gate sees it. A refusal leaves the
        attempt VERIFYING with its workspace reserved and no tracker effect."""
        if self.finalizer is None:
            raise ContractError('dirty candidate requires a configured finalizer')
        adapter = self._adapter(backend)
        branch, allowed_paths = self.finalization_scope(task, run)

        def policy_check():
            self.authorize(task, adapter.describe(), 'finalize')
            return True

        committed = self.finalizer.finalize(
            candidate, terminated=lambda: adapter.recover(run).status == 'exited',
            current_generation=run.claim.generation, policy_check=policy_check,
            branch=branch, allowed_paths=allowed_paths)
        return self.attempts.record_finalized(run.attempt_id, self.owner_id, run.claim.generation, committed)

    def reconcile_verification(self, attempt_id, verifier):
        """Clear a stopped check claim for retry; this never marks a candidate successful."""
        self._check_open()
        if attempt_id not in self.active:
            raise ContractError('attempt must be recovered before verifier reconciliation')
        run = self.attempts.get(attempt_id)
        if run.state != S.VERIFYING:
            raise ContractError('candidate is not awaiting verification reconciliation')
        token = self.attempts.verification_claim(attempt_id)
        if token is None:
            raise ContractError('no verifier claim to reconcile')
        candidate = self.attempts.result(attempt_id)
        if candidate is None or verifier.reconcile(run, candidate, token) is not True:
            raise VerificationUncertain('verifier termination remains uncertain')
        # Like cancellation, proving termination must remain possible after policy
        # revocation. A retry still passes fresh authorization in verify().
        run = self.attempts.heartbeat(attempt_id, self.owner_id, run.claim.generation)
        return self.attempts.reconcile_verification(attempt_id, self.owner_id, run.claim.generation,
                                                   token=token, termination_confirmed=True)

    def cancel(self, attempt_id, reason='operator'):
        self._check_open()
        _, backend = self.active[attempt_id]
        adapter = self._adapter(backend)
        run = self.attempts.get(attempt_id)
        if run.state == S.CANCELLED:
            return Cancellation(requested=True, termination_confirmed=True, reason=reason)
        if run.state != S.CANCELLING:
            # Persist intent before sending signals; restart cannot turn an interrupted
            # cancellation into a successful candidate or a fresh execution.
            run = self.attempts.request_cancellation(attempt_id, self.owner_id, run.claim.generation)
        cancelled = adapter.cancel(run, reason)
        if not cancelled.termination_confirmed:
            return cancelled
        state = adapter.recover(run)
        if state.process is None:
            raise ContractError('termination lacks process identity')
        run = self.attempts.adopt_process(attempt_id, self.owner_id, run.claim.generation,
                                          pid=state.process.pid, start_time=state.process.start_time)
        self.attempts.finish_cancellation(
            attempt_id, self.owner_id, run.claim.generation, termination_confirmed=True,
            tracker_payload={'status': 'Blocked', 'attempt_id': attempt_id,
                             'execution_state': 'cancelled', 'reason': reason})
        self._close_authority(attempt_id)
        self._release_resource(attempt_id)
        return cancelled

    def deliver_tracker(self, transport, *, backoff=exponential_backoff()):
        """One delivery pass with remote dedupe: a lost acknowledgement reconciles instead of
        duplicating, a conflicting remote record waits for an operator, and failures back off."""
        self._check_open()
        return TrackerDelivery(self.attempts, transport, backoff=backoff, clock=self.attempts.clock).deliver_pending()

    def deliver_outbox(self, deliver):
        """At-least-once and without remote dedupe; use deliver_tracker for a real tracker."""
        self._check_open()
        for effect in self.attempts.pending_effects():
            try:
                deliver(effect['effect_id'], effect['task_id'], json.loads(effect['payload']))
            except Exception as exc:
                self.attempts.note_delivery(effect['effect_id'], error=type(exc).__name__)
            else:
                self.attempts.note_delivery(effect['effect_id'])
