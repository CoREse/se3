<!-- spec-format: v1 -->
# worktree-management Specification

## Purpose

The worktree-management subsystem provides the git branch and worktree lifecycle primitives that back SE3's loop mode and merge isolation. It owns loop branch naming (with slugification of task IDs), git worktree creation with retry/timeout handling, isolated execution via a context manager, merge-back of loop results with human or LLM-based conflict resolution, branch deletion gated on worktree state, and resilient multi-step cleanup of orphaned worktrees, locked worktrees, and stale `.git/worktrees/` metadata. It also exposes repository state queries (current branch, merge-in-progress detection, unmerged-index detection) and an auto-resolver for stale unmerged-index leftovers from prior aborted merges.

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

The subsystem produces two branch-name shapes and treats the legacy shape as deprecated but functional.

#### Scenario: New naming convention

- **WHEN** `create_loop_branch` is called with both `task_id` and `iteration`
- **THEN** the branch is named `loop/{slug}-{iteration}` where `{slug}` is the slugified `task_id`
- **AND** if slugification yields an empty string, the slug defaults to `"task"`

#### Scenario: Legacy naming fallback

- **WHEN** `create_loop_branch` is called without `task_id` or without `iteration`
- **THEN** the branch is named `se3-loop/{timestamp}` using the supplied timestamp or `datetime.now().strftime("%Y%m%d-%H%M%S")` if none

#### Scenario: Task ID slugification rules

- **WHEN** `_slugify_task_id(task)` runs
- **THEN** the result is lowercased
- **AND** any run of non-`[a-z0-9]` characters becomes a single `-`
- **AND** leading and trailing hyphens are stripped
- **AND** consecutive hyphens are collapsed to one
- **AND** the result is truncated to 30 characters and any trailing `-` from truncation is stripped

#### Scenario: Branch creation from current HEAD

- **WHEN** `create_loop_branch` runs
- **THEN** it records the current branch as `original_branch` via `get_current_branch`
- **AND** runs `git branch {branch_name}` to create the new ref at HEAD
- **AND** returns `(loop_branch_name, original_branch_name)`

### Requirement: Worktree Creation

Worktrees are created under `{project_root}/se3/worktrees/{safe_name}` where `safe_name` replaces `/` with `-`. Creation prunes stale entries beforehand and retries on timeout.

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

#### Scenario: Cleanup with optional branch delete

- **WHEN** `cleanup_loop(project_root, loop_branch, worktree_path, delete_branch_flag)` is called
- **THEN** `remove_worktree` runs first
- **AND** `delete_branch` runs only if `delete_branch_flag` is `True`

### Requirement: Merge-Back of Loop Branches

`merge_loop_branch` checks out the target, stashes uncommitted changes, and runs a non-interactive merge. The conflict outcome depends on the requested strategy.

#### Scenario: Pre-merge stash and checkout

- **WHEN** `merge_loop_branch` runs and the current branch differs from `target_branch`
- **THEN** `git checkout {target_branch}` runs first
- **AND** `git stash --include-untracked` runs; a stash is considered created when the exit is zero and stdout does not contain `"No local changes"`

#### Scenario: Clean merge restores stash

- **WHEN** `git merge {loop_branch} --no-edit -m "Merge loop branch {loop_branch}"` succeeds
- **THEN** if a stash was created, `git stash pop` runs
- **AND** the function returns `True`
- **AND** a `git stash pop` conflict is logged at warning level but is not raised

#### Scenario: Non-conflict merge failure

- **WHEN** the merge fails and stdout/stderr do not contain `"CONFLICT"`
- **THEN** `git merge --abort` runs
- **AND** any stash is popped
- **AND** the function returns `False`

#### Scenario: Conflict with `human` strategy

- **WHEN** the merge fails with `"CONFLICT"` in stdout/stderr and `conflict_strategy == "human"`
- **THEN** the conflict state is preserved (no abort, no stash pop)
- **AND** a Rich-formatted block is rendered with the loop/target branches and conflicting files (with a plain-print fallback if Rich is unavailable)
- **AND** a call file `se3/calls/merge_conflict_{ts}.json` is written with type, ISO timestamp, loop_branch, target_branch, conflict_files, and human instructions
- **AND** the function returns the string `"pending_human"`

#### Scenario: Conflict with `llm` strategy succeeds

- **WHEN** `conflict_strategy == "llm"` and `_resolve_conflicts_with_llm` resolves every file
- **THEN** any stash is popped
- **AND** the function returns `True`

#### Scenario: Conflict with `llm` strategy falls back to human

- **WHEN** `conflict_strategy == "llm"` and LLM resolution does not resolve all files
- **THEN** the same human-mode behavior runs (display + call file)
- **AND** the function returns `"pending_human"`

### Requirement: LLM Conflict Resolution (Basic)

`_resolve_conflicts_with_llm` issues a per-file LLM call to strip conflict markers. It writes the resolved content, stages it, and finishes with `git commit --no-edit`.

#### Scenario: Per-file prompt and verification

- **WHEN** each conflicting file is processed
- **THEN** the prompt includes the file path and full content inside fenced code, and instructs the model to emit only the resolved content with no markers or explanation
- **AND** if the LLM output still contains `<<<<<<<` or `>>>>>>>` the function returns `False` immediately
- **AND** otherwise the resolved content is written and staged via `git add {filepath}`

#### Scenario: Missing LLMCaller dependency

- **WHEN** `from .llm_caller import LLMCaller` raises `ImportError`
- **THEN** a warning is logged and the function returns `False`

#### Scenario: Final merge commit

- **WHEN** every file is resolved without error
- **THEN** `git commit --no-edit` is run
- **AND** the function returns `True` only when that commit exits zero

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

#### Scenario: Listing both naming conventions

- **WHEN** `list_loop_branches(project_root)` is called
- **THEN** `git branch --list loop/*` and `git branch --list se3-loop/*` are queried in turn
- **AND** every matched branch yields a dict with keys `branch`, `commit_count`, `base_branch`, `is_legacy`
- **AND** `commit_count` is `git rev-list --count {current_branch}..{branch_name}` (defaults to 0 on failure)
- **AND** `base_branch` is the current branch as resolved by `get_current_branch`
- **AND** legacy `se3-loop/*` branches set `is_legacy=True` and emit a warning log noting the new format

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

A context manager wraps creation and exception-safe cleanup of a worktree while preserving the branch for recovery.

#### Scenario: Enter validates and creates

- **WHEN** `WorktreeContext(project_root, branch).__enter__()` runs
- **THEN** if `exists_for_branch` returns `True`, it raises `RuntimeError("Worktree already exists for branch {branch}. Remove it first or use a different branch.")`
- **AND** otherwise `create_worktree` runs and the worktree path is returned

#### Scenario: Exit always removes worktree, preserves branch

- **WHEN** `__exit__` runs (whether or not an exception is propagating)
- **THEN** if a worktree path was set, `remove_worktree` runs
- **AND** the branch itself is NOT deleted, regardless of exception state
- **AND** a log line records whether an exception was active