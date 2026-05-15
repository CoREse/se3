<!-- spec-format: v1 -->
# issue-management Specification

## Purpose

The issue-management subsystem provides the `se3 issue` CLI and the underlying `IssueManager` storage API for SE3 project issues. Issues are persisted as YAML files under `se3/issues/open/` and `se3/issues/closed/`, with monotonic zero-padded numeric IDs maintained via a counter file. The subsystem exposes commands to list, show, create (interactively), and reset issues, and provides programmatic primitives (create, load, list, update status, close, lookup, tag filtering) consumed both by the CLI and by automatic discovery flows. It also defines the canonical status enumeration, the legal state-transition graph, and which statuses live in the `closed/` directory on disk.

## Requirements

### Requirement: Issue Data Model

An issue is represented by the `Issue` dataclass with fields: `id` (string, zero-padded 3-digit), `title`, `description`, `status` (`IssueStatus` enum), `priority` (defaults `"medium"`), `scope` (defaults `"in_scope"`), `type` (defaults `"bug"`), `tags` (list of strings), `created_at`, and `updated_at` (datetimes). Issues serialize to and from YAML dictionaries via `to_dict` and `from_dict`, where status is stored by its string value and timestamps as ISO-format strings.

#### Scenario: Default field values on construction
- **WHEN** an `Issue` is constructed with only `id`, `title`, and `description`
- **THEN** `status` defaults to `IssueStatus.OPEN`
- **AND** `priority` defaults to `"medium"`
- **AND** `scope` defaults to `"in_scope"`
- **AND** `type` defaults to `"bug"`
- **AND** `tags` defaults to an empty list
- **AND** `created_at` and `updated_at` default to the current time

#### Scenario: Round-trip via dict
- **WHEN** an issue is serialized with `to_dict()` and re-hydrated with `Issue.from_dict()`
- **THEN** `status` is converted to/from its string value
- **AND** `created_at` and `updated_at` are converted to/from ISO-format strings
- **AND** missing optional fields in the input dict fall back to defaults (`description=""`, `status="open"`, `priority="medium"`, `scope="in_scope"`, `type="bug"`, `tags=[]`)
- **AND** if timestamps are missing or invalid, they fall back to `datetime.now()`

### Requirement: Issue Status Enumeration

The `IssueStatus` enum defines five statuses: `OPEN ("open")`, `IN_PROGRESS ("in-progress")`, `RESOLVED ("resolved")`, `WONT_FIX ("won't-fix")`, and `CLOSED ("closed")`. The constant `KNOWN_TYPES` enumerates recommended issue types (`bug`, `feature`, `enhancement`, `idea`, `task`), but the `type` field is free-form and not enforced.

#### Scenario: Statuses considered "closed" on disk
- **WHEN** an issue has status `RESOLVED`, `WONT_FIX`, or `CLOSED`
- **THEN** it is stored under `se3/issues/closed/`
- **AND** all other statuses (`OPEN`, `IN_PROGRESS`) are stored under `se3/issues/open/`

### Requirement: State Transition Graph

`IssueManager` enforces valid transitions between statuses. Allowed transitions are:
- `OPEN` → `IN_PROGRESS`, `WONT_FIX`, `CLOSED`
- `IN_PROGRESS` → `OPEN`, `RESOLVED`, `WONT_FIX`
- `RESOLVED` → `CLOSED`, `OPEN`
- `WONT_FIX` → `OPEN`, `CLOSED`
- `CLOSED` → `OPEN`

#### Scenario: Invalid transition rejected
- **WHEN** `update_status` is called with a `new_status` not in the valid transitions list for the issue's current status
- **THEN** a `ValueError` is raised whose message names the attempted transition and lists the valid next states

#### Scenario: Valid transition applied
- **WHEN** `update_status` is called with a valid target status
- **THEN** the issue's `status` is updated and `updated_at` is set to the current time
- **AND** the YAML file is rewritten in place
- **AND** if the new status moves the file to a different directory (open ↔ closed), the file is moved via `shutil.move`
- **AND** on move failure (`OSError`), the YAML update is preserved and a warning is logged (update_status does not raise)

