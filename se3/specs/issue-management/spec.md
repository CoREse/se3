<!-- spec-format: v1 -->
# issue-management Specification

## Purpose

The issue-management subsystem provides the `se3 issue` CLI and the underlying `IssueManager` storage API for SE3 project issues. Issues are persisted as YAML files under `se3/issues/open/` and `se3/issues/closed/`, with monotonic zero-padded numeric IDs maintained via a counter file. The subsystem exposes commands to list (with `--type` / `--source` filters), show, create (a single-step command taking a positional/stdin/interactive description plus optional flags, or `--editor` for full external-editor editing), edit, close, and reset issues, and provides programmatic primitives (create, load, list, update fields, update status, close, reopen, lookup, tag filtering) consumed by the CLI, the webui issue API, and automatic discovery flows. Issues carry an optional `title`/`priority`/`type` (only `description` is required) and a `source` (`human` / `system`) origin. It also defines the canonical status enumeration, the legal state-transition graph, and which statuses live in the `closed/` directory on disk.

## Requirements

### Requirement: Issue Data Model

An issue is represented by the `Issue` dataclass with fields: `id` (string, zero-padded 3-digit), `title` (`Optional[str]`, defaults `None`), `description` (string), `status` (`IssueStatus` enum), `priority` (`Optional[str]`, defaults `None`), `type` (`Optional[str]`, defaults `None`), `tags` (list of strings), `source` (string, one of `"human"` / `"system"`, defaults `"system"`), `created_at`, and `updated_at` (datetimes). Only `description` is conceptually required — `title`, `priority`, and `type` are optional and their `None` value faithfully represents "not specified" rather than being coerced to a placeholder default. The model carries **no** `scope` field: the `in_scope` / `out_of_scope` classification is a transient, flow-relative relationship (the boundary between a finding and one particular flow), not an intrinsic property of a persisted issue, so it is never frozen onto the `Issue` record. Issues serialize to and from YAML dictionaries via `to_dict` and `from_dict`, where status is stored by its string value and timestamps as ISO-format strings. `from_dict` tolerates a legacy `scope:` key present in historical YAML — it is silently ignored (neither stored nor re-emitted), so old issue files load without error and shed the key on the next rewrite (no batch migration is performed).

**Optional fields are omitted from `to_dict()` when `None`:** `title`, `priority`, and `type` are written to the YAML dict only when they are not `None`, so an issue with no title/priority/type produces a YAML file that simply lacks those keys (rather than carrying `null` or a placeholder). `description`, `status`, `tags`, `source`, and the timestamps are always present.

**Derived display title.** Because `title` is optional, the `Issue.display_title` property derives a human-readable title with the priority: explicit `title` → the first non-empty line of `description` → the literal `"untitled"` only when both are empty. This replaces the previous `"untitled"` fallback as the *only* default; an issue created with just a description shows (and is filed under a slug derived from) the description's first line, never `"untitled"`.

**`source` field.** `source` records the issue's origin: `"human"` for issues created via `se3 issue create` or the webui, and `"system"` for issues created by programmatic discovery paths (see the issue-discovery *Two-class Discovery Model* requirement). It is used by list/detail filtering (`--source human|system`) and rendering. The field does not participate in the state-transition graph.

#### Scenario: Default field values on construction
- **WHEN** an `Issue` is constructed with only `id`, `title`, and `description`
- **THEN** `status` defaults to `IssueStatus.OPEN`
- **AND** `priority` defaults to `None`
- **AND** `type` defaults to `None`
- **AND** `tags` defaults to an empty list
- **AND** `source` defaults to `"system"`
- **AND** `created_at` and `updated_at` default to the current time

#### Scenario: Display title derived from description when title is absent
- **GIVEN** an `Issue` whose `title` is `None` and whose `description` begins with a non-empty first line
- **WHEN** `display_title` is read
- **THEN** it returns the first non-empty line of `description`
- **AND** when both `title` and `description` are empty it returns `"untitled"`

