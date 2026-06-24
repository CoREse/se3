<!-- spec-format: v1 -->
# se3-scaffold Specification

## Purpose

Define the SE3 project scaffold system, including the standard project structure, configuration system, and project initialization via `se3 init`.

## Requirements

### Requirement: SE3 Project Structure

The system SHALL define the standard SE3 project file structure.

**Standard structure:**
```
project/
├── se3.yaml               # Framework configuration (optional)
├── se3.local.yaml         # Optional developer-local override (gitignored)
├── README.md              # Project documentation
├── README.<lang>.md       # Optional localized READMEs (e.g. README.zh.md)
├── VERSIONS.md            # Optional version-history changelog (init-seeded,
│                          # maintained by version_analyze + commit)
├── se3/                   # SE3 runtime directory
│   ├── specs/             # Documented snapshot of code (spec-assistant)
│   │   ├── base/          # Base project specification
│   │   │   └── spec.md    # Required: project conventions
│   │   └── <capability>/  # Capability specs
│   │       └── spec.md
│   └── version-rules.md   # Optional project-level natural-language
│                          # version rules consumed by version_analyze
├── src/                   # Source code (conventional)
└── tests/                 # Test files (conventional)
```

**Optional file — `VERSIONS.md`:** A project-root version-history
changelog. It is OPTIONAL (it is NOT in Required Files), but `se3 init`
seeds an initial `VERSIONS.md` (see *Project Initialization via
se3 init*) and the `version_analyze` + `commit` pipeline maintains it on
each version bump (the `documentation-updater` subsystem prepends a new
`## <version> - <date>` entry). When absent, the commit pipeline creates
it on the first bump.

**Localized README naming:** Translations of `README.md` SHALL follow
the BCP 47 short form `README.<lang>.md` by default — e.g.
`README.zh.md`, `README.ja.md`, `README.fr.md`. Only when two or more
regional variants of the *same* language must coexist is the
region-qualified form `README.<lang>-<REGION>.md` used — e.g.
`README.zh-CN.md` alongside `README.zh-TW.md`. The short form is the
default; the region form is an upgrade reserved for the multi-region
case. Underscore or main-name-hyphen spellings are NOT permitted as
counter-examples: do NOT use `README_zh.md`, `README-zh.md`, or
`README_zh_CN.md`.

**Optional file — `se3/version-rules.md`:** When this file exists, its
plain-Markdown contents are injected into the `version_analyze` LLM
prompt as the authoritative version policy (see the `se3-versioning`
*Custom Version Rules File* requirement). When absent, the default
SemVer 2.0.0 rules apply. The file is included alongside `se3/specs/`,
`se3/issues/`, and `se3/scripts/` in the default `.gitignore` whitelist
so it is committed with the project.

**Required Files:**
- `se3/specs/base/spec.md` — Base project specification (auto-loaded in all flows)
- `se3.yaml` — Project configuration (optional but recommended)

**Key Directories:**
- `se3/specs/` — Spec files (documented snapshot of code; spec-assistant maintained by `se3 sync`)

#### Scenario: Project initialization
- **WHEN** SE3 is initialized in a directory via `se3 init`
- **THEN** the standard structure is created with `se3/specs/base/spec.md`

### Requirement: Base Specification

**RENAMED to the charter by the code-first knowledge-system refactor.** The former `base` spec has been shrunk and renamed to the **charter**, materialized at `se3/charter.md` (top-level, outside `specs/`) with its template source at `src/se3/templates/charter.md` (renamed from `src/se3/templates/base_spec.md`). The system SHALL require a charter at `se3/charter.md` in every SE3 project. The legacy path `se3/specs/base/spec.md` is retained below only as the historical contract; new projects scaffold the charter directly.

**Charter purpose and admission standard (altitude gate):** The charter retains the base spec's role of being injected **unconditionally and in full into every step of every flow**, and additionally serves as the conventions channel for sandbox subprocesses (which cannot read `CLAUDE.md`). Its size is therefore a fixed cost paid on every LLM call, so its content is governed by an altitude gate: it MAY carry ONLY content that is *both* (a) un-expressible by the code itself and (b) needed in full by the whole project. Concretely:
- Project identity / positioning (what the project is, its primary language / framework)
- The top-level architecture picture (how the major subsystems fit together, including the subjective "why these modules form one subsystem" layering that the mechanical code-index deliberately omits)
- Project-wide cross-cutting mandatory conventions