### Requirement: Storage Layout

Issues are stored as YAML files under `<project_root>/se3/issues/`, split into `open/` and `closed/` subdirectories. File names follow the pattern `{id}_{slug}.yaml` where `id` is a zero-padded 3-digit integer and `slug` is derived from the title. A counter file at `se3/issues/.next_id` tracks the next sequential ID.

#### Scenario: Directories created on demand
- **WHEN** `create` is called and `se3/issues/open/` or `se3/issues/closed/` does not exist
- **THEN** both directories are created with `parents=True, exist_ok=True`

#### Scenario: Slug generation
- **WHEN** a filename slug is generated from a title
- **THEN** the first 30 characters are used, lowercased, with non-alphanumeric runs collapsed to single hyphens
- **AND** leading and trailing hyphens are stripped
- **AND** if the result would be empty, the slug is `"untitled"`

#### Scenario: Monotonic ID assignment via counter file
- **WHEN** `_next_id` is called and `.next_id` exists with an integer value `N`
- **THEN** the returned ID is `N` formatted as a 3-digit zero-padded string
- **AND** the counter file is updated to `N+1`

#### Scenario: Counter bootstrap on first run
- **WHEN** `_next_id` is called and `.next_id` does not exist (or is unreadable/invalid)
- **THEN** all `*.yaml` files in both `open/` and `closed/` are scanned for a leading numeric prefix
- **AND** the next ID is `max(existing) + 1` (or `1` if none exist)
- **AND** the counter file is written with the value following the assigned ID

### Requirement: Issue Creation

`IssueManager.create()` creates a new issue and writes it to the `open/` directory regardless of project state.

#### Scenario: Programmatic create
- **WHEN** `mgr.create(title, description, priority, scope, tags, type)` is called
- **THEN** a new `Issue` is constructed with `status=OPEN`, a freshly allocated ID, and `created_at`/`updated_at` set to now
- **AND** the YAML file `{id}_{slug}.yaml` is written under `open/`
- **AND** an info log line records `"Created issue {id}: {title}"`
- **AND** the created `Issue` is returned

### Requirement: Issue Lookup by ID

`IssueManager.load()` and the internal `_find_issue_file()` locate issue files by ID across both `open/` and `closed/`. ID matching tolerates differences in zero-padding (e.g., `"5"` matches `"005"`).

#### Scenario: Load by exact ID
- **WHEN** `load("042")` is called and a file `042_something.yaml` exists in either directory
- **THEN** the file is parsed and returned as an `Issue`

#### Scenario: Load tolerates unpadded IDs
- **WHEN** `load("5")` is called and a file `005_*.yaml` exists
- **THEN** the file is matched and returned (leading zeros are stripped on both sides for comparison)

#### Scenario: Issue not found
- **WHEN** no file with the requested ID exists in either directory
- **THEN** `load` returns `None`

#### Scenario: Malformed YAML
- **WHEN** an issue file fails to parse as YAML, has a non-dict payload, or is missing required keys
- **THEN** `_read_issue` returns `None` and logs a warning
- **AND** `list_issues` silently skips such files

### Requirement: Listing Issues

`IssueManager.list_issues()` returns issues sorted by ID. By default only open issues are listed; `include_closed=True` extends the scan to the `closed/` directory. A `type_filter` argument restricts results to a single issue type.

#### Scenario: Default lists only open
- **WHEN** `list_issues()` is called with no arguments
- **THEN** only files in `open/` are scanned
- **AND** results are sorted by issue `id`

#### Scenario: Include closed
- **WHEN** `list_issues(include_closed=True)` is called
- **THEN** both `open/` and `closed/` are scanned

#### Scenario: Filter by type
- **WHEN** `list_issues(type_filter="bug")` is called
- **THEN** only issues whose `type` exactly equals `"bug"` are returned

### Requirement: Tag-Based Listing

