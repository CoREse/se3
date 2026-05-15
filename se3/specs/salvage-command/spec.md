<!-- spec-format: v1 -->
# salvage-command Specification

## Purpose

The `se3 salvage` command implements a best-effort rescue pipeline for abnormally terminated SE3 sessions. It runs a fixed sequence of five independent recovery steps — locate the project root, tolerantly load session state, assess uncommitted git changes, commit those changes, create tracking issues for unfinished work, and archive the session — each guarded by its own exception handler so failures in one step do not abort the others. Results are presented as a Rich-formatted summary table and a non-zero exit code is returned if any step failed.

## Requirements

### Requirement: Project Root Resolution

When invoked without an explicit project root, the command walks from the current working directory upward through its ancestors and selects the first directory that either contains a `.git` entry or is recognized as an SE3 project root (via `is_se3_project_root`, which checks for `se3.yaml`, `se3.local.yaml`, or `se3.config.yaml`). If no such directory is found, the command aborts before running the pipeline.

#### Scenario: Explicit project root supplied
- **WHEN** `salvage()` is called with a non-`None` `project_root`
- **THEN** the supplied path is coerced to `Path` and used directly
- **AND** no parent-directory walk is performed

#### Scenario: Auto-detect from current directory
- **WHEN** `salvage()` is called with `project_root=None`
- **THEN** the function inspects `Path.cwd()` and each parent in order
- **AND** the first ancestor containing `.git` or an SE3 config file is selected

#### Scenario: No project root found
- **WHEN** auto-detection finds neither `.git` nor an SE3 config in any ancestor
- **THEN** a red error message naming `.git`, `se3.yaml`, `se3.local.yaml`, and `se3.config.yaml` is printed
- **AND** the function returns exit code `1` without invoking any pipeline step

### Requirement: Independent Per-Step Fault Tolerance

Every pipeline step (read session, assess git diff, commit changes, create issues, archive session) is wrapped in its own `try`/`except Exception`. A raised exception in one step records a `FAIL` row with the exception message truncated to 80 characters and logs a warning at `WARNING` level, but execution continues with the next step.

#### Scenario: A step raises an exception
- **WHEN** any step's helper function raises `Exception`
- **THEN** a `(step_name, "FAIL", str(e)[:80])` tuple is appended to results
- **AND** a warning is logged identifying the step number and exception
- **AND** the next step still executes

#### Scenario: Final exit code reflects any failure
- **WHEN** the pipeline finishes
- **THEN** if any results row has status `"FAIL"`, the function returns `1`
- **AND** otherwise it returns `0` (including when steps are `SKIP`)

### Requirement: Tolerant Session State Loading

Step 1 reads the persisted flow via `PersistenceManager.load_flow_tolerant`, which returns a `(flow, warnings)` tuple. The flow may be `None` if no session is present. All returned warnings are logged at `INFO` level.

#### Scenario: Flow loaded successfully
- **WHEN** `load_flow_tolerant` returns a non-`None` flow
- **THEN** the result is recorded as `("Read session", "OK", f"Flow {flow.flow_id}")`

#### Scenario: No session present
- **WHEN** `load_flow_tolerant` returns `(None, warnings)`
- **THEN** the result is recorded as `("Read session", "SKIP", "No session found, using git diff")`
- **AND** the pipeline continues with `flow=None`

#### Scenario: Warnings emitted during load
- **WHEN** the returned warnings list is non-empty
- **THEN** each warning is logged at `INFO` level with the prefix `"Session load warning:"`

### Requirement: Git Diff Assessment

Step 2 invokes three `git` subprocesses with `cwd=project_root` to populate a diff info dict:
- `git status --porcelain` — non-empty lines are stored as `status_lines`, their count as `changed_file_count`, and file paths (substring from index 3 onward, with trailing whitespace stripped) as `changed_files`.
- `git diff --stat` — stdout stored as `diff_stat` after `.strip()` removes leading and trailing whitespace.
- `git diff HEAD` — stdout truncated to the first 4000 characters stored as `diff_summary`.

#### Scenario: Files are modified
- **WHEN** `git status --porcelain` returns one or more lines
- **THEN** `changed_file_count` is the line count
- **AND** the result is recorded as `("Assess git diff", "OK", f"{n} files changed")`

#### Scenario: Working tree clean
- **WHEN** `git status --porcelain` returns no non-empty lines
- **THEN** `changed_file_count` is `0`
- **AND** the result is recorded as `("Assess git diff", "OK", "No uncommitted changes")`

#### Scenario: Diff summary truncated
- **WHEN** `git diff HEAD` stdout exceeds 4000 characters
- **THEN** only the first 4000 characters are kept in `diff_summary`

#### Scenario: Changed file paths trimmed
- **WHEN** a porcelain status line is parsed
- **THEN** the substring beginning at index 3 has trailing whitespace stripped before being stored in `changed_files`

#### Scenario: Diff stat whitespace stripped
- **WHEN** `git diff --stat` stdout has leading or trailing whitespace (including a trailing newline)
- **THEN** the value stored in `diff_stat` is the stdout with `.strip()` applied