#### Scenario: Round-trip via dict
- **WHEN** an issue is serialized with `to_dict()` and re-hydrated with `Issue.from_dict()`
- **THEN** `status` is converted to/from its string value
- **AND** `created_at` and `updated_at` are converted to/from ISO-format strings
- **AND** `title`, `priority`, and `type` are omitted from `to_dict()` output when `None`, and re-hydrate to `None` when absent from the input dict
- **AND** other missing fields in the input dict fall back to defaults (`description=""`, `status="open"`, `tags=[]`)
- **AND** a legacy `scope:` key present in the input dict is ignored (neither stored on the `Issue` nor re-emitted by a subsequent `to_dict()`)
- **AND** a missing `source` key falls back to `"system"` (legacy YAML written before the field existed reads as `system`)
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
- **THEN** the slug source is the issue's `display_title` (explicit `title`, else the first non-empty line of `description`), so a title-less issue is still filed under a description-derived slug rather than `"untitled"`
- **AND** the first 30 characters of that source are used, lowercased, with non-alphanumeric runs collapsed to single hyphens
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

`IssueManager.create()` creates a new issue and writes it to the `open/` directory regardless of project state. Its signature is `create(description, *, title=None, priority=None, tags=None, type=None, source="system")`: only `description` is required (an empty or whitespace-only `description` raises `ValueError`), `title`/`priority`/`type` are optional and default to `None`, and `source` defaults to `"system"`. Programmatic callers therefore default to `system`; the CLI and webui pass `source="human"` explicitly (see the `se3 issue` CLI requirement and the base spec's *Server Modules* issue API).

#### Scenario: Programmatic create
- **GIVEN** the keyword-only signature `create(description, *, title=None, priority=None, tags=None, type=None, source="system")` where only `description` is required
- **WHEN** `mgr.create(title, description, priority, tags, type)` is called
- **THEN** a new `Issue` is constructed with `status=OPEN`, a freshly allocated ID, and `created_at`/`updated_at` set to now
- **AND** the YAML file `{id}_{slug}.yaml` is written under `open/`, the slug derived from `display_title`
- **AND** an info log line records the created issue's id and display title
- **AND** the created `Issue` is returned

#### Scenario: Create requires a non-empty description
- **WHEN** `create` is called with an empty or whitespace-only `description`
- **THEN** a `ValueError` is raised and no issue file is written

#### Scenario: Source defaults to system, callers may pass human
- **WHEN** `create` is called without a `source` argument
- **THEN** the created issue's `source` is `"system"`
- **AND** when called with `source="human"` (the CLI / webui create path) the created issue's `source` is `"human"`

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

`IssueManager.list_issues()` returns issues sorted by ID. By default only open issues are listed; `include_closed=True` extends the scan to the `closed/` directory. A `type_filter` argument restricts results to a single issue type, and a `source_filter` argument (`"human"` or `"system"`) restricts results to a single `source` origin.

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

#### Scenario: Filter by source
- **WHEN** `list_issues(source_filter="human")` is called
- **THEN** only issues whose `source` exactly equals `"human"` are returned
- **AND** `source_filter="system"` likewise returns only `system`-sourced issues (including legacy issues whose YAML lacked a `source` key, which read as `system`)

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

### Requirement: Field Update and Canonical Rename

`IssueManager.update_fields(issue_id, *, title, description, priority, type, tags)` edits the mutable fields of an existing issue and rewrites its YAML file in place, renaming the file when the derived slug changes. Only fields passed as non-`None` are applied; an omitted (`None`) field retains its current value, while an **empty string** clears `title` / `priority` / `type` back to `None`. `description` may be changed but may never be cleared — an empty/whitespace-only `description` raises `ValueError`. This method is the shared write primitive behind the CLI `se3 issue edit` and the webui edit operation.

#### Scenario: Selective field update preserves omitted fields
- **WHEN** `update_fields(id, priority="high")` is called
- **THEN** the issue's `priority` becomes `"high"` and `updated_at` is refreshed
- **AND** fields not passed (title, description, type, tags) keep their previous values

#### Scenario: Empty string clears an optional field
- **WHEN** `update_fields(id, title="")` is called on an issue with a title
- **THEN** the issue's `title` becomes `None`
- **AND** the display title falls back to the description's first non-empty line

#### Scenario: Description cannot be cleared
- **WHEN** `update_fields(id, description="")` is called
- **THEN** a `ValueError` is raised and the issue is left unchanged

#### Scenario: Slug rename on title change
- **WHEN** an update changes the effective `display_title` so its derived slug differs from the on-disk slug
- **THEN** the YAML file is rewritten under the canonical `{id}_{slug}.yaml` name (preserving the zero-padded stored ID) and the stale file is removed

#### Scenario: Update missing issue
- **WHEN** `update_fields` is called for an ID that does not exist
- **THEN** a `ValueError` is raised

### Requirement: Reopening an Issue

`IssueManager.reopen_issue()` transitions a closed-directory issue (`RESOLVED`, `WONT_FIX`, or `CLOSED`) back to `OPEN`, moving its file from `closed/` to `open/`. It is the inverse of `close_issue` and backs the webui reopen operation.

#### Scenario: Reopen a closed issue
- **WHEN** `reopen_issue(id)` is called on an issue whose status is `RESOLVED`, `WONT_FIX`, or `CLOSED`
- **THEN** the status becomes `OPEN`, `updated_at` is refreshed, and the YAML file is moved into `se3/issues/open/`

#### Scenario: Reopen missing issue
- **WHEN** `reopen_issue(id)` is called and no issue file with that ID exists
- **THEN** a `ValueError` is raised

### Requirement: YAML Persistence Format

Issue files are written with `yaml.dump(..., default_flow_style=False, allow_unicode=True, sort_keys=False)` so fields appear in dataclass declaration order, unicode is preserved, and block style is used. Files are read and written as UTF-8.

#### Scenario: Field order on write
- **WHEN** an issue is serialized to YAML
- **THEN** the always-present keys appear in this order: `id`, `description`, `status`, `tags`, `source`, `created_at`, `updated_at`
- **AND** the optional keys `title`, `priority`, and `type` are appended only when their value is not `None`, so a title-less / priority-less / type-less issue omits the corresponding key entirely

### Requirement: `se3 issue` CLI

The `se3 issue` Typer app exposes sub-commands `list`, `show`, `create`, `edit`, `close`, and `reset`. Invoking `se3 issue` with no sub-command lists open issues. The CLI locates the project root by walking up from the current working directory looking for a `.git` directory or an SE3 project marker (via `is_se3_project_root`).

#### Scenario: No subcommand defaults to list
- **WHEN** `se3 issue` is invoked without a sub-command
- **THEN** the default callback runs `list_cmd` with the open-only defaults

#### Scenario: `list` flags
- **WHEN** `se3 issue list` is invoked
- **THEN** only open issues are shown by default
- **AND** `--all` / `-a` includes closed issues
- **AND** `--type` / `-t <type>` filters to a specific type
- **AND** `--source <human|system>` filters to a specific origin (an invalid `--source` value is rejected with a usage error)

#### Scenario: Empty list message
- **WHEN** the list result is empty
- **THEN** the message is `"No open issues found."` by default, or `"No issues found."` when `--all` is used

#### Scenario: List rendering
- **WHEN** issues are listed
- **THEN** a Rich table is printed with columns `ID`, `Title`, `Type`, `Status`, `Priority`, `Tags`, `Created`
- **AND** the `Title` column shows the issue's `display_title` (explicit title, else the description's first non-empty line)
- **AND** an unset `Type` or `Priority` renders as `-` (and does not participate in any sort weighting)
- **AND** titles longer than 50 characters are truncated with an ellipsis (`"..."`)
- **AND** the table title is `"Open Issues"` by default or `"All Issues"` with `--all`
- **AND** the `Type`, `Status`, and `Priority` columns are colorized (e.g., `bug`=red, `feature`=green, `open`=yellow, `in-progress`=blue, `resolved`/`closed`=green, `won't-fix`=dim, `critical`=red bold, `high`=red, `medium`=yellow, `low`=dim)