`IssueManager.list_by_tags()` returns issues that contain ALL specified tags (subset match).

#### Scenario: All tags required
- **WHEN** `list_by_tags(["a", "b"])` is called
- **THEN** only issues whose tags include both `"a"` and `"b"` are returned
- **AND** results are sorted by ID

#### Scenario: Empty tag list delegates
- **WHEN** `list_by_tags([])` is called
- **THEN** the method delegates to `list_issues` with the same `include_closed` flag

### Requirement: Open-Issue Title Lookup

`IssueManager.find_open_by_title()` returns the first open issue whose title equals the given title, case-insensitively.

#### Scenario: Case-insensitive exact match
- **WHEN** `find_open_by_title("Fix Login Bug")` is called and an open issue with title `"fix login bug"` exists
- **THEN** that issue is returned

#### Scenario: No match or empty title
- **WHEN** the title is empty, the `open/` directory does not exist, or no open issue's title matches
- **THEN** `None` is returned

### Requirement: Reset In-Progress Issue

`IssueManager.reset_to_open()` transitions an in-progress issue back to open, including moving its file from `closed/` to `open/` if needed.

#### Scenario: Reset valid in-progress issue
- **WHEN** `reset_to_open(id)` is called on an issue with status `IN_PROGRESS`
- **THEN** the issue's status becomes `OPEN` and `updated_at` is refreshed

#### Scenario: Reset rejects non-in-progress issues
- **WHEN** `reset_to_open(id)` is called on an issue whose status is not `IN_PROGRESS`
- **THEN** a `ValueError` is raised with a message indicating the current status

#### Scenario: Reset missing issue
- **WHEN** `reset_to_open(id)` is called and no issue file with that ID exists
- **THEN** a `ValueError` is raised with the message `"Issue '{id}' not found"`

### Requirement: Closing an Issue

`IssueManager.close_issue()` transitions an open or in-progress issue to a closed-directory status. It is idempotent: closing an already-closed issue returns the existing issue unchanged.

#### Scenario: Already closed is a no-op
- **WHEN** `close_issue(id)` is called on an issue whose status is already `RESOLVED`, `WONT_FIX`, or `CLOSED`
- **THEN** the existing issue is returned without modification

#### Scenario: Prefer CLOSED, fall back to RESOLVED
- **WHEN** `close_issue(id)` is called and `CLOSED` is a valid transition from the current status
- **THEN** the status is set to `CLOSED`
- **AND** if `CLOSED` is not valid but `RESOLVED` is, the status is set to `RESOLVED` instead
- **AND** if neither transition is valid, a `ValueError` is raised

#### Scenario: File moved to closed directory
- **WHEN** an issue is successfully closed
- **THEN** the YAML file is rewritten with the new status
- **AND** the file is moved into `se3/issues/closed/` via `shutil.move`
- **AND** on `OSError` during move the error is re-raised (unlike `update_status`, which swallows it)

#### Scenario: Optional reason recorded in log
- **WHEN** `close_issue(issue_id, reason)` is called with an optional `reason` string (defaulting to `""`)
- **THEN** on a successful close-and-move, an info log line is emitted in the form `"Closed issue {id}: {reason}"`
- **AND** the `reason` is not persisted to the issue's YAML file

### Requirement: YAML Persistence Format

Issue files are written with `yaml.dump(..., default_flow_style=False, allow_unicode=True, sort_keys=False)` so fields appear in dataclass declaration order, unicode is preserved, and block style is used. Files are read and written as UTF-8.

#### Scenario: Field order on write
- **WHEN** an issue is serialized to YAML
- **THEN** the keys appear in this order: `id`, `title`, `description`, `status`, `priority`, `scope`, `type`, `tags`, `created_at`, `updated_at`

### Requirement: `se3 issue` CLI

The `se3 issue` Typer app exposes sub-commands `list`, `show`, `create`, and `reset`. Invoking `se3 issue` with no sub-command lists open issues. The CLI locates the project root by walking up from the current working directory looking for a `.git` directory or an SE3 project marker (via `is_se3_project_root`).

