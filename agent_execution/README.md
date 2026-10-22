# Shared execution contracts

Implementation slices #1453/#1454 of approved feature #1451 / initiative #1450.
This package defines wire objects and pure transition/adapter checks. It starts no agents,
reads no configuration, grants no permissions and implements no persistent claim store.
Existing Autopilot and Agent Bridge runners are unchanged.

## Files

- `contracts.py`: frozen, versioned task/agent/attempt/result/handoff/event types and strict JSON decoding.
- `adapter.py`: structural adapter protocol, prepared-plan/result correlation and capability preflight.
- `lifecycle.py`: legal state transitions and evidence requirements, to be enforced atomically by a future store.
- `context.py`: bounded content-addressed artifacts, immutable package validation and lossless rendering.
- `tests/`: synthetic Claude/Codex/local profiles; no provider compatibility is implied.

Run offline tests from the repository root:

```sh
python3 -m unittest discover -s agent_execution/tests -v
```

## Wire rules

Use keyword constructors for trusted code and `from_json`/`from_dict` for wire input.
Every serialized object, including nested objects, carries `schema_version: 1`. Unknown fields,
missing versions, duplicate JSON keys and unsupported versions are refused. Constructors
validate as well. Lists become tuples for typed collection fields; booleans are not numbers.
Costs require provenance and unknown usage remains null. Explicit inherited budget policy is
required for a null cost limit. The wire version is exact version 1, not an assumed minor-version
compatibility promise.

`TaskSpec` contains specification fields only. Controller-owned assignment/state belong in
`Attempt`; the design document's combined illustrative task object is not accepted verbatim.
Typed nested records replace loosely structured design examples. `Usage` groups cost provenance
with resource usage; `Handoff.task_ref` pins acceptance requirements. The design remains the
requirement source; Python types are the current first-slice wire implementation.

Objects with tuple/record fields are frozen. Event `payload` is a copied JSON dictionary and is
not deeply immutable; reserialize/revalidate at trust boundaries. Artifact digest format is checked,
but bytes, allowed roots and repository/task package compatibility are validated by the context
builder in #1454. Neither a digest nor an agent capability declaration grants authorization.

## Controller responsibilities

The adapter protocol separates prepare/start/events/cancel/collect/checkpoint/recover.
`require_agent` checks availability, headless mode, privacy compatibility and validated required
capabilities only. Tools, network, budget, resources and context access still need effective-policy
enforcement. `require_prepared` and `require_result` refuse scope mismatches; the store must also
check the active generation atomically before accepting any update.

`transition` is pure: callers supply trusted evidence flags and claim identity. Success requires
termination and independent verification. Approval resumption requires a matching approval;
handoff requires an accepted checkpoint plus termination. Cancellation requested is distinct
from termination confirmed. Expired lease timestamps never imply termination. Retry and handoff
successors get new attempt IDs; terminal attempts cannot be reset.

A candidate that left uncommitted changes (`diff_ref` without `result_commit`) is committed by
`CandidateFinalizer` before any gate sees it. The controller needs both a finalizer and a trusted
`finalization_scope(task, run)` that returns the branch and allowed paths; the agent's own output
never supplies them. Finalization requires proven termination, the current generation and a
`finalize` effective-policy check. `store.record_finalized` swaps only the stored dirty
candidate for the same result with its commit. Any refusal leaves the attempt VERIFYING, its
workspace untouched and no tracker effect. A retry after a crash between commit and record
recovers the same commit.

With `integration=DefaultBranchIntegration(author, branch=...)` (a name, or a mapping from canonical
repository path to name when repositories differ), the verified commit must also
contain the current tip of the default branch (V17). If it does not, the controller merges the tip
into the feature branch after an `integrate` policy check and proven termination. The merge is
computed with `git merge-tree` and applied only as a fast-forward of a clean worktree, and it is
recorded through `store.record_integrated`. The gates then run on that merge. If the tip moves
while the gates run, the stopped check claim is reconciled, and the candidate is merged and
verified again, at most `max_rounds` times. After that the attempt stays VERIFYING for a later
retry. A conflict blocks the attempt for rework and leaves the worktree untouched. Local edits in
the worktree refuse the merge; nothing is stashed or reset. The default branch itself never moves.

Tracker effects leave through `controller.deliver_tracker(transport)`, which runs `TrackerDelivery`.
It looks up the stable effect ID remotely before applying, so a lost acknowledgement reconciles
instead of posting twice. A remote record with a different payload is parked (`conflict=1`, listed
by `store.conflicted_effects()`) and never retried or overwritten; it still counts as undelivered
for the rollback inventory. Failed passes back off exponentially (30 s doubling, capped at 1 h).
`deliver_outbox(deliver)` remains the older at-least-once path with no remote dedupe.