#### Scenario: `show` displays full detail
- **WHEN** `se3 issue show <id>` is invoked and the issue exists
- **THEN** the display title, type, status, priority, tags, created/updated timestamps (formatted `"%Y-%m-%d %H:%M"`), and description are printed inside a Rich block bordered by `render_block_header` / `render_block_footer` in cyan
- **AND** an unset type or priority renders as `-`
- **AND** the tags line reads `"none"` if there are no tags

#### Scenario: `show` for missing ID
- **WHEN** `se3 issue show <id>` is invoked and no such issue exists
- **THEN** `"Issue '<id>' not found."` is printed to stderr and the process exits with code 1

#### Scenario: `create` resolves description from positional arg, stdin pipe, or single interactive prompt
- **WHEN** `se3 issue create "<description>"` is invoked with a positional description argument
- **THEN** that argument is used as the description (it takes priority over stdin and the interactive prompt)
- **AND** when no positional argument is given and stdin is piped (non-TTY), the entire stdin content is read as the description
- **AND** when no positional argument is given and stdin is a TTY, exactly **one** multi-line description is collected via the shared `_read_multiline_input` (the same prompt_toolkit multi-line editor `se3 run` uses) — the command does NOT step through separate Title/Type/Priority/Tags prompts, and it does NOT open an external editor
- **AND** the remaining fields are taken from optional flags `--title`, `--type`, `--priority`, and `--tags` (comma-separated, stripped, empties discarded); any flag left unset stays unspecified (`None`)
- **AND** the issue is created with `source="human"`, and its assigned id and display title are printed
- **AND** an empty/whitespace-only resolved description aborts with an error and a non-zero exit code