#### Scenario: No subcommand defaults to list
- **WHEN** `se3 issue` is invoked without a sub-command
- **THEN** the default callback runs `list_cmd(show_all=False, type_filter=None)`

#### Scenario: `list` flags
- **WHEN** `se3 issue list` is invoked
- **THEN** only open issues are shown by default
- **AND** `--all` / `-a` includes closed issues
- **AND** `--type` / `-t <type>` filters to a specific type

#### Scenario: Empty list message
- **WHEN** the list result is empty
- **THEN** the message is `"No open issues found."` by default, or `"No issues found."` when `--all` is used

#### Scenario: List rendering
- **WHEN** issues are listed
- **THEN** a Rich table is printed with columns `ID`, `Title`, `Type`, `Status`, `Priority`, `Tags`, `Created`
- **AND** titles longer than 50 characters are truncated with an ellipsis (`"..."`)
- **AND** the table title is `"Open Issues"` by default or `"All Issues"` with `--all`
- **AND** the `Type`, `Status`, and `Priority` columns are colorized (e.g., `bug`=red, `feature`=green, `open`=yellow, `in-progress`=blue, `resolved`/`closed`=green, `won't-fix`=dim, `critical`=red bold, `high`=red, `medium`=yellow, `low`=dim)

#### Scenario: `show` displays full detail
- **WHEN** `se3 issue show <id>` is invoked and the issue exists
- **THEN** title, type, status, priority, tags, created/updated timestamps (formatted `"%Y-%m-%d %H:%M"`), and description are printed inside a Rich block bordered by `render_block_header` / `render_block_footer` in cyan
- **AND** the tags line reads `"none"` if there are no tags

#### Scenario: `show` for missing ID
- **WHEN** `se3 issue show <id>` is invoked and no such issue exists
- **THEN** `"Issue '<id>' not found."` is printed to stderr and the process exits with code 1

#### Scenario: `create` interactive flow
- **WHEN** `se3 issue create` is invoked
- **THEN** the user is prompted (in order) for `Title`, `Description`, `Type` (default `bug`, prompt lists `KNOWN_TYPES`), `Priority` (default `medium`), and `Tags` (comma-separated, default empty)
- **AND** in TTY mode, input uses `_read_multiline_input` (from `cli`): Description naturally accepts multiple lines, all fields submit with Ctrl+D, cancel with Ctrl+C
- **AND** any field cancelled with Ctrl+C prints `"Cancelled."` and exits with code 1
- **AND** in non-TTY mode (piped stdin, tests), each field reads a single line from `sys.stdin`
- **AND** empty input for any field falls back to its default value
- **AND** the tags string is split on commas, individual entries are stripped, and empty entries are discarded
- **AND** the new issue is created with the provided fields and `"Created issue {id}: {title}"` is printed

#### Scenario: `reset` valid issue
- **WHEN** `se3 issue reset <id>` is invoked on an in-progress issue
- **THEN** the issue is transitioned to `OPEN` and `"Issue {id} reset to open."` is printed

#### Scenario: `reset` failure
- **WHEN** `reset_to_open` raises `ValueError` (issue missing, or not in-progress)
- **THEN** `"Error: <message>"` is printed to stderr and the process exits with code 1

### Requirement: Project Root Resolution

The CLI's `get_project_root()` walks up from the current working directory to find a parent containing either a `.git` directory or recognized as an SE3 project root by `is_se3_project_root`. If neither is found, the current working directory is used.

#### Scenario: Git repo discovery
- **WHEN** any ancestor of the CWD contains a `.git` directory
- **THEN** that ancestor is returned as the project root

#### Scenario: SE3 marker discovery
- **WHEN** no `.git` is found but an ancestor satisfies `is_se3_project_root`
- **THEN** that ancestor is returned

#### Scenario: Fallback
- **WHEN** neither marker is found in any ancestor
- **THEN** the CWD itself is returned