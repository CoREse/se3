<!-- spec-format: v1 -->
# se3-commands Specification

## Purpose

Define the command-line interface for SE3 core commands. SE3 3.0 uses `se3 run` as the unified entry point for all development workflows, with supporting commands for project initialization and spec protection.

## Requirements

### Requirement: Unified Entry Point `se3 run`

The system SHALL provide `se3 run` as the primary entry point for all SE3 workflows.

**Interface:**
```bash
# New task
se3 run "Implement feature X"

# Resume interrupted flow
se3 run --resume

# Loop mode (continuous execution)
se3 run --loop

# Specify task type
se3 run "Fix bug" --type=bugfix

# Discovery mode
se3 run --discover "I want to build..."
```

**Task Types:**
| Type | Description | Steps |
|------|-------------|-------|
| `feature` | New functionality | Full 10-step workflow |
| `bugfix` | Fixing a bug | Skip update_spec step |
| `review` | Code review/analysis | analyze → read_spec → verify_spec → summarize |
| `small` | Minor fix/typo | analyze → implement → test → commit → summarize |
| `directive` | Following specific instructions | analyze → read_spec → plan → implement → commit → summarize |

#### Scenario: New task execution
- **WHEN** user executes `se3 run "Implement user authentication"`
- **THEN** the flow engine creates a new flow instance
- **AND** starts execution from the analyze step

#### Scenario: Resume interrupted flow
- **WHEN** user executes `se3 run --resume` with an active flow
- **THEN** the flow engine loads the persisted state
- **AND** continues execution from the interrupted step

#### Scenario: Loop mode execution
- **WHEN** user executes `se3 run --loop`
- **THEN** the flow engine continuously executes tasks

#### Scenario: Loop mode with branch isolation
- **WHEN** user executes `se3 run --loop` (without `--no-worktree`)
- **THEN** creates a `se3-loop/{timestamp}` branch and git worktree
- **AND** all tasks execute in the worktree
- **AND** on completion, prompts user to merge/defer/discard

#### Scenario: List loop branches
- **WHEN** user executes `se3 run --list-loops`
- **THEN** displays all unmerged loop branches with commit counts
- **AND** shows instructions for merging or discarding

#### Scenario: Merge loop branch with diff summary
- **WHEN** user executes `se3 run --loop --merge <branch>`
- **THEN** shows diff stat summary before merging
- **AND** prompts for confirmation before proceeding
- **AND** on conflict, displays conflicting file list with resolution instructions

#### Scenario: Discovery mode execution
- **WHEN** user executes `se3 run --discover "Idea"`
- **THEN** the flow engine starts in discovery mode
- **AND** explores requirements through multi-turn conversation
- **AND** after LLM confirms requirements are clear, prompts the user via the regular discovery input; typing the exact string `1` confirms and proceeds to analyze, any other non-empty input is treated as the next user turn of discovery (empty input is a no-op: the prompt is re-displayed), no separate numbered-choice UI

### Requirement: `se3 init` Command

The `se3 init` command SHALL initialize a new SE3 project with the standard directory structure.

**Interface:**
```bash
se3 init [--project-root PATH] [--name PROJECT_NAME] [--force]
```

**Created Structure:**
```
project/
├── se3.yaml              # Project configuration
└── se3/
    └── specs/
        └── base/
            └── spec.md   # Base project specification
```

#### Scenario: Initialize new project
- **GIVEN** a directory without SE3 configuration
- **WHEN** user runs `se3 init`
- **THEN** it creates se3.yaml, se3/specs/, and se3/specs/base/spec.md

#### Scenario: Initialize with custom name
- **GIVEN** a directory at /path/to/my-project
- **WHEN** user runs `se3 init --name "My Project"`
- **THEN** the base spec contains "My Project" as project name

### Requirement: `se3 guardrails` Command

The `se3 guardrails` command SHALL check spec files against SE3 Spec Guardrails.

**Interface:**
```bash
se3 guardrails <spec-file> [--original <original-file>]
```

**Guardrail Checks:**
1. **must_not_delete**: Detect deleted WHEN/THEN scenarios
2. **must_not_weaken**: Detect weakened language (SHALL → SHOULD, MUST → SHOULD)

