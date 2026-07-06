# SE3 Framework Version History

























































































## 11.12.1 - 2026-07-06

- Fix live chat view no longer receiving new messages after a running flow crosses the discovery→analyze boundary by correcting the daemon disk-cache stale-parse blind spot during dense engine.json rewrites
- Fix daemon active-flow detection and read_flow cursor advancement across the first steps-list write, PAUSED→RUNNING flip, and new per-step jsonl creation
- Harden the server history relay so a dropped first/full bundle self-heals and subsequent appends still broadcast over /ws/ui instead of being permanently swallowed
- Change the WebUI silent-rebuild fallback from a one-shot 5s rebuild to a periodic retry that keeps pulling mid-step content until WS increments resume, so users no longer must exit and re-enter the chat view
- Fix the scroll jump on fallback rebuild: stay pinned to the bottom when the user is near it, and keep viewport-anchored content stable otherwise
- Add discovery→analyze boundary end-to-end delivery tests plus fallback-retry and scroll-anchor coverage, and document manual browser verification steps
## 11.12.0 - 2026-07-06

- Add `code_index.max_concurrency` config to generate file/dir summaries concurrently across multiple LLM tasks, with output identical to the serial path
- Preserve resumability and single-group failure isolation so a concurrent index build can recover from interruptions without aborting the whole run
- Emit per-node index-build progress (path, kind, done, total) recorded as NDJSON history via `record_index_progress` during the commit step
- Show live 'updating code-index: <path> (done/total)' progress in the WebUI, themed via style.css, reusing the existing history push channel
- Keep engine/server isolation intact — progress reporting adds no server imports on the engine side
## 11.11.2 - 2026-07-06

- Fix source issue staying in-progress when a `--from-issue --worktree` flow is paused then resumed to completion by a new process (daemon or `se3 run --resume`)
- Base issue finalization on the persisted flow.source_issue_id and flow terminal state instead of the original wrapper process's exit code, so finalization is consistent across original-process, resume, and daemon completion
- Stop prematurely resolving the source issue on the first pause, which previously happened because json-mode pause returns exit code 0
- Resolve worktree source issues only when the flow COMPLETED and its commits landed as ancestors of the origin branch; on merge failure keep the issue in-progress and print an explicit notice (merge failed, issue kept in-progress, branch retained, how to retry with `se3 merge`)
- Backfill still-in-progress source issues to resolved when a later `se3 merge` run lands the branch commits as origin-branch ancestors, including already-ancestor leftover branches from earlier interrupted merges
- Return a FAILED worktree or sync flow's source issue to open on the resume path, matching existing non-resume semantics
- Add regression tests covering pause->resume->COMPLETED+merge->resolved, merge-failure kept in-progress, merge-retry backfill, stash-pop-incomplete non-zero exit with commits already landed, no-resolve-on-pause, FAILED->open, and sync from-issue resume finalization
## 11.11.1 - 2026-07-05

- Stop eagerly rebuilding the open flow's conversation on step progression; wait for the WS incremental path to deliver new records first
- Add a 5-second grace period after a detected progression, triggering a single silent full rebuild only if no WS increment arrives for that flow in time
- Keep the healthy push path free of silent full rebuilds, eliminating scroll jumps and stale token-usage badges without exit/reopen
- Preserve refresh semantics: at most one trigger per progression, current-open-flow only, reply-area draft/focus untouched, pending grace timer cancelled on flow switch/close
- Update frontend progression-refresh tests to cover the healthy (zero rebuild), fallback (exactly one rebuild), duplicate-snapshot, and stop-state (FAILED/PAUSED) cases
- Retain the SE3_HISTORY_DIAG gated diagnostic instrumentation (fully lazy by default) for future recurrence diagnosis
## 11.11.0 - 2026-07-05

- Add `se3 worktree gc` command that archives completed/failed worktree runs older than a threshold into se3/worktrees/.archive/ and prunes stale git worktrees
- Reclaim disk and unblock the daemon event loop by reaping leaked worktree runs that were never merged after a pause/resume or manual merge
- Preserve refs for unmerged branches during GC and emit an explicit warning listing completed-but-unmerged worktree branches so no work is silently lost
- Delete branches that are already merged as part of cleanup, keeping the worktree directory tidy without risking unmerged work
- Report each GC run: what was archived, which unmerged branches were retained, and how much space was freed
- Run GC automatically as a low-frequency daemon tick (offloaded to a background thread) in addition to the on-demand CLI command
## 11.10.1 - 2026-07-04

- Fix implement/fix steps silently running the wrong model: the spec-write guard no longer overrides the agent's --settings (model, permissions, effortLevel), so oclaude correctly reports claude-opus-4-8[1m] again
- Deliver the spec-write guard hook via --plugin-dir instead of a second --settings flag, which the claude CLI replaces rather than merges
- Generate an idempotent guard plugin at se3/tmp/spec_write_guard_plugin (.claude-plugin/plugin.json + hooks/hooks.json) in place of the guard settings file
- Preserve spec-write protection behavior: writes to se3/specs/ are still denied with the custom reason, writes elsewhere are unaffected, and update_spec plus sync steps remain exempt
- Keep the post-step spec-diff snapshot/capture as an unchanged second line of defense
## 11.10.0 - 2026-07-04

- Make adjudicate rulings auto-pass by default so unattended runs no longer pause for human confirmation
- Add opt-in human/LLM review for adjudicate via a standard `confirmation.steps.adjudicate` config entry
- Preserve the carve-out that plan-only rulings are never human-gated, even when `reviewer: human` is configured
- Update se3.yaml, `se3 init` scaffold, and config.py docs to document the免确认 default and opt-in human review
- Default the config-resolution failure edge to auto-pass (with a warning) instead of reviving the old human fallback
- Add integration/reflow tests covering both the auto-pass default and the explicit human opt-in paths
## 11.9.0 - 2026-07-04

- Add approve/reject buttons with an optional note box to confirm chips in the webui, replying with a structured {approved, feedback} payload instead of only free text
- Render an adjudicate review panel on confirm chips showing the adjudication rationale and the adjudicated-description diff against the pre-adjudication baseline
- Enrich confirm-call data contract with a dedicated CALL_KIND_CONFIRM kind and human-readable prompt so the frontend can render structured controls
- Expand free-text answer interpretation to recognize common Chinese approval/rejection words (同意/通过/批准/驳回/拒绝/打回)
- Add a second-confirmation prompt and placeholder hint for free-text replies that would be treated as a revision request, eliminating silent misjudgment of inputs like "1"
- Keep the free-text reply channel fully backward compatible alongside the new structured payload
## 11.8.1 - 2026-07-04

- Fix daemon/webui-initiated flows hanging at human confirm gates (plan confirm, adjudication approval): CONFIRM pauses now set engine.json status to PAUSED and exit 0 instead of exiting 130 with status left at 'running'
- Make the CONFIRM pause path honor --output-format json non-interactively, mirroring the DISCOVERY pause branch, so the daemon re-spawns and consumes the response automatically after a webui reply
- Preserve interactive-terminal confirm behavior unchanged (in-process polling, no process exit)
- Audit and annotate the remaining non-interactive pause exits in run.py to guard against the same 'exit without setting PAUSED' failure mode
- Add regression test covering CONFIRM non-interactive pause: PAUSED status, exit code 0, call file, and FLOW_PAUSED event, plus a guardrail for the interactive path
## 11.8.0 - 2026-07-04

- Add a unified (path, mtime, size)-keyed disk-JSON parse cache for the daemon so unchanged engine.json, archive, and snapshot files are parsed at most once, eliminating per-tick full re-parsing that froze the event loop and pinned executor threads
- Add a 5 MiB size guardrail with bounded head+tail degraded reading that extracts flow_id/status/is_worktree_mode/project_root from oversized legacy engine.json files without full parsing, keeping worktree runs visible in the webui while capping CPU and memory
- Ensure all daemon disk-JSON parsing runs off the asyncio event loop, restoring reliable webui push updates so new tasks respond even with tens-of-MB engine.json files present
- Introduce a hot/cold engine.json format that keeps a KB-scale header and stores each step's inputs/outputs in per-flow external step files, bounding the header below 100KB regardless of step count
- Make step persistence incremental, rewriting only the header and the changed step's cold file so write volume scales with per-step output instead of accumulating across steps; resumable snapshots use the same split format
- Read both legacy inline and new split engine.json formats without migrating existing files, tolerating missing or corrupt cold step files by degrading in-memory only and never overwriting the on-disk cold file
- Adapt resume, se3 history, context export, and daemon/webui read paths to the new format with lazy cold-data loading and full-fidelity archiving on clear_state
## 11.7.1 - 2026-07-03

- Gate the ADJUDICATE LLM ruling into two phases: only a real spec contradiction takes the patch/reflow path
- Route benign (review_divergence) rulings as a transparent no-op that passes the triggering round's fix_instructions straight into IMPLEMENT
- Stop charging an extra fix iteration, clearing issues, superseding fix_instructions, or reflowing to SELF_CHECK pass #1 when no true contradiction is found
- Preserve two mechanical bookkeeping effects on the no-op path: benign positions recorded to rejected_positions and the period_baseline reset
- Record an audit-only verdict (candidate_verdicts, rationale) on no-op rulings without writing adjudicated_description / adjudicated_plan / superseded_fix_instructions
- Keep the mechanical trigger layer and the true-contradiction adjudication path behavior unchanged
- Update engine and integration tests to cover the transparent no-op pass-through and confirm the contradiction path is unaffected
## 11.7.0 - 2026-07-03

- Add a new ADJUDICATE step that resolves specification contradictions in the fix loop, breaking review-oscillation deadlocks that previously required manual engine.json edits
- Introduce a cross-iteration fingerprint ledger persisted in flow context that survives --resume and accumulates self_check issues and fix resolutions
- Add three oscillation triggers (candidate oscillation, contradiction/'打脸', recurrence) plus a periodic fallback governed by the configurable adjudicate_period (default 10) fix iterations
- Emit override-style adjudication patches (adjudicated_description / adjudicated_plan) that take effect at highest priority (adjudicated > refined > original) while leaving original discovery/plan outputs untouched and fully auditable
- Switch self_check verbatim_quote validation to the adjudicated source pool so issues citing abolished clauses are dropped, and rerun SELF_CHECK from pass #1 after adjudication while skipping IMPLEMENT/TEST
- Add a human confirmation gate for task-description adjudications via existing se3 calls / PAUSED approval, with LLM/no-confirm options for plan- or test-only rulings
- Suppress the convergence shortcut when an oscillation fingerprint fires so buggy flows cannot silently reach COMPLETED
## 11.6.0 - 2026-07-02

- Guarantee that no issue is lost during `se3 merge` across both git three-way merge and runtime-sync channels
- Detect duplicate issue IDs in committed-issue merges and automatically renumber the incoming worktree's copy instead of silently leaving a collision
- Coordinate new ID assignment with the `.next_id` counter, reserving under the fcntl lock and advancing it monotonically (only ever increasing) to at least the library max ID + 1
- Rewrite all provably-attributable `#old` issue cross-references to `#new` library-wide using exact `#NNN` token matching, leaving ambiguous references untouched
- Record each renumber as a traceable old→new note inside the affected issue and report unresolved ambiguous references in the merge report
- Align the existing runtime-sync `adopt_issue` renumbering path with the new git-channel logic so both satisfy the same never-lost / never-collide guarantee
## 11.5.0 - 2026-07-01

- Show each history session's flow_id on its list card meta row, truncated with ellipsis and a full-value tooltip for readability and copy
- Display the complete flow_id on its own dedicated line in the history detail header, independent of the task_description fallback
- Add a session-total token-usage badge to the history detail view, reusing the running-flow usage rendering and hiding when no usage exists
- Clear the history header flow_id and usage badge when closing a session so stale values don't bleed into the next opened session
- Anchor the resume button insertion to the stable flow_id line so it cleans up reliably with the new header elements present

## 11.4.0 - 2026-07-01

- Inject recency context into the discovery step's initial (round 0) prompt so it no longer cold-starts and can resolve cross-session references like 'continue the last one'
- Summarize the most recent 3 session reports (se3/state/summary-*.md) via structured extraction of the Task line and report body, with per-entry length caps
- Include the latest 10 non-merge git commit subjects (subjects only, --no-merges) as context to help discovery avoid duplicate proposals and recognize follow-ups
- Gate recency injection to round 0 only; later rounds inherit it through conversation history, saving repeated prompt token cost
- Skip any missing or unavailable recency source silently, preserving the discovery step's read-only nature and never failing on absent summaries or git data
- Apply identical round-0 recency injection behavior in worktree mode

## 11.3.2 - 2026-06-30

