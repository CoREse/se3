<!-- spec-format: v1 -->
# worktree-management Specification

## Purpose

The worktree-management subsystem provides the generic git branch and worktree lifecycle primitives that back SE3's `se3 run --worktree` isolation mode, the implement step's DAG-parallel worktrees, and merge isolation. It owns git worktree creation with retry/timeout handling (`create_worktree` / `fork_worktree`), isolated execution via a context manager (`WorktreeContext`), branch deletion gated on worktree state, resilient multi-step cleanup of orphaned worktrees, locked worktrees, and stale `.git/worktrees/` metadata (`force_cleanup_worktree`), and context-aware LLM merge-conflict resolution. It also exposes repository state queries (current branch, merge-in-progress detection, unmerged-index detection) and an auto-resolver for stale unmerged-index leftovers from prior aborted merges. (The loop-mode-specific primitives — loop branch naming, loop merge-back, basic per-file LLM conflict stripping, loop cleanup composition, and loop branch listing — have been removed along with loop mode.)

## Requirements

### Requirement: Git Command Execution

All git operations route through a single helper that runs `git -C <project_root> <args>` with captured stdout/stderr, a default 30-second timeout, `stdin` set to `DEVNULL`, and `check=True` by default. Callers may override `check` and `timeout` per call.

#### Scenario: Standard git invocation

- **WHEN** a function needs to run any git command
- **THEN** it builds the command as `["git", "-C", str(project_root), ...args]`
- **AND** subprocess is invoked with `capture_output=True`, `text=True`, and `stdin=subprocess.DEVNULL`
- **AND** non-zero exits are logged at debug level only when `check=False`

#### Scenario: Default timeout enforcement

- **WHEN** no explicit timeout is supplied
- **THEN** the command times out after 30 seconds and raises `subprocess.TimeoutExpired`

### Requirement: Repository State Queries

The subsystem exposes safe queries that work across normal repos, fresh `git init` repos with no commits, and linked worktrees.

#### Scenario: Detecting whether the repo has commits

- **WHEN** `has_commits(project_root)` is called
- **THEN** it returns `True` iff `git rev-parse HEAD` exits zero

#### Scenario: Resolving the current branch on a normal repo

- **WHEN** `get_current_branch(project_root)` is called on a repo with commits
- **THEN** it first tries `git symbolic-ref --short HEAD`
- **AND** returns the trimmed branch name if non-empty

#### Scenario: Resolving the current branch on an empty repo

- **WHEN** the repo has no commits but has an orphan branch ref
- **THEN** `symbolic-ref` succeeds and returns the orphan branch name
- **AND** the function does not fall through to `rev-parse`

#### Scenario: Detached HEAD raises

- **WHEN** `symbolic-ref` fails and `rev-parse --abbrev-ref HEAD` returns `"HEAD"`
- **THEN** `get_current_branch` raises `RuntimeError("Detached HEAD state — cannot determine branch")`

#### Scenario: Detecting an in-progress merge

- **WHEN** `merge_in_progress(project_root)` is called
- **THEN** it resolves the real `.git` directory via `git rev-parse --git-dir` (works in linked worktrees)
- **AND** returns `True` iff any of `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge`, or `rebase-apply` exists inside that git dir

### Requirement: Loop Branch Naming

**DEPRECATED — removed.** The loop branch naming primitives (`create_loop_branch`, `_slugify_task_id`, and the `loop/{slug}-{iteration}` / legacy `se3-loop/{timestamp}` naming shapes) have been removed along with loop mode. Branch creation for isolated runs is now handled by the generic `fork_worktree` / `create_worktree` primitives (see *Worktree Creation*); `get_current_branch` (see *Repository State Queries*) remains available to capture the original branch.

#### Scenario: Loop branch naming removed
- **WHEN** an isolated run needs a branch and worktree
- **THEN** the loop-specific naming helpers no longer exist
- **AND** the generic `fork_worktree(project_root, source_branch, new_branch)` / `create_worktree` primitives are used instead

### Requirement: Worktree Creation

Worktrees are created under `{project_root}/se3/worktrees/{safe_name}` where `safe_name` replaces `/` with `-`. Creation prunes stale entries beforehand and retries on timeout. These generic primitives (`create_worktree` / `fork_worktree`) are reused by `se3 run --worktree` to build an isolation worktree per run and by the implement step's DAG-parallel execution — both share the same `se3/worktrees/` parent directory, distinguished by per-branch slug subdirectories so they do not collide.