### Requirement: Salvage Commit

Step 3 commits any uncommitted changes under a constructed salvage message when `changed_file_count > 0`. Staging uses `git add -A`, and committing uses `git commit -m <msg> --no-verify` (bypassing hooks). The commit message header is `[salvage] <task_desc>` where `task_desc` is the first 80 characters of `flow.task_description` if a flow was loaded, else the literal `"unknown task"`. The message body is `Salvage commit: <n> files from interrupted session.`. On success the function returns the full HEAD commit hash via `git rev-parse HEAD`; only the first 8 characters appear in the displayed detail.

#### Scenario: Nothing to commit
- **WHEN** `diff_info["changed_file_count"]` is `0`
- **THEN** the helper returns `None` without invoking git
- **AND** the result is recorded as `("Commit changes", "SKIP", "Nothing to commit")`

#### Scenario: Commit succeeds
- **WHEN** there are changes and `git commit` exits with code `0`
- **THEN** all changes are staged via `git add -A` first
- **AND** the commit is created with `--no-verify`
- **AND** the resolved HEAD hash is returned
- **AND** the result is recorded as `("Commit changes", "OK", f"Committed: {hash[:8]}")`

#### Scenario: Commit reports nothing to commit
- **WHEN** `git commit` exits non-zero and stdout+stderr contains the phrase `"nothing to commit"`
- **THEN** the helper returns `None` (treated as no-op)

#### Scenario: Commit fails for another reason
- **WHEN** `git commit` exits non-zero and the output does not contain `"nothing to commit"`
- **THEN** a warning is logged with the stderr contents
- **AND** the helper returns `None`

#### Scenario: Commit message uses flow task description
- **WHEN** a flow was loaded
- **THEN** the header is `f"[salvage] {flow.task_description[:80]}"`

#### Scenario: Commit message without flow
- **WHEN** `flow` is `None`
- **THEN** the header is `"[salvage] unknown task"`

### Requirement: Salvage Issue Creation

Step 4 creates `auto-discovered`, `source:salvage`-tagged, `medium`-priority issues via `IssueManager.create` to track unfinished work. The shape of the issue depends on whether a flow was loaded.

#### Scenario: Flow available — full report issue
- **WHEN** a flow was loaded
- **THEN** exactly one issue is created with title `f"Incomplete: {flow.task_description[:80]}"`
- **AND** the description begins with `"Session interrupted while working on: {flow.task_description}"`
- **AND** for each `step_id` in `flow.state.step_history` that resolves to an entry in `flow.state.steps`, a completed-step line `f"- {step_type}: {status}"` is built (using enum `.value` where available); if at least one such line exists, a `"**Step history:**"` section listing them is added
- **AND** if the current step is identified within history, an `"**Interrupted at step:** {step_type}"` line is added
- **AND** if `diff_info["changed_files"]` is non-empty, a `"**Changed files:**"` section lists up to 20 files, with a `"- ... and N more"` line when more exist

#### Scenario: No flow but uncommitted changes
- **WHEN** `flow` is `None` and `diff_info["changed_file_count"] > 0`
- **THEN** one issue is created with title `"Incomplete: interrupted session (no session state)"`
- **AND** the description notes the absence of session state and lists up to 20 changed files

#### Scenario: No flow and no changes
- **WHEN** `flow` is `None` and there are no changed files
- **THEN** no issue is created
- **AND** the result is recorded as `("Create issues", "SKIP", "No issues to create")`

#### Scenario: Issues created — result row
- **WHEN** one or more issues were created
- **THEN** the result is recorded as `("Create issues", "OK", f"Created: {ids}")` where `ids` is a comma-separated list of `issue.id` values

### Requirement: Session Archival

Step 5 clears the persisted session state if a state file exists. It uses `PersistenceManager.clear_state()` and only acts when `pm.state_file.exists()`.

#### Scenario: State file present
- **WHEN** `PersistenceManager.state_file` exists
- **THEN** `clear_state()` is invoked
- **AND** the helper returns `True`
- **AND** the result is recorded as `("Archive session", "OK", "Session archived")`

#### Scenario: No state file
- **WHEN** `PersistenceManager.state_file` does not exist
- **THEN** `clear_state()` is not called
- **AND** the result is recorded as `("Archive session", "SKIP", "No session to archive")`

### Requirement: Results Reporting

After all pipeline steps complete, results are rendered as a Rich `Table` titled `"Salvage Results"` with columns `Step` (cyan), `Status` (bold), and `Detail`, surrounded by blank lines on the console.

#### Scenario: Status styling
- **WHEN** rendering a results row
- **THEN** `OK` is wrapped in green markup, `SKIP` in yellow, `FAIL` in red
- **AND** any unrecognized status value is displayed verbatim

#### Scenario: Table rendering
- **WHEN** `_display_results` is invoked
- **THEN** a blank line, the table, and another blank line are printed to the shared `Console`
- **AND** rows appear in pipeline execution order (read session → assess git diff → commit → issues → archive)