- Prevent silent, unrecoverable loss of uncommitted untracked files (e.g. concurrently created se3/issues/open/NNN_*.yaml and .next_id) during worktree/branch merge stash-pop
- Archive any stashed content that cannot be cleanly restored to se3/worktrees/.archive/ with full file bodies, original relative paths, and timestamp/stash-label, and only drop the stash after recoverability is confirmed
- Stop unconditionally taking-ours on every stash-pop conflict: untracked-collision (case b) files are renamed aside and archived instead of destroyed, while true 3-way tracked conflicts (case a) allow optional LLM-based resolution with the discarded side archived
- Upgrade stash-pop audit issues/events to record recoverable content pointers (archive path plus blob sha and/or body) instead of only file paths
- Apply the no-data-loss archival behavior uniformly across both merge paths (merge_cmd fast strategy and implement leaf-branch merge-back) via the shared engine/stash_utils.py primitive
- Correct the _fast_stash_pop docstring that incorrectly stated the LLM is intentionally never consulted

## 11.3.1 - 2026-06-30

- Extend the discovery HARD INVARIANT to forbid reader-facing decision-point / changeable annotations (e.g. '默认选择，可改', '(default, changeable)', 'you can change this') from appearing in refined_description, not just open-item phrasing
- Require already-made decisions to appear in refined_description only as clean conclusions (e.g. 'Decided: use X') with no 'default / changeable' parenthetical attached
- Clarify that all 'default / changeable / adjustable' meta-notes belong solely in the content field shown to the user, never in the downstream-consumed refined_description
- Update both discovery prompt templates' content and refined_description field descriptions and the 'route every unsettled matter' Guidelines to match the tightened invariant, keeping the two templates consistent
- Add explicit WRONG/RIGHT examples showing how a settled-but-changeable decision should be split between refined_description and content
## 11.3.0 - 2026-06-30

- Add a dedicated plan-confirm prompt that decomposes embedded requirements and verifies every requirement is covered by a plan task
- Make plan-step requirement-coverage confirmation always-on, independent of whether confirmation.steps.plan is configured
- Narrow the confirmation.steps.plan config to only select the reviewer and max_iterations, defaulting to an LLM reviewer when omitted
- Add a strict per-task correctness section to self_check that hard-audits each plan task against the actual diff
- Add a regression/side-effect section to self_check that flags changes affecting behavior outside the task scope
- Flip self_check's task_groups handling from soft reference to strict audit, removing the prior 'do not flag missing plan compliance' restriction
## 11.2.0 - 2026-06-30

- Generate new-project .gitignore as root `/*` default-deny plus explicit `!/<name>` whitelist entries for every standard top-level artifact, hardening against stray root files being staged by `git add -A`
- Extend `migrate` to introduce the root `/*` default-deny while enumerating already-tracked top-level paths via `git ls-files` and re-allowing each, so no existing project silently loses tracking
- Keep `migrate`'s gitignore rewrite idempotent so repeated runs produce no further changes
- Add a fault-tolerant commit-time detector that emits a WARNING when a new top-level path is excluded by the root rule, surfacing otherwise-silent untracked work without ever blocking or failing the commit
- Limit the commit-time detector to warning only — it never edits .gitignore or auto-whitelists, preserving the default-deny guarantee
## 11.1.1 - 2026-06-29

- Fix daemon mis-detecting `python -c <code> ... se3 run` inline subprocesses as real se3 runs, which spawned phantom RUNNING sessions in the WebUI
- Restrict `_cmdline_is_se3_run` to the two genuine launch shapes: console-script `se3 run` (argv[0] or shebang-rewritten argv[1]) and module form `python -m se3 run`, requiring `run` as the immediate subcommand token
- Structurally exclude the interpreter inline-code form by treating a leading `-c` as inline mode while still recognizing the CLI's own `se3 run -c/--change` option
- Add regression tests asserting the inline `-c` stub returns False and is not registered by `_scan_external`, plus positive coverage for real console-script and `python -m se3 run` forms
- Preserve existing worktree-sandbox attribution behavior and live discovery of genuine se3 run processes
## 11.1.0 - 2026-06-29

- Add a secondary 'list-fp' fingerprint for files and directories so normal rebuild only re-summarizes a file/dir when its direct members' name/kind list changes (add/remove/rename)
- Stop ancestor-chain re-summarization on body-only edits: changing a function body without renaming re-summarizes only that symbol, leaving its file and parent directories untouched
- Preserve full content-fingerprint cascade behavior under 'rebuild --force' to still catch same-name behavioral drift and deep semantic changes
- Extend the code-index.md inline fingerprint comment format to carry both content-fp and optional list-fp, strictly backward compatible with the prior single-fp format
- Migrate existing indexes lazily and at zero LLM cost: nodes missing list-fp reuse their old summary via content-fp while list-fp is computed and backfilled mechanically
- Carry list-fp through checkpoint flush re-renders so untouched nodes keep their fingerprints and are not falsely rebuilt on the next run
## 11.0.1 - 2026-06-26

- Display the running-flow card worktree tag as `project_name (worktree)` with a half-width space and half-width parentheses instead of full-width Chinese parentheses
- Sync code comments describing the worktree label format to the new output
- Update frontend pure-function tests to expect the new worktree label format
## 11.0.0 - 2026-06-24

- Replace the spec-mirror knowledge system with a code-index + charter + why-comment triple; source of truth returns to the code itself
- Add the `se3 code-index` command family: a hierarchical, drill-down code structure map (authoritative se3/code-index.md plus volatile se3/cache/code-index.json) built by deterministic enumeration with LLM one-line summaries and incremental fingerprint caching
- Add the `se3 migrate` command as a registry-based migration channel, with the first migrator converting an existing spec corpus into charter + code-index + colocated why-comments in one git-revertable change
- Retire the `se3 spec` and `se3 sync` command families and the entire spec-governance machinery (spec baselines, verify/update/gate steps, sync_* stack)
- Rename and shrink the base spec into se3/charter.md (template src/se3/templates/charter.md), gated by an altitude admission standard and a byte-threshold monitoring light
- Rewrite all six task-type flow sequences: insert INVARIANT_CHECK (verbatim-quote anchored over task description, charter, and touched-code why-comments) after self_check and the advisory CHARTER_FRESHNESS before version_analyze
- Add a code_index configuration section exposing degrade/chunk thresholds (2000 lines / 256 KiB trigger, 200 lines / 16 KiB chunks) and an explicit exclude list
- Update README/README.zh and self-hosted specs to document the new three-piece design and its advantages over the spec-index system
## 10.8.1 - 2026-06-23

- Stop web console modals from closing when clicking the backdrop outside them, preventing accidental loss of typed input
- Require the new-issue, edit-issue, and other dialog modals to be dismissed explicitly via the × or cancel button
- Preserve the mobile sidebar drawer's tap-the-backdrop dismiss and the nav menu's outside-click close behavior
- Retain every modal's explicit close-button binding so dialogs remain fully dismissable
## 10.8.0 - 2026-06-23

- Display worktree-based session projects as '<项目名>（worktree）' instead of the long opaque worktree slug
- Add a worktree-aware projectDisplayLabel helper that detects the se3/worktrees/<name> layout and derives the real owning project name
- Apply the friendlier project label to both the running-flow card badge and the sidebar Overview Project row
- Retain the full project path as a hover title for the project label
- Fall back to the plain project basename for non-worktree or degenerate project roots
## 10.7.1 - 2026-06-23