#### Scenario: `create` interactive single-description flow
- **GIVEN** no positional description argument and a TTY stdin
- **WHEN** `se3 issue create` is invoked
- **THEN** the command collects exactly **one** multi-line description via the shared `_read_multiline_input` (from `cli`), and does NOT step through separate Title/Description/Type/Priority/Tags prompts
- **AND** it does NOT open an external editor (that is the `--editor` path)
- **AND** any other fields come only from the optional `--title` / `--type` / `--priority` / `--tags` flags, and the created issue uses `source="human"`

#### Scenario: `create --editor` opens an external editor with a prefilled template
- **WHEN** `se3 issue create --editor` is invoked
- **THEN** an external editor is launched on a prefilled YAML template containing all editable fields
- **AND** the editor command is taken from `$EDITOR`, falling back to `vi` when `$EDITOR` is unset or empty
- **AND** on save the parsed fields create a new issue with `source="human"`; only `description` is required

#### Scenario: `edit <id>` opens the issue's YAML in an external editor
- **WHEN** `se3 issue edit <id>` is invoked for an existing issue
- **THEN** an external editor (`$EDITOR`, falling back to `vi`) is opened on the issue's current field values
- **AND** on save the edited fields are applied via `IssueManager.update_fields`, clearing an optional field when its value is blanked and rejecting an emptied `description`
- **AND** for a missing ID an error is printed to stderr and the process exits non-zero

#### Scenario: `close <id>` closes an issue with an optional reason
- **WHEN** `se3 issue close <id>` is invoked on an open or in-progress issue
- **THEN** the issue is moved to a closed-directory status via `IssueManager.close_issue`
- **AND** an optional `--reason <text>` is recorded in the close log line (not persisted into the issue YAML)
- **AND** for a missing ID, or when the close transition is invalid, an error is printed to stderr and the process exits non-zero

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