The charter MUST NOT carry per-module locators (those are now the `se3/code-index.md` structure map's job) or any low-altitude module detail. Because charter content is decoupled from project size (it grows with architectural complexity, not with LOC), full-load cost stays bounded for large projects. The byte threshold is a **monitoring light, not a hard wall**: exceeding it triggers a review for low-altitude content leakage rather than building an index over the charter. Cross-file, no-single-owner architecture decisions live in the charter, human-maintained, accepting that they cannot be auto-synced; preserved historical decisions or future intent do NOT go in the charter — they continue through issues (`se3 issue`).

**Charter auto-loading:**
- The charter SHALL be automatically and fully loaded into every `se3 run` step
- It provides project-wide context for discovery, analyze, and all downstream steps, and the conventions channel for sandbox subprocesses
- When the charter file is absent, injection degrades to an empty string rather than failing

#### Scenario: Charter discovered and injected in full
- **GIVEN** a project with `se3/charter.md`
- **WHEN** `se3 run` executes any step
- **THEN** the charter content is automatically loaded into context in full

#### Scenario: Charter missing
- **GIVEN** a project without `se3/charter.md`
- **WHEN** `se3 init` is run
- **THEN** a charter template (from `src/se3/templates/charter.md`) is created automatically

#### Scenario: Gitignore whitelists the committed charter and code-index
- **WHEN** `se3 init` writes the project `.gitignore`
- **THEN** the `.gitignore` whitelists the committed artifacts via `!/se3/charter.md` and `!/se3/code-index.md` (under the `/se3/*` blanket-ignore + `!`-whitelist scheme), while the volatile `se3/cache/code-index.json` stays ignored

### Requirement: Configuration System

The system SHALL support configuring framework behavior via `se3.yaml`.

**Configuration file location:** Project root (`se3.yaml`)

**Optional local override:** Developers MAY place a `se3.local.yaml`
at the project root. When present, it fully replaces `se3.yaml` as
the project-level config source (no deep merge). `se3.local.yaml` is
gitignored by default so machine-specific overrides stay local. See
the `se3-config` *Configuration File Format* requirement for the
authoritative semantics.

**Configuration options written into the default `se3.yaml` by `se3 init`:**
- `project_name`: Project name (set from `--name` or the directory name)
- `version.enabled`: Enable automatic version bumping (default: true)

**Configuration options shown only as commented examples in the default `se3.yaml`:**
- `confirmation.steps`: Per-step confirmation dict `{<step_name>: {reviewer?, max_iterations?}}` — steps not listed are NOT confirmed
- `agents`: Top-level dict registry `{name: {type, cmd, priority?}}` — see the `se3-config` Agent Registry requirement
- `llm_caller.defaults`: Default caller chain as a list of agent names referencing `agents`

Other supported configuration options (e.g. `version.auto_bump`, `llm_caller.steps.<step_name>`) are NOT included in the file generated by `se3 init`. They are documented in the `se3-config` spec and take their built-in defaults unless the user adds them manually.

#### Scenario: Using default configuration
- **WHEN** no se3.yaml file exists in the project
- **THEN** the framework runs with built-in default values

#### Scenario: Custom configuration
- **WHEN** se3.yaml exists and specifies custom settings
- **THEN** the framework uses those settings to customize behavior

### Requirement: Project Initialization via se3 init

The system SHALL initialize a new SE3 project via the `se3 init` command.

**Interface:**
```bash
se3 init [--project-root PATH | -p PATH] [--name PROJECT_NAME | -n PROJECT_NAME] [--force | -f]
```

**Short option aliases:** Each long option has a single-character short
alias for convenience:
- `-p` for `--project-root`
- `-n` for `--name`
- `-f` for `--force`

Short and long forms are equivalent and accept the same values, so any
invocation using long options can be expressed equivalently using the
corresponding short options.

**Created Files:**
1. **se3.yaml** — Project configuration
2. **se3/specs/base/spec.md** — Base specification template
3. **VERSIONS.md** — Initial version-history changelog, rendered from the
   packaged `src/se3/templates/versions_md.md` template (starts with a
   `# Version History` title and a `## 0.1.0 - <date>` initial entry).
   Skipped when a `VERSIONS.md` already exists unless `--force` is
   passed; its create/skip state is reported distinctly from the other
   created/skipped files.

#### Scenario: Initialize new project
- **GIVEN** a clean project directory without SE3 configuration
- **WHEN** a user runs `se3 init` in the project directory
- **THEN** the system creates:
  - `se3.yaml` with default configuration
  - `se3/specs/` directory structure
  - `se3/specs/base/spec.md` with base specification template

#### Scenario: Initialize with custom name
- **GIVEN** a directory at /path/to/my-project
- **WHEN** user runs `se3 init --name "My Project"`
- **THEN** the base spec contains "My Project" as project name

#### Scenario: Force re-initialization
- **GIVEN** a project with existing se3.yaml
- **WHEN** user runs `se3 init --force`
- **THEN** existing files are overwritten with fresh templates

#### Scenario: Initial VERSIONS.md created
- **GIVEN** a clean project directory without a `VERSIONS.md`
- **WHEN** a user runs `se3 init`
- **THEN** a `VERSIONS.md` is created from the packaged template
- **AND** its first line is `# Version History`
- **AND** it contains an initial `## 0.1.0 - <date>` entry
- **AND** an existing `VERSIONS.md` is left untouched unless `--force`
  is passed

#### Scenario: Initialize using short option aliases
- **GIVEN** a clean project directory
- **WHEN** a user runs `se3 init -p /path/to/project -n "My Project" -f`
- **THEN** the command behaves identically to
  `se3 init --project-root /path/to/project --name "My Project" --force`

### Requirement: Git Repository Initialization

The `se3 init` command SHALL ensure the project root is inside a git
repository, initializing one if necessary.

**Detection:** Before initializing, `se3 init` walks upward from the
project root looking for a `.git` entry in the path or any parent.
If one is found, the project is considered to already be inside a
git repository and no new repository is created.

**Initialization:** When no enclosing git repository is found,
`se3 init` runs `git init` in the project root to create a new
repository. This happens unconditionally — it is not gated by
`--force` — because the gitignore created later in the same run
relies on having a repository in place.

**Result reporting:** The init result distinguishes three git
outcomes for the UI:
- `git_already_existed` — the project root was already inside a
  git repository; no new repository was created.
- `git_initialized` — a new git repository was successfully
  created at the project root.
- Neither flag set with a non-empty `git_message` — the
  initialization attempt failed (for example, `git` is not
  installed or `git init` returned a non-zero exit code). The
  failure does not abort the rest of `se3 init`; other scaffold
  files are still created.

#### Scenario: Init inside existing git repository
- **GIVEN** the project root is already inside a git repository
  (a `.git` entry exists at the root or any ancestor)
- **WHEN** `se3 init` runs
- **THEN** no new git repository is created
- **AND** the result indicates the repository already existed

#### Scenario: Init outside any git repository
- **GIVEN** the project root is not inside any git repository
- **WHEN** `se3 init` runs
- **THEN** `git init` is invoked in the project root to create
  a new repository
- **AND** the result indicates a new git repository was
  initialized

#### Scenario: Git not available
- **GIVEN** `git` is not installed or not on PATH
- **WHEN** `se3 init` runs in a directory that is not already a
  git repository
- **THEN** the git initialization step fails with an explanatory
  message
- **AND** `se3 init` still creates the other scaffold files

### Requirement: Gitignore Creation and Update

The `se3 init` command SHALL ensure the project root has a `.gitignore`
that ignores SE3 runtime content while whitelisting committed artifacts
and that ignores `se3.local.yaml`.

**Default `.gitignore` template:** When `se3 init` writes a new
`.gitignore`, it uses a built-in template that includes:
- Standard Python ignores (`__pycache__/`, `*.py[cod]`, build/dist
  directories, `*.egg-info/`, etc.)
- Virtual environment directories (`venv/`, `ENV/`, `env/`, `.venv`)
- Common IDE/editor artifacts (`.vscode/`, `.idea/`, `*.swp`, `*.swo`,
  `*~`, `.DS_Store`)
- An SE3 whitelist block that ignores `/se3/*` except for
  `/se3/specs/`, `/se3/issues/`, `/se3/scripts/`, and
  `/se3/version-rules.md`
- A `se3.local.yaml` ignore line so machine-local config overrides are
  never committed

**Outcomes:** The init result distinguishes five `.gitignore` outcomes:

- `gitignore_created` — the file did not exist (or `--force` was
  passed); the full default template was written from scratch.
- `gitignore_appended` — a `.gitignore` already existed without an
  `se3.local.yaml` ignore pattern; `se3 init` appended a
  local-config-ignore block (with one blank line of separation from
  prior content). This is idempotent — re-running is a no-op.
- `gitignore_negated` — a `.gitignore` already existed and contained
  an explicit negation (`!se3.local.yaml`) that would conflict with a
  plain `se3.local.yaml` append. The file is left untouched and a
  warning is surfaced rather than creating two rules whose ordering
  would silently determine the outcome.
- `gitignore_already_existed` — a `.gitignore` already existed and
  already ignored `se3.local.yaml`; no changes are made.
- `gitignore_error` — an I/O error prevented reading or writing the
  file. This is distinct from `gitignore_already_existed` so the UI
  surfaces a real failure instead of a misleading "already exists"
  message.

**Force behavior:** Passing `--force` causes `se3 init` to overwrite
any existing `.gitignore` with the full default template (the
`gitignore_created` outcome). Without `--force`, an existing file is
preserved and only the `se3.local.yaml` block is appended when
missing.

**Non-fatal failures:** A `gitignore_error` or `gitignore_negated`
outcome does not abort `se3 init`; the other scaffold files
(`se3.yaml`, base spec, etc.) are still created.

**Pattern matching semantics:** Detection of whether `se3.local.yaml`
is already ignored or explicitly un-ignored uses glob-style matching,
not literal line comparison, so existing broader rules are recognised
and no redundant append is produced. The matcher SHALL:

- Strip a leading `/` (gitignore root anchor) from each pattern before
  matching, so an anchored rule like `/se3.local.yaml` is recognised
  as ignoring the file.
- Strip a leading `**/` (git recursive-glob prefix) before matching,
  so `**/se3.local.yaml` is recognised. `fnmatch` does not model `**`
  on its own.
- Strip a trailing `/` (directory-only marker) before matching. The
  user has already spelled the name out, so treat it as intent to
  ignore and avoid appending a duplicate line, even though strictly
  speaking a directory-only pattern does not ignore a regular file.
- Skip blank lines, comment lines (`#…`), and — when scanning for
  ignore patterns — negation lines (`!…`).

**Broad ignore patterns count as already-ignored:** A pre-existing
glob that happens to cover `se3.local.yaml` (for example `*.yaml`,
`*.local.yaml`, `*.local.*`, or `se3.local.*`) SHALL be treated as
already ignoring the file. The `gitignore_already_existed` outcome is
reported and no append occurs. This prevents `se3 init` from adding a
redundant `se3.local.yaml` line every run when the user has set up a
broader rule.

**Narrow vs. broad negations:** Only a *narrow* negation triggers the
`gitignore_negated` outcome. A negation pattern is narrow when, after
prefix/suffix stripping, it matches `se3.local.yaml` but does NOT also
match `se3.yaml`. Examples:

- Narrow (triggers warning, no append): `!se3.local.yaml`,
  `!/se3.local.yaml`, `!**/se3.local.yaml`, `!*.local.yaml`,
  `!*.local.*`, `!se3.local.*`.
- Broad (does NOT trigger warning): `!*.yaml`, `!se3.*`, `!*`. These
  re-include the file only as a side effect of a general rule, so
  `se3 init` proceeds to append the `se3.local.yaml` ignore block as
  usual.

**Negation check ordered before ignore check:** When a `.gitignore`
contains both a broad ignore (e.g. `*.yaml`) and an explicit narrow
negation (`!se3.local.yaml`), git's last-line-wins semantics mean the
negation wins and the file is tracked. `se3 init` SHALL therefore
check for a narrow negation BEFORE checking for an ignore pattern, so
the operator is warned rather than receiving a misleading
`gitignore_already_existed` outcome.

**Atomic append:** When appending the local-config-ignore block,
`se3 init` SHALL write the combined `existing + separator + block`
content with a single `write_text` call rather than read+append in two
syscalls. This prevents a concurrent writer slipping in between read
and append from causing a duplicated pattern line; the worst remaining
case is normal last-writer-wins clobber semantics.

#### Scenario: Existing .gitignore covers se3.local.yaml with a broad glob
- **GIVEN** a `.gitignore` exists and contains a pattern such as
  `*.yaml`, `*.local.yaml`, `*.local.*`, or `se3.local.*` (any glob
  whose match covers `se3.local.yaml`)
- **AND** the file contains no narrow `!se3.local.yaml`-style negation
- **WHEN** `se3 init` runs without `--force`
- **THEN** the file is left unchanged
- **AND** the result reports the `gitignore_already_existed` outcome

#### Scenario: Existing .gitignore uses an anchored or recursive form
- **GIVEN** a `.gitignore` already contains `/se3.local.yaml`,
  `**/se3.local.yaml`, or `se3.local.yaml/` (any of the anchor /
  recursive / directory-marker variants)
- **WHEN** `se3 init` runs without `--force`
- **THEN** the file is left unchanged
- **AND** the result reports the `gitignore_already_existed` outcome

#### Scenario: Narrow glob negation triggers the negated outcome
- **GIVEN** a `.gitignore` contains a narrow negation pattern such as
  `!*.local.yaml`, `!*.local.*`, or `!se3.local.*` (any negation that
  matches `se3.local.yaml` but not `se3.yaml`)
- **WHEN** `se3 init` runs without `--force`
- **THEN** the file is left untouched
- **AND** the result reports the `gitignore_negated` outcome with a
  warning to resolve the negation manually

#### Scenario: Broad negation does not trigger the negated outcome
- **GIVEN** a `.gitignore` contains only broad negation patterns such
  as `!*.yaml`, `!se3.*`, or `!*` (patterns that match `se3.yaml` as
  well as `se3.local.yaml`)
- **AND** no narrow negation of `se3.local.yaml` is present
- **AND** no existing pattern already ignores `se3.local.yaml`
- **WHEN** `se3 init` runs without `--force`
- **THEN** the `se3.local.yaml` ignore block is appended as normal
- **AND** the result reports the `gitignore_appended` outcome

#### Scenario: Both broad ignore and narrow negation present
- **GIVEN** a `.gitignore` contains both a broad ignore (e.g.
  `*.yaml`) and an explicit narrow negation (`!se3.local.yaml`)
- **WHEN** `se3 init` runs without `--force`
- **THEN** the file is left untouched
- **AND** the result reports the `gitignore_negated` outcome rather
  than `gitignore_already_existed`, so the conflict is surfaced
  instead of silently masked

#### Scenario: No existing .gitignore
- **GIVEN** the project root has no `.gitignore`
- **WHEN** `se3 init` runs
- **THEN** a new `.gitignore` is created from the default template
- **AND** the result reports the `gitignore_created` outcome

#### Scenario: Existing .gitignore without se3.local.yaml
- **GIVEN** a `.gitignore` exists at the project root and does not
  contain an `se3.local.yaml` ignore pattern or negation
- **WHEN** `se3 init` runs without `--force`
- **THEN** a block ignoring `se3.local.yaml` is appended, separated
  from prior content by exactly one blank line
- **AND** the result reports the `gitignore_appended` outcome

#### Scenario: Existing .gitignore already ignores se3.local.yaml
- **GIVEN** a `.gitignore` exists and already ignores `se3.local.yaml`
- **WHEN** `se3 init` runs without `--force`
- **THEN** the file is left unchanged
- **AND** the result reports the `gitignore_already_existed` outcome

#### Scenario: Existing .gitignore negates se3.local.yaml
- **GIVEN** a `.gitignore` exists and contains an explicit
  `!se3.local.yaml` negation
- **WHEN** `se3 init` runs without `--force`
- **THEN** the file is left untouched
- **AND** the result reports the `gitignore_negated` outcome with a
  warning that the operator must resolve the negation manually

#### Scenario: Force overwrites existing .gitignore
- **GIVEN** a `.gitignore` exists with arbitrary content
- **WHEN** `se3 init --force` runs
- **THEN** the file is overwritten with the full default template
- **AND** the result reports the `gitignore_created` outcome

### Requirement: Local Config Override Warning

The `se3 init` command SHALL detect an existing `se3.local.yaml` at the
project root and warn the operator that it will shadow the generated
`se3.yaml` at load time.

**Detection rule:** The check uses a real-file test (not mere path
existence) so the warning fires only when `se3.local.yaml` is a regular
file that will actually be picked up as the project-level config
source. A directory or dangling symlink at that path is not treated as
a shadowing override and does not trigger the warning. This matches
the file-resolution rule used when loading project configuration (see
the `se3-config` *Configuration File Format* requirement).

**Result reporting:** The init result exposes a `local_overrides_yaml`
boolean that is `True` when a shadowing `se3.local.yaml` was detected
and `False` otherwise. The flag is independent of whether `se3.yaml`
was created, skipped, or overwritten in this run.

**Non-destructive:** `se3 init` SHALL NOT modify, move, or delete an
existing `se3.local.yaml`, even with `--force`. The command only
surfaces a warning so the operator can decide what to do.

#### Scenario: Existing se3.local.yaml shadows generated se3.yaml
- **GIVEN** the project root contains an `se3.local.yaml` regular file
- **WHEN** `se3 init` runs (with or without `--force`)
- **THEN** the result reports `local_overrides_yaml = True`
- **AND** the command surfaces a warning that `se3.local.yaml` will
  override `se3.yaml` at load time
- **AND** the existing `se3.local.yaml` is left untouched

#### Scenario: No se3.local.yaml present
- **GIVEN** the project root has no `se3.local.yaml`
- **WHEN** `se3 init` runs
- **THEN** the result reports `local_overrides_yaml = False`
- **AND** no override-shadowing warning is emitted

#### Scenario: se3.local.yaml is a directory or dangling symlink
- **GIVEN** the project root contains `se3.local.yaml` as a directory
  or as a symlink whose target does not exist
- **WHEN** `se3 init` runs
- **THEN** the result reports `local_overrides_yaml = False`
- **AND** no override-shadowing warning is emitted

### Requirement: Spec Directory Structure

The system SHALL define the specs directory structure.

**Specs Location:**
- Primary: `se3/specs/` (SE3 3.0+)

**Spec Organization:**
```
se3/specs/
├── base/                   # Base project specification (REQUIRED)
│   └── spec.md
├── _changelog/             # Spec change log (optional)
│   └── YYYY-MM-DD-change.md
├── _backlog/               # Backlog specs (optional)
├── flow-engine/            # Flow engine spec (if customizing)
│   └── spec.md
├── se3-commands/           # Commands spec (if customizing)
│   └── spec.md
└── <project-specific>/     # Project capability specs
    └── spec.md
```

**Spec Format:**
- Markdown format
- Required sections: Purpose, Requirements
- Scenario format: WHEN/THEN

**Spec role:** Specs are the documented snapshot of code (a spec-assistant view), maintained by `se3 sync`. They have no routine manual-edit entry; future intent belongs in issues.

#### Scenario: Spec discovery
- **WHEN** flow engine reads specs
- **THEN** it discovers all `*/spec.md` files under `se3/specs/`
- **AND** it always includes `se3/specs/base/spec.md` first

## Base Spec Template

The base specification template SHALL include the following sections:

```markdown
# {project_name} — Base Specification

## Purpose

Project base conventions. This spec is generated by `se3 init` and is automatically loaded in all `se3 run` flows.

## Requirements

### Requirement: Project Identity

- **Project Name**: {project_name}
- **Description**: (fill in the project description)
- **Primary Language/Framework**: (fill in the language and framework)

### Requirement: Directory Structure

- `src/` — source code directory
- `tests/` — test directory
- `se3/specs/` — SE3 specs directory

### Requirement: Coding Conventions

- (fill in the coding conventions)

### Requirement: Key Constraints

- (fill in the key constraints)

### Requirement: Workflow Conventions

- Use `se3 run "task description"` to start the development flow
- A feature may be marked complete only after running tests
- Keep the main branch in a runnable state

### Requirement: Version Management

The project SHALL use Semantic Versioning 2.0.0 as the version management standard.

**Version format:** Follows SemVer 2.0.0 `MAJOR.MINOR.PATCH[-prerelease][+build]`
- MAJOR: incompatible API changes
- MINOR: backward-compatible feature additions
- PATCH: backward-compatible bug fixes

**Version decision model:**
- The `suggested_version` field of the `version_analyze` step is the single authoritative source for the new version number.
- Optional custom rules: write natural-language rules into `se3/version-rules.md`,
  and `version_analyze` injects them into the LLM prompt as the decision basis; when the file
  does not exist it falls back to the default SemVer 2.0.0 rules.

#### Scenario: Automatic version update
- **GIVEN** the current version is 1.2.3
- **WHEN** a feature task is completed and the commit step runs
- **THEN** the version is automatically updated to 1.3.0
```

The generated base spec template (`src/se3/templates/base_spec.md`) is
the single source of truth for this section; `se3 init` renders it
rather than emitting a hardcoded copy, so the `se3/version-rules.md`
custom-rules mechanism is always disclosed to newly initialized
projects.