- Fix LLMCaller agent rotation so each internal retry sequence restarts from the preferred (first) agent instead of getting permanently stuck on the last agent
- Reset _current_agent_index and refresh the active runner at the entry of every new retry sequence (json_retry_count == 0), fixing model lock-in when one LLMCaller is reused across rounds in se3 sync
- Preserve JSON-continuation recursion semantics: json_retry_count > 0 retries keep the current agent and conversation context without resetting rotation
- Keep within-sequence rotation one-way (tail-on-last): when max_retries exceeds the agent count, rotation stops at the last agent and terminates under the max_retries cap with no wrap-around
- Apply the fix at the core LLMCaller layer so all callers (se3 run, se3 sync, merge) benefit uniformly
- Clarify the llm-caller spec's rotation-state lifecycle and add scenarios for cross-call/cross-round reset, JSON-continuation non-reset, and independent reset per two-phase phase
- Add unit tests covering cross-sequence reset, tail-on-last termination, two-phase independent rotation, and JSON-continuation context retention
- Hide the Resume button for a RUNNING flow whose process is still alive, while keeping it visible for interrupted RUNNING-but-dead flows
- Return an explicit rejection ('该 flow 仍在运行，无法 resume') instead of a misleading resume_dispatched when resuming a still-running flow
- Pre-validate POST /api/flows/{id}/resume so unknown or cross-owner flows return 404 and completed flows return 409 ('已完成，无法 resume')
- Align Resume-button visibility with the daemon's double-spawn guard so the UI never offers a resume that the backend would reject
- Preserve existing PAUSED, FAILED, and interrupted-RUNNING resume behavior with no regressions
- Fix the recurring WebUI bug where the main conversation area stopped updating after a flow step switch or in-step retry, requiring a manual session exit/re-entry to recover
- Auto-recover the conversation view when a flow advances (current_step/current_step_index change or a FAILED/PAUSED→RUNNING retry resume) by performing a one-time full /api/history rebuild for the flow currently being viewed
- Perform the recovery refresh silently — fetch history in the background and swap the DOM only once data arrives, eliminating the blank 'Loading conversation…' flash at each step boundary
- Preserve the reader's scroll position across the refresh via isNearBottom(), so users scrolled up in history are no longer yanked back to the bottom
- Isolate the refresh to the conversation area, leaving the reply region (#flow-interventions / #flow-reply-context) untouched so in-progress draft text, focus, and textbox height are never disturbed
- Cover all step transitions and in-step retries (e.g. update_spec failure → manual retry), not just the discovery→analyze boundary, with single-fire-per-advance and zero-refresh-when-no-advance semantics
## 10.6.0 - 2026-06-22

- Persist a per-flow resumable snapshot at se3/state/resumable/<flow_id>.json on every non-completed save and remove it on completion, so paused, interrupted, or recoverable-error flows survive a later run overwriting the single-slot engine.json
- Show a Resume button in the webui for any flow that is not normally completed but has recoverable state (paused for user interjection, mid-step exit including discovery, or recoverable error)
- Support se3 run --resume --flow-id locating and loading the per-flow snapshot back into engine.json to resume an overwritten flow
- Surface persisted paused/interrupted/failed flows as resumable across the daemon index, server, and frontend via an authoritative resumable flag, while keeping completed flows free of any resume entry
- Fix empty task descriptions for history-only sessions by forward-scanning past content-less event records (e.g. step_started) to the first real user prompt when extracting titles
- Keep history display and archival of normally completed flows unchanged
- Show each running flow card's owning project as the project_root basename, with the full path available on hover
- Add a 'Project' row to the session view sidebar Overview displaying the same project label and full-path title
- Gracefully omit the project annotation (or show a placeholder) when a flow has no project_root, avoiding render errors
- Include project_root in the flow card and sidebar diff-aware signatures so project changes correctly trigger rerender
- Add minor CSS styling for the new project badge consistent with existing card meta elements
## 10.5.1 - 2026-06-18

- Fix WebUI conversation pane freezing after a discovery→analyze step transition, so new step content now appears within seconds without re-entering the session
- Fix the same conversation-pane freeze after manually retrying a failed step (e.g. update_spec), which rewrites a step's jsonl file in place
- Detect in-place per-step jsonl rewrites in the daemon incremental reader via a full-prefix blake2b digest, forcing a clean re-read instead of trusting a stale byte offset
- Preserve the existing {jsonl-filename: line-count} cursor contract and all dedupe/reconnect/render mechanisms while fixing the dropped-frame root cause in the daemon layer
- Add deterministic regression tests (daemon step-transition/truncation unit tests and frontend node-stub frame-replay) that lock down issue #209 against future recurrence
## 10.5.0 - 2026-06-18

- Add a user-directed issue_operations channel to the discovery step contract, letting users create, edit, or delete issues only when they explicitly instruct it
- Restrict discovery update/delete operations to issues created within the same discovery step, rejecting any write to historical or out-of-scope issues
- Limit discovery issue edits to title, description, priority, type, and tags, with no status transitions (no close/reopen/reset)
- Mark issues created through the discovery interface with source=human to distinguish them from automatic source=system discovered issues
- Add IssueManager.delete_issue as a pure file-unlink primitive that removes the issue YAML (zero-padding tolerant) and raises when missing
- Persist the discovery_created_issue_ids tracking set in step inputs so operation scope survives across dialogue rounds and --resume
- Preserve the discovery step's read-only and no-Bash-CLI guarantees; the LLM still cannot initiate issue operations on its own
## 10.4.3 - 2026-06-17

- Fix WebUI running-flow console freezing on the discovery→analyze transition so live transcript keeps streaming without a full-page reload
- Fix the same console freeze after a step error when the user manually retries, keeping subsequent content visible in real time
- Make the live incremental-append render path consistent with the full-snapshot reload path so step-event records no longer collapse onto a single dedup key
- Clear the streamed 'waiting for lock' anchor once a contended merge lock is acquired, preventing the transcript from sticking on that row
- Add regression tests covering the step-transition freeze, retry-after-error freeze, and the lock-acquired clearing event
## 10.4.2 - 2026-06-17

- Fix typing lag in reply textarea caused by unnecessary DOM rebuilds on frequent WebSocket status updates
- Add diff-aware rebuild skipping for renderMachines, renderFlows, renderFlowSidebar, and renderInterventions
- Skip conversation re-render when no new records arrive via polling or WebSocket push
- Preserve real-time responsiveness: genuine data changes still trigger immediate DOM updates
- Preserve textarea draft text, focus, auto-grow height, and scroll position during no-change updates
## 10.4.1 - 2026-06-17

- Fix Available Specifications injection to guide sub-agents through `se3 spec index`/`se3 spec show` instead of reading whole spec.md files that exceed the Read tool size limit
- Fix discovery step question and synthesis templates to use bounded spec consultation protocol consistent with analyze/update_spec/verify_spec steps
- Eliminate contradictory guidance in verify_spec and update_spec steps that simultaneously instructed using `se3 spec` commands and reading entire spec files with the Read tool
- Update flow-engine spec documentation to describe the bounded index-first spec consultation protocol for discovery and downstream steps
## 10.4.0 - 2026-06-16

- Add PreToolUse hook that blocks Write/Edit/NotebookEdit on se3/specs/** for non-exempt steps (update_spec and sync steps exempt)
- Add within-flow spec-diff fallback guard that detects Bash-bypassed spec writes and fails the step
- Add reusable spec-write-protection prompt injection auto-applied to all non-read-only LLM steps except update_spec and sync
- Add plan-specific constraint preventing downstream tasks from writing spec files while preserving spec_changes declarations
- Add spec_write_protection config section with hook_enabled and diff_fallback_enabled options (both default True)
- Preserve behavior-change channel: plan.spec_changes → verify_spec lenient judgment → update_spec write remains fully operational
- Add comprehensive test coverage for all guard layers and behavior-change channel regression
## 10.3.2 - 2026-06-16

- Fix implement step group_status cards stacking 2–3 duplicates per group in the WebUI live console
- Converge group_status rendering to a single card per group keyed on (step_id, group_id), updating in place as agent and model names arrive
- Ensure terminal cards (completed/failed) supersede prior non-terminal running cards without leaving stale duplicates
- Keep distinct groups under the same implement step as independent cards, preventing incorrect cross-group folding
- Preserve card sort order and surrounding fold/raw/chip state unaffected by the deduplication pass
## 10.3.1 - 2026-06-16

- Fix discovery structured fields (content, refined_description, questions) being silently dropped when assistant responses contain bare JSON followed by trailing text
- Fix webui freezing on 'paused' state after answering a discovery prompt, requiring exit and re-entry to recover
- Extend collectJsonRegions to recognize unfenced JSON objects at block-start as valid result regions
- Include lifecycle status in append record key to prevent paused/running collision in live-append deduplication
## 10.3.0 - 2026-06-15

- Add ClaudeInteractiveRunner (type=claude-interactive) as the third AgentRunner adapter, driven by PTY with JSONL transcript watching
- Support wall timeout and inactivity timeout with process-group kill and guaranteed resource reclamation for interactive Claude sessions
- Normalize JSONL transcript output to stream-json NDJSON format compatible with existing upstream consumers
- Register pexpect>=4.8.0 as a lazy-imported runtime dependency for PTY process management
- Add LLMCaller dispatch for claude-interactive runner type with lazy pexpect import, keeping default claude-code -p mode unchanged
- Add comprehensive test suite (126 tests) for ClaudeInteractiveRunner covering lifecycle, JSONL parsing, timeout, and infra-error detection
## 10.2.3 - 2026-06-15

- Fix worktree backup archives landing in .se3/ (un-ignored) by redirecting to se3/worktrees/.archive/ under the sole ignored runtime root
- Add denylist guard in the commit step to detect and unstage runtime artifacts that escape the se3/ runtime root before committing
- Prevent se3 runtime products (cache, history, logs, state, worktrees, etc.) from leaking into git when they appear outside se3/
- Harden commit step to be fault-tolerant: leaked-path unstage failures are logged as warnings and never block the commit
## 10.2.2 - 2026-06-15

- Fix incomplete real-time chat display for worktree flows during discovery step — first assistant reply (thinking + result) and all subsequent messages now stream live
- Fix worktree directories appearing as independent projects in webui project list and New Task dropdown
- Normalize worktree paths to main repository root at all project registration entry points
- Clean up residual worktree entries from persisted project registry on daemon startup
- Ensure worktree flows remain viewable through the observation-only channel without being registered as separate projects
## 10.2.0 - 2026-06-15

- Add `se3 merge-unlock` command to manually release or clean up stale merge locks
- Support `--force` / `-f` flag to override live-holder safety check when releasing a lock
- Display lock status (holder PID, alive/stale/corrupt state, absolute path) on every invocation
- Exit with non-zero code when a live holder is detected and `--force` is not specified
## 10.1.4 - 2026-06-15

- Fix live-rendering stall in webui chat that occurred after responding to an interjection prompt
- Bound dedupeAppendRecords comparison to recent tail window instead of entire held array to prevent false-positive record suppression
- Ensure new append records are not incorrectly filtered by recordKey collisions with distant old records
- Fix expanded message-details scroll position resetting to top on every automatic panel refresh
- Persist reply-prompt scroll offset across 3-second polling and WebSocket status updates
- Preserve scroll position only when the message-details section is expanded, respecting collapse state
## 10.1.3 - 2026-06-14

- Fix live chat view stopping new message display after pressing 1 to confirm (respond) in normal sessions
- Fix worktree/discovery sessions showing empty first assistant reply and no subsequent messages
- Ensure mode:append real-time increments are always broadcast to subscribed WebSocket clients even when serving a pending REST pull
- Preserve project_root across worktree history read path so respond/interject land under the correct session root
- Branch un-terminated trailing line handling on JSON parseability to correctly consume complete sidecar records without trailing newlines
## 10.1.2 - 2026-06-14

- Fix discovery step holding the main-worktree mutex for the entire flow duration, which blocked subsequent webui-initiated tasks from starting
- Show flows waiting on the main-worktree lock as running with 'waiting for lock' status instead of silently stuck at 'published'
- Display spawn and init failures as visible errors in the webui instead of leaving tasks stuck at 'published'
- Restore complete chat history display for --worktree sessions by reading *.jsonl.from-<branch> sidecar files
- Prevent duplicate issue display during worktree runs by skipping worktree-copy issue directories in the aggregator
- Renumber worktree-created issues against the main project's .next_id during merge to avoid number conflicts
- Promote worktree terminal COMPLETED engine.json to main project archive so completed worktree flows appear in the unified lifecycle
- Stop registering worktree subdirectories as standalone projects in the daemon
## 10.1.0 - 2026-06-14

- Add worktree isolation checkbox to the issue-launch modal, allowing flows started from issues to run in an isolated worktree
- Thread the worktree option through buildIssueFlowBody into the POST /api/flows request body for from-issue launches
- Preserve backward compatibility — existing two-arg calls to buildIssueFlowBody default to worktree:false
- Add unit tests covering worktree flag threading, boolean coercion, and legacy two-arg default behavior
## 10.0.1 - 2026-06-14

- Render same-step conversation blocks with a continuous background band instead of isolated per-block islands
- Fill inter-block gaps within a step region so the per-type step color reads as one unbroken lane
- Preserve step-to-step boundary separation — the continuous band stops at each .history-step-header
- Maintain block-level distinction (borders, cards, tool markers) within the continuous step background
- Apply the continuous background consistently to both running-flow and history views
## 10.0.0 - 2026-06-14

- Remove deprecated loop mode entirely, including --loop, --max-iterations, --no-worktree, --merge (loop), and --list-loops CLI options
- Add --worktree isolation mode to se3 run for executing flows in isolated git worktrees with automatic merge-back on success
- Change MergeLock from non-blocking fail-fast to blocking queue semantics, serializing synchronous runs and merges on the main worktree
- Add worktree mode checkbox in WebUI new-task form for creating isolated runs via the web interface
- Replace loop-related FlowInstance fields (is_loop_mode, loop_branch, loop_worktree_path, loop_original_branch) with worktree equivalents (is_worktree_mode, worktree_branch, worktree_path, worktree_original_branch)
- Preserve generic worktree primitives (create_worktree, WorktreeContext, force_cleanup_worktree) for reuse by the new --worktree mode
## 9.5.4 - 2026-06-14

- Fix false 'Could not send — network error' toast when confirming discovery (输入 1) via WebUI despite backend having accepted the reply
- Ensure successfully-sent replies are inserted into the conversation message list instead of being silently dropped
- Make optimistic echo rendering (appendLocalReply) best-effort so rendering failures do not mask a successful backend response
- Add regression tests to prevent recurrence of the success-path misreported-as-network-error bug
## 9.5.3 - 2026-06-13

- Remove semantically misplaced `scope` field from persisted Issue data model and YAML serialization
- Remove `--scope` option from `se3 issue create` CLI command
- Remove scope handling from daemon aggregator snapshots and protocol layer
- Remove scope dropdown and display from webUI issue create/edit forms
- Preserve backward compatibility: legacy issue YAML files with `scope:` key load without error
- Keep transient flow-internal in_scope/out_of_scope classification unchanged
## 9.5.2 - 2026-06-13

- Eliminate visible gap between floating step name header and scroll viewport top in both running-flow and history views
- Ensure floating step header pins flush to scroll container edge on both desktop and mobile layouts
## 9.5.1 - 2026-06-13

- Fix webui issue creation falsely reporting timeout failure when the issue already landed on disk
- Reuse cached project_roots in daemon issue handler to avoid re-running heavy snapshot on every issue command
- Send MSG_ISSUE_RESULT acknowledgment before triggering fast push to minimize daemon-side ack latency
- Add server-side reconcile window on issue command timeout to confirm persistence via in-memory mirror before reporting failure
- Add tests for daemon project_roots caching and ack-before-push ordering
- Add tests for server-side issue reconcile logic across create, edit, close, and reopen operations
## 9.5.0 - 2026-06-13

- Show step regions immediately when a step enters RUNNING status, including non-LLM steps like TEST, COMMIT, and SPEC_GATE
- Group all records sharing the same step_id into a single visual region — step_completed, step_failed, and step_output no longer create duplicate named regions
- Label all step report cards with explicit result or summary semantics to eliminate ambiguity, especially for IMPLEMENT
- Apply stable low-saturation per-step-type visual grouping with text and icon status indicators for clear step boundaries
- Add viewport-driven sticky step header that tracks the current scroll position and allows click-to-locate navigation
- Ensure history view shares the same step grouping, report labeling, and sticky header behavior as the running flow view
- Improve mobile responsive layout for step grouping and report cards
## 9.4.0 - 2026-06-13

- Display agent name badge from the first streaming fragment instead of waiting for completion
- Upgrade badge in-place to 'agent · model' once model name is parsed from NDJSON metadata
- Attribute each retry or agent rotation to its own agent name (no stale carry-over)
- Show live agent/model on DAG worktree-group status cards with identical formatting
- Preserve legacy records without agent/model fields — no empty placeholders displayed
## 9.3.0 - 2026-06-13

- Add `se3 spec index` command with size-bounded deterministic-greedy folding for navigating large spec collections within 16KB output limits
- Add `se3 spec show <spec>::<requirement>` command to display individual requirement content with physical file location
- Add SpecGovernanceConfig section to se3-config with configurable thresholds for base size (32KB), index output (16KB), spec file (64KB), and single requirement (8KB)
- Add guardrails size checks with warn/enforce modes for base spec, spec files, and individual requirements
- Restructure analyze step to use root-view + drilldown protocol instead of injecting all items into prompt
- Restructure update_spec and verify_spec to use index-first retrieval with targeted Read+Edit instead of reading entire spec files
- Slim base spec from 141KB to under 32KB by relocating module-level detail to new daemon, server, and engine-internals specs
- Add `<!-- domain: -->` header metadata to spec format for hierarchical index grouping
## 9.2.0 - 2026-06-12

- Add self_check_defer_fix_threshold workflow config to defer fix when self-check finds few non-critical issues, merging findings across passes
- Retry test step timeouts once in-place before entering the fix loop, distinguishing timeouts from assertion failures
- Fix self_check_passes_required output to record the effective value from nested chain length instead of defaulting to 1
- Slim passed-test archive in chat history to count summary plus bounded tail, reducing storage for verbose pytest output
- Fix history loading regression where old bundles without generation field always fell back to full load instead of delta
- Ensure full record completeness across both delta and full delivery modes in the history view
## 9.1.0 - 2026-06-12

- Add incremental delta loading to GET /api/history/{flow_id} via optional after progress-token parameter
- Return delivery (delta|full) and progress fields in history API response to indicate incremental vs full payload
- Switch WS-reconnect path to incremental delta append for running-flow view, preserving existing records, DOM, and scroll position
- Switch WS-reconnect path to incremental delta append for history-detail view using shared progress token mechanism
- Fall back to full reload on progress token mismatch, cache miss, generation change, or any unsafe condition
- Preserve existing ownership verification and on-demand daemon-pull semantics for cache-miss cases
## 9.0.0 - 2026-06-12

- Add summarize as the default final step in all task-type sequences (feature, bugfix, review, small, directive, discovery); existing `steps.append: [summarize]` configs become silent no-ops
- Support nested per-pass self_check chain configuration (`[[agentA], [agentB, agentC]]`) with automatic pass-count derivation and last-chain reuse on overflow
- Remove priority-based agent rotation ordering; agent chain order now follows the written list order in `llm_caller.defaults` / `llm_caller.steps.<step>`, deprecating `agents.<name>.priority`
- Add ability to start a new flow session from an open issue in the webui, with optional discovery mode
- Extend `MSG_SPAWN_FLOW` protocol and `POST /api/flows` with optional `from_issue_id` field for issue-driven flow spawning
- Document `se3 run --discover --from-issue <id>` combination as a supported CLI contract with test coverage
## 8.14.0 - 2026-06-12

- Display agent name and model name badges on assistant message bubbles in WebUI when metadata is available
- Record agent_name and model_name per LLM call attempt in chat history JSONL for observability
- Fix token usage not appearing in CLI step renderers and WebUI report cards for non-terminal rounds (e.g., self_check REVISION_NEEDED)
- Add project dropdown filter to WebUI issue management panel alongside existing source/type filters
- Ensure backward compatibility for new ChatMessage optional fields — old JSONL records without agent/model metadata load and display without errors
- Ensure non-terminal round token usage is visible without special-case logic for individual step types
## 8.13.0 - 2026-06-11

- Add Resume button in webui for FAILED/PAUSED flows, allowing direct flow resumption from history and live views
- Add full issue management panel in webui with list, detail, create, edit, close, and reopen capabilities
- Refactor `se3 issue create` to single-step input (positional/stdin/multiline) with optional `--editor` flag for external editor mode
- Add `se3 issue edit <id>` to open an issue in the external editor ($EDITOR, falling back to vi)
- Add `se3 issue close <id> [--reason <text>]` to close issues from the CLI
- Add `--source human|system` filter to `se3 issue list`
- Make issue title, priority, and type optional; only description is required; derive display title from description when title is empty
- Add source field (human/system) to issues to distinguish user-created from programmatically-discovered issues
## 8.12.5 - 2026-06-11

- Fix discovery step cumulative token usage not displaying in CLI after programmatic confirmation
- Fix Codex runner silently dropping usage records when total_cost_usd is missing or null
- Add Codex shell snapshot validation failure detection and clear error reporting instead of misclassifying as success
- Preserve original Codex stderr context for shell snapshot and infrastructure startup failures
- Extend InfraErrorType enum with STARTUP_FAILURE for shell snapshot and environment validation errors
- Pass stderr_tail through LLMCaller to agent runner error classification for richer diagnostics
## 8.12.4 - 2026-06-11

- Fix History view list and detail panes producing horizontal page overflow on phone-portrait screens
- Constrain project dropdown, item meta, step titles, and message chips within mobile viewport bounds
- Add CSS guardrail tests verifying #history-view containers enforce mobile overflow containment
- Add DOM-level regression tests for long-path and unbreakable-string rendering in history detail
## 8.12.3 - 2026-06-10

- Fix long file paths and unbreakable text overflowing step report card boundaries in the running flow console
- Prevent unintended horizontal scrollbar in #flow-view when report lists contain long paths or no-space text
- Apply consistent word-break and overflow-wrap rules to all reportList() consumers (Tests Added, Incomplete Tasks, Restricted Edits, etc.)
- Add CSS guardrail and DOM-stub regression tests for step report list item text wrapping
## 8.12.2 - 2026-06-10

- Fix codex agent runner failing with 'unexpected argument -a' by removing the invalid --ask-for-approval flag from codex exec argv
- Migrate codex writable sandbox mode from legacy --dangerously-bypass-approvals-and-sandbox to explicit --sandbox danger-full-access
- Update codex runner docstring to reflect correct command-line form for read-only and writable branches
- Add real CLI smoke test for codex exec to prevent argument-construction regressions
## 8.12.1 - 2026-06-10

- Fix duplicate rendering of batched records during live flow streaming when snapshot fetch overlaps with WS broadcast delivery
- Add dedupeAppendRecords pure function to deduplicate incoming records against already-held records before merging
- Apply append deduplication to both running-flow and history-view record consumers
- Preserve incremental-render cursor semantics when no new records arrive after deduplication
- Ensure partial and stream_progress accumulating fragments are not falsely deduplicated
## 8.12.0 - 2026-06-10

- Add CodexRunner agent type (type: codex) allowing OpenAI Codex CLI to be registered and used as an agent alongside Claude
- Implement intent-passing architecture for CLI argument construction, moving runner-specific flags out of the shared LLMCaller into each runner
- Support read-only sandbox enforcement for Codex via --sandbox read-only on read-only steps
- Inline context files into the prompt for Codex (codex has no --file equivalent)
- Add CodexEventConverter to normalize Codex JSONL output into Claude-compatible stream-json NDJSON format
- Implement infrastructure error detection for Codex runner (usage limits, auth failures, timeouts) to participate in agent rotation
- Remove dead collab remnants: popen/retry_with_next methods, test_collab.py, framework_patterns entries, and TIER_A_DIRS collab/tasks entry
## 8.11.3 - 2026-06-10

- Fix daemon-spawned flows failing with exit 127 when launched under systemd or other environments with an impoverished PATH
- Resolve login shell PATH at daemon startup and merge it into spawned child process environments
- Add streaming first-line extraction for history title parsing instead of reading entire JSONL files
- Cache history directory metadata by content signature to skip re-parsing unchanged directories during index rebuilds
- Switch incremental flow reads from whole-file line splitting to byte-offset-based partial reads
- Reduce daemon CPU usage from near-continuous full-core consumption to negligible levels during normal operation
## 8.11.2 - 2026-06-09

- Fix daemon event-loop starvation that caused webui-initiated flows to grey out and fail repeatedly by collapsing expensive per-tick full history-tree walks into a TTL-cached build_index with explicit invalidation on state changes
- Add BUILD_INDEX_TTL (3s) monotonic-clock cache to DaemonHistoryReader.build_index to prevent thread-pool worker saturation from repeated directory walks and JSON parses
- Add invalidate_index_cache() call sites in client push loop (on history_changed), spawn handler, and force-index-request handler to ensure fresh data when disk state changes
- Add _is_still_active() live single-engine.json re-check in read_active_flows to preserve active-flow liveness despite cached stale active flags
- Add regression test proving force-index path invalidates the TTL cache so newly-spawned flows are immediately visible
## 8.11.1 - 2026-06-09

- Fix the running-flow console reply box auto-collapsing the 'message details' block whenever a status update or websocket push triggered a re-render
- Preserve a manually expanded message-details block across automatic re-renders until the user collapses it themselves
- Reset the message-details expand state to default-collapsed only when the flow view is opened or closed, keeping it as a per-session UI preference
- Update the running-flow-console spec to document session-level, intervention-id-keyed persistence of the reply-context prompt expand state
- Add DOM-stub unit coverage verifying the expanded state survives re-render
## 8.11.0 - 2026-06-09

- Add an 'auto' option to the Web Console New Task 'Task type' form that submits the existing 'pending' auto-classification value and is selected by default, matching the CLI's pending default
- Retain the existing feature, bugfix, and other task-type options so users can still pick an explicit step sequence
- Fix malformed spacing of the mobile tool-marker Detail toggle by giving the button 3px vertical padding for readable breathing room
- Keep folded tool-marker chip height stable on mobile by shedding an equal 3px of vertical padding from the card, leaving desktop layout unchanged
## 8.10.3 - 2026-06-08

- Fix spec_gate steps dumping full raw pytest stdout/stderr to the web console; they now render a friendly summary card
- Add a dedicated web spec_gate report renderer showing gate conclusion (PASSED/FAILED), fallback route (update_spec/implement), and no-op skips
- Reuse the test step's summary-only rendering for spec_gate test_results, showing pass/fail counts, phase totals, and command instead of raw output
- Add a matching CLI spec_gate summary renderer so the terminal no longer dumps full test output
- Register spec_gate result fields so its structured records are recognized correctly across CLI and web
- Keep the spec_gate data layer unchanged so raw output stays reachable via the 'View raw' entry
## 8.10.2 - 2026-06-08

- Fix mobile portrait (max-width:600px) folded tool-call cards staying tall by suppressing the native button's intrinsic vertical floor (appearance:none) and centering the chip row, so the details toggle no longer stretches the card
- Keep the mobile tool-marker fix strictly scoped to the mobile breakpoint, leaving desktop tool-marker appearance unchanged
- Fix desktop user replies rendering twice by reconciling the optimistic local echo against the daemon's authoritative record, so each reply shows exactly once
- Preserve reply ordering and prevent reply loss when de-duplicating optimistic echoes against authoritative history
- Update frontend smoke checklist and tests to cover the mobile chip height and desktop single-reply render checks
## 8.10.1 - 2026-06-08

- Fix the tool-call card details toggle being inflated by the mobile touch-target rule, so the card no longer stretches on screens ≤600px
- Fix the idle reply-box placeholder wrapping on mobile by shrinking its font size and showing a shortened single-line prompt
- Tidy the discovery_confirm dock on mobile: remove the duplicate status head, restyle the expand toggle as a lightweight aligned text link, and align the chip, link, and confirm button
- Preserve desktop appearance and behavior unchanged — all fixes are confined to the ≤600px breakpoint or gated behind isMobilePortrait()
## 8.10.0 - 2026-06-05

- Show per-round and cumulative token usage on each interactive turn, so discovery and confirm now report consumption every round instead of only at completion
- Render a compact dim single-line footer ('本轮 X in / Y out · 累计 X in / Y out') inline at the tail of CLI assistant messages, preserving interaction continuity without the large reverse-video table
- Display per-round and cumulative token usage on web running-flow console assistant bubbles, with cumulative summed client-side per step
- Fix discovery cumulative token undercount where per-round step.outputs.clear() dropped carried_token_usage, so the terminal total now reflects the whole discovery run
- Only show usage footers on rounds/steps that actually invoked the LLM; empty-input redraws and --resume re-displays no longer fabricate a footer
- Persist per-call token_usage on assistant chat-history records (parsed from result NDJSON), remaining backward compatible with legacy records lacking the field
- Leave the existing non-interactive per-step 'Step Token Usage' tables unchanged
## 8.9.1 - 2026-06-05

- Fix step counter so a completed flow shows total/total (e.g. 13/13) with progress 1.0 instead of stopping one short, corrected in the engine so the daemon and all status consumers report it consistently
- Tile the call-type intervention chip onto the reply context header row on mobile portrait to reclaim vertical space
- Collapse the running tool-call chip to a single line with ellipsis when collapsed on mobile portrait, expanding to full detail only when toggled open
- Collapse the reply input box to a single line by default on mobile portrait so it no longer hides chat history, still growing WeChat-style up to ~35vh while typing
- Leave desktop layout and behavior unchanged; all WebUI fixes are scoped to the mobile-portrait breakpoint
## 8.9.0 - 2026-06-05

- Reclaim horizontal whitespace in the mobile-portrait flow chat area by removing the redundant history-record left stripe and indent and widening conversation bubbles to near-full width, with sender identity conveyed by bubble color alone
- Add a WeChat-style auto-grow reply textarea on mobile portrait that starts single-line, grows with content up to ~35vh then scrolls internally, and resets height on send, clear, or chip switch
- Compress tool-call markers to a single line on mobile portrait with ellipsis-truncated detail while keeping the expandable details panel fully functional
- Tile the docked reply meta row (TO/KIND/callid and the expand-message toggle) horizontally on mobile portrait to reduce vertical space usage
- Introduce the DOM-free pure helper `replyTextareaHeight` for testable auto-grow height computation, consistent with existing mobile state helpers
- Keep desktop appearance and the non-flow history-list view unchanged, scoping all refinements to the running-flow console under the 600px media query
## 8.8.0 - 2026-06-05

- Show the user's original input as the title in `se3 history` and the web UI history list, instead of the system-prompt-prefixed raw prompt
- Align history-list titles with the web chat view by reusing the USER_CONTENT marker logic (splitUserPromptByMarker equivalent)
- Add a three-tier title extraction: USER_CONTENT markers, then `Task description:` regex, then raw-content fallback
- Recompute flow titles live from the first jsonl at index-build time, so existing history self-heals with no data migration
- Add prompt_markers.extract_user_content, a pure Python helper mirroring the web console's marker-based extraction
## 8.7.1 - 2026-06-05

- Fix GET /api/history/{flow_id} returning 504 when flow history exceeded the default WebSocket frame size
- Raise the daemon↔server WebSocket inbound frame cap to 256 MiB so large MSG_HISTORY_DATA frames are no longer silently dropped
- Add a shared MAX_WS_MESSAGE_BYTES constant in protocol.py as the single source of truth for the frame cap on both ends
- Apply the cap via max_size on the daemon WebSocket client and ws_max_size on the server's uvicorn runtime
- Add regression tests covering the new constant and its propagation through both the daemon and server WebSocket setup
## 8.7.0 - 2026-06-04

- Enforce that the discovery step's Proposed Task Description (refined_description) is always a clean, finalized, zero-open-item executable description
- Forbid any to-be-confirmed/TBD/待确认/待定/undecided either-or phrasing inside refined_description across both initial and continue discovery prompts
- Route true blockers into questions, keeping discovery looping and out of the confirmation gate until all genuine open decisions are resolved
- Route non-blockers into refined_description as already-made decisions, with a changeable-default note surfaced in content for the user
- Document the new 'Discovery refined_description Clean-Final Invariant' requirement and scenarios in the requirement-intake spec
- Eliminate the case where a user is asked to confirm (type 1) a description that still carries unresolved items
## 8.6.1 - 2026-06-04

- Fix daemon connecting wss:// URLs without an explicit port to :8080 instead of :443, which caused silent 'not connected' failures behind TLS reverse proxies
- Make default WebSocket port completion scheme-aware: wss/https default to 443, ws/http keep defaulting to 8080
- Preserve existing behavior for URLs with explicit ports, custom paths (e.g. /ws), and IPv6 literals
- Fix se3 daemon status showing an empty reason: every connection failure (handshake, TLS/port mismatch, WELCOME rejection, missing websockets) now records a readable cause, with a 'reason unavailable — see daemon.log' fallback
- Correct the --server-url help text to describe scheme-aware port completion
- Add a wss reverse-proxy deployment section to daemon-and-server docs (and zh mirror) with nginx/Caddy examples and a curl --http1.1 101 handshake probe
## 8.6.0 - 2026-06-04

- Add a phone-portrait responsive layout (@media max-width:600px) covering all webui screens: login, Machines+Flows list, flow-view console, History, and all modals
- Introduce a top-bar overflow menu and single-view panel switches so two-pane interfaces stay fully usable on narrow screens
- Add an off-canvas sidebar drawer for the running flow-view console, letting the conversation fill the main column with no horizontal scroll
- Provide a touch-optimized docked reply area for interjection/call replies on mobile
- Render modals near-full-screen on phones for easier touch interaction
- Preserve full feature parity with desktop on mobile — no controls hidden or downgraded — while keeping the desktop layout unchanged via narrow-screen-scoped breakpoints
## 8.5.1 - 2026-06-04

- Fix daemon incorrectly showing as offline/'未连接' and not auto-recovering when creating a task in the webui, by offloading the status-snapshot build (including the historical project-root disk walk) off the asyncio event loop so heartbeats and SPAWN_FLOW handling are no longer stalled
- Throttle historical project-root enumeration with a 60s TTL cache so the full se3/history directory is no longer traversed on every status tick, while still merging active roots immediately and invalidating the cache when a new root is added
- Stop daemon.log flooding (previously growing to ~210MB) by deduplicating 'skipping unreadable meta/archive file' warnings per file path and demoting repeats to DEBUG
- Eliminate the need to manually restart the daemon to recover connectivity after task creation under large history sizes
## 8.5.0 - 2026-06-04

- Add GET /api/users to list owners with a strict field whitelist (no password or key hashes) and break-glass identities excluded
- Add DELETE /api/users/{owner_id} to remove a user with cascade cleanup of bindings, credentials, and keys
- Add POST /api/users/{owner_id}/password to reset passwords for local-provider users only (409 for OIDC/proxy-header users)
- Add POST /api/users/{owner_id}/admin to toggle a user's admin flag, backed by a new Store.set_admin persistence method
- Enforce server-side authorization on all user-management routes: non-admin 403, no self or last-admin delete/demote (atomic guard), break-glass owner hidden as 404
- Add an admin-only '用户管理' top-bar panel mirroring the daemon-key modal for listing, creating, deleting, resetting passwords, and toggling admin
## 8.4.0 - 2026-06-03

- Add a post-update_spec verification gate (mechanism A): after a flow edits spec.md, validate the spec structure and ensure requirements are not silently dropped, then re-run the full test suite so spec-content regressions are caught before commit.
- Route gate failures programmatically: invalid/parse-broken/requirement-losing artifacts go back to update_spec, while red tests enter the existing fix loop under code-first rules (fix the test, do not revert a legitimate spec change).
- Fold inherited baseline test failures into the bounded fix loop (mechanism B): pre-existing failures are now actively repaired with parallel, equal-priority instructions instead of merely surfaced.
- Add workflow.baseline_fix_max_attempts config option (default 3; 0 disables) governing an independent budget for baseline fixes, separate from max_fix_iterations.
- Persist per-test_id give-up memory (se3/state/baseline_fix_attempts.json) so genuinely unfixable baseline failures (environment, flaky, needs-human) are not retried every flow.
- Scope baseline-fix unlock narrowly: relaxed focus applies only to annotated baseline failures and never crosses se3 guardrail SHALL/MUST contracts; introduced failures keep the existing guardrails.
## 8.3.0 - 2026-06-03

- Document the 8.0.0 webui/central-server authentication flow end-to-end in docs/daemon-and-server.md and its Chinese mirror (server.auth providers, fail-closed startup, sqlite identity persistence, bootstrap-token break-glass admin, local user creation, owner daemon-key issuance, and owner-isolated visibility)
- Correct outdated 8.0.0 descriptions in the daemon-and-server docs: the central server now persists identity in ~/.se3/server.db, and the Web frontend requires login rather than being open-and-use
- Add a concise webui authentication overview to README.md and README.zh.md with a link into the daemon-and-server guide
- Generalize the doc-sync preset from README-only to reconciling all published user-facing documentation, treating a docs/ tree (or equivalent) as an additive layer that is a no-op when absent
- Preserve the README item-by-item reconciliation baseline (CLI surface, specs, structure, version-display correction without bumping) intact under the new generalized scope
- Add a report-only documentation-gap rule to doc-sync so uncovered subsystems are surfaced for human triage and never auto-created, keeping the pass deterministic and re-runnable
- Extend the localized-naming convention to docs pages (docs/<name>.<lang>.md) and keep all language variants in sync
## 8.2.0 - 2026-06-03

- Capture per-call LLM token usage (input/output, cache read, cache creation) and total cost from the Claude CLI result stream
- Aggregate token usage and cost at both step and whole-session granularity, merging retries, rotations, and two-phase extraction calls per step
- Display a per-step token/cost summary block and a session total block in the CLI with aligned, unobtrusive formatting
- Show a low-key per-step usage footnote and a running session usage badge in the running-flow web console
- Persist session token usage in engine state with backward-compatible loading of older engine.json files
## 8.1.0 - 2026-06-03

- Capture a deterministic test baseline before implement runs, freezing the set of pre-existing failures so introduced regressions can never be laundered into 'known' status
- Pre-warm the baseline as a background subprocess during analyze/plan/confirm, adding ~0 wall-clock; implement blocks only if the run is not yet ready
- Cache the baseline by git HEAD sha plus working-tree dirty hash and persist baseline_failures in flow state so --resume and parallel flows reuse it
- Stop scoped flows (e.g. doc-sync) from infinite-looping on inherited test failures: the test gate and verify_spec now block only on failures this session introduced
- Retire the auto-populated known_test_failures.json laundering vector; the inherited-vs-introduced exemption is now the measured baseline only
- Log inherited test failures and out_of_scope observations once (留痕) instead of re-filing duplicate issues every fix iteration
- Reconcile workflow.max_fix_iterations between se3.yaml and se3.local.yaml and log the resolved value and winning source at load time
## 8.0.0 - 2026-06-02

- Require authenticated, owner-scoped access for all webui/REST and daemon channels; remove the prior unauthenticated control-plane mode
- Default to fail-closed: refuse to serve when no usable auth provider is configured instead of running open
- Add a pluggable authentication layer with a built-in local multi-user provider (accounts, argon2-hashed passwords, server-side sessions) plus disabled-by-default OIDC and reverse-proxy-header seams
- Introduce an internal owner_id identity model with (provider, external_id) identity-bindings so daemons and machines are scoped to a trust domain
- Add the first persistence layer using embedded sqlite for owners, identity-bindings, local credentials, daemon-key hashes, and break-glass token hashes; machine/flow state stays in-memory
- Evolve the daemon HELLO protocol to carry a daemon key, binding each machine to its owner and rejecting unkeyed daemons with WELCOME(accepted=false)
- Add the `se3-server bootstrap-token` CLI to mint a one-time, hash-stored break-glass admin token for bootstrap and fail-closed recovery, plus owner-managed daemon-key issuance/revocation in the UI
- Add login/session UI, owner-scoped machine/flow/history views, and admin user provisioning (no public self-registration in v1)
## 7.11.4 - 2026-06-02

- Fix running-flow console docked reply box overflowing when an intervention prompt is long (e.g. discovery_confirm carrying a full refined task description), which previously pushed the textarea, options, and Send button off-screen
- Collapse the reply-context prompt body (消息详情) by default behind an expand/collapse trigger, since the prompt is already shown in the conversation stream above
- Cap the expanded prompt body at 30vh with an internal scrollbar so the header, options, textarea, and Send button stay visible and clickable no matter how long the prompt is
- Scroll the prompt body into view on expand and leave scroll position untouched on collapse, matching the view's foldable behavior
- Limit the change to the #flow-view docked reply box, leaving the history view and other interfaces unaffected
## 7.11.3 - 2026-06-01

- Fix incomplete 'View raw' coverage in the running-flow console so every conversation role (user/assistant/system/other) consistently exposes the original payload
- Add an always-available 'View raw' fold below the assistant inline thinking on no-result turns, falling back to the message content when no raw_json/raw_ndjson exists
- Make collapsed system and other-role chips always show 'View raw' instead of intermittently hiding it, dispatching by role to keep the user envelope fallback intact
- Keep DAG group-status markers and step report cards affordance-free, preserving the shared makeRawToggle 'no raw payload returns null' contract for non-conversation UI
- Add targeted frontend and server-render tests covering both the raw-payload and content-fallback cases and asserting synthetic non-conversation UI stays button-free
- Update the running-flow-console spec with a Universal View-Raw for Conversation Messages requirement codifying the unified principle
## 7.11.2 - 2026-06-01

- Fix the Web console implement step Summary rendering each group as 'GNaN' instead of G1…Gn
- Make reportList pass the 0-based iteration index to its formatItem callback, restoring CLI field parity with step_renderers.py
- Degrade Summary numbering to plain 1…n ordinals when implemented_groups is empty
- Sweep app.js for other NaN/undefined render points and confirm reportList's Summary callback was the only affected site
- Add frontend pure-function regression tests covering correct index threading and both Summary numbering branches
## 7.11.1 - 2026-06-01

- Fix missing 'View raw' (Layer 3) on user turns in the running-flow console: user records now stably expose their original .jsonl envelope record even when no second-layer raw payload (raw_ndjson/raw_json) is present
- Make the user-prompt expand toggle always available so the Layer 3 raw view is consistently reachable, including for empty user-content chips
- Fix excess blank space below collapsed tool-call chips by collapsing the entire details wrapper instead of only its inner body, removing the stray full-width empty row and margin
- Preserve the shared makeRawToggle 'no raw payload returns null' contract and leave assistant no-result turns and group_status markers unchanged (by design)
- Add targeted frontend and server-render tests covering user-turn Layer 3 reachability and the folded chip layout fix
## 7.11.0 - 2026-06-01

- Show real-time per-group status (queued/running/completed/failed/skipped) in the running-flow web console during DAG parallel implement, instead of leaving the view blank until step end
- Add an optional on_group_status callback to the DAG scheduler, fired at each group lifecycle transition with exceptions swallowed and omission preserving prior behavior
- Persist per-group status as group_status NDJSON lines in the main-repo step history so the daemon's active_flow_signature changes and pushes incremental updates before the step completes
- Render affordance-free per-group status markers in the implement section of the web UI in strict chronological order, without replacing the full G1–G5 conversation salvaged at step end
- Exclude group_status records from get_step_history and retry context so CLI output and retry behavior are unchanged
## 7.10.1 - 2026-06-01

- Rebuild VERSIONS.md into a single reverse-ordered changelog using the unified `## <version> - <date>` heading and bullet format
- Merge and deduplicate the three overlapping Version History tables and the Current Version prose into one continuous list
- Backfill a complete version history from 5.1.0 to 7.10.0 with tag-derived dates and git-log-traceable entries
- Preserve the Pre-1.0 Development History as a separate, format-cleaned section
- Translate all remaining Chinese spec prose to en-US (spec_language) while preserving Requirement/Scenario structure and technical symbols verbatim
- Align test_item_loading_e2e.py to the translated flow-engine requirement names
## 7.10.0 - 2026-05-30

- Establish a single authoritative code-first / spec-assistant role definition and inject it into discovery, analyze, and plan prompts so specs are treated as a read-only reference to current code rather than a driver
- Demote 'Available Specifications' in discovery to a read-only reference of current code state and forbid discovery from proposing new or rewritten specs (deferred to update_spec / se3 sync)
- Inject language instructions into the se3 sync spec-writing paths (sync_engine, sync_discovery, sync_analyzer) so regenerated specs honor language.spec_language
- Strengthen language-instruction wording to preserve technical symbols (code identifiers, command and API names, paths) verbatim and to treat spec_language as authoritative for the spec body
- Add a repository-level anti-regression guardrail test that scans prompts and docs for curated spec-driven framing phrases while excluding compliant terms (contract, source of truth, two-way governance)
- Clean up residual spec-driven framing in README.md, README.zh.md, and base/spec.md while preserving the compliant asymmetric within-flow drift-guard wording
- Converge verify_spec and merge guardrails wording to a within-flow drift-prevention framing without making the spec authoritative over code

## 7.9.1 - 2026-05-29

- Pause on step failure through both channels: always write the retry_decision call file and emit FLOW_PAUSED so the web console shows the failure and Retry/Skip/Abort, with the CLI prompt and web response racing whoever-first-wins under a TTY
- Prune dangling depends_on edges when disaster recovery drops an already-pre-merged group, and skip rather than crash on unknown dependencies during DAG build
- Validate the leaf-merge target branch ref before merging to avoid the opaque 'not something we can merge' error
- Retain the history cursor for terminal flows on final flush so --resume reconnects incremental push instead of freezing the web console on the failure snapshot
- Batch/throttle os.fsync in the merge llm_trace writer to reduce dirty-page write-back stalls under parallel streaming writes
- Preserve existing non-interactive (daemon/CI/pipe) pause-and-decision behavior unchanged

## 7.9.0 - 2026-05-29

- Wire DocumentationUpdater into the commit step so README.md and VERSIONS.md are kept current, and add the preset-prompts mechanism
- Sync README.md and README.zh.md with the current CLI, specs, and design philosophy

## 7.8.0 - 2026-05-29

- Add the web console interjection lifecycle: route mid-run interjections from the browser to the running flow and drain them at step boundaries, including while the flow is PAUSED
- Persist interjection/history state across the daemon↔server WebSocket lifecycle so interjections survive reconnects

## 7.7.0 - 2026-05-28

- Add a generic outputs fallback renderer and reposition the tool-call chip toggle in the running-flow console
- Unify narrative tool-call rendering through a shared helper
- Fix daemon retry_decision call visibility and exclude already-consumed CLI/webui calls from pending
- Harden assistant JSON region collection with a string-aware scanner and fix the bare-JSON guard skipping non-JSON fence bodies
- Replace the History project tab bar with a select dropdown

## 7.5.0 - 2026-05-27

- Group History panel sessions by project_root via a tab bar
- Resolve the spawned se3 command from the sys.executable sibling first (same-prefix), falling back to `python -m se3` then PATH
- Offload daemon history I/O to worker threads and raise the history pull timeout to keep the daemon responsive on large sessions

## 7.4.0 - 2026-05-26

- Unify tool-call display into a single chip with collapsible details
- Align the `_call_extract` contract with `_call_two_phase` to fix sync JSON extraction
- Align live-stream tool events with the final-state bracket markers

## 7.3.0 - 2026-05-26

- Merge same-turn stream fragments into a single live assistant bubble
- Persist project roots so history and the New Task form work even with no live run in progress

## 7.2.0 - 2026-05-26

- Stream live step progress to the web console, split history empty/loading/connected states, and dedupe the reply context

## 7.1.0 - 2026-05-26

- Add a history refresh hint and two-layer assistant rendering
- Fix running-flow console rendering to match the message-paradigm spec
- Fix the sync new-spec write path and enforce read-only spec generation
- Fix the approved-only confirm check and bound cross-revision review

## 7.0.0 - 2026-05-26

- BREAKING: Add a critical-test gate, rework the summarize step, and drop B-class discovery
- Rebuild the running-flow web console with CLI-equivalent session reading, chip-bar reply, and flow-scoped pending calls
- Add the daemon, central server, and pluggable event-stream sinks, plus web session history views and a discovery start option
- Inject an authoritative step_type and mirror CLI discovery to the web console; add the three-segment marker protocol and discovery JSON renderer
- Add the discovery_confirm intervention and per-step terminal report cards
- Add persistent incremental sync with three-level skip and obsolete-spec cleanup
- Inject se3 runtime environment capabilities into LLM prompts and expose daemon connection state and project_roots

## 6.2.0 - 2026-05-15

- feat: Replace `typer.prompt()` with multiline input in `se3 issue create`, and fix analyze `selected_items` extraction

## 6.1.0 - 2026-05-14

- Add sync `--validate-only` / `--resume`, the sync checkpoint, and the standalone spec validator

## 6.0.0 - 2026-05-14

- BREAKING: Refactor sync into a one-directional (code → spec), run-to-convergence flow
- Remove the `selected_specs` output and the dead READ_SPEC code path
- Auto-recover stale unmerged-index state in the DAG leaf merge
- Include flow_id, history path, and refined description in discovery context

## 5.3.0 - 2026-05-13

- Rotate the agent on any LLM call failure, not only infrastructure errors
- Add the reverse-color block title/footer rendering convention
- Preserve `pre_session_version` and rebase `session_commits` on re-entry
- Forward `project_root` from LLMCaller to ClaudeCodeRunner

## 5.2.0 - 2026-05-12

- fix: Isolate the claude subprocess from the target project's `.claude/settings.json` via `--setting-sources` (default `user`); new `claude_subprocess.setting_sources` config as escape hatch

## 5.1.0 - 2026-05-12

- Make `suggested_version` authoritative and drop the `bump_rules` config
- Refactor merge conflict resolution to an LLM-as-editor model
- Add the agent-safety spec for LLM self-targeting process cleanup and inject process-cleanup safety guidance into implement prompts
- Replace outer Panel borders with markdown headings in renderers
- chore: Switch the license from MIT to Apache-2.0

## 3.22.0 - 2026-03-06

- feat: Restore the `se3 init` command. Align all specs with 3.0 CLI reality.

## 3.21.0 - 2026-03-06

- BREAKING: Remove deprecated commands for 3.0. Only `se3 run` remains as the unified entry point.

## 3.20.1 - 2026-03-06

- fix: Fix loop now creates new implement steps per iteration. Fixed REVISION_NEEDED transition handling.

## 3.20.0 - 2026-03-05

- feat: Test-verify-fix loop. Auto-routes to implement on test failure with `max_fix_iterations` config.

## 3.19.4 - 2026-03-05

- fix: Confirmation steps preservation after analyze. Fixed step sequence overwrite bug and revision transition logic.

## 3.18.9 - 2026-03-04

- fix: Discover mode JSON parsing with tool calls. Cleans `[Tool Call: ...]` previews and handles markdown blocks correctly.

## 3.18.8 - 2026-03-04

- fix: Correct bump rules per SemVer 2.0.0. Small/docs/test/chore all bump patch.
- small: patch (small fixes like typo corrections are still fixes)
- docs: patch (documentation fixes)
- test: patch (test additions/fixes)
- chore: patch (maintenance tasks)
- Only 'review' mode uses 'none' (no code changes)

## 3.18.7 - 2026-03-04

- fix: `_get_task_type` returns string. Fixes version bump for small/review/directive task types.

## 3.18.6 - 2026-03-04

- fix: Commit step version bump fix. Properly skip bump for 'none' bump type tasks (small, review, directive).

## 3.18.5 - 2026-03-04

- fix: Implement step code validation and version bump fix. Prevents file corruption from LLM descriptive text, small/review/directive tasks no longer bump version.

## 3.18.4 - 2026-03-04

- feat: `se3 init` now creates VERSIONS.md and README.md. Auto-generates initial project documentation with version tracking.

## 3.18.3 - 2026-03-04

- fix: Add missing WHEN/THEN scenarios to spec requirements. Fixed spec lint errors in se3-commands (7), se3-workflows (1), and test-project (1) specs.

## 3.18.2 - 2026-03-03

- feat: Discovery mode with project context. Provides project info (type, name, git), specs list, and base spec content to help exploration.

## 3.18.1 - 2026-03-03

- fix: Allow user confirmation at max discovery rounds. Fix boundary case where confirmation at round 10 would trigger fallback.

## 3.18.0 - 2026-03-03

- feat: Add Discovery Workflow for requirements exploration. New `discovery` step type with multi-turn conversation support. New `--discover` / `-d` CLI flag. Supports pause/resume, max 10 rounds, refined description passed to analyze.

## 3.17.6 - 2026-03-03

- fix: Add newline after each assistant message to separate text/thinking content from subsequent output.

## 3.17.5 - 2026-03-03

- feat: Stream full text and thinking content in real-time during LLM calls. Text content streams directly, thinking content streams in gray italic style.

## 3.17.4 - 2026-03-03

- fix: Fix JSON parsing bug in EXTRACT and TWO_PHASE modes. Return plain JSON string instead of stream-json wrapper. Remove wall time timeout from all LLM calls, use only inactivity timeout (30 minutes).

## 3.17.0 - 2026-03-03

- feat: Three JSON extraction modes (STRICT/EXTRACT/TWO_PHASE) for LLM output handling. STRICT mode forces JSON with retry. EXTRACT mode uses LLM extraction on failure without retry. TWO_PHASE mode uses natural generation + LLM extraction for large outputs. Summarize and project_summary steps now use plain text output instead of JSON. Updated flow-engine spec with JSON mode documentation.

## 3.16.0 - 2026-03-03

- feat: Implement full-content display system for LLM outputs. New display.py module with render_full(), render_proposal(), render_design(), render_spec_content(). New output.py module with truncation-free formatting. Updated run.py and cli.py to use full-content display.

## 3.15.0 - 2026-03-03

- feat: Remove wall timeout limit and extend inactivity timeout to 30 minutes for se3 run. Fix version display and commit step output propagation.

## 3.14.0 - 2026-03-02

- feat: Fix retry context inheritance in implement step. Add complete version management spec and base_spec template. Add --bump flag to se3 commit command.

## 3.13.0 - 2026-03-02

- feat: Add DocumentationUpdater class for README.md and VERSIONS.md updates. Add version bump integration in commit step. Add load_session_config, load_confirmation_config, load_claude_commands, get_language_labels, is_chinese_language functions to config.py.

## 3.12.0 - 2026-03-02

- fix: Chat history retry context now preserves full conversation structure. Extracts tool calls and results from raw NDJSON instead of using simplified summaries. Each user message has independent tool_results. Supports both snake_case and camelCase field names. Proper field access with defaults to prevent KeyError.

## 3.11.0 - 2026-02-28

- feat: Add version bumper framework with auto-bump on se3 run commit step. Supports pyproject.toml, package.json, version.py, setup.py. Task type -> bump level mapping (feature->minor, bugfix->patch, breaking->major). Fix JSON parser truncated response handling. Fix Version dataclass order conflict.

## 3.10.1 - 2026-02-27

- fix: Commit step now handles None values in `changes_made` and `proposal` inputs using `or {}` fallback. Prevents AttributeError when previous steps don't produce these outputs.

## 3.10.0 - 2026-02-27

- feat: `se3 run` without arguments enters interactive multiline input mode. Supports paste detection (>3 lines abbreviates display). Fix task group handling for directive tasks: create default task group when PLAN_TASKS is skipped, create minimal design doc when DESIGN is skipped. Fix undefined variable bug in plan_tasks.py.

## 3.9.0 - 2026-02-27

- feat: Task group architecture for plan_tasks and implement steps. plan_tasks now generates `task_groups` (logical task groups with group_id, name, description, group_order, depends_on). implement step executes each group in separate LLM call with isolated context - same base context (design_doc, proposal, project_context) but only group's tasks. Each group has independent retry mechanism with max_retries. Sequential execution with immediate file application per group. Backward compatible with legacy task_list format. Updated STEP_POOL definitions for plan_tasks and implement.

## 3.8.0 - 2026-02-27

- feat: Confirmation steps with configurable human or LLM review. New `CONFIRM` step type inserted after `propose`, `design`, and optionally `plan_tasks`. Human reviewer mode creates MCP call files and listens for file edits or CLI input (y=approve, n=abort, anything else=revision feedback). LLM reviewer mode uses separate LLM calls to evaluate completeness, spec compliance, and maintainability. Supports review loop: if changes requested, flow returns to original step with feedback for revision. New `confirmation` config in se3.yaml with `enabled`, `steps`, `reviewer`, `llm_reviewer` options. Updated propose/design/plan_tasks handlers to support revision mode. 3 new step handlers, state machine supports review iteration tracking.

## 3.7.0 - 2026-02-27

- feat: Chat history system. New `ChatMessage`/`ChatSession` data model records every LLM prompt and response to `se3/history/{flow_id}/{step_id}.jsonl`. LLMCaller now accepts `flow_id`/`step_id`/`step_type` and automatically records all calls. On retry, previous conversation context is injected into the prompt. New `se3 history` CLI command for browsing chat history (list flows, view step conversations, JSON/text output). All 10 LLM-using step handlers updated. 24 new tests. Updated flow-engine and se3-commands specs.

## 3.6.0 - 2026-02-27

- feat: Base spec mechanism. New `se3 init` command generates project structure and `se3/specs/base/spec.md` from template. `read_spec` step auto-loads base spec before LLM selection, ensuring all flows have access to project-level conventions. New `src/se3/templates/` package with base spec skeleton. 9 new tests.

## 3.5.0 - 2026-02-27

- feat: LLM-driven read_spec replaces keyword matching. New PROJECT_SUMMARY step generates project context summary for downstream LLM steps. New `se3 summary` CLI command. ProjectContextCollector collects git, flow engine, backlog, specs. Migrated roadmap.md to se3/specs/_backlog/ (9 backlog items). Updated propose/design prompts with project context.

## 3.4.11 - 2026-02-26

- feat: improved spec matching with expanded keywords. Add comprehensive keyword-to-spec mapping covering all 17 specs (agent-team, se3-commands, se3-config, etc.). Add word boundary matching to prevent partial matches (e.g., "about" matching "agent"). Add fallback content-based matching when keyword matching finds no results.

## 3.4.10 - 2026-02-26

- fix: se3 run hangs due to stdin inheritance. Add `stdin=subprocess.DEVNULL` to prevent subprocess from waiting for input. Add `require_json` parameter to LLMCaller.call() with automatic JSON format retry logic. When LLM doesn't return valid JSON, automatically prompt it to retry with JSON format.

## 3.4.9 - 2026-02-26

- feat: real-time stream-json progress output in se3 run. Add StreamJSONTracker class to process each NDJSON line immediately, printing live progress including text chunks, tool calls, and tool results. Users can now see Claude Code's progress in real-time instead of waiting for all output.

## 3.4.8 - 2026-02-26

- fix: simplify stream-json parsing logic. Parse NDJSON line by line, collect text from assistant messages only. Remove redundant extraction functions. All steps now work correctly with stream-json format.

## 3.4.7 - 2026-02-26

- fix: add `--verbose` flag required for `--output-format stream-json`. Claude CLI requires verbose mode when using stream-json format. Fix exit code 1 error.

## 3.4.6 - 2026-02-26

- fix: extract only assistant message text from stream-json. Remove `result` type extraction to avoid appending non-JSON text to valid JSON responses. Fix JSON parsing when result summary was being appended to assistant's JSON output.

## 3.4.5 - 2026-02-26

- fix: handle single-line stream-json format. Remove `len(lines) < 2` check in `_extract_from_stream_json()` to properly parse single-line stream-json events. All JSON parser test cases now pass.

## 3.4.4 - 2026-02-26

- feat: properly implement stream-json parsing. Add `_extract_from_stream_json()` to extract text content from stream-json format. Rewrite `parse_json_response()` to handle stream-json, NDJSON, and single JSON formats correctly.

## 3.4.3 - 2026-02-26

- fix: remove `--output-format stream-json` due to parsing compatibility issues. Revert to standard text format which works correctly with existing JSON parsing. Add debug logging for response diagnosis.

## 3.4.2 - 2026-02-26

- fix: handle NDJSON (newline-delimited JSON) format from `--output-format stream-json`. Add `_parse_ndjson()` function to extract valid JSON from stream output. Fix JSON parsing error that caused "Failed to parse LLM response".

## 3.4.1 - 2026-02-26

- feat: add `--output-format stream-json` and `--verbose` to Claude CLI calls for streaming JSON output. Change retry behavior: exit immediately on max retries reached instead of prompting user.

## 3.4.0 - 2026-02-26

- feat: simplify version management — single source of truth from pyproject.toml, dynamic version loading in `__init__.py`, remove duplicate SE3_FRAMEWORK_VERSION definition. feat: add step execution timing display in `se3 run` (shows duration in seconds after each step completes). Update version checks in commit, version, and utils modules to use pyproject.toml.

## 3.3.6 - 2026-02-26

- feat: centralized robust JSON parsing utility in `src/se3/engine/utils/json_parser.py`. Refactor all 8 step handlers (analyze, propose, design, plan_tasks, implement, verify_spec, summarize, update_spec) to use the centralized JSON parser, improving resilience against malformed LLM outputs.

## 3.3.5 - 2026-02-26

- fix: add missing `from pathlib import Path` in propose.py and plan_tasks.py, fixing NameError on `Path.cwd()` during step execution.

## 3.3.4 - 2026-02-26

- fix: enforce step dependency constraints — auto-insert PROPOSE before DESIGN when LLM's analyze step omits it, preventing "No proposal available" error. Fix missing Path import in design.py.

## 3.3.3 - 2026-02-26

- feat: LLM response summary display (JSON keys, size, duration) after every LLM call in llm_caller. Two-layer Ctrl+C: first interrupts step and prompts for extra instruction to inject into retry, second saves state and exits.

## 3.3.2 - 2026-02-26

- feat: summarize step now prints formatted summary to terminal including work summary, key changes, files modified, testing status, remaining work, and suggested next steps. Summary is still saved to se3/state/summary-{flow_id}.json for persistence.

## 3.3.1 - 2026-02-26

- fix: get_project_root() now also checks for se3.yaml/se3.config.yaml to find project root when .git is not available (fixes SSH execution). Improve JSON parsing in all step handlers (analyze, design, implement, plan_tasks, propose, summarize, update_spec, verify_spec) to extract JSON from LLM responses that include extra text before or after the JSON object.

## 3.3.0 - 2026-02-26

- refactor: SE3 3.3 pure CLI architecture. Delete output/ directory (no more template distribution). Remove init/update/sync commands (no more .claude/ writes). Consolidate .se3/ + specs/ + se3/ into unified se3/ directory. Rename se3.config.yaml to se3.yaml with legacy fallback.

## 3.2.0 - 2026-02-26

- refactor: Migrate SE3 spec system from openspec/ to native specs/ directory. Engine reads specs/ first with openspec/specs/ fallback. Remove openspec CLI dependency from start/init. Fix pre-existing test_claude_runner failures.

## 3.1.1 - 2026-02-25

- fix: 4 se3 run bugs. (1) Convert run from add_typer to @app.command for correct --type after positional arg. (2) Add max_retries check + EOFError defaults to Abort. (3) dashboard/status read engine.json instead of globbing flow_*.json. (4) LLMCaller strips CLAUDECODE env var. Doc: fix state file path in run.md.

## 3.1.0 - 2026-02-25

- feat: Mark all 2.x commands as deprecated with migration guidance to `se3 run`. Add `se3 dashboard` and `se3 status --log` commands. Flow engine status detection in diagnostics. New output specs (run, sync, verify, guardrails, handoff). Clean up stale openspec changes.

## 3.0.0 - 2026-02-24

- feat: SE3 3.0 flow engine. State machine driven workflow replaces prompt-driven agent. 11 step handlers (analyze→summarize), LLMCaller with retry, JSON state persistence, structured logging, spec index, ContextBuilder. Unified `se3 run` entry point (new/resume/loop). 41 tests. Architecture: .claude/ SE3 spec installation no longer needed.

## 2.23.1 - 2026-02-20

- fix: Cherry-pick improvements from loop branches. Stash uncommitted changes during branch creation, fix typer.Exit handling, proper worktree validation via git worktree list, auto mode in loop collab.

## 2.23.0 - 2026-02-20

- feat: Auto-merge loop branch with Claude after all iterations complete. Spawns Claude process to handle merge and conflict resolution automatically. Falls back to manual instructions on failure.

## 2.22.7 - 2026-02-20

- Fix: Version substring matching bug in documentation consistency check. Uses regex with negative lookbehind/ahead to ensure distinct version matching.

## 2.22.6 - 2026-02-20

- Fix: Non-blocking documentation checks now catch missing VERSIONS.md.

## 2.22.5 - 2026-02-20

- Fix: Unify documentation check logic across all commands and prevent --skip-commit bypass of documentation requirements.

## 2.22.4 - 2026-02-20

- Fix: Correct version.py get_readme_versions function and add documentation consistency check to se3 done command.

## 2.22.3 - 2026-02-20

- Fix: Strengthen README version consistency checks. VERSIONS.md reference now required when VERSIONS.md exists; README version check is now unconditional.

## 2.22.2 - 2026-02-20

- Fix: Correct version_updated detection in se3 commit. Now checks for +/- prefix in diff lines to distinguish actual version changes from context lines.

## 2.22.1 - 2026-02-20

- Fix: Handle None current_version in commit check to prevent TypeError when version extraction fails. Adds explicit check for missing version extraction.

## 2.22.0 - 2026-02-20

- Feature: README.md update check is now mandatory (blocking) when framework files change. Checks for inline version history and version reference.

## 2.21.0 - 2026-02-20

- Feature: Move version history to VERSIONS.md, enforce VERSIONS.md update in `se3 commit` (blocking check). Simplified README.md.

## 2.18.6 - 2026-02-20

- Fix: Collab base_branch propagation in all modes (foreground, manual, daemon). Ensures task branches correctly inherit base branch from loop branch.

## 2.18.5 - 2026-02-20

- Fix: Collab base_branch correctly set when creating task branches from loop branch.

## 2.18.4 - 2026-02-20

- Fix: `merge_loop_branch` now handles dirty working tree (warns user to commit/stash), restores original branch after merge, and improved `infer_loop_branch_base` with precise ancestor detection.

## 2.18.3 - 2026-02-20

- Fix: Ensure collab task branches correctly merge to loop branch by checking out base branch before merge. Simplified base branch inference logic.

## 2.18.0 - 2026-02-20

- Feature: SE3 Loop branch isolation and merge support. Each loop session creates a dedicated branch (se3-loop/{timestamp}) for isolation. Collab tasks branch from the loop branch. Added `se3 loop --merge <branch>` command to merge loop branch back.

## 2.17.1 - 2026-02-20

- Fix: `se3 handoff` now auto-commits `progress.md` changes. Previously progress.md was updated but left uncommitted.

## 2.17.0 - 2026-02-20

- **Version correction**: `.se3/` → `se3/` was incorrectly marked as breaking; this was a temp fix, not API change. Added: `se3 health` command, `--strict`/`--archive` flags, auto health checks in `se3:done`/`se3:work`. Fix: loop summary timeout 60s→300s.

## 2.16.4 - 2026-02-20

- Fix: Increase loop inter-iteration summary timeout from 60s to 300s.

## 2.16.3 - 2026-02-20

- Feature: Auto-run OpenSpec health checks in `se3:done` and `se3:work` commands.

## 2.16.2 - 2026-02-20

- Feature: Add `--strict` and `--fail-on-warning` flags to `se3 health` command for stricter integrity checks.

## 2.16.1 - 2026-02-20

- Feature: OpenSpec integrity improvements - name validation, format checks, stale change detection.

## 2.16.0 - 2026-02-20

- Feature: Add `se3 health` command for OpenSpec integrity monitoring with directory structure, zombie changes, and naming convention checks.

## 2.15.0 - 2026-02-20

- Change: SE3 runtime directory moved from hidden `.se3/` to visible `se3/` for human-as-MCP discoverability. Add `se3 migrate` command for migration.

## 2.14.1 - 2026-02-19

- Test: add comprehensive tests for `se3 loop` stdin prompt delivery and Ctrl-C supplemental mode.

## 2.14.0 - 2026-02-19

- Feat: `se3 loop` Ctrl-C supplemental mode now interrupts Claude and restarts with updated prompt. Press Ctrl-C once to enter supplemental mode, type your additional prompt, and Claude restarts immediately with the new context. Empty input continues without changes.

## 2.13.2 - 2026-02-19

- Fix: `se3 loop` uses `start_new_session` to isolate Claude from Ctrl-C signals.

## 2.12.19 - 2026-02-19

- Fix: Add missing `meta` and `off-topic` intent classification to `se3 start`. These intent types were defined in openspec but not implemented in the classifier.

## 2.12.18 - 2026-02-19

- Fix test expectations in `test_fullcycle.py`. `sanitize_change_name` correctly handles empty strings (fallback to `loop-{timestamp}`) and slashes (converted to hyphens for filesystem safety).

## 2.12.17 - 2026-02-19

- Fix: Use `max_tasks_per_change` from config instead of hardcoded value.

## 2.12.16 - 2026-02-19

- Iteration 31: Comprehensive project review. Fixed test expectation mismatches in fullcycle tests. All 207 tests pass.

## 2.11.0 - 2026-02-19

- Add `se3 loop --no-summary` flag. Iteration summary is now enabled by default — Claude Code summarizes each iteration and passes it to the next. Use `--no-summary` to disable.

## 2.10.8 - 2026-02-18

- Fix `se3 loop`: add `--verbose` flag required for `--output-format stream-json` mode.

## 2.10.7 - 2026-02-18

- Refactor `se3 loop`: eliminate bash script generation, run claude directly in Python with real-time stream-json rendering. Simpler and more reliable.

## 2.10.5 - 2026-02-18

- Fix `se3 loop`: use subshell with `set +m` to properly disable job control for wrapper scripts (kclaude) that spawn claude.

## 2.10.4 - 2026-02-18

- Fix `se3 loop`: add `set +m` to disable job control, preventing claude processes from being stopped (T state) in pipeline.

## 2.10.3 - 2026-02-18

- Fix `se3 loop`: add `--print` flag for proper `--output-format stream-json` output, matching collab launcher configuration.

## 2.10.2 - 2026-02-18

- Fix `se3 loop`: use correct `--output-format stream-json` instead of invalid `--stream-json` flag.

## 2.10.0 - 2026-02-18

- Add stream-json renderer to `se3 loop`. Replaces `--print` with `--stream-json | python3 renderer` for real-time visibility of Claude's thinking, tool calls, and progress. Zero external dependencies.

## 2.9.0 - 2026-02-18

- Add SE3 1.x features: Input Classification & Stage Routing (`se3 start -i`), Spec Guardrails (`se3 guardrails` command), full shutdown protocol with spec scenario verification and archive support.

## 2.8.1 - 2026-02-18

- Refactor `se3 loop` - exclusive execution is now the default. Removed manual mode. Generates bash while-loop script, takes over terminal, auto-executes all iterations. Removed `--exec` flag (no longer needed).

## 2.8.0 - 2026-02-18

- Add `se3 loop --exec` exclusive execution mode. Auto-generates bash while-loop script, takes over terminal, runs Claude Code for each iteration automatically. Supports all loop flags including `--iterations`, `--quick`.

## 2.7.0 - 2026-02-18

- Add `se3 loop` command for running SE3 workflow repeatedly. Creates a new change for each iteration, tracks progress via `.se3-loop-state.json`, continues from interruption. Default 10 iterations, supports `--iterations`, `--quick`, and `--reset` flags.

## 2.6.1 - 2026-02-18

- Fix collab mode detection in `se3 done` and `se3 handoff`. Interactive sessions no longer incorrectly detect as collab agents when `.collab/config.json` exists. Only `SE3_AGENT_ROLE` env var indicates collab mode.

## 2.6.0 - 2026-02-18

- Add "Interpretation & Recommendations" section to human calls. Explains what the call is about, how to handle it, and how to respond. Supports Chinese and English.

## 2.5.0 - 2026-02-18

- Add `check-human-responses.py` script for optimized human call detection in collab mode. Fixes issue where orchestrator couldn't detect human replies.

## 2.4.0 - 2026-02-18

- Add `se3 full-cycle` command — runs complete start-work-done workflow in one command. Supports `--quick` flag for small tasks using 'small' workflow. Streamlines simple/quick tasks by combining session initialization, change creation, implementation, and shutdown.

## 2.3.0 - 2026-02-17

- Merge CLAUDE.md into SE3.md, remove CLAUDE.md.template. Session Guard and simplified templates for 2.x manual trigger mode.

## 2.2.0 - 2026-02-17

- Add Session Guard mechanism. `se3 work` and `se3 done` now check if session was started via `se3 start`. If not, return error with prompt to start session first. Simplified SE3.md.template and CLAUDE.md.template to reflect "manual trigger" philosophy (no longer expecting agent自觉性).

## 2.1.0 - 2026-02-17

- Add Chinese language guidance to human calls context content. When language is set to Chinese (zh*), agent calls now include a note prompting humans to respond in Chinese, ensuring the entire interaction is localized.

## 2.0.0 - 2026-02-17

- **BREAKING**: Programmatic workflow driver architecture. SE3.md reduced to ~80 lines (principles + entry points). All workflows encoded in CLI (`se3 start`, `se3 work`, `se3 done`) returning JSON actions arrays. Skills (`/se3:start`, `/se3:work`, `/se3:done`) are thin wrappers. Eliminates agent-interpreted prose in favor of CLI-driven execution.

## 1.10.0 - 2026-02-17

- Tool-enforced progress tracking: `se3 commit` auto-appends to progress.md, `se3 handoff` generates session summaries, `se3 status` computes live state (removes status.md dependency), collab do_complete generates reports, worker FINDINGS.md convention

## 1.9.0 - 2026-02-17

- Add `se3 handoff` command — enforces commit-before-handoff rule, supports both direct usage (auto-commits) and collab mode (creates human-call)

## 1.8.8 - 2026-02-17

- Fix collab: archive old human-calls on startup, fix JSON extraction from manager, fix escalation output to stdout, limit manager turns to prevent verbose analysis loops

## 1.8.7 - 2026-02-17

- Fix collab: claude_runner temp .prompt files leaked due to unreachable cleanup code

## 1.8.6 - 2026-02-17

- Fix collab: orchestrator no longer exits when escalation happens before tasks exist

## 1.8.5 - 2026-02-17

- Fix: se3 update no longer writes back to output/SE3.md.template, preserving one-way data flow

## 1.8.4 - 2026-02-16

- Fix collab: detect_usage_limit() only checks last 3000 chars / 20 lines to avoid false positives from source code

## 1.8.3 - 2026-02-16

- Fix collab: detect_usage_limit() returns False when returncode=0 to avoid false positives from source code content

## 1.8.2 - 2026-02-16

- Fix collab: Resolve git merge conflict in claude_runner.py docstring

## 1.8.1 - 2026-02-16

- Fix framework version management: update SE3_FRAMEWORK_VERSION to 1.8.1, add version history entry

## 1.7.8 - 2026-02-16

- 优化 human calls 检测和处理机制：使用文件系统事件和缓存提升变更检测效率，增强响应完整性检查（重复内容检测、结构验证、长度限制），改进处理流程（批量处理、状态管理优化）

## 1.7.7 - 2026-02-16

- Fix collab: set max-turns to 0 (unlimited) instead of arbitrary limits; rely on timeout for control

## 1.7.6 - 2026-02-16

- Fix collab: increase manager max-turns from 3 to 10 (was hitting limit when reading files for planning)

## 1.7.5 - 2026-02-16

- Fix collab: use `@file` syntax for prompt passing to avoid CLI parsing issues (manager/worker launchers)

## 1.7.4 - 2026-02-16

- Fix collab: use `--output-format stream-json --verbose` for real-time output (enables activity-based timeout)

## 1.7.3 - 2026-02-16

- Fix collab: `-p` is a flag not an option; pass prompt as se3 handoff needs auto-commit progress.md changes

## 1.7.2 - 2026-02-16

- Fix collab: remove bash alias support (too complex), remove error_max_turns from usage limit detection

## 1.7.1 - 2026-02-16

- Fix collab: support bash aliases/functions for claude commands (use bash -i)

## 1.7.0 - 2026-02-16

- Fix collab: skip missing commands (exit 127), improve JSON extraction from Claude envelope

## 1.6.9 - 2026-02-16

- Fix collab: add `error_max_turns` to usage limit detection (was preventing command switch)

## 1.6.8 - 2026-02-16

- Fix collab: launcher crash handling, select() EINTR handling, worker exit code capture

## 1.6.7 - 2026-02-16

- Fix collab: add YAML parse error warning; fix se3.config.yaml comment format

## 1.6.6 - 2026-02-16

- Fix collab: add "hit your limit" to usage limit detection keywords (was causing command not to switch)

## 1.6.5 - 2026-02-16

- Fix collab: manager launcher outputs valid JSON on failure, signal handling moved to launcher, import error handling, escalation JSON

## 1.6.4 - 2026-02-16

- Fix collab launchers: JSON extraction, race conditions, signal handling, task dir creation, stderr handling

## 1.6.3 - 2026-02-16

- Fix collab orchestrator: manager now uses activity-based monitoring via launcher, proper limit detection

## 1.6.2 - 2026-02-16

- Fix collab orchestrator: treat `error_max_turns` as usage limit and switch command

## 1.6.1 - 2026-02-16

- Fix collab orchestrator: clear CLAUDECODE on daemon start, command switch on JSON parse error

## 1.6.0 - 2026-02-16

- Activity-based timeout for collab workers: real-time I/O monitoring, automatic command fallback on inactivity, `run_with_monitor()` API

## 1.5.5 - 2026-02-16

- Fix collab orchestrator: extract JSON from Claude CLI envelope via Python, add --dangerously-skip-permissions for daemon mode, reduce manager max-turns

## 1.5.4 - 2026-02-16

- Fix collab orchestrator: do_plan supports full tasks array

## 1.5.3 - 2026-02-16

- Fix collab orchestrator: clean stale tasks on new session, handle empty task list

## 1.5.2 - 2026-02-16

- Fix collab manager: use text output format, strip markdown fences from Claude response before JSON parsing

## 1.5.1 - 2026-02-16

- Fix collab daemon: pass objective to orchestrator script, clear CLAUDECODE env to prevent nested session error

## 1.5.0 - 2026-02-16

- Simplified collab: removed External Controller v2 (daemon, API, README.md changes). Bash orchestrator only. File-based async communication.

## 1.4.0 - 2026-02-16

- Claude Command Resolver: priority-based multi-command fallback on usage limit/timeout, unified config module, `se3 claude-cmd` CLI

## 1.3.0 - 2026-02-16

- Complete External Controller: MCP server, HTTP API, session persistence, auto-commit, collab v2 integration

## 1.2.0 - 2026-02-16

- External Controller: session daemon with auto-commit, collaboration outside Claude (fixes nested process limitation)

## 1.1.2 - 2026-02-16

- Fix collab orchestrator: jq-complete pipe assignments (`|`) and `+=` operator, MOCK_MODE unbound variable

## 1.1.1 - 2026-02-16

- Version management module with strict enforcement rules

## 1.1.0 - 2026-02-16

- Git Worktree Collaboration: independent entry mode (`--daemon`, `--manual`), multi-language human calls, version consistency check

## 1.0.0 - 2026-02-16

- Initial stable release: `se3 init`, `se3 update`, `se3 commit`, `se3 collab`, Semantic Versioning 2.0.0

## Pre-1.0 Development History

These pre-release milestones use the original `vN.N` design-iteration numbering and are kept as a separate historical record; they are not part of the semver `1.x`–`7.x` line above.

- v7.0 — 2026-02-14 — SE3 Module System: Separate framework (SE3.md) from project config (CLAUDE.md), `se3 init` command
- v6.1 — 2026-02-14 — Requirement intake: three-source taxonomy for structured requirement capture
- v6.0 — 2026-02-14 — CLI tools: se3 lint, sync, verify, status for enforceable framework
- v5.1 — 2026-02-14 — Diagnostic dashboard: status.md for single-source-of-truth session state
- v5.0 — 2026-02-14 — Verification protocol, spec guardrails, init.sh environment automation
- v4.1 — 2026-02-14 — Adaptive formality: match SDD ceremony to scope
- v4.0 — 2026-02-14 — Remove demands.md, specs as truth, adaptive commit/context rules
- v3.0 — 2026-02-14 — English rewrite, native agent team, global CLAUDE.md
- v2.0 — 2026-02-14 — Remove intentions.md, unified Human-as-MCP, progressive startup
- v1.0 — 2026-02-14 — Initial concept