#### Scenario: Path layout

- **WHEN** `create_worktree(project_root, branch)` is called
- **THEN** the target path is `project_root / "se3" / "worktrees" / branch.replace("/", "-")`
- **AND** the parent `se3/worktrees/` directory is created with `parents=True, exist_ok=True`

#### Scenario: Pre-creation prune

- **WHEN** `create_worktree` begins
- **THEN** it runs `git worktree prune` (non-checking) before attempting `git worktree add`

#### Scenario: Timeout retry with exponential backoff

- **WHEN** `git worktree add` times out (initial 120s timeout)
- **THEN** any partial worktree directory is removed via `shutil.rmtree(..., ignore_errors=True)`
- **AND** `git worktree prune` is run again
- **AND** the timeout doubles for the next attempt
- **AND** up to `max_retries=2` retries are attempted (3 attempts total)

#### Scenario: All retries exhausted

- **WHEN** every retry times out
- **THEN** `subprocess.TimeoutExpired` is re-raised carrying the last error's `cmd`, `timeout`, `output`, and `stderr`

#### Scenario: Forking a worktree

- **WHEN** `fork_worktree(project_root, source_branch, new_branch)` is called
- **THEN** `git branch {new_branch} {source_branch}` is run first
- **AND** `create_worktree(project_root, new_branch)` produces the worktree

### Requirement: Worktree Removal

Removal handles three states: missing directory with stale metadata, normal removal, and locked worktrees.

#### Scenario: Directory already gone

- **WHEN** `remove_worktree` is called and `worktree_path` does not exist
- **THEN** `git worktree prune` runs
- **AND** if `git worktree list --porcelain` still shows the path, `git worktree remove -f -f {path}` is invoked to force-clear the stale entry
- **AND** the function returns without error

#### Scenario: Normal force removal

- **WHEN** the worktree directory exists
- **THEN** `git worktree remove {path} --force` is attempted (non-checking)
- **AND** on success the function returns

#### Scenario: Locked worktree double-force

- **WHEN** the first removal fails and stderr contains `"locked"` (case-insensitive)
- **THEN** `git worktree remove -f -f {path}` is retried
- **AND** failures of the retry are logged at warning level but do not raise

#### Scenario: Non-locked removal failure

- **WHEN** removal fails for a non-lock reason
- **THEN** a warning is logged
- **AND** `git worktree prune` is run as a fallback

### Requirement: Forceful Worktree Cleanup

`force_cleanup_worktree` executes a six-step, independently fault-tolerant sequence suitable for resume scenarios where the worktree may be locked, partial, or referenced only by stale metadata. Each step is wrapped in `try/except` for both `TimeoutExpired` and generic `Exception`.

#### Scenario: Six-step cleanup ordering

- **WHEN** `force_cleanup_worktree(project_root, branch_name)` runs
- **THEN** the steps execute in this order: (1) `git worktree unlock` with 60s timeout, (2) `git worktree remove -f -f` with 60s timeout, (3) `shutil.rmtree` of the directory if present, (4) `git worktree prune` with 60s timeout, (5) direct deletion of `.git/worktrees/{safe_name}` metadata, (6) verification via `exists_for_branch`
- **AND** a failure or timeout in any step is logged at warning level but does not stop subsequent steps

#### Scenario: Metadata last-resort cleanup

- **WHEN** step 5 runs and `.git/worktrees/{safe_name}` exists
- **THEN** the directory is removed with `shutil.rmtree` (no `ignore_errors`); any exception is caught and warned

#### Scenario: Post-cleanup verification

- **WHEN** step 6 runs and `exists_for_branch` still returns `True`
- **THEN** a warning is logged stating the worktree is still registered
- **AND** the function returns without raising

### Requirement: Worktree Existence Query

#### Scenario: Branch-to-worktree lookup

- **WHEN** `exists_for_branch(project_root, branch)` is called
- **THEN** it runs `git worktree list --porcelain` and parses each `branch refs/heads/<name>` line
- **AND** returns `True` iff some `<name>` equals the requested branch
- **AND** returns `False` when the git command itself fails

### Requirement: Branch Deletion

Deleting a branch first ensures no worktree references it; if a worktree is registered, full force cleanup is attempted before the deletion proceeds.

#### Scenario: Pre-delete worktree cleanup