#### Scenario: Detect spec violations
- **GIVEN** a modified spec file
- **WHEN** user runs `se3 guardrails <spec-file>`
- **THEN** the command compares with original spec
- **AND** reports any deleted requirements or weakened language

#### Scenario: No violations found
- **GIVEN** a spec file with no guardrail violations
- **WHEN** user runs `se3 guardrails <spec-file>`
- **THEN** the command reports success

### Requirement: `se3 history` Command

The `se3 history` command SHALL list and inspect all flow executions across three data sources: the active engine state, the archive, and chat-history-only directories.

**Interface:**
```bash
se3 history                          # List all flows (default)
se3 history list                     # List all flows
se3 history list --active-only       # Show only the active flow
se3 history list --archived-only     # Show only archived flows
se3 history list --json              # Output as JSON
se3 history show <flow_id>           # Show detailed info for a flow
se3 history show <flow_id> --detailed          # Show LLM call details (structured prompt + final response)
se3 history show <flow_id> --detailed --verbose  # Show full response including tool calls
se3 history show <flow_id> --detailed --json     # Output detailed chat history as JSON
se3 history restore <flow_id>        # Resume a flow (delegates to se3 run --resume)
se3 history archived                 # List only archived flows
```

**Data Sources (aggregated by `list` / default command):**
| Source | Path | Label |
|--------|------|-------|
| Active | `se3/state/engine.json` | `active` |
| Archived | `se3/state/archive/engine_*.json` | `archived` |
| History-only | `se3/history/{flow_id}/` | `history` |

Results are de-duplicated by `flow_id` and sorted by `updated_at` descending.

#### Scenario: List all flows
- **WHEN** user runs `se3 history` or `se3 history list`
- **THEN** all flows from all three sources are displayed in a table
- **AND** each row includes flow_id, status, task description, progress, updated time, and source

#### Scenario: Filter active or archived flows
- **WHEN** user adds `--active-only` or `--archived-only`
- **THEN** only flows matching that source are displayed

#### Scenario: Show flow details
- **GIVEN** a valid flow_id (or unambiguous prefix)
- **WHEN** user runs `se3 history show <flow_id>`
- **THEN** the system searches for the flow across three data sources in order: active flow, archived flows, then history-only flows
- **AND** if an exact flow_id match is not found, performs prefix matching against all flows from all three sources
- **AND** displays detailed step-by-step breakdown of the flow