`RollbackCoordinator` stops new dispatch, reports what still holds a workspace and refuses
legacy handback until every hybrid writer is reconciled. `schema_rollback(target)` lowers the
store's version so an older writer can open it. It runs only once the same blockers clear,
re-read inside one write transaction, and it drops nothing; the change is recorded in
`schema_rollbacks`, and reopening with current code migrates forward again. A pending GPU
restore-state file (`gpu_restore_paths`) blocks both until its own recovery removes it;
rollback never restores or evicts models.

Future tasks integrate these types with the current runners and implement durable claims,
effective permissions and resource admission. No live backend is
enabled by adding this package.

## Portable context

`ArtifactStore` accepts bytes explicitly supplied by the controller. It never resolves arbitrary
file paths or URLs from a context reference. SHA-256 blobs are published atomically; reads are
bounded and reject symlinks, hardlinks, nonregular files and digest mismatches. The configured
store root and its ancestors are trusted controller configuration, not agent-supplied paths.
The caller must keep that directory private and must not put credentials in portable artifacts.

`build_context` pins the complete task, selected instruction records, required artifacts and
optional accepted predecessor checkpoint. `load_context` validates every required reference
and requires the current expected task, including revision, repository, base and policy.
`render_context` includes mandatory fields for all backends, regardless of native session hints;
an insufficient budget fails rather than silently truncating requirements or known failures.
Digest integrity does not establish instruction authority. Repository-derived checkpoint text
is explicitly labeled untrusted; adapters still require effective-policy enforcement.

## External effects (MCP)

Governed agents get no native MCP servers. When the controller registers `ToolRoute`s for an
attempt, `ClaudeEnforcement(..., effect_tools_for=...)` passes a strict MCP config holding only
`mcp_gateway.py` (server `nomad_effects`), bound to that attempt's socket and generation. Each
gateway call goes to `ToolAuthority`, which checks the attempt is running, the task's privacy
against the route's boundary, the route's `result_privacy` against the agent's boundary, and an
operator approval whose scope digest matches the exact arguments. The approval is consumed before
the handler runs and nothing is retried. The PreToolUse hook admits only `mcp__nomad_effects__*`
and refuses every other MCP tool. Codex keeps every MCP server disabled (#1486).

## Codex launch (#1486)

NativeAdapter launches Codex only with `CodexTrustedEnforcement`. Per attempt it creates a private
CODEX_HOME (0700, outside the workspace and every sandbox read root) holding one PreToolUse hook,
asks the installed CLI (`app-server` `hooks/list`) for that hook's hash, records it as trusted,
then lists and verifies the hook set again immediately before launch. A tampered, extra,
untrusted or absent hook refuses the launch, since Codex itself skips such hooks silently. The
adapter also refuses without the `unified_exec_tty=false` override or with any environment beyond
CODEX_HOME. Login: `auth.json` is copied in at 0600 and the home is removed only on proven exit;
nothing is written back unless `auth_writeback=True`. Before removing the home, `release` records
the login outcome on the attempt (`cleanup_outcome.auth`: unchanged, changed_not_persisted,
persisted or conflict, by digest only), and a refresh that was not persisted becomes a warning on
the result (#1503). Persistence is workspace-only: writes outside the worktree land in a
per-command tmpfs that is gone before cleanup, so they are dropped and cannot be listed. `codex.codex_adapter_factory(policy_revision=..., tool_profile_ref=...)` builds the
adapter in the `make(agent, artifacts, store, root)` shape `autopilot/hybrid.py` registers.
Real-CLI proof (operator's ChatGPT login, one real attempt):
`PYTHONPATH=$PWD python3 -m agent_execution.probes.codex_launch`.

Each dispatch is written to `external_effects` before the handler runs (`dispatching`), then
marked `done` or, when the handler raises, `uncertain`. An uncertain effect blocks further effects
for the attempt before any new approval is spent. Any unreconciled row, including a `dispatching`
row left by a crash, blocks `complete()` and rollback handback until an operator records
`applied` or `not_applied` through `EffectivePolicy.reconcile_effect`. Store schema version 3
fences older writers out.

Installed-CLI proof, fake provider only: `PYTHONPATH=$PWD python3 -m agent_execution.probes.claude_gateway`.

Launch masks cover existing paths; newly created protected names are refused at collection and finalization, not masked at launch.