- **WHEN** `delete_branch` is called and `exists_for_branch` returns `True`
- **THEN** `force_cleanup_worktree` runs
- **AND** if `exists_for_branch` still returns `True` afterwards, a warning is logged and the branch deletion proceeds anyway

#### Scenario: Branch deletion

- **WHEN** the pre-delete step completes
- **THEN** `git branch -D {branch}` is run (non-checking)
- **AND** non-zero exits are logged at warning level but not raised

### Requirement: Loop Cleanup Composition

**DEPRECATED — removed.** The `cleanup_loop` composition helper has been removed along with loop mode. Worktree teardown is now performed directly via the generic `remove_worktree` / `force_cleanup_worktree` primitives (see *Worktree Removal* and *Forceful Worktree Cleanup*) and `delete_branch` (see *Branch Deletion*).

#### Scenario: Loop cleanup composition removed
- **WHEN** an isolated run's worktree must be torn down
- **THEN** `cleanup_loop` no longer exists
- **AND** callers compose `remove_worktree` / `force_cleanup_worktree` and `delete_branch` directly

### Requirement: Merge-Back of Loop Branches

**DEPRECATED — removed.** The lightweight loop merge-back helper (`merge_loop_branch`, with its stash/checkout/non-interactive-merge and human/`llm` conflict-strategy branches) has been removed along with loop mode. Merge-back for `se3 run --worktree` runs now goes through the heavy `se3 merge` orchestrator (version bump, postcondition assertions, typed `FailureReason`, and context-aware LLM conflict resolution); see the `se3-commands` `se3 merge` requirements and *Context-Aware LLM Conflict Resolution* below.

#### Scenario: Loop merge-back replaced by heavy orchestrator
- **WHEN** an isolated `se3 run --worktree` flow succeeds and must fold its branch back
- **THEN** `merge_loop_branch` no longer exists
- **AND** the heavy `se3 merge` orchestrator performs the merge-back instead

### Requirement: LLM Conflict Resolution (Basic)

**DEPRECATED — removed.** The basic per-file conflict-marker stripper (`_resolve_conflicts_with_llm`) used by the removed loop merge-back has been removed along with loop mode. The remaining conflict-resolution path is the richer `resolve_merge_conflicts_with_context` (see *Context-Aware LLM Conflict Resolution*), used by the heavy `se3 merge` orchestrator.

#### Scenario: Basic LLM conflict resolver removed
- **WHEN** merge conflicts must be resolved by an LLM
- **THEN** `_resolve_conflicts_with_llm` no longer exists
- **AND** the context-aware `resolve_merge_conflicts_with_context` resolver is used instead

### Requirement: Context-Aware LLM Conflict Resolution

`resolve_merge_conflicts_with_context` provides a richer resolution path that includes task description, per-group summaries, spec content, and retry logic.

#### Scenario: Prompt composition

- **WHEN** each conflicting file is processed
- **THEN** the prompt includes sections `## Task Description`, `## What Each Group Did`, `## Project Conventions`, and `## Conflicting File: {filepath}` followed by the file content in fences
- **AND** group summaries are rendered as `- Group {id}: {summary} (files: {files_changed_joined})`; if `group_summaries` is empty the literal text `"No group context available."` is used

#### Scenario: Skipping already-resolved files

- **WHEN** a file in `conflict_files` no longer contains `<<<<<<<`
- **THEN** it is silently skipped for this attempt (no LLM call, no failure)

#### Scenario: Retry loop with checkout reset

- **WHEN** an attempt fails (LLM error, missing markers in output, or commit failure)
- **THEN** for each filepath already resolved in that attempt, `git checkout --merge -- {filepath}` resets the conflict state before retrying
- **AND** up to `max_retries` attempts (default 3) are made
- **AND** `external_attempt=attempt - 1` is passed to `LLMCaller` for history recording
- **AND** the function never falls back to `--theirs`

#### Scenario: Optional flow and step identifiers for history recording

- **WHEN** `resolve_merge_conflicts_with_context` is invoked
- **THEN** it accepts optional `flow_id: str | None = None` and `step_id: str | None = None` keyword arguments
- **AND** both values are forwarded to `LLMCaller` on every per-file call (alongside `external_attempt`) so that conflict-resolution LLM calls are correlated with the originating flow and step in history records
- **AND** when either argument is omitted, `None` is passed through and `LLMCaller` records the call without that correlation

#### Scenario: Successful resolution writes and commits