#### Scenario: Show flow details with LLM call details
- **GIVEN** a valid flow_id with chat history
- **WHEN** user runs `se3 history show <flow_id> --detailed`
- **THEN** displays flow metadata and step table as usual
- **AND** sessions are passed through `interleave_sessions_for_display()` before rendering so that fix-loop `implement` sessions are split into virtual `-iter{N}` sub-sessions and chronologically interleaved with `test` / `self_check` sessions (see the Chat History interleave scenario in `flow-engine` spec)
- **AND** appends each step's LLM call details: prompt is shown as structured segments (auto-detected sections such as JSON Mode Instruction, Step Instructions, Available Specifications, Discovery Context, Read-Only Constraint, Language Instruction, Additional User Instruction, etc.) using Rich Panels with labeled titles
- **AND** prompt segments containing embedded spec content are folded into compact reference annotations for readability:
  - Segments titled "Relevant Specifications", "Specifications (for context only)", or "Project Conventions" fold each `### spec-name` subsection into `[spec] @spec-name  (折叠, size)` with `bold magenta` Rich styling on `@spec-name`
  - Segments titled "Base Specification" fold the entire body into `[spec] @base  (折叠, size)`
  - Segments that only list spec names (e.g., "Available Specifications") are NOT folded
  - `### name` headings that appear inside fenced code blocks (``` or ~~~) or indented code blocks are NOT treated as spec subsections and are preserved as-is; only `### name` headings outside code contexts qualify for folding
  - Spec recognition tolerates arbitrary blank lines between the `### spec-name` marker and its following `# Title` H1 heading, so unusually formatted specs are still folded via the primary recognition path
- **AND** response shows only the final assistant text block (skipping intermediate tool calls and tool results)
- **AND** multiple attempts within a step are shown separately with attempt labels

#### Scenario: Detailed with verbose response
- **WHEN** user runs `se3 history show <flow_id> --detailed --verbose`
- **THEN** prompt display is identical to `--detailed` (structured segments)
- **AND** response display reuses `_render_ndjson_for_human()` to show the full conversation flow including text content and tool call/result summaries (via `format_tool_use_preview()` / `format_tool_result_preview()`), consistent with `se3 run` streaming style
- **AND** `--verbose` implies `--detailed`

#### Scenario: Detailed JSON output
- **WHEN** user runs `se3 history show <flow_id> --detailed --json`
- **THEN** outputs structured JSON containing flow metadata plus a `chat_history` array
- **AND** each entry in `chat_history` includes `step_id`, `step_type`, and `messages`
- **AND** user messages include `segments` (auto-segmented prompt sections) and full `content`
- **AND** assistant messages include `content`, and `raw_json` (original NDJSON data)
- **AND** the array order matches the Rich display path: fix-loop implement sessions are virtually split into `-iter{N}` entries and interleaved chronologically with test/self_check entries (via the same `interleave_sessions_for_display()` applied in `get_detailed_json`)

#### Scenario: Restore a flow
- **WHEN** user runs `se3 history restore <flow_id>`
- **THEN** delegates to `se3 run --resume --flow-id <flow_id>`

### Requirement: `se3 sync` Command

The `se3 sync` command SHALL check and synchronize `se3/specs/` with project code, identifying gaps, extensions, and conflicts between specifications and the actual codebase.

**Interface:**
```bash
se3 sync                    # Sync with default mode
se3 sync --mode=default     # Same as above (explicit)
se3 sync --mode=strict      # All conflicts require human decision
se3 sync --mode=fast        # LLM handles all conflicts automatically
```

**Mode Parameter:**
| Mode | Behavior |
|------|----------|
| `default` | LLM auto-resolves high-confidence conflicts; low-confidence conflicts are batched into an MCP call file for human decision |
| `strict` | All conflicts are batched into a single MCP call file for human decision |
| `fast` | LLM fully auto-resolves all conflicts; no human intervention |

#### Scenario: Sync with existing base spec
- **GIVEN** the project has a base spec at `se3/specs/base/`
- **WHEN** user runs `se3 sync`
- **THEN** the engine loads all specs starting from base
- **AND** performs LLM-driven comparison of each spec against project code
- **AND** classifies each difference as gap, extension, or conflict

#### Scenario: Sync without base spec (SE3 bootstrapping)
- **GIVEN** the project has no `se3/specs/base/` directory
- **WHEN** user runs `se3 sync`
- **THEN** the engine first explores the project codebase and generates a base spec
- **AND** then proceeds with the iterative sync flow

#### Scenario: Gap detected (spec leads code)
- **WHEN** a spec describes a requirement that is NOT implemented in the code
- **THEN** the engine creates an issue tagged `["auto-discovered", "source:sync"]`
- **AND** the issue title follows the format `[sync] {spec_name}: {description}`
- **AND** idempotency uses normalized matching: titles are normalized by extracting the description portion, lowercasing, removing articles (a/an/the), stripping punctuation, and collapsing whitespace before comparison
- **AND** if a normalized-matching open issue already exists, it is NOT created (idempotency)

#### Scenario: Extension detected (code extends spec)
- **WHEN** the code contains functionality that the spec does NOT describe, with no contradiction
- **THEN** the engine uses LLM to update the spec file to reflect the code's actual behavior
- **AND** the update preserves all existing requirements (add-only)
- **AND** a content length safety guard rejects suspiciously short LLM outputs (< 50% of original)
- **AND** markdown code fences wrapping the LLM response are stripped before writing to spec files

#### Scenario: Conflict detected in default mode
- **WHEN** the code implements something differently from what the spec describes
- **AND** mode is `default`
- **THEN** high-confidence conflicts are auto-resolved by LLM (update spec or create issue)
- **AND** low-confidence conflicts are collected for human decision

#### Scenario: Conflict detected in strict mode
- **WHEN** a conflict is detected and mode is `strict`
- **THEN** all conflicts are batched into a single MCP call file in `se3/calls/`
- **AND** flow pauses, awaiting human input

#### Scenario: Conflict detected in fast mode
- **WHEN** a conflict is detected and mode is `fast`
- **THEN** LLM auto-resolves every conflict (deciding update_spec or create_issue)
- **AND** no human intervention is triggered

#### Scenario: Conflict spec update safety guards
- **WHEN** a conflict is resolved by updating the spec (via LLM)
- **THEN** a content length safety guard rejects LLM outputs shorter than 50% of the original spec content
- **AND** markdown code fences wrapping the LLM response are stripped before writing to spec files

#### Scenario: Issue lifecycle — auto-close resolved gaps
- **WHEN** sync detects that a previously reported gap is no longer present
- **THEN** the corresponding sync-tagged issue is automatically closed using a three-layer matching strategy:
  1. **Normalized match**: the issue title is normalized and compared against current gap titles
  2. **Prefix fallback**: if normalized match fails but the issue's spec still has gaps, the issue is conservatively kept open
  3. **Close**: only when neither condition holds is the issue closed
- **AND** the close reason indicates the gap was resolved
- **AND** only gap issues are processed (conflict issues have their own lifecycle)

#### Scenario: MCP call file generation for human intervention
- **WHEN** conflicts require human decision (default or strict mode)
- **THEN** all pending conflicts are written to a single JSON file in `se3/calls/`
- **AND** the file includes conflict ID, spec name, description, code location, spec content (truncated to 2000 chars), and decision options
- **AND** the CLI displays the call file path and the `se3 sync-respond` command to process it

### Requirement: `se3 sync-respond` Command

The `se3 sync-respond` command SHALL process an MCP call response file for sync conflicts.

**Interface:**
```bash
se3 sync-respond <call-file-path>
```

#### Scenario: Process call response
- **GIVEN** an MCP call file has been created by `se3 sync`
- **AND** the user has filled in the `.response` file with decisions for each conflict
- **WHEN** user runs `se3 sync-respond <call-file-path>`
- **THEN** the engine reads each conflict decision from the response file
- **AND** for `update_spec` decisions: uses LLM to update the spec to match the code
- **AND** for `create_issue` decisions: creates an issue recording the discrepancy
- **AND** responses with invalid decision values (not `update_spec` or `create_issue`) are skipped
- **AND** responses referencing unknown conflict IDs (not present in the original call file) are skipped

### Requirement: Sync Operation Permission Limits

`se3 sync` SHALL only directly modify spec files (`se3/specs/`) and issue files (`se3/issues/`). All situations requiring code changes SHALL be recorded as issues, never applied directly to project source code.

### Requirement: `se3 merge` Command

The `se3 merge` command SHALL sequentially merge one or more named branches into the current branch, targeting same-repo multi-task parallel aggregation. Branches are merged pairwise in the order given (no octopus merge); the command is unaware of the source workflow that produced each branch and coexists with `se3 run --loop --merge` (which remains the in-loop single-branch path).

**Interface:**
```bash
se3 merge <branch> [<branch> ...] [--strategy default|strict|fast] [--delete-merged | --no-delete-merged]
```

**Behavior contract:**

1. **Sequential pairwise merge.** Git owns the merge topology. For each branch in argument order, the command runs `git merge <branch>` against the current HEAD. The minimum unit of conflict resolution is a single `git merge` invocation: all conflicting files of that one merge are handed to the LLM in a single call, written back, committed, and only then does the next branch start.

2. **Conflict-resolution context contract.** When a `git merge` reports conflicts, the LLM call SHALL receive at minimum:
   - Merge metadata: ours/theirs branch names, merge-base commit, both HEAD commit hashes and messages.
   - For every conflicting file: the full base/ours/theirs three-way contents (`git show :1:`/`:2:`/`:3:`) plus the working-tree file with `<<<<<<<` / `=======` / `>>>>>>>` markers.
   - The path and hunk line ranges of each conflict.
   - The selected strategy tier (default/strict/fast).

   The call SHOULD additionally receive `git log <merge-base>..<theirs>` and `git log <merge-base>..<ours>` (oneline), a flag identifying spec files (subject to spec-guardrails), and a project-conventions summary.

3. **Structured LLM output.** The LLM SHALL return structured JSON. For each file: `resolved_content` (full file text), per-hunk `confidence` and `reasoning`, and an `overall_confidence`. Top-level `flags` MAY include `requires_human_review` and `spec_guardrail_concern`. The strategy tier consumes this structured output to decide accept / human / reject.

4. **Three strategy tiers (aligned with `se3 sync`):**
   | Tier | Behavior |
   |------|----------|
   | `default` | LLM auto-resolves conflicts. Low confidence, `requires_human_review`, or `spec_guardrail_concern` → MCP human call. LLM resolution failure also → human call. Post-merge guardrails violation → rollback + human call. |
   | `strict` | LLM is NOT invoked; every conflict or post-merge guardrails violation escalates directly to human call. |
   | `fast` | LLM auto-resolves all conflicts (including spec files). If LLM resolution fails or post-merge guardrails violation cannot be repaired by LLM → abort with failure (no human call). Exception: when the LLM repair loop *stalls* (consecutive repair iterations produce the same violation set, indicating no progress) the merge is escalated to a human call instead of aborting (see "Fast-Mode Guardrail Repair Stall Escalation"). |

   <!-- Preserved original table row for guardrails compatibility:
   | `strict` | Accept only when every hunk is high-confidence AND guardrails pass; otherwise raise a human call. |
   -->

5. **Spec-guardrail enforcement.** Whenever a merge touches a `se3/specs/**/spec.md` file (whether or not it had a textual conflict), the merge product SHALL be re-checked by `se3 guardrails`. The check is mandatory in all three tiers. Violations (deleted requirements, weakened language SHALL→SHOULD, weakened quantifiers all→some, deleted scenarios) cause the merge to be rolled back and escalated to a human call.

6. **Failure handling.** A merge that cannot be accepted (rejected by strategy, guardrails violation, LLM failure) defaults to `git merge --abort`, restoring the working tree. Branches successfully merged earlier in the sequence are preserved.

7. **SemVer aggregation after merge.** After all branches are processed, the per-branch SemVer bump types (patch/minor/major), each computed against the merge base of that branch, are reduced via SemVer's max rule and a single `pyproject.toml` update is amended onto the last merge commit. Example: base `4.4.0` + patch + patch + minor → `4.5.0`. Per-branch historical commits are NOT rewritten — SemVer uniqueness is guaranteed by tags.

8. **Branch and worktree cleanup.** Default behavior is to keep merged branches. With `--delete-merged`:
   - Each merged branch is removed via `git branch -d` (lowercase) so that branches not reachable from HEAD are not silently destroyed.
   - If a branch has a bound git worktree, `git worktree remove` is called when the worktree is clean (`git status --porcelain` empty); when dirty the cleanup is refused with an error and `--force` is NEVER used.
   - The current branch and `main`/`master` are NEVER deleted.

9. **Infrastructure reuse.** Execution logs go to `se3/logs/`. Human-decision artifacts go to `se3/calls/` as MCP call files (e.g., `se3/calls/merge_<timestamp>_<branch>.json`), consistent with `se3 sync` and the existing `merge_loop_branch` flow.

**Out of scope (first version):** octopus merge (git's strategy supports only conflict-free combinations); single LLM call resolving multiple branches simultaneously (no ground truth); cross-branch hunk-level batching (git does not support partial layered merges); rewriting per-branch historical commits' versions; auto-deciding which branches to merge (the list MUST be explicit); injecting unrelated full-file context or historical-merge few-shot examples into the LLM prompt (possible later enhancement).

#### Scenario: Successful sequential merge with no conflicts
- **GIVEN** branches `feat/a`, `feat/b`, `feat/c` all merge cleanly into the current branch
- **WHEN** user runs `se3 merge feat/a feat/b feat/c`
- **THEN** each branch is merged in order, producing one merge commit per branch
- **AND** the aggregated SemVer bump (max of each branch's bump type) is applied as a single `pyproject.toml` update amended onto the last merge commit

#### Scenario: Conflict resolved automatically in default strategy
- **GIVEN** merging `feat/x` produces text conflicts in non-spec files
- **AND** the LLM returns high `overall_confidence` with no `spec_guardrail_concern`
- **WHEN** strategy is `default`
- **THEN** the resolved contents are written back, staged, and committed
- **AND** the merge proceeds to the next branch

#### Scenario: Low-confidence conflict escalates to human call
- **GIVEN** the LLM resolution for a merge has low `overall_confidence` or sets `requires_human_review`
- **WHEN** strategy is `default`
- **THEN** `git merge --abort` restores the working tree
- **AND** an MCP call file is created at `se3/calls/merge_<timestamp>_<branch>.json` containing the conflict context, LLM proposal, and confidence data
- **AND** subsequent branches in the argument list are NOT attempted

#### Scenario: Strict strategy skips LLM and escalates directly to human
- **GIVEN** a merge produces conflicts in any file
- **WHEN** strategy is `strict`
- **THEN** the LLM is NOT invoked for conflict resolution
- **AND** a human call is created directly at `se3/calls/`
- **AND** previously successfully merged branches in the same invocation are preserved

#### Scenario: Fast strategy still enforces guardrails on spec files
- **GIVEN** a merge produces a change to `se3/specs/foo/spec.md` that weakens a SHALL to SHOULD
- **WHEN** strategy is `fast`
- **THEN** the guardrails check fails after the merge commit
- **AND** the violation list is fed to the LLM to repair the spec file (in a bounded repair loop, see "Fast-Mode Guardrail Repair Stall Escalation")
- **AND** if the LLM repair succeeds, the merge commit is amended with the corrected spec
- **AND** if the LLM repair fails *and* repair iterations are still making progress, the merge is aborted without creating a human call
- **AND** if the LLM repair *stalls* (no-progress detection fires), the merge is escalated to a human call instead of aborting
- **AND** fast does NOT bypass spec guardrails detection

#### Scenario: Fast strategy aborts when LLM cannot resolve a conflict
- **GIVEN** merging `feat/z` produces text conflicts in a spec file
- **AND** the LLM returns low confidence or sets `requires_human_review`
- **WHEN** strategy is `fast`
- **THEN** the merge is aborted without creating a human call
- **AND** a failure message indicates the fast strategy could not resolve the conflict

#### Scenario: Branch cleanup with --delete-merged
- **GIVEN** `feat/x` was merged successfully and has no bound worktree
- **WHEN** the user passes `--delete-merged`
- **THEN** `git branch -d feat/x` removes the branch
- **AND** `main`/`master` and the current branch are never touched

#### Scenario: Cleanup refuses to delete a dirty worktree
- **GIVEN** `feat/y` was merged and has a bound worktree containing uncommitted changes
- **WHEN** `--delete-merged` is in effect
- **THEN** the cleanup reports an error for `feat/y`, leaves both worktree and branch intact, and does NOT use `git worktree remove --force`

#### Scenario: Failure messages distinguish conflict vs guardrails categories
- **GIVEN** a merge fails for any reason
- **WHEN** the CLI renders the failure summary and the log file is written
- **THEN** the message clearly identifies the failure category, distinguishing at minimum:
  - `git merge conflict (could not be resolved)` — text conflicts the resolver could not handle
  - `post-merge guardrails violation` — spec guardrails rejected the merge result
  - `failed to build conflict context` — the resolver could not even prepare conflict input (strategy-neutral phrasing applies to default, strict, and fast)
  - fast-mode aborts (`fast strategy could not resolve conflict`, `fast strategy could not auto-repair guardrails violation`, `fast strategy LLM resolution failed`)
- **AND** the same category labels are used in the CLI summary and the corresponding log entry, so that users do not confuse a guardrails-driven failure with an unresolved git conflict

#### Scenario: Human call required but call file cannot be written
- **GIVEN** the merge needs to escalate to a human call (low-confidence LLM resolution, post-merge guardrails violation in default/strict, etc.)
- **AND** writing the MCP call file fails (filesystem error, permission issue, etc.)
- **WHEN** the merge command finalizes the report
- **THEN** the report is treated as an outright failure rather than a pending-human state
- **AND** the CLI exits with the general-failure code rather than the interrupted/paused code, because there is no call file for the user to respond to with `se3 merge-respond`
- **AND** the summary explicitly states that the human call file could not be written

### Requirement: Fast-Mode Guardrail Repair Stall Escalation

The fast-strategy post-merge guardrail repair loop SHALL detect when the LLM is no longer making progress and escalate to a human MCP call instead of aborting.

**Stall detection contract:**

1. After each guardrail repair iteration, the orchestrator SHALL compute a deterministic hash of the current violation set, derived from `(file, violation_type, normalized_message)` triples sorted to be order-insensitive.
2. When two consecutive repair iterations produce the *same* violation-set hash (the LLM's repair did not change the violation set), the orchestrator SHALL stop further repair attempts and treat the situation as a *stall*.
3. On stall, the merge SHALL NOT be aborted. Instead, the orchestrator SHALL write a human MCP call file under `se3/calls/` with a distinct call type (e.g., `guardrail_repair_stalled`) and route the merge to a `pending_human` state, consistent with how default/strict tiers escalate guardrail violations.
4. The stalled-repair call file SHALL embed the structured detector evidence from each violation (paired strong/weak lines, line numbers, branch identification) so the human reviewer can act without re-running the detector.
5. If repair iterations are still changing the violation set but the maximum iteration cap is exhausted, fast mode SHALL fall back to the original abort-without-human-call behavior — only the *stall* condition triggers escalation.
6. The repair loop's per-iteration semantics (one LLM call per iteration) live in the orchestrator/strategy layer, not inside the single-call repair primitive.

**Rationale:** A persistently false-positive detector or a degenerate repair prompt can otherwise burn many minutes of LLM time looping without convergence; the stall exit lets a human resolve the disagreement quickly while preserving fast mode's "no human in the loop unless absolutely necessary" guarantee for the common case.

#### Scenario: Repair loop stalls on repeated identical violation set
- **GIVEN** a fast-mode merge whose post-merge guardrails check reports a violation
- **AND** the LLM repair iteration produces a fix that the detector still rejects with the *same* violation set on the next iteration
- **WHEN** the orchestrator hashes the new violation set and finds it equal to the previous iteration's hash
- **THEN** further repair attempts are stopped at the second consecutive identical hash
- **AND** a `guardrail_repair_stalled` MCP call file is written under `se3/calls/` with the detector evidence
- **AND** the merge enters `pending_human` rather than aborting

#### Scenario: Repair loop makes progress but never converges
- **GIVEN** a fast-mode merge whose post-merge guardrails check reports a violation
- **AND** each LLM repair iteration produces a *different* violation set (hash keeps changing)
- **WHEN** the iteration cap is reached without the violation set becoming empty
- **THEN** the merge is aborted with the standard fast-mode "could not auto-repair guardrails violation" failure
- **AND** no human call file is created (stall escalation does not apply)

### Requirement: `se3 merge-respond` Command

The `se3 merge-respond` command SHALL process an MCP call response file produced by `se3 merge` when conflicts were escalated for human decision.

**Interface:**
```bash
se3 merge-respond <call-file-path>
```

#### Scenario: Process merge call response
- **GIVEN** an MCP call file has been created by `se3 merge`
- **AND** the user has filled in the `.response` file with resolutions or directives
- **WHEN** user runs `se3 merge-respond <call-file-path>`
- **THEN** the engine consumes the response and resumes the merge sequence (re-applying the resolved contents or skipping the merge per the user's decision)

## Command Summary

| Command | Purpose | Status |
|---------|---------|--------|
| `se3 run` | Unified workflow entry point | **Required** |
| `se3 init` | Initialize SE3 project structure | **Required** |
| `se3 guardrails` | Check spec against guardrails | **Required** |
| `se3 history` | View and manage flow history | **Required** |
| `se3 sync` | Check and synchronize specs with project code | **Required** |
| `se3 sync-respond` | Process MCP call response for sync conflicts | **Required** |
| `se3 merge` | Sequentially merge one or more branches into current with LLM-assisted conflict resolution | **Required** |
| `se3 merge-respond` | Process MCP call response for merge conflicts | **Required** |

### Requirement: Loop Mode CLI Options

The system SHALL provide the following CLI options for loop mode:

| Option | Description |
|--------|-------------|
| `--loop, -l` | Enable loop mode (continuous task execution) |
| `--max-iterations, -n` | Maximum iterations for loop mode (default: 10) |
| `--no-worktree` | Disable branch isolation in loop mode |
| `--merge BRANCH` | Merge an existing loop branch (shows diff summary, prompts confirmation) |
| `--list-loops` | List existing unmerged loop branches with commit counts |

## Error Codes

| Code | Description |
|------|-------------|
| 0 | Success |
| 1 | General error / Guardrails violation |
| 130 | Interrupted by user (Ctrl+C) |