- **WHEN** every file is resolved in an attempt
- **THEN** all resolved contents are written and `git add`-ed
- **AND** `git commit --no-edit` runs; success returns `True`, failure logs a warning and continues to the next attempt

### Requirement: Conflict and Unmerged-Index Detection

Two related queries surface conflicting paths from different vantage points.

#### Scenario: Worktree-vs-index conflicts

- **WHEN** `get_conflicting_files(project_root)` is called
- **THEN** it runs `git diff --name-only --diff-filter=U`
- **AND** returns a list of stripped non-empty path lines (empty list on git failure or empty output)

#### Scenario: Index-level unmerged paths

- **WHEN** `detect_unmerged_paths(project_root)` is called
- **THEN** it runs `git ls-files --unmerged` and parses paths from the tab-delimited stage entries
- **AND** returns a deduplicated, sorted list — surfacing modify/delete combinations that the worktree-vs-index diff can miss

### Requirement: Stale Unmerged-Index Recovery

`recover_stale_unmerged_paths` clears unmerged-index leftovers from a prior abandoned merge only when doing so is a content no-op.

#### Scenario: Working-tree matches HEAD blob

- **WHEN** a path's working-tree file hashes (via `git hash-object`) to the same blob HEAD has for that path
- **THEN** `git add -- {path}` is run to mark resolved
- **AND** the path is appended to the `resolved` list on success; otherwise to `unresolved`

#### Scenario: Path exists on neither side

- **WHEN** the working-tree path is absent AND `git ls-tree HEAD -- {path}` shows no blob for it
- **THEN** `git rm --cached -- {path}` clears the index entry
- **AND** success appends to `resolved`; failure to `unresolved`

#### Scenario: Divergent content requires human

- **WHEN** working-tree content differs from HEAD (and is not the both-absent case)
- **THEN** the path is appended to `unresolved` without any index mutation

#### Scenario: No-op when no unmerged paths

- **WHEN** `detect_unmerged_paths` returns an empty list
- **THEN** `recover_stale_unmerged_paths` returns `([], [])` without invoking any git commands

#### Scenario: Caller must gate on merge_in_progress

- **WHEN** an active merge marker exists
- **THEN** the docstring contract requires callers to check `merge_in_progress` separately; this function does not itself check it

### Requirement: Loop Branch Listing

**DEPRECATED — removed.** The loop branch listing helper (`list_loop_branches`) has been removed along with loop mode; there is no longer a `loop/*` / `se3-loop/*` branch convention to enumerate. Commit-count comparisons against a base branch remain available via the generic *Ahead-of-Base Check* and *Branch Diff Stat* primitives.

#### Scenario: Loop branch listing removed
- **WHEN** a caller wants to inspect isolation branches
- **THEN** `list_loop_branches` no longer exists
- **AND** the generic ahead-of-base / diff-stat queries are used directly on the relevant branch

### Requirement: Branch Diff Stat

#### Scenario: Diff stat summary

- **WHEN** `get_diff_stat(project_root, branch, base_branch)` is called
- **THEN** it returns the stripped stdout of `git diff --stat {base_branch}..{branch}`
- **AND** returns an empty string on git failure

### Requirement: Ahead-of-Base Check

#### Scenario: Counting new commits

- **WHEN** `has_new_commits(project_root, branch, base_branch)` is called
- **THEN** it runs `git rev-list --count {base_branch}..{branch}`
- **AND** returns `True` iff the parsed count is greater than zero
- **AND** returns `False` if the git command fails

### Requirement: WorktreeContext Manager

A context manager wraps creation and exception-safe cleanup of a worktree while preserving the branch for recovery. It is reused by `se3 run --worktree` so that a failed or interrupted isolated run leaves its worktree branch intact for a later `se3 run --resume`.

#### Scenario: Enter validates and creates

- **WHEN** `WorktreeContext(project_root, branch).__enter__()` runs
- **THEN** if `exists_for_branch` returns `True`, it raises `RuntimeError("Worktree already exists for branch {branch}. Remove it first or use a different branch.")`
- **AND** otherwise `create_worktree` runs and the worktree path is returned

#### Scenario: Exit always removes worktree, preserves branch

- **WHEN** `__exit__` runs (whether or not an exception is propagating)
- **THEN** if a worktree path was set, `remove_worktree` runs
- **AND** the branch itself is NOT deleted, regardless of exception state
- **AND** a log line records whether an exception was active