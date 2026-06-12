<!-- spec-format: v1 -->

# se3-config Specification

## Purpose

Define the SE 3.0 framework configuration system via `se3.yaml`. This spec governs configurable parameters, default values, and how the framework adapts its behavior based on project-specific settings.

## Requirements

### Requirement: Configuration File Format

The system SHALL support configuring framework behavior via `se3.yaml`.

The configuration file is located at the project root using YAML format. All configuration items MUST have sensible defaults.

The system SHALL also support a global config at `~/.se3/config.yaml`. Project-level config overrides global config at the top-level key level (no deep merge).

**Local override file (`se3.local.yaml`):**

The system SHALL support an optional `se3.local.yaml` in the project
root for developer-local overrides.

- When `<project_root>/se3.local.yaml` exists, the framework reads **it
  instead of** `se3.yaml` as the project-level config source. The two
  files are NOT deep-merged — `se3.local.yaml` entirely replaces
  `se3.yaml` for the duration of the load. Developers who want to
  retain values from `se3.yaml` must copy them explicitly into
  `se3.local.yaml`.
- The global + project merge rules (entry-level for `agents` and
  `confirmation.steps`, whole-replace for `llm_caller.defaults` and
  `llm_caller.steps.<step>`) still apply, using `se3.local.yaml` as
  the project source.
- `se3.local.yaml` SHALL be gitignored by default (added by `se3 init`
  to the generated `.gitignore`) so that machine-specific overrides do
  not leak into commits.
- Project-root parent-walk detection (used by `se3 run`, `se3 issue`,
  `se3 history`, `se3 salvage`) SHALL recognise a directory containing
  only `se3.local.yaml` as a valid SE3 project root, for parity with
  `se3.yaml`.
- Project-root marker detection SHALL additionally recognise the
  legacy filename `se3.config.yaml` as a project marker, in addition
  to `se3.local.yaml` and `se3.yaml`. A directory containing only
  `se3.config.yaml` is treated as a valid SE3 project root by
  parent-walk detection. This marker is recognised only for
  project-root identification — `se3.config.yaml` is NOT loaded as a
  config source by the project-config lookup (which still consults
  `se3.local.yaml` then `se3.yaml`). The marker check uses
  `is_file()` semantics so a stray directory or dangling symlink at
  one of these paths does NOT count as a marker.
- Warning / deprecation log messages emitted while loading the project
  config SHALL reference the actual filename that was read
  (`se3.local.yaml` when it is present, otherwise `se3.yaml`).
- When the framework attempts to read `se3.local.yaml` and the file
  is unreadable or fails YAML parsing, the loader SHALL emit a
  one-shot WARNING identifying the offending `se3.local.yaml` path
  and the `se3.yaml` it is shadowing, and noting that project
  configuration is falling back to built-in defaults until the local
  file is fixed or removed. This warning fires only for
  `se3.local.yaml` — a malformed `se3.yaml` does not trigger it,
  since `se3.yaml` is not itself a silent override. Deduplication is
  one-shot per `(process, resolved-path)`: a long-running process
  (daemon, test session, IDE integration) that sees the same path
  break, get fixed, and break again will NOT re-warn for the second
  breakage; restarting the process is required for a fresh warning.

**Worktree-aware four-tier lookup:**

When the resolved project root is the working tree of a git **worktree**
(i.e. an additional working tree linked to a main repository via
`git worktree add`), the project-config file lookup SHALL extend across
both the worktree and its main repository so that an `se3.local.yaml`
placed in the main repo can drive runs that execute inside any
worktree. Because `se3.local.yaml` is gitignored, it does NOT travel
with `git checkout` into a worktree; without this rule, a worktree
would silently ignore the developer's main-repo override.

- Worktree identity SHALL be detected via git itself, e.g. by comparing
  `git rev-parse --git-common-dir` with `git rev-parse --git-dir` (a
  worktree has the two diverge; a normal clone has them coincide). The
  main repository's working-tree root is derived from the common git
  directory and verified with `git rev-parse --show-toplevel` against
  that directory.
- When a worktree is detected, the framework probes the following
  paths in order and uses the **first existing** file as the project
  config source (pick-first, no merging):
  1. `<worktree>/se3.local.yaml`
  2. `<main_repo>/se3.local.yaml`
  3. `<worktree>/se3.yaml`
  4. `<main_repo>/se3.yaml`
- When no candidate exists, the framework returns the canonical
  worktree path (`<worktree>/se3.yaml`) as the not-found location, so
  downstream "no project config" handling is unchanged.
- When the project root is NOT a worktree (plain clone or non-git
  directory) the lookup is unchanged: `se3.local.yaml` first, then
  `se3.yaml`, both inside the project root only. No upward search
  toward parent directories or unrelated git roots is performed.
- Failures of the git probes (timeout, non-zero exit, malformed output,
  `git` not on PATH) SHALL be caught and SHALL silently fall back to
  the legacy two-tier lookup inside the project root. Worktree
  detection MUST NOT raise from the config loader.
- Git probe invocations SHALL sanitize the inherited environment
  (clearing `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`) so an
  unrelated outer git context cannot misclassify the project root as a
  worktree of some other repository.
- The framework MAY memoize the resolved main-repo root keyed by the
  absolute project-root path for the duration of a single run, and
  SHALL invalidate that memoization at the start of each loop iteration
  so subsequent iterations re-probe a possibly relocated worktree.
- The above lookup applies uniformly to every loader that reads the
  project YAML (`load_project_yaml` and its callers), so version,
  agents, confirmation, implement, test, language, and any other
  top-level section all observe the same resolved file.
- The SE3 runtime directory (`se3/state`, `se3/calls`, `se3/logs`,
  …) continues to live under the **worktree** root, not the main
  repo. Only the project-config file lookup crosses the worktree
  boundary.

**Configurable options:**
- `version.enabled`: Enable automatic version bumping (default: true)
- `version.version_file`: Path to version file (auto-detect if null)
- `version.auto_bump`: Auto-apply version bump without confirmation (default: true)
- `version.script_path`: Custom version script path (default: null = use default `se3/scripts/version.py`)
- `agents`: Top-level dict registry `{name: {type, cmd, priority?}}` (authoritative identity layer; see Agent Registry requirement)
- `claude_commands`: Legacy alias for `agents`, auto-migrated at load time (deprecated — use `agents` + `llm_caller.defaults`)
- `llm_caller.defaults`: Default caller chain as a list of agent names referencing `agents` (default: built-in `[claude]` fallback)
- `llm_caller.steps.<step_name>`: Per-step hard override as a list of agent names (optional)
- `confirmation.steps`: Per-step confirmation dict `{<step_name>: {reviewer?, max_iterations?}}` — steps not listed are NOT confirmed (there is no global `enabled` switch; see Confirmation Configuration requirement)
- `language.language`: Language for human-facing steps (default: null)
- `language.spec_language`: Language for spec writing (default: null)
- `issue_discovery.steps`: Steps that receive issue discovery prompt injection (string list, default: `[]` — empty; `summarize` no longer participates, see issue-discovery *Whitelist Configuration*)
- `test.critical_tests`: Critical acceptance test ID/substring patterns; a listed test that is skipped or missing is treated as not-passed (string list, default: `[]`; see Test Configuration requirement)
- `conflict_resolver.strategy`: In-loop branch-merge conflict resolution strategy used by `se3 run --loop --merge` — `"human"` or `"llm"` (default: `"human"`)
- `merge.strategy`: Default conflict-resolution tier for the standalone `se3 merge` command — `"fast"` (new default), `"safe"`, or `"strict"`. The previous `"default"` / `"robust"` values have been removed and trigger fail-fast at config load (see Merge Configuration requirement).
- `merge.delete_merged_default`: Whether `se3 merge` defaults to deleting merged branches and archiving their worktrees under `.se3/archive/` (default: `true`).
- `merge.max_conflict_resolve_iterations`: Maximum batched LLM-as-editor rounds the merge conflict resolver performs per `git merge` before the active strategy's cap-exhaustion policy kicks in (default: `10`, must be `>= 1`).
- `implement.group_loc_threshold`: LOC threshold for collapsing task groups into a single LLM call (default: 300)
- `implement.use_worktree`: Whether the implement step may use per-group worktrees and `impl/*` branches on the DAG parallel path (default: true). Set to `false` to force fully sequential execution on the original branch regardless of DAG topology.
- `workflow.max_fix_iterations`: Max fix loop iterations before FAILED (default: 100). A value of `0` (or `null`) is the sentinel for "unlimited" — the fix loop will never exit due to the iteration upper bound.
- `workflow.self_check_passes_required`: Number of consecutive clean self_check passes required within a fix-loop round before advancing to the next step (default: 1, must be >= 1 — startup fail-fast otherwise)
- `workflow.baseline_fix_max_attempts`: Independent per-flow cap on how many fix-loop attempts may target inherited (baseline) test failures under mechanism B (default: 3, must be `>= 0`). `0` disables baseline looping entirely; negatives fail-fast; bool/float/non-integer types warn and fall back to the default. Deliberately independent of `workflow.max_fix_iterations` so baseline failures stay bounded even when the global fix loop is unlimited (see Workflow Configuration requirement and the flow-engine *Test Step Configuration and Multi-Phase Execution* mechanism B).
- `workflow.self_check_convergence_enabled`: Enable cross-fix-loop convergence detection in self_check (default: false). When true, the first self_check instance of each fix-loop round (pass_index=1) compares its issues against the last self_check instance of the previous fix-loop round; identical issues short-circuit to COMPLETED. Same-round self_check instances never compare against each other.
- `spec_loading.steps.<step_name>`: Per-step spec loading mode — `"items"` (default, header + selected requirements only) or `"full_spec"` (entire spec file). `update_spec` defaults to `full_spec`; all other steps default to `items`.
- `test.command`: Primary test command override (default: null = auto-detect)
- `test.timeout`: Fallback timeout (seconds) when dynamic timeout is unavailable (default: 1800)
- `test.timeout_multiplier`: Multiplier applied to implement's `estimated_test_duration` to compute the dynamic timeout for the primary test command (default: 2.0, clamped to >= 1.0)
- `test.min_dynamic_timeout`: Lower bound (seconds) on the computed dynamic timeout (default: 30)
- `test.max_dynamic_timeout`: Upper bound (seconds) on the computed dynamic timeout, preventing runaway escalation in the timeout fix loop (default: 14400)
- `test.phases`: Additional test phases (list of phase configs; each phase's own `timeout` is always used and is NOT affected by the dynamic timeout)
- `presets.<name>.type`: Task type for a project-level preset prompt (default: `feature`); see the `presets:` section below and the `preset-prompts` spec.
- `presets.<name>.prompt_file`: Path (relative to project root) to the markdown prompt body for a project-level preset.
- `documentation.readme_badge_template` / `documentation.versions_entry_template` / `documentation.readme_header_template`: Template overrides for the `DocumentationUpdater` wiring used by the `commit` pipeline (see the `documentation:` section below).

**`presets:` section — project-level preset prompts:**

The optional `presets:` section declares project-local preset prompt
metadata consumed by `se3 run --preset <name>`. Each entry is keyed by
preset name:

```yaml
presets:
  doc-sync:
    type: feature                 # task type the preset runs as (default: feature)
    prompt_file: se3/prompts/doc-sync.md  # path (relative to project root) to the prompt body
```

- `type` is the task type the preset's flow runs as; when omitted it
  defaults to `feature`.
- `prompt_file` redirects the preset to a specific markdown file. When
  omitted, the loader uses the matching `se3/prompts/<name>.md` file.
- The `presets:` section carries ONLY project-level metadata/overrides;
  the built-in preset library ships as package data and is not declared
  here. A project preset whose name matches a built-in one overrides the
  built-in. See the `preset-prompts` spec for the full two-layer
  registry semantics.

**`documentation:` section — DocumentationUpdater template overrides:**

The optional `documentation:` section overrides the templates the
`DocumentationUpdater` uses when it is wired into the `commit` pipeline
to maintain `README.md` and `VERSIONS.md`:

```yaml
documentation:
  readme_badge_template: "![Version](https://img.shields.io/badge/version-{{version}}-blue)"
  versions_entry_template: "## {{version}} - {{date}}\n\n{{changes}}\n"
  readme_header_template: "# Project (v{{version}})"
```

- All three keys are optional and use `{{placeholder}}` substitution
  (`{{version}}`, `{{date}}`, `{{changes}}`, …). A missing or non-string
  value is dropped, so an absent or empty `documentation:` section
  leaves the updater on its built-in defaults.
- `documentation:` is the authoritative configuration source for the
  commit-pipeline `DocumentationUpdater` wiring. It is deliberately
  separate from the legacy `version.templates` block (see Version
  Configuration): `version.templates` retains its own existing behavior
  for the version bumper and is NOT modified or read by this wiring.

#### Scenario: Project preset metadata is read from presets:
- **GIVEN** `se3.yaml` declares `presets: { doc-sync: { type: feature, prompt_file: se3/prompts/doc-sync.md } }`
- **WHEN** `se3 run --preset doc-sync` resolves the preset
- **THEN** the preset's task type is `feature`
- **AND** the prompt body is read from `se3/prompts/doc-sync.md`

#### Scenario: documentation: overrides DocumentationUpdater templates
- **GIVEN** `se3.yaml` declares `documentation: { versions_entry_template: "## {{version}}\n\n{{changes}}\n" }`
- **WHEN** the commit pipeline constructs the `DocumentationUpdater`
- **THEN** the updater renders VERSIONS.md entries using the supplied
  `versions_entry_template`
- **AND** the legacy `version.templates` block is unaffected

#### Scenario: Empty documentation section keeps built-in defaults
- **GIVEN** `se3.yaml` has no `documentation:` section (or an empty one)
- **WHEN** the commit pipeline constructs the `DocumentationUpdater`
- **THEN** the updater uses its built-in `readme_badge` / `versions_entry`
  defaults and registers no `readme_header` template

#### Scenario: Using default configuration
- **WHEN** no se3.yaml file exists in the project
- **THEN** the framework runs with built-in default values

#### Scenario: Custom version configuration
- **WHEN** se3.yaml specifies custom version settings (e.g., `version.version_file` or `version.script_path`)
- **THEN** the framework uses those settings when bumping the version

#### Scenario: Global configuration
- **WHEN** `~/.se3/config.yaml` defines top-level `agents` entries
- **THEN** the framework uses the global config as fallback

#### Scenario: Project overrides global
- **WHEN** both global and project configs define the same top-level key
- **THEN** the project-level config takes precedence
- **AND** `agents` dict and `confirmation.steps` dict merge entry-level (by name / step_name), while `llm_caller.defaults` and `llm_caller.steps.<step>` are whole-replaced

#### Scenario: se3.local.yaml replaces se3.yaml when present
- **GIVEN** the project root contains both `se3.yaml` and `se3.local.yaml`
- **WHEN** the framework loads project-level configuration
- **THEN** only `se3.local.yaml` is consulted as the project source
- **AND** values from `se3.yaml` that are absent from `se3.local.yaml`
  do NOT leak through (no deep merge)

#### Scenario: Only se3.local.yaml exists
- **GIVEN** the project root contains `se3.local.yaml` but no `se3.yaml`
- **WHEN** the framework loads project-level configuration
- **THEN** `se3.local.yaml` is used as the project-level config source

#### Scenario: Only se3.yaml exists (no local override)
- **GIVEN** the project root contains `se3.yaml` but no `se3.local.yaml`
- **WHEN** the framework loads project-level configuration
- **THEN** `se3.yaml` is used as the project-level config source
  (existing behavior, unchanged)

#### Scenario: Project detected with only se3.local.yaml
- **GIVEN** a directory contains `se3.local.yaml` but no `se3.yaml`
- **WHEN** any SE3 CLI command (`se3 run`, `se3 issue`, `se3 history`,
  `se3 salvage`) performs parent-walk project-root detection
- **THEN** the directory is recognised as the SE3 project root

#### Scenario: Project detected with only legacy se3.config.yaml
- **GIVEN** a directory contains `se3.config.yaml` but neither
  `se3.yaml` nor `se3.local.yaml`
- **WHEN** any SE3 CLI command performs parent-walk project-root
  detection
- **THEN** the directory is recognised as the SE3 project root
- **AND** the project-config lookup still resolves to the canonical
  `se3.yaml` location (i.e. `se3.config.yaml` is not loaded as a
  config source)

#### Scenario: Directory-shaped marker is not treated as project root
- **GIVEN** a directory contains an entry named `se3.yaml`,
  `se3.local.yaml`, or `se3.config.yaml` that is itself a directory
  or a dangling symlink rather than a regular file
- **WHEN** parent-walk project-root detection inspects the directory
- **THEN** the directory is NOT recognised as the SE3 project root
- **AND** the walk continues toward the parent directory

#### Scenario: se3.local.yaml is gitignored by se3 init
- **WHEN** `se3 init` generates the project `.gitignore`
- **THEN** the generated `.gitignore` contains an entry that ignores
  `se3.local.yaml` so the local override file is not committed

#### Scenario: Worktree falls back to main repo se3.local.yaml
- **GIVEN** a main repository at `<main>` with `<main>/se3.local.yaml`
  present and `<main>/se3.yaml` tracked in git
- **AND** a linked worktree at `<wt>` created via `git worktree add`
  whose checkout contains the tracked `<wt>/se3.yaml` but no
  `<wt>/se3.local.yaml`
- **WHEN** any SE3 command runs inside `<wt>` and loads the project
  config
- **THEN** the framework reads `<main>/se3.local.yaml` (tier 2),
  bypassing the worktree's tracked `<wt>/se3.yaml`

#### Scenario: Worktree-local override wins over main repo
- **GIVEN** both `<wt>/se3.local.yaml` and `<main>/se3.local.yaml`
  exist
- **WHEN** SE3 loads the project config from inside `<wt>`
- **THEN** `<wt>/se3.local.yaml` (tier 1) is used and the main-repo
  copy is ignored

#### Scenario: Worktree falls through to main se3.yaml when nothing else exists
- **GIVEN** neither `<wt>/se3.local.yaml`, `<main>/se3.local.yaml`,
  nor `<wt>/se3.yaml` exist, and only `<main>/se3.yaml` exists
- **WHEN** SE3 loads the project config from inside `<wt>`
- **THEN** `<main>/se3.yaml` (tier 4) is used

#### Scenario: Plain clone is unaffected
- **GIVEN** the project root is a normal git clone (not a worktree),
  with both `se3.yaml` and `se3.local.yaml` at the project root
- **WHEN** SE3 loads the project config
- **THEN** `se3.local.yaml` is used (legacy two-tier behavior),
  unchanged by the worktree lookup rule

#### Scenario: Non-git project is unaffected
- **GIVEN** the project root is not a git repository at all
- **WHEN** SE3 loads the project config
- **THEN** the framework uses the existing `se3.local.yaml` →
  `se3.yaml` order inside the project root, with no parent walk and
  no git probes propagated to the user

#### Scenario: Git probe failure falls back silently
- **GIVEN** `git` is unavailable, times out, or returns a non-zero or
  malformed result while resolving the main-repo root
- **WHEN** SE3 loads the project config
- **THEN** the framework treats the project root as a non-worktree
  location and uses only the in-root two-tier lookup
- **AND** no exception is raised from the config loader

#### Scenario: Inherited git environment does not misclassify worktree
- **GIVEN** the SE3 process is launched with `GIT_DIR`, `GIT_WORK_TREE`,
  or `GIT_COMMON_DIR` set to point at an unrelated repository
- **WHEN** worktree detection runs
- **THEN** the git probes execute with those variables stripped from
  the child environment so they reflect the project root, not the
  outer git context

#### Scenario: Malformed se3.local.yaml emits one-shot shadow warning
- **GIVEN** the project root contains both `se3.yaml` and a
  `se3.local.yaml` that is unreadable or contains invalid YAML
- **WHEN** the framework loads the project config
- **THEN** a WARNING is logged identifying the `se3.local.yaml` path
  and the `se3.yaml` it is shadowing, and noting that project
  configuration is falling back to built-in defaults until the local
  file is fixed or removed
- **AND** a second load attempt within the same process for the same
  resolved path does NOT re-emit the warning (one-shot dedup keyed by
  resolved path)
- **AND** a malformed `se3.yaml` (with no `se3.local.yaml` present)
  does NOT trigger this warning

#### Scenario: Loop iterations re-probe worktree identity
- **GIVEN** SE3 is running in `--loop` mode and each iteration may
  create or remove a worktree
- **WHEN** a new iteration starts
- **THEN** any cached main-repo-root resolution is invalidated so the
  next config load re-runs the git probes against the current project
  root

### Requirement: Version Configuration

The system SHALL support version management configuration.

**Version section options:**
- `enabled`: Whether automatic version bumping is enabled (default: true)
- `version_file`: Path to version file (null = auto-detect)
- `auto_bump`: Apply the LLM-suggested version automatically (default: true)
- `confidence_threshold`: Require confirmation for low confidence (default: null). Documented accepted values are `null` (no threshold — even "low" confidence is auto-confirmed), `"medium"` (require confirmation for "low" confidence), or `"high"` (require confirmation for "medium" or "low" confidence). The `VersionConfig` loader does NOT validate or normalize this value at load time: whatever the user writes is stored verbatim on the config object, with no warning or fail-fast for unknown strings, wrong types (e.g. integers, booleans, lists), or alternate casings. Downstream consumers are responsible for interpreting the stored value and for treating any value other than the three documented forms as a non-threshold (equivalent to `null`). Authors of `se3.yaml` SHOULD therefore use only the documented values, since typos like `"hi"` or `"HIGH"` will not be flagged at config load.
- `script_path`: Custom version script path (null = use default `se3/scripts/version.py`)
- `auto_generate_script`: Auto-generate a version script when none is found (default: true)
- `prerelease_prefix`: Pre-release identifier prefix used when building pre-release version strings (default: `""` — no pre-release component).
- `prerelease_number`: Numeric suffix paired with `prerelease_prefix` for pre-release version strings (default: `0`).
- `templates`: Dict of named string templates used when rendering version-related artifacts. Built-in defaults:
  - `readme_badge`: `"![Version](https://img.shields.io/badge/version-{version}-blue)"`
  - `versions_entry`: `"## {version} - {date}\n\n{changes}\n"`
  User-supplied entries are merged on top of the defaults (per-key replace; default keys remain in effect when not overridden).
- `readme_enabled`: Whether the version bumper updates the project README with the rendered `readme_badge` template (default: `true`).
- `readme_marker`: Marker string embedded in the README that anchors the version-badge replacement region (default: `"<!-- SE3-VERSION -->"`).
- `versions_enabled`: Whether the version bumper maintains a `VERSIONS.md`-style history file (default: `true`).
- `versions_file`: Path (relative to project root) of the version history file (default: `"VERSIONS.md"`).
- `versions_header`: Header text written at the top of the version history file (default: `"# Version History\n\n"`).
- `include_in_commit_message`: Whether the new version string is appended to the version-bump commit message (default: `true`).

**Deprecated keys (accepted, ignored, warned):**

The previous fields `bump_rules` and `smart_version_analysis` are
removed; version decisions now flow through the `version_analyze`
step's `suggested_version` (optionally guided by
`se3/version-rules.md`). When either key is present under `version.`
in `se3.yaml`, a deprecation warning is logged at `VersionConfig`
load time and the value is ignored. One warning is emitted per
deprecated key present, so a config that sets both `bump_rules` and
`smart_version_analysis` produces two warnings. The framework does
NOT translate these legacy keys into the new schema.

The new version number is computed by the `version_analyze` step's
`suggested_version` field (see the `se3-versioning` spec). The
configuration system does NOT carry any static task-type-to-bump-type
mapping, and there is no global "smart analysis" on/off switch — the
`version_analyze` step is the single source for the new version, and
project-specific policy is expressed via the optional `se3/version-rules.md`
file (see `se3-versioning` *Custom Version Rules File* requirement).

#### Scenario: Disable version bumping
- **GIVEN** se3.yaml has `version.enabled: false`
- **WHEN** commit step executes
- **THEN** no version bump is performed

#### Scenario: User templates merge over defaults
- **GIVEN** `version.templates: { readme_badge: "v{version}" }` in se3.yaml
- **WHEN** `VersionConfig` is loaded
- **THEN** the `readme_badge` template is the user-supplied string
- **AND** the unspecified `versions_entry` retains its built-in default value

#### Scenario: README and VERSIONS.md updates are independently toggleable
- **GIVEN** `version.readme_enabled: false` and `version.versions_enabled: true`
- **WHEN** the version bumper runs
- **THEN** the README is not rewritten with the rendered `readme_badge` template
- **AND** the `VERSIONS.md`-style history file at `version.versions_file` is still updated

#### Scenario: Commit message inclusion is configurable
- **GIVEN** `version.include_in_commit_message: false`
- **WHEN** the commit step composes the version-bump commit message
- **THEN** the new version string is NOT appended to the commit message

#### Scenario: confidence_threshold accepts arbitrary values without validation
- **GIVEN** `version.confidence_threshold` in se3.yaml is set to a
  value outside the documented set, such as `"hi"`, `"HIGH"`, `42`,
  `true`, or a list
- **WHEN** the framework loads `VersionConfig`
- **THEN** no `ConfigError` is raised and no warning is logged for
  the unrecognised value
- **AND** the raw user-supplied value is stored verbatim on the
  config object
- **AND** downstream consumers that compare against the documented
  set (`null` / `"medium"` / `"high"`) treat the unrecognised value
  as equivalent to `null` (no threshold)

#### Scenario: Deprecated bump_rules / smart_version_analysis ignored with warning
- **GIVEN** se3.yaml sets `version.bump_rules: …` and/or `version.smart_version_analysis: …`
- **WHEN** `VersionConfig` is loaded
- **THEN** a deprecation warning is logged for each deprecated key
- **AND** the keys have no effect — version decisions are driven by `version_analyze`'s `suggested_version`

### Requirement: Confirmation Configuration

The system SHALL support confirmation (review) step configuration
through a **per-step dict model**. There is no global on/off switch —
the presence of a step in `confirmation.steps` is itself the signal
that the step should be confirmed. Steps not listed are not confirmed.

**Schema:**

```yaml
confirmation:
  steps:                      # dict[step_name, step_config]
    plan:    { reviewer: human }
    design:  { reviewer: reviewer_bot, max_iterations: 3 }
    propose: {}               # reviewer omitted → falls back to llm_caller.defaults
```

Each `step_config` accepts:

- `reviewer`: either
  - `"human"` — route review through the MCP call file / interactive
    pathway (never looked up in the agent registry), or
  - an agent name registered in top-level `agents` — build a
    single-agent `LLMCaller` for that agent and run LLM review, or
  - omitted or `null` — run LLM review using the chain defined by
    `llm_caller.defaults`.
- `max_iterations`: positive integer bound on the review ↔ revise
  cycle. Applied only when `reviewer` resolves to an LLM path (never
  for `reviewer: human`). Omitted values use the built-in default.

**No global toggle:**

The old fields `confirmation.enabled`, the global `confirmation.reviewer`,
the `confirmation.llm_reviewer` subtree (including `llm_reviewer.model`
and `llm_reviewer.max_iterations`), and the list-form
`confirmation.steps: [step, step]` are all **removed**. When present in
a legacy config, each is logged once as a deprecation warning and
ignored — the framework does NOT auto-map them to the new schema.
Temporary global-off workflows are intentionally left to a future CLI
flag / environment variable; they are NOT covered by the config.

**Name resolution and fail-fast:**

A string `reviewer` value other than `"human"` MUST resolve to an entry
in the agent registry. Unknown names produce a startup-time error (see
Agent Registry requirement for the error-message format; the reference
location is `confirmation.steps.<step>.reviewer`).

**Step-entry shape validation:**

Each `confirmation.steps.<step>` entry MUST be either `null` (treated
as an empty `{}`) or a mapping containing only the supported keys
`reviewer` and `max_iterations`.

- A non-mapping, non-`null` value (e.g. a list, a bare string, a
  number) is structurally invalid: a WARNING is logged identifying the
  source, the step name, and the offending type, and the entry is
  dropped — the step is treated as if it were not listed under
  `confirmation.steps`.
- Unknown keys other than `reviewer` and `max_iterations` (for example
  `enabled`, `model`, typos like `reviewers`) are NOT a fatal error.
  A WARNING is logged once per `(source, step, sorted-extra-keys)`
  combination, naming the unknown fields and noting that only
  `reviewer` and `max_iterations` are supported. The unknown fields
  are then ignored — the rest of the entry (its valid `reviewer` /
  `max_iterations` values) is parsed normally.
- An empty mapping `{}` and `null` are both valid: they leave
  `reviewer` and `max_iterations` unset and the step falls back to
  `llm_caller.defaults` for the chain and the built-in default for
  the iteration cap.

**Global + project merge:**

`confirmation.steps` is merged **entry-level** by step name: a project
entry for `plan` overrides a global entry for `plan`; a global entry
for `design` without a project counterpart remains in effect.

**Human review response mechanism (unchanged):**

The CONFIRM step for a `reviewer: human` config supports two response
pathways:

1. **Interactive**: When `se3 run` is running, the run loop displays
   the reviewed step's outputs and prompts the user to approve or
   request changes directly in the terminal.
2. **File-based**: A call file is created at
   `se3/calls/confirm_{step_id}_{timestamp}.json`. A response file
   with `.response` suffix containing
   `{"approved": bool, "feedback": string}` can be placed alongside
   it. On resume, the confirm step detects and processes the response
   file.

The interactive pathway writes the `.response` file automatically, so
both pathways converge on the same mechanism.

#### Scenario: Steps not listed are not confirmed
- **GIVEN** `confirmation.steps: { plan: { reviewer: human } }`
- **WHEN** the flow completes `design`, `implement`, or any step other
  than `plan`
- **THEN** no CONFIRM step is inserted after it

#### Scenario: Listed step triggers CONFIRM insertion
- **GIVEN** `confirmation.steps: { plan: { reviewer: human } }`
- **WHEN** the flow completes `plan`
- **THEN** a CONFIRM step is inserted after it

#### Scenario: reviewer 'human' uses MCP call pathway
- **GIVEN** a step's config is `{ reviewer: human }`
- **WHEN** the CONFIRM step executes
- **THEN** a call file is created at
  `se3/calls/confirm_{step_id}_{timestamp}.json`
- **AND** the flow pauses awaiting interactive approval or a
  `.response` file

#### Scenario: reviewer = agent name uses single-agent LLM review
- **GIVEN** `agents.reviewer_bot` is registered
- **AND** a step's config is `{ reviewer: reviewer_bot, max_iterations: 3 }`
- **WHEN** the CONFIRM step executes
- **THEN** the framework constructs an `LLMCaller` whose chain is just
  `[reviewer_bot]` and performs LLM review
- **AND** the review ↔ revise loop honours `max_iterations: 3`

#### Scenario: reviewer omitted falls back to llm_caller.defaults
- **GIVEN** `llm_caller.defaults: [primary, backup]`
- **AND** a step's config is `{}` (no `reviewer` field) or
  `{ reviewer: null }`
- **WHEN** the CONFIRM step executes
- **THEN** the framework constructs an `LLMCaller` whose chain is the
  resolved `llm_caller.defaults` list and performs LLM review

#### Scenario: max_iterations ignored for reviewer 'human'
- **GIVEN** a step's config is `{ reviewer: human, max_iterations: 5 }`
- **WHEN** the CONFIRM step executes
- **THEN** `max_iterations` has no effect — the human MCP call pathway
  waits on the user with no iteration cap

#### Scenario: Unknown fields in a step entry warn and are ignored
- **GIVEN** `confirmation.steps.plan: { reviewer: human, enabled: true, model: opus }`
- **WHEN** the framework loads confirmation config
- **THEN** a WARNING is logged once for the `(source, plan, [enabled, model])`
  combination naming the unknown fields
- **AND** the entry is still applied — `plan` is confirmed with
  `reviewer: human`; the unknown `enabled` and `model` fields have
  no effect

#### Scenario: Non-mapping step entry warns and is dropped
- **GIVEN** `confirmation.steps.plan: [reviewer, human]` (a list,
  bare string, or other non-mapping non-`null` value)
- **WHEN** the framework loads confirmation config
- **THEN** a WARNING is logged identifying the source, step name, and
  the offending type
- **AND** `plan` is treated as if it were not listed under
  `confirmation.steps` — no CONFIRM step is inserted after `plan`

#### Scenario: Unknown reviewer name fails fast
- **GIVEN** `confirmation.steps.plan: { reviewer: ghost }` and `ghost`
  is not in the agent registry
- **THEN** the framework raises a configuration error at startup
- **AND** the error message names the reference location
  (`confirmation.steps.plan.reviewer`) and lists registered agent names

#### Scenario: Deprecated confirmation.enabled is ignored with warning
- **GIVEN** a legacy config sets `confirmation.enabled: false`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged
- **AND** `enabled` has no effect — step membership in
  `confirmation.steps` alone determines whether CONFIRM is inserted

#### Scenario: Deprecated list-form steps is ignored with warning
- **GIVEN** a legacy config sets `confirmation.steps: [plan, design]`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged
- **AND** no steps are confirmed (the list is not auto-mapped to the
  new dict form)

#### Scenario: Deprecated global reviewer is ignored with warning
- **GIVEN** a legacy config sets top-level `confirmation.reviewer: human`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged
- **AND** the field has no effect — reviewers are read per step from
  `confirmation.steps.<step>.reviewer`

#### Scenario: Deprecated llm_reviewer subtree is ignored with warning
- **GIVEN** a legacy config sets `confirmation.llm_reviewer.model: …`
  and/or `confirmation.llm_reviewer.max_iterations: …`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged for the subtree
- **AND** the fields have no effect — `max_iterations` is configured
  per step and the model is chosen via the agent registry

#### Scenario: Entry-level merge of confirmation.steps
- **GIVEN** global config declares
  `confirmation.steps: { plan: { reviewer: human } }`
- **AND** project config declares
  `confirmation.steps: { design: { reviewer: reviewer_bot } }`
- **THEN** the merged config confirms BOTH `plan` (with `human`, from
  global) AND `design` (with `reviewer_bot`, from project)

#### Scenario: Project overrides global entry for same step
- **GIVEN** global config declares
  `confirmation.steps.plan: { reviewer: human }`
- **AND** project config declares
  `confirmation.steps.plan: { reviewer: reviewer_bot, max_iterations: 2 }`
- **THEN** the merged config confirms `plan` with reviewer
  `reviewer_bot` and `max_iterations: 2`; the global entry is replaced
  entirely

#### Scenario: Interactive approval
- **GIVEN** a CONFIRM step for a `reviewer: human` config is paused
- **WHEN** the run loop detects the pause
- **THEN** it displays the reviewed step's outputs
- **AND** prompts the user to approve, request changes, or exit

#### Scenario: File-based approval
- **GIVEN** a CONFIRM step for a `reviewer: human` config created a
  call file
- **WHEN** a `.response` file is placed alongside it
- **AND** user runs `se3 run --resume`
- **THEN** the confirm step reads the response and continues

### Requirement: Conflict Resolver Configuration

The system SHALL support configuring merge conflict resolution strategy for loop branch merges.

**Conflict resolver section options:**
- `conflict_resolver.strategy`: Resolution strategy (default: `"human"`). Any value other than `"human"` or `"llm"` is silently coerced to `"human"` at load time with no warning or error.
  - `"human"`: Preserve conflict state in working tree, create a call file at `se3/calls/merge_conflict_{timestamp}.json` with conflict details, and return `pending_human` to the caller. The user resolves conflicts manually.
  - `"llm"`: Attempt per-file LLM-based conflict resolution. Each conflicting file is sent to the LLM for resolution. If all files are resolved successfully, the merge completes automatically. If any file fails (LLM output still contains conflict markers), falls back to `"human"` mode.

#### Scenario: Default conflict resolution
- **WHEN** no `conflict_resolver` section exists in se3.yaml
- **THEN** the framework uses `"human"` strategy (preserve conflicts, create call file)

#### Scenario: LLM conflict resolution
- **GIVEN** `conflict_resolver.strategy: "llm"` in se3.yaml
- **WHEN** a merge conflict occurs during loop branch merge
- **THEN** the framework attempts to resolve each conflicting file via LLM
- **AND** if all files are resolved, the merge completes automatically
- **AND** if any file fails, falls back to human mode

### Requirement: Merge Configuration

The system SHALL support configuration of the `se3 merge` command via a top-level `merge` section in `se3.yaml`.

**Merge section options:**
- `merge.strategy`: Default conflict-resolution tier for `se3 merge` (default: `"fast"`). Allowed values: `"fast"`, `"safe"`, `"strict"`. The CLI flag `--strategy` overrides this value for a single invocation. The previous values `"default"` and `"robust"` have been removed without silent aliasing: providing them at config load SHALL raise a `ConfigError` whose message points at the replacement (`safe` replaces `default`; `fast` replaces `robust`).
- `merge.delete_merged_default`: Whether `se3 merge` defaults to deleting merged branches and archiving their bound worktrees under `.se3/archive/` (default: `true`). The CLI flags `--delete-merged` / `--no-delete-merged` override this value for a single invocation.
- `merge.max_conflict_resolve_iterations`: Maximum number of batched LLM-as-editor rounds the conflict resolver performs per `git merge` invocation before applying the active strategy's cap-exhaustion policy (default: `10`). MUST resolve to a positive integer (`>= 1`) after coercion. The loader passes the raw value through Python's `int(...)` and only catches `TypeError` / `ValueError`:
  - **Values `int(...)` rejects** — strings that do not parse as integers, lists, dicts, and similar non-numeric types — do NOT fail fast. The loader emits a WARNING identifying the offending value and falls back to the built-in default `10`. The rationale is that a clearly-invalid type is a likely typo where the safest action is to keep `se3 merge` working with sane defaults.
  - **Values `int(...)` silently coerces** are accepted as the coerced integer and then run through the `>= 1` check with no warning of their own:
    - Floats such as `2.5` are truncated (`int(2.5) == 2`) and used as-is.
    - Booleans are coerced via Python's `bool` ⊂ `int` rule: `True` becomes `1` (accepted), `False` becomes `0` which then trips the `>= 1` check and raises a fail-fast `ConfigError`.
  - Non-positive integer values produced by coercion (literal `0`, `-1`, `False`, etc.) trigger a fail-fast `ConfigError` at config load — a structurally valid but semantically wrong choice is rejected loudly.
  The strategy decides what happens on cap exhaustion: `fast` exits with a failure, `safe` escalates to a human MCP call, `strict` never enters the loop in the first place. There is no separate `merge.conflict_resolver` subtree — conflict-resolution behavior is fully determined by `merge.strategy` and this iteration cap.
- `merge.strict_runtime_sync`: Whether `se3 merge` treats a tier A runtime sync collision as a fatal error that halts the merge sequence (default: `false`). When `true`, the old strict behavior is preserved: a collision raises `runtime_sync_collision` and stops the sequence. When `false` (default), collisions are bypassed by writing the source version to a sidecar file (`<dest>.from-<branch>`) and the sequence continues. Accepts boolean values and common string forms (`"true"`, `"false"`, `"1"`, `"0"`, `"yes"`, `"no"`); unrecognized strings fall back to the default `false`.

**Orthogonality with `conflict_resolver.strategy`:**

`merge.strategy` is independent of and orthogonal to `conflict_resolver.strategy`. The latter governs only the in-loop branch merge performed by `se3 run --loop --merge`; the former governs the standalone `se3 merge <branch> ...` command. Setting one has no effect on the other.

**Example configuration:**
```yaml
merge:
  strategy: fast                       # fast (default) | safe | strict
  delete_merged_default: true          # default; pass --no-delete-merged to keep
  max_conflict_resolve_iterations: 10  # cap on batched LLM-as-editor rounds
  strict_runtime_sync: false           # true = halt on collision, false = bypass via sidecar
```

#### Scenario: Default merge configuration
- **WHEN** no `merge` section exists in se3.yaml
- **THEN** `se3 merge` uses `strategy: fast`, `delete_merged_default: true`, `max_conflict_resolve_iterations: 10`, and `strict_runtime_sync: false`

#### Scenario: Strategy override via CLI flag
- **GIVEN** `merge.strategy: fast` in se3.yaml
- **WHEN** the user runs `se3 merge feat/x --strategy strict`
- **THEN** the invocation uses the `strict` tier
- **AND** the configured default is unchanged for future invocations

#### Scenario: Removed strategy names rejected fail-fast
- **GIVEN** `merge.strategy: default` or `merge.strategy: robust` in se3.yaml
- **WHEN** the framework loads `MergeConfig`
- **THEN** a `ConfigError` is raised before any `se3 merge` invocation runs
- **AND** the error message points the user at the replacement strategy (`safe` for `default`, `fast` for `robust`)

#### Scenario: delete_merged_default honored when no CLI flag is given
- **GIVEN** `merge.delete_merged_default: true` in se3.yaml (which is also the default)
- **WHEN** the user runs `se3 merge feat/x` without `--no-delete-merged`
- **THEN** merged branches (and their clean bound worktrees) are deleted and archived to `.se3/archive/`

#### Scenario: max_conflict_resolve_iterations cap honored
- **GIVEN** `merge.max_conflict_resolve_iterations: 3` in se3.yaml
- **WHEN** `se3 merge feat/x` runs the LLM-as-editor loop on a conflicted merge
- **THEN** the orchestrator performs at most 3 batched LLM rounds before applying the active strategy's cap-exhaustion policy

#### Scenario: max_conflict_resolve_iterations non-positive value fail-fast
- **GIVEN** `merge.max_conflict_resolve_iterations: 0` (or any value `< 1`) in se3.yaml
- **WHEN** the framework loads `MergeConfig`
- **THEN** a `ConfigError` is raised before any `se3 merge` invocation runs
- **AND** the error message identifies the offending key and value

#### Scenario: max_conflict_resolve_iterations non-coercible value warns and falls back
- **GIVEN** `merge.max_conflict_resolve_iterations` in se3.yaml is set
  to a value that Python's `int(...)` rejects with `TypeError` or
  `ValueError` — for example a non-numeric string like `"ten"`, a
  list, or a dict
- **WHEN** the framework loads `MergeConfig`
- **THEN** no `ConfigError` is raised
- **AND** a WARNING is logged identifying the offending value and the
  expected type (positive integer)
- **AND** the effective `max_conflict_resolve_iterations` is the built-in
  default `10`
- **AND** subsequent `se3 merge` invocations use the default cap

#### Scenario: max_conflict_resolve_iterations float value silently truncates
- **GIVEN** `merge.max_conflict_resolve_iterations: 2.5` in se3.yaml
- **WHEN** the framework loads `MergeConfig`
- **THEN** no `ConfigError` is raised and no warning is logged for the
  type — `int(2.5)` truncates the value to `2`
- **AND** the effective `max_conflict_resolve_iterations` is `2`
- **AND** a float that truncates to a non-positive integer (e.g.
  `0.5` → `0`, `-1.2` → `-1`) is rejected fail-fast by the `>= 1`
  check, just like a literal `0` or `-1`

#### Scenario: max_conflict_resolve_iterations boolean value coerces to int
- **GIVEN** `merge.max_conflict_resolve_iterations` in se3.yaml is set
  to a boolean
- **WHEN** the framework loads `MergeConfig`
- **THEN** `True` is coerced to `1` and accepted (no warning, no
  fail-fast)
- **AND** `False` is coerced to `0`, which trips the `>= 1` check and
  raises a fail-fast `ConfigError` — the loader does NOT warn-and-default
  for `False`

#### Scenario: Independence from conflict_resolver.strategy
- **GIVEN** `conflict_resolver.strategy: "llm"` and no `merge` section
- **WHEN** the user runs `se3 merge feat/x`
- **THEN** the standalone merge command uses `merge.strategy = fast`, NOT the `conflict_resolver` value
- **AND** `se3 run --loop --merge` continues to honor `conflict_resolver.strategy: "llm"`

#### Scenario: Strict runtime sync halts on collision
- **GIVEN** `merge.strict_runtime_sync: true` in se3.yaml
- **WHEN** `se3 merge feat/y` succeeds at the git level but a tier A runtime sync collision is detected
- **THEN** the merge sequence halts with the `runtime_sync_collision` failure category
- **AND** the merge commit is preserved (not rolled back)
- **AND** subsequent branches in the argument list are NOT attempted

#### Scenario: Lenient runtime sync bypasses collision via sidecar
- **GIVEN** `merge.strict_runtime_sync: false` (or omitted) in se3.yaml
- **WHEN** `se3 merge feat/y` succeeds at the git level but a tier A runtime sync collision is detected
- **THEN** the source version is written to a sidecar file `<dest>.from-<branch>`
- **AND** the target file remains unchanged
- **AND** the collision is recorded in the merge report for auditability
- **AND** the merge sequence continues with the next branch

### Requirement: Implement Configuration

The system SHALL support configuration for the implement step's execution strategy.

**Implement section options:**
- `implement.group_loc_threshold`: Total estimated LOC threshold below which all task groups are collapsed into a single LLM call (default: 300). When the sum of `estimated_loc` across all tasks in all groups is at or below this threshold, the implement step merges all groups into one call instead of executing them as separate DAG-parallel groups. The loader passes the raw value through Python's `int(...)` without a surrounding `try/except`, so coercion behavior is whatever `int(...)` natively provides:
  - **Values `int(...)` rejects** — non-numeric strings, lists, dicts, and similar non-coercible types — propagate the underlying `TypeError` / `ValueError` out of `ImplementConfig.from_dict` and abort config load. There is no clamp-and-warn fallback for this field (unlike `merge.max_conflict_resolve_iterations`, which warns-and-defaults on the same input shapes).
  - **Values `int(...)` silently coerces** are accepted as the coerced integer with no further validation:
    - Floats are truncated (`int(2.5) == 2`, `int(-1.7) == -1`) and used as-is.
    - Booleans are coerced via Python's `bool` ⊂ `int` rule: `True` becomes `1`, `False` becomes `0`.
    - Numeric strings (e.g. `"500"`) are parsed as their integer value.
  - Non-positive and negative results (zero, negative integers, `False` → `0`) are NOT rejected — the field has no `>= 1` floor at load time. Authors of `se3.yaml` SHOULD therefore supply a positive integer; surprising values such as `0`, `-5`, or `True` are stored verbatim and may cause unintended collapse / no-collapse behavior downstream.
- `implement.use_worktree`: Boolean gate for the DAG parallel path's per-group worktree creation (default: `true`). When `false`, the implement step never creates `impl/*` branches or per-group worktrees and executes all groups sequentially on the original branch, regardless of DAG topology. This is the `implement`-step-scoped setting and is orthogonal to the loop mode `--no-worktree` CLI flag, which controls a different layer (whether the whole loop iteration runs in an isolated worktree). Accepts boolean values and common string forms (`"true"`, `"false"`, `"1"`, `"0"`, `"yes"`, `"no"`); unrecognized strings fall back to the default `true`.
- Environment variable `SE3_IMPLEMENT_USE_WORKTREE`: Overrides `implement.use_worktree` from the environment (useful for CI or one-off runs). Accepts the same string forms as the config value. Takes precedence over `se3.yaml`.

**Example configuration:**
```yaml
implement:
  group_loc_threshold: 300  # Collapse groups when total LOC ≤ 300
  use_worktree: true        # Allow DAG parallel path to create per-group worktrees
```

#### Scenario: Default implement configuration
- **WHEN** no `implement` section exists in se3.yaml
- **THEN** the framework uses a default `group_loc_threshold` of 300
- **AND** `use_worktree` defaults to `true`

#### Scenario: Custom LOC threshold
- **GIVEN** `implement.group_loc_threshold: 500` in se3.yaml
- **WHEN** `plan` produces groups with total estimated_loc = 400
- **THEN** the implement step collapses all groups into a single LLM call

#### Scenario: Non-int-convertible group_loc_threshold aborts config load
- **GIVEN** `implement.group_loc_threshold` in se3.yaml is set to a
  value that Python's `int(...)` rejects with `TypeError` or
  `ValueError` — for example a non-numeric string like `"big"`, a
  list, or a dict
- **WHEN** `ImplementConfig.from_dict` parses the value
- **THEN** the underlying `TypeError` / `ValueError` is NOT caught and
  propagates out of `from_dict`, aborting config load
- **AND** no warning is logged and no fallback to the built-in default
  `300` occurs (this field has no clamp-and-warn safety net)

#### Scenario: Float group_loc_threshold silently truncates
- **GIVEN** `implement.group_loc_threshold: 299.9` in se3.yaml
- **WHEN** `ImplementConfig.from_dict` parses the value
- **THEN** the value is truncated via `int(299.9) == 299` and used as-is
- **AND** no warning is logged

#### Scenario: Boolean group_loc_threshold coerces to int without validation
- **GIVEN** `implement.group_loc_threshold: true` in se3.yaml
- **WHEN** `ImplementConfig.from_dict` parses the value
- **THEN** `True` is coerced to `1` via Python's `bool` ⊂ `int` rule
  and stored verbatim
- **AND** no warning is logged and no `>= 1` floor check is applied
  (a `False` value would similarly be stored as `0` rather than rejected)

#### Scenario: Disable worktree via se3.yaml
- **GIVEN** `implement.use_worktree: false` in se3.yaml
- **WHEN** the implement step would otherwise enter the DAG parallel path (large multi-group implementation with forks)
- **THEN** `ImplementConfig.load()` yields `use_worktree=False`
- **AND** the implement step executes all groups sequentially on the original branch
- **AND** no `impl/*` branch or worktree is created

#### Scenario: Environment variable overrides config file
- **GIVEN** `implement.use_worktree: true` in se3.yaml
- **AND** environment variable `SE3_IMPLEMENT_USE_WORKTREE=0`
- **WHEN** `ImplementConfig.load()` runs
- **THEN** the effective `use_worktree` is `False`
- **AND** the environment variable takes precedence over the file value

#### Scenario: Orthogonality with loop --no-worktree
- **GIVEN** the user runs `se3 run ... --loop --no-worktree`
- **AND** `implement.use_worktree: true` in se3.yaml
- **THEN** the loop iteration runs without its own isolated loop-level worktree
- **AND** the implement step inside each iteration may still use DAG parallel worktrees when the topology justifies it
- **AND** the two settings do not conflict because they govern different layers of the execution pipeline

### Requirement: Workflow Configuration

The system SHALL support workflow-level configuration for the fix loop mechanism and the self_check N-pass / convergence behavior.

**Workflow section options:**
- `workflow.max_fix_iterations`: Maximum number of fix loop iterations before the flow is marked FAILED (default: 100). The fix loop counter is shared across TEST, SELF_CHECK, and VERIFY_SPEC steps. When exhausted, the state machine sets the flow to FAILED status, generates an A-class issue, and stops execution. **Sentinel:** a value of exactly `0` (or `null`, which is normalized to `0` at load time) means "unlimited" — every fix-loop comparison point treats `max_iter == 0` as no upper bound, so the flow is never marked FAILED purely on iteration count and prompts/log lines render the iteration as `N (unlimited)` rather than `N of M`. **Negative values are rejected fail-fast** at config load (mirrors the `< 1` rejection on `self_check_passes_required`), so a typo like `-1` cannot silently disable exhaustion. The default deliberately remains finite (100) to avoid new users accidentally burning tokens; users must set `0`/`null` explicitly to opt into unlimited mode.
- `workflow.self_check_passes_required`: Number of consecutive clean self_check passes required within a single fix-loop round before advancing to the next step (default: 1). MUST be an integer `>= 1`. When set to N>1, each fix-loop round repeats the self_check step up to N times: any single instance reporting issues short-circuits to fix-loop immediately (remaining instances are not created). Only after N consecutive clean instances does the flow advance. Values `< 1` (including 0 and negatives) trigger startup fail-fast in `WorkflowConfig` loading.

  **Interaction with nested `llm_caller.steps.self_check` chains.** When `self_check` is configured with the nested per-pass form (`[[...], [...]]`; see the *LLM Caller Configuration* requirement), the effective per-round pass count is reconciled with this field as follows:
  - **Nested chains, count not explicitly set:** when the user has NOT explicitly set `workflow.self_check_passes_required`, the effective pass count is automatically the **number of nested chains** — the chain list alone fully expresses the intent, so the count need not be restated.
  - **Both explicitly set:** when both the nested form and an explicit `self_check_passes_required` are present, they are NOT required to agree and no error is raised. The explicit count wins and determines the actual number of passes:
    - If the count is **greater than** the number of chains, the passes beyond the last chain **reuse the last chain**.
    - If the count is **smaller than** the number of chains, the surplus chains are simply not executed, and a single WARNING is logged noting that one or more configured chains will not be used.
  - **Flat form:** the flat `list[str]` form (and the no-override case) leaves the pass count governed entirely by `self_check_passes_required` (default 1), fully back-compatible.
  - The per-pass index resets at the start of each fix-loop round (consistent with the per-round semantics of `self_check_passes_required`).
- `workflow.baseline_fix_max_attempts`: Independent per-flow bound on how many fix-loop attempts may target inherited (baseline) test failures under mechanism B (default: `3`). MUST be an integer `>= 0`. This budget is **deliberately not shared** with `workflow.max_fix_iterations`: the global cap may be the "unlimited" sentinel (`0`), but baseline failures — which are not this flow's regression and may be fundamentally un-fixable (a missing system library, a flaky test, one needing a human decision) — must always be bounded. A value of `0` disables baseline looping entirely (inherited failures are surfaced but never looped, the historical behavior). **Negative values are rejected fail-fast** at config load (mirrors `self_check_passes_required`'s `< 1` rejection). Non-integer types — YAML booleans (`true`/`false`/`yes`/`no`/`on`/`off`) and floats — log a WARNING and fall back to the default `3`, symmetric with `self_check_passes_required` / `max_fix_iterations` handling of the same types. The per-flow attempt counter lives in the flow state (`flow.state.context["baseline_fix_attempts"]`) and is incremented by the state machine whenever a fix transition targeted baseline failures; once it reaches this cap, the active baseline failures are recorded as given-up in `se3/state/baseline_fix_attempts.json` (a cross-flow persistent memory) and surfaced without further looping (see the flow-engine *Test Step Configuration and Multi-Phase Execution* mechanism B and base *Engine Module Extensions*).
- `workflow.self_check_convergence_enabled`: Toggle for the cross-fix-loop self_check convergence shortcut (default: `false`). When `false`, the state machine never compares the current round's issues against the previous round's issues, and `_issues_converged` is not invoked. When `true`, only the first self_check instance of a new fix-loop round (pass_index=1) receives `prev_self_check_issues` and participates in the comparison; instances #2..#N within the same round never participate. **NOTE:** the default flipped from on to off in this revision; this flip is intentionally not announced via changelog or startup log because the project requires every issue to be resolved, making convergence-based early exit a no-op on the happy path.

**Local-override shadowing and effective-source logging:**

Because `se3.local.yaml` replaces `se3.yaml` as a whole (whole-file pick-one, no key-level merge — see *Configuration File Format*), a `workflow.max_fix_iterations` set in `se3.yaml` is silently shadowed whenever `se3.local.yaml` exists. To make that override visible, `WorkflowConfig.load` SHALL log the resolved value together with the file it came from (`_log_effective_source`): `workflow config: max_fix_iterations=<N> (effective source: <se3.local.yaml|se3.yaml>)`. The log line is deduped per resolved config path so the per-step `load` calls do not flood the log. The two shipped files SHOULD carry the **same** reconciled value so the shadow is a no-op (the cap is only a backstop — with the pre-implement baseline mechanism eliminating the inherited-failure fix loop, exhaustion should rarely be reached).

**Engine.json output schema:**

When the fix loop branches in `verify_spec` or `self_check`, the step writes `max_fix_iterations` (int) into `step.outputs` for downstream renderers. `0` is the documented sentinel for "unlimited" and SHOULD be displayed as `N (unlimited)` rather than `N of 0`. Negatives never appear here (rejected at config load). The field is written only on the fix-loop trigger branch (when the step returns `REVISION_NEEDED`); downstream renderers MUST treat the key as optional on success-path steps.

**Example configuration:**
```yaml
workflow:
  max_fix_iterations: 100               # Allow up to 100 fix loop iterations (default; use 0 or null for unlimited)
  self_check_passes_required: 3         # Require 3 consecutive clean self_check passes per round
  baseline_fix_max_attempts: 3          # Per-flow cap on looping inherited baseline failures (default; 0 disables)
  self_check_convergence_enabled: false # Disable cross-round convergence shortcut (default)
```

#### Scenario: Effective max_fix_iterations source is logged
- **GIVEN** both `se3.yaml` and `se3.local.yaml` set `workflow.max_fix_iterations` (reconciled to the same value)
- **WHEN** `WorkflowConfig.load` resolves the workflow config
- **THEN** a one-shot (per resolved config path) INFO line is logged naming the resolved value and the effective source file (`se3.local.yaml` when present, since it shadows `se3.yaml` whole-file)
- **AND** the resolved `max_fix_iterations` is the value from the shadowing file, making the override visible rather than silent

#### Scenario: Default workflow configuration
- **WHEN** no `workflow` section exists in se3.yaml
- **THEN** the framework uses `max_fix_iterations=100`, `self_check_passes_required=1`, `baseline_fix_max_attempts=3`, and `self_check_convergence_enabled=false`
- **AND** self_check executes once per fix-loop round (legacy behavior, single pass)
- **AND** convergence detection is OFF — even when current and previous round issues are identical, the flow still enters fix-loop

#### Scenario: Custom max fix iterations
- **GIVEN** `workflow.max_fix_iterations: 10` in se3.yaml
- **WHEN** the fix loop reaches 10 iterations without resolving all issues
- **THEN** the state machine sets the flow to FAILED status
- **AND** an A-class issue is generated describing the unresolved problems

#### Scenario: max_fix_iterations=0 disables exhaustion
- **GIVEN** `workflow.max_fix_iterations: 0` in se3.yaml
- **WHEN** the fix loop has completed any number of iterations (including values much larger than the default 100)
- **THEN** the state machine never sets the flow to FAILED solely because of iteration count
- **AND** no A-class fix-loop-exhaustion issue is generated by exhaustion alone
- **AND** prompts and log lines render the iteration display as `N (unlimited)` instead of `N of M`

#### Scenario: max_fix_iterations=null treated as unlimited
- **GIVEN** `workflow.max_fix_iterations: null` (or omitting the value but keeping the key) in se3.yaml
- **WHEN** `WorkflowConfig.from_dict` parses the value
- **THEN** the value is normalized to the sentinel `0`
- **AND** the runtime behaves identically to `max_fix_iterations: 0` — the fix loop never exits because of an iteration upper bound

#### Scenario: Negative max_fix_iterations fail-fast
- **GIVEN** `workflow.max_fix_iterations: -1` (or any negative integer) in se3.yaml
- **WHEN** the framework loads `WorkflowConfig` at startup
- **THEN** a `ConfigError` is raised before any flow runs
- **AND** the error message identifies the offending key and value (e.g., "max_fix_iterations=-1 must be >= 0 (use 0 or null for unlimited)")
- **AND** the flow is not allowed to start; negatives are NOT silently treated as unlimited (only `0`/`null` opt into that mode)

#### Scenario: Boolean / float max_fix_iterations warns and falls back
- **GIVEN** `workflow.max_fix_iterations: true` (or `false`/`yes`/`no`/`on`/`off`, all parsed as YAML booleans, or any float like `0.5`) in se3.yaml
- **WHEN** the framework loads `WorkflowConfig` at startup
- **THEN** a WARNING is logged identifying the offending value
- **AND** `max_fix_iterations` falls back to the default (100) — symmetric with `self_check_passes_required` handling of the same types

#### Scenario: Nested self_check chains derive the pass count
- **GIVEN** `llm_caller.steps.self_check: [[agentA], [agentB, agentC]]` and `workflow.self_check_passes_required` is NOT explicitly set
- **WHEN** a fix-loop round enters self_check
- **THEN** the effective pass count is 2 (the number of nested chains)
- **AND** pass 1 uses `[agentA]` and pass 2 uses `[agentB, agentC]`

#### Scenario: Explicit pass count larger than chain count reuses the last chain
- **GIVEN** `llm_caller.steps.self_check: [[agentA], [agentB]]` and `workflow.self_check_passes_required: 3`
- **WHEN** a fix-loop round enters self_check
- **THEN** there are 3 passes: pass 1 → `[agentA]`, pass 2 → `[agentB]`, pass 3 → `[agentB]` (the last chain is reused for the overflow pass)
- **AND** no error or warning is raised for the count exceeding the number of chains

#### Scenario: Explicit pass count smaller than chain count warns and drops surplus chains
- **GIVEN** `llm_caller.steps.self_check: [[agentA], [agentB], [agentC]]` and `workflow.self_check_passes_required: 2`
- **WHEN** the framework reconciles the configuration
- **THEN** only 2 passes run (pass 1 → `[agentA]`, pass 2 → `[agentB]`)
- **AND** a WARNING is logged noting that the third chain (`[agentC]`) is configured but will not be used

#### Scenario: Custom baseline_fix_max_attempts
- **GIVEN** `workflow.baseline_fix_max_attempts: 5` in se3.yaml
- **WHEN** the framework loads `WorkflowConfig`
- **THEN** the resolved `baseline_fix_max_attempts` is `5`
- **AND** mechanism B may attempt to fix inherited baseline failures across up to 5 per-flow fix-loop attempts before recording them as given-up, independently of `max_fix_iterations`

#### Scenario: baseline_fix_max_attempts=0 disables baseline looping
- **GIVEN** `workflow.baseline_fix_max_attempts: 0` in se3.yaml
- **WHEN** a flow encounters inherited (baseline) test failures
- **THEN** mechanism B never loops them — inherited failures are surfaced (留痕) but never enter the fix loop (the historical surface-only behavior)

#### Scenario: Negative baseline_fix_max_attempts fail-fast
- **GIVEN** `workflow.baseline_fix_max_attempts: -1` (or any negative integer) in se3.yaml
- **WHEN** the framework loads `WorkflowConfig` at startup
- **THEN** a `ConfigError` is raised before any flow runs
- **AND** the error message identifies the offending key and value (e.g., "baseline_fix_max_attempts=-1 must be >= 0 (use 0 to disable baseline looping)")

#### Scenario: Boolean / float baseline_fix_max_attempts warns and falls back
- **GIVEN** `workflow.baseline_fix_max_attempts: true` (a YAML boolean) or any float like `2.5` in se3.yaml
- **WHEN** the framework loads `WorkflowConfig` at startup
- **THEN** a WARNING is logged identifying the offending value
- **AND** `baseline_fix_max_attempts` falls back to the default (3) — symmetric with `self_check_passes_required` / `max_fix_iterations` handling of the same types

#### Scenario: Custom N-pass self_check
- **GIVEN** `workflow.self_check_passes_required: 3` in se3.yaml
- **WHEN** a fix-loop round enters the self_check step
- **THEN** the state machine creates self_check Step instances #1, #2, #3 sequentially as each prior instance returns COMPLETED with no issues
- **AND** only after #3 returns clean does the flow advance to verify_spec
- **AND** if any instance reports issues, the flow enters fix-loop immediately and remaining instances are skipped

#### Scenario: self_check_passes_required=0 fail-fast
- **GIVEN** `workflow.self_check_passes_required: 0` in se3.yaml (or any value `< 1`)
- **WHEN** the framework loads `WorkflowConfig` at startup
- **THEN** a `ConfigError` is raised before any flow runs
- **AND** the error message identifies the offending key and value (e.g., "self_check_passes_required must be >= 1, got 0")
- **AND** the flow is not allowed to start with an invalid value (no silent clamping to 1)

#### Scenario: Explicit convergence enabled
- **GIVEN** `workflow.self_check_convergence_enabled: true` in se3.yaml
- **WHEN** a fix-loop round's first self_check instance (pass_index=1) reports issues that match the previous round's last self_check issues
- **THEN** `_issues_converged` is invoked and returns True
- **AND** the self_check instance returns COMPLETED with `outputs["converged"]=true`, short-circuiting the fix-loop
- **AND** subsequent instances #2..#N within the same round do not participate in convergence comparison even if `prev_self_check_issues` data exists, because the input is only injected at pass_index=1

### Requirement: Test Configuration

The system SHALL support configuration for the test step's execution, including a dynamic timeout mechanism driven by the implement step's estimate of the test suite runtime.

**Test section options:**
- `command`: Primary test command (default: null = auto-detect)
- `timeout`: Fallback timeout in seconds, used when `estimated_test_duration` from the implement step is missing or invalid (default: 1800)
- `timeout_multiplier`: Multiplier applied to implement's `estimated_test_duration` to derive the actual timeout for the primary test command (default: 2.0). Values below 1.0 are clamped up to 1.0 at load time so a typo cannot silently disable the feature.
- `min_dynamic_timeout`: Lower bound in seconds on the computed dynamic timeout (default: 30)
- `max_dynamic_timeout`: Upper bound in seconds on the computed dynamic timeout (default: 14400). When the user's `test.timeout` is larger than the default ceiling, the framework raises the ceiling to at least `test.timeout` so an explicit high fallback is never silently capped.
- `phases`: List of additional test phases. Each phase's own `timeout` is always used; the dynamic timeout mechanism does NOT apply to phases.
- `critical_tests`: List of test ID / substring patterns marking **critical acceptance tests** (default: `[]` — opt-in). This is the configuration surface for the "critical acceptance test" annotation mechanism. A listed test that is **skipped**, or a listed pattern that is **missing** (matches neither a real PASSED/FAILED run nor a SKIPPED line in the parseable per-test output), is treated as **not passed / not verified** — the test step forces `overall_passed`/`tests_passed` to `False` and triggers the fix loop (see flow-engine *Test Step Configuration and Multi-Phase Execution*). Ordinary non-critical skips are unaffected, so platform/optional-dependency skips are not swept up. **Tolerant parsing:** an invalid value (not a list) falls back to `[]` with a warning, and each element is coerced to `str`. **Verbose prerequisite:** detection relies on pytest's verbose per-test output. When `critical_tests` is configured, the framework ensures the pytest command carries a per-test verbose flag (the default auto-detected command already includes `-v`; `-v` is appended when missing). Under a custom non-verbose command the missing-detection safely skips with a logged warning rather than producing false positives.

**Example configuration:**
```yaml
test:
  command: null
  timeout: 1800
  timeout_multiplier: 2.0
  min_dynamic_timeout: 30
  max_dynamic_timeout: 14400
  critical_tests:
    - "test_render_paradigm_in_headless_browser"
  phases:
    - name: "e2e"
      command: "python -m pytest tests/e2e -v"
      timeout: 600
      required: false
      in_fix_loop: false
```

#### Scenario: Default test configuration
- **WHEN** no `test` section exists in se3.yaml
- **THEN** the framework uses `timeout=1800`, `timeout_multiplier=2.0`, `min_dynamic_timeout=30`, `max_dynamic_timeout=14400`

#### Scenario: Dynamic timeout from implement estimate
- **GIVEN** implement produced `estimated_test_duration: 180`
- **AND** `test.timeout_multiplier: 2.0` in se3.yaml
- **WHEN** the test step runs the primary command
- **THEN** the primary command's timeout is `180 * 2.0 = 360` seconds (within min/max bounds)

#### Scenario: Fallback when implement estimate missing
- **GIVEN** `estimated_test_duration` is missing or non-positive in implement's output
- **WHEN** the test step runs the primary command
- **THEN** the primary command uses `test.timeout` (default 1800 seconds)

#### Scenario: Invalid timeout_multiplier is clamped
- **GIVEN** `test.timeout_multiplier: 0.1` in se3.yaml (or a non-numeric value)
- **WHEN** TestConfig is loaded
- **THEN** the value is normalized (clamped to >= 1.0 or reset to the default 2.0) and a warning is logged

#### Scenario: max_dynamic_timeout respects user's fallback timeout
- **GIVEN** `test.timeout: 20000` in se3.yaml (legitimately slow suite) with no explicit `max_dynamic_timeout`
- **WHEN** TestConfig is loaded
- **THEN** `max_dynamic_timeout` defaults to at least `test.timeout` (20000), not the built-in 14400 ceiling

#### Scenario: critical_tests gates skipped or missing acceptance tests
- **GIVEN** `test.critical_tests` lists one or more patterns
- **WHEN** a listed critical test is skipped in the test step, or a listed pattern matches neither a run nor a skip in the parseable per-test output (missing)
- **THEN** that test is treated as not passed (the test step forces `overall_passed`/`tests_passed` to `False`)
- **AND** ordinary skips that match no critical pattern keep their current behavior

#### Scenario: critical_tests unset preserves skip behavior
- **GIVEN** no `test.critical_tests` is configured (default empty)
- **WHEN** the test step runs
- **THEN** all skips behave as before (a skip alone never forces a not-passed verdict)

#### Scenario: critical_tests invalid value tolerated
- **GIVEN** `test.critical_tests` is set to a non-list value
- **WHEN** TestConfig is loaded
- **THEN** the value falls back to `[]` and a warning is logged

### Requirement: Language Configuration

The system SHALL support two-tier language configuration for controlling output language.

**Language section options:**
- `language.language`: Language for human-facing steps — summarize, discovery, and steps with human confirmation (default: null = no restriction)
- `language.spec_language`: Language for spec writing in the update_spec step (default: null = no restriction)

When a language is set, a language instruction is appended to the LLM prompt for applicable steps. When null (default), no language instruction is added and the LLM freely chooses language.

**Language instruction content:** Every language-restricted instruction SHALL additionally direct the LLM to preserve all technical symbols verbatim — code identifiers, function/class names, command names, API names, file paths, and literal config keys/values MUST NOT be translated. For spec-writing paths the instruction SHALL additionally state that the configured `spec_language` is authoritative for the written spec body — both prose and every SHALL/MUST requirement statement — so the generated spec does not mirror the source code's incidental language. The `get_language_instruction(language, context, *, for_spec=False)` helper carries this: `for_spec=True` selects the spec-flavored wording. The `language is None`/empty → `""` ("no config → no injection") contract is preserved regardless of `for_spec`.

**Affected steps by `language.language`:**
- `summarize` — always affected
- `discovery` — always affected
- Steps listed in `confirmation.steps` whose per-step `reviewer` is `"human"` (e.g., `plan`)

**Affected steps by `language.spec_language`:**
- `update_spec` — always affected
- The `se3 sync` spec-writing paths — `sync_engine` (Way A edits + Way B rewrites), `sync_discovery` (new-spec generation), and `sync_analyzer` (diff descriptions written into specs, plus base-spec generation) — always affected. These are not `se3 run` state-machine steps, so they obtain the spec-flavored instruction via `get_spec_language_instruction(project_root)` (a `context_builder` helper that wraps `get_language_instruction(spec_language, for_spec=True)`) rather than `get_step_language_instruction`. When `spec_language` is unset they inject nothing, preserving prior sync behavior.

**Unaffected steps (LLM decides language):**
- `analyze`, `plan`, `implement`, `test`, `verify_spec`, `commit`

`analyze` and `verify_spec` are read-only and `implement` writes code rather than spec, so none of them is a spec-writing path; their non-injection is intentional, not a gap. The genuine spec-writing language gap lived in the `sync_*` modules above, which now inject via `get_spec_language_instruction`.

**Example configuration:**
```yaml
language:
  language: zh-CN        # Human-facing outputs in Chinese
  spec_language: en      # Specs written in English
```

#### Scenario: Default language configuration (null)
- **WHEN** no language section exists in se3.yaml, or both values are null
- **THEN** no language instruction is added to any step
- **AND** the LLM freely chooses language for all outputs

#### Scenario: General language set
- **GIVEN** `language.language: zh-CN` in se3.yaml
- **WHEN** the summarize or discovery step runs
- **THEN** the LLM prompt includes a language instruction to respond in zh-CN

#### Scenario: Spec language set
- **GIVEN** `language.spec_language: en` in se3.yaml
- **WHEN** the update_spec step runs
- **THEN** the LLM prompt includes a language instruction to respond in English

#### Scenario: Confirmed steps use general language
- **GIVEN** `language.language: zh-CN` and `confirmation.steps: { plan: { reviewer: human } }`
- **WHEN** the plan step runs
- **THEN** the LLM prompt includes a language instruction to respond in zh-CN

#### Scenario: Independent language settings
- **GIVEN** `language.language: zh-CN` and `language.spec_language: en`
- **WHEN** summarize step runs, it uses zh-CN
- **AND** when update_spec step runs, it uses English
- **AND** when implement step runs, no language instruction is added

#### Scenario: sync write paths honor spec_language
- **GIVEN** `language.spec_language: en` in se3.yaml
- **WHEN** `se3 sync` regenerates or edits a spec via `sync_engine`, `sync_discovery`, or `sync_analyzer`
- **THEN** the prompt includes the spec-flavored language instruction stating English is authoritative for the spec body
- **AND** when `spec_language` is unset, none of these sync paths add a language instruction

#### Scenario: Technical symbols preserved under language restriction
- **GIVEN** any language-restricted prompt (a human-facing step, `update_spec`, or a `sync_*` write path)
- **WHEN** the language instruction is appended
- **THEN** it directs the LLM to keep code identifiers, command names, API names, and file paths verbatim in their original form rather than translating them

### Requirement: Agent Registry

The system SHALL maintain a top-level **agent registry** at the `agents`
key of `se3.yaml` (and `~/.se3/config.yaml`). The registry is the sole
identity layer for agents: every other part of the configuration that
needs an agent (`llm_caller.defaults`, `llm_caller.steps.<step>`,
`confirmation.steps.<step>.reviewer`) references it **by name**.

**Registry shape:**

`agents` MUST be a dict whose keys are unique agent names and whose
values are `AgentDef` dicts. Each `AgentDef` has:

- `type`: agent type identifier (default `"claude-code"`). Recognized
  values are `"claude-code"` (constructs a `ClaudeCodeRunner`) and
  `"codex"` (constructs a `CodexRunner` for the OpenAI Codex CLI) — see
  the agent-runner-infrastructure *Codex CLI Runner* requirement.
  `LLMCaller._create_runner` dispatches on this value.
- `cmd`: CLI command invoked when the agent is selected (required).
  Model selection and other extra parameters are expressed via flags
  carried directly on `cmd` (e.g. `-m <model>`); no additional config
  surface is introduced for them.
- `priority`: **deprecated — accepted but ignored.** Historically an
  integer used to reorder a chain (higher tried first). The global
  agent-priority ordering system has been removed: resolved chains
  (`llm_caller.defaults`, `llm_caller.steps.<step>`, and the
  self_check per-pass chains) now preserve the **written order** of
  their reference lists, and agent rotation on failure follows that
  order. A `priority` value is still parsed onto the `AgentDef` for
  backward compatibility (its presence does not fail-fast), but it no
  longer affects any ordering. When any `agents.<name>.priority` field
  is present, the loader logs a **one-shot per-source** deprecation
  warning noting that `priority` is ignored and that list order is now
  authoritative.

Name uniqueness is inherent to the dict form: duplicate names in the
same YAML are resolved by YAML itself (last wins). Across global +
project configs, `agents` is merged **entry-level by name**: a project
entry overrides a global entry with the same name; non-conflicting
entries coexist.

**String shorthand for AgentDef values:**

In addition to the full `AgentDef` dict form, each entry in the
`agents` dict MAY be written as a bare string. When the value is a
string, it is treated as shorthand for an `AgentDef` whose `cmd`
field is the string and whose other fields take the standard
defaults:

- `type`: `"claude-code"`
- `cmd`: the string value itself
- `priority`: `0`

This shorthand is useful for the common case where the agent name is
descriptive and only the CLI command needs to be specified. Mixing
shorthand and full-dict forms within the same `agents` dict is
permitted — each entry is parsed independently.

Example:
```yaml
agents:
  primary: claude                                                  # shorthand
  opus:    { type: claude-code, cmd: claude-opus, priority: 20 }   # full form
```

The shorthand resolves to:
```yaml
agents:
  primary: { type: claude-code, cmd: claude,      priority: 0 }
  opus:    { type: claude-code, cmd: claude-opus, priority: 20 }
```

**Example:**
```yaml
agents:
  primary:      { type: claude-code, cmd: claude,      priority: 10 }
  backup:       { type: claude-code, cmd: claude-dev,  priority: 5 }
  opus:         { type: claude-code, cmd: claude-opus, priority: 20 }
  reviewer_bot: { type: claude-code, cmd: claude-opus }
```

**Legacy compatibility:**

- **Top-level list form `agents: [...]`** — removed. Detected at load
  time, emits a warning, and is **silently ignored**. The default caller
  chain then falls back to the built-in `[claude]`.
- **`claude_commands: [...]`** — legacy alias. Auto-migrated at load
  time when (and only when) the same source does NOT also set the new
  dict-form `agents`:
  - Each entry's `cmd` is slugified to produce a registry name;
    collisions append `_2`, `_3`, etc.
  - Migrated entries are registered in the top-level `agents` dict.
  - The original entry order is also copied into `llm_caller.defaults`
    so the default chain semantics survive; the migrated chain
    preserves the order in which the `claude_commands` entries appear,
    and any `priority` carried by a legacy entry is ignored (order, not
    priority, drives the chain).
  - A `DeprecationWarning` is emitted showing the equivalent new
    config snippet.
  - When the source already sets a dict-form `agents`, `claude_commands`
    is ignored with a warning (no migration).

**Reference integrity (fail-fast):**

Any reference to an agent name from `llm_caller.defaults`,
`llm_caller.steps.<step>`, or `confirmation.steps.<step>.reviewer` that
does not resolve to a registry entry is a startup-time configuration
error. The error message SHALL include the reference location and a
sorted list of registered agent names. The special `reviewer: human`
value is not looked up in the registry.

#### Scenario: Registry loaded from dict form
- **WHEN** `agents` is a dict at the top level
- **THEN** each entry is parsed into an `AgentDef` with the given
  `type`, `cmd`, and `priority`
- **AND** the name is taken from the dict key
- **AND** `priority` is retained on the `AgentDef` for backward
  compatibility but does NOT influence chain ordering or rotation

#### Scenario: Deprecated priority field is accepted, ignored, and warned per source
- **GIVEN** at least one `agents.<name>` entry declares a `priority`
  field (in either the global or project config source)
- **WHEN** the framework loads the agent registry
- **THEN** loading does NOT fail fast — the entry is registered
  normally and its `priority` value is ignored for ordering purposes
- **AND** a one-shot deprecation warning is logged per source noting
  that `agents.<name>.priority` is deprecated and that the written
  order of the reference list is now authoritative
- **AND** the resolved chains (`llm_caller.defaults`,
  `llm_caller.steps.<step>`) preserve their list order regardless of
  any `priority` values

#### Scenario: Codex agent registered and referenced in caller chains
- **GIVEN** an `agents` entry declaring `type: codex` (with its own
  `cmd` such as `codex` or `codex -m <model>`, and an optional
  `priority`), e.g.
  `agents: { gpt: { type: codex, cmd: codex, priority: 5 } }`
- **WHEN** the entry is referenced by name from `llm_caller.defaults`
  or `llm_caller.steps.<step>`
- **THEN** config loading/validation accepts the `codex` type value and
  the reference resolves to the registry entry
- **AND** `LLMCaller._create_runner` constructs a `CodexRunner` for that
  agent, so it participates in the default chain, per-step override, and
  agent rotation exactly like a `claude-code` agent

#### Scenario: String shorthand expands to AgentDef with defaults
- **GIVEN** `agents.primary: "claude"` in se3.yaml (a bare string value
  rather than a dict)
- **WHEN** the framework loads the agent registry
- **THEN** the entry is parsed into an `AgentDef` whose `cmd` is
  `"claude"`, `type` is `"claude-code"`, and `priority` is `0`
- **AND** the name `primary` is taken from the dict key
- **AND** the entry can be referenced by name from `llm_caller.defaults`,
  `llm_caller.steps.<step>`, and `confirmation.steps.<step>.reviewer`
  exactly like a full-dict entry

#### Scenario: Mixed shorthand and full-dict forms coexist
- **GIVEN** `agents` contains both a string-shorthand entry and a
  full-dict entry (e.g. `primary: claude` and
  `opus: { type: claude-code, cmd: claude-opus, priority: 20 }`)
- **WHEN** the framework loads the agent registry
- **THEN** both entries are registered with their respective fields
- **AND** each entry is parsed independently of the other

#### Scenario: Top-level list form is ignored with warning
- **WHEN** `agents` is a list at the top level
- **THEN** the framework emits a warning
- **AND** the registry is built without those entries
- **AND** the default caller chain falls back to the built-in `[claude]`

#### Scenario: Legacy claude_commands auto-migrates
- **GIVEN** a config sets `claude_commands` but not `agents`
- **WHEN** the framework loads the registry
- **THEN** each `claude_commands` entry is registered in the top-level
  `agents` dict under a name slugified from `cmd` (with `_2`, `_3`
  suffixes on collision)
- **AND** the original order is replicated in `llm_caller.defaults`
- **AND** a `DeprecationWarning` is emitted showing the equivalent
  new-schema YAML

#### Scenario: claude_commands ignored when agents is present
- **GIVEN** a config sets both top-level `agents` (dict) and
  `claude_commands`
- **WHEN** the framework loads the registry
- **THEN** `claude_commands` is ignored
- **AND** a warning is logged indicating the alias was discarded

#### Scenario: Global + project entry-level merge
- **GIVEN** global config declares `agents.primary` and `agents.backup`
- **AND** project config declares `agents.primary` and `agents.opus`
- **THEN** the merged registry contains `primary` (project version),
  `backup` (from global), and `opus` (from project)

#### Scenario: Unknown agent name fails fast
- **GIVEN** any reference location (`llm_caller.defaults`,
  `llm_caller.steps.<step>`, or `confirmation.steps.<step>.reviewer`)
  names an agent absent from the registry
- **THEN** the framework raises a configuration error at startup
- **AND** the error message includes the reference location and a
  sorted list of registered agent names

### Requirement: LLM Caller Configuration

The system SHALL drive Claude CLI invocation through the `llm_caller`
section, which references the agent registry by name. `llm_caller`
contains two keys:

- `defaults`: a list of agent names forming the default caller chain
- `steps.<step_name>`: a list of agent names forming a hard per-step
  override chain

Both lists accept **only** registered agent names — anonymous / inline
`{cmd: ...}` entries are rejected. Entries are invoked in the list's
**written order**, which is the sole authority for chain order and for
agent rotation on failure. The registry's `priority` field is
deprecated and plays no role in ordering (see the Agent Registry
requirement); the global priority-based reordering that previously
re-sorted chains has been removed.

**Default chain (`defaults`):**

- When `llm_caller.defaults` is declared, the framework uses it as the
  default caller chain for any step without a per-step override.
- When absent, the chain falls back to built-in `[claude]` (or to the
  chain produced by legacy `claude_commands` auto-migration).
- Global + project merge: **whole replace** — if project config sets
  `llm_caller.defaults`, it fully replaces the global `defaults`.

**Per-step hard override (`steps.<step>`):**

- A step with a declaration under `llm_caller.steps.<step>` uses
  **only** that list as its chain. The `defaults` chain is NOT
  appended as a fallback — users who want a default tail MUST list
  those agents explicitly in the step's override.
- Agent rotation on any LLM call failure (usage-limit, timeout, or
  any other error such as network / certificate / unknown failures)
  happens strictly within the step's override list. When exhausted,
  the call fails rather than silently falling back to `defaults`.
  Each rotation consumes one of the `max_retries` attempt slots; the
  total number of attempts per call remains bounded by `max_retries`
  regardless of chain length. When the chain is exhausted before
  `max_retries` is reached, any remaining attempts run on the last
  agent in the chain (tail-on-last-agent fallback).
- Error classification (usage-limit / timeout / other) is retained
  for diagnostic logging only — it labels the failure in log output
  and does NOT gate whether rotation happens. All three categories
  trigger rotation identically.
- When `agents` is explicitly passed to the `LLMCaller` constructor
  (e.g. by internal helpers such as the JSON extractor), that argument
  takes the highest priority and bypasses both the per-step override
  and `defaults`.
- Steps with no declaration under `llm_caller.steps` continue to use
  `defaults`.
- Global + project merge: **whole replace per step** — if project
  config sets `llm_caller.steps.<step>`, it fully replaces the global
  declaration for that step; other step overrides remain from global.

**Per-pass chains for `self_check` (nested list form):**

`llm_caller.steps.self_check` — and **only** `self_check` — additionally
accepts a **nested list** of agent-name lists, expressing a distinct
agent chain per self_check pass within a single fix-loop round:

```yaml
llm_caller:
  steps:
    self_check: [[agentA], [agentB, agentC]]   # pass 1 → [agentA]; pass 2 → [agentB, agentC]
```

- **Flat form (back-compatible):** a plain `list[str]` such as
  `self_check: [agentA, agentB]` continues to mean a single chain used
  by **every** pass; the number of passes is governed solely by
  `workflow.self_check_passes_required` (default 1). This is the
  existing schema and behavior, unchanged.
- **Nested form:** a `list[list[str]]` assigns chain *i* (1-based) to
  self_check pass *i*. The per-pass chain and rotation within it still
  follow written order. See the *Workflow Configuration* requirement
  for how the nested form interacts with
  `workflow.self_check_passes_required` to derive the effective pass
  count, reuse the last chain when the count exceeds the number of
  chains, and warn when the count is smaller than the number of chains.
- **Mixed form is a configuration error:** a list that contains *both*
  bare strings and sub-lists (e.g. `[agentA, [agentB]]`) is invalid.
  The loader logs a WARNING and falls back **entirely** to
  `llm_caller.defaults` for `self_check` (it does not partially parse
  the valid entries).
- **Scope:** the nested form is recognized for `self_check` only. Every
  other `llm_caller.steps.<step>` remains a flat `list[str]`; a nested
  list under any other step is not a supported schema.

**Fail-fast on unknown names:**

Any name in `llm_caller.defaults` or `llm_caller.steps.<step>` (including
any name inside a `self_check` per-pass sub-list) that is absent from
the agent registry is a startup-time error (see the Agent Registry
requirement for the error-message format).

**Typo detection on `llm_caller.steps` keys:**

When `llm_caller.steps` contains a key that is not a valid `StepType`
value (i.e. it does not match any executable step name), the framework
SHALL log a WARNING identifying the source and the offending key(s),
noting that the declaration is likely a typo and that those entries
will be ignored. This warning is idempotent per
`(source, sorted-unknown-keys)` combination, so it is emitted at most
once per unique typo set even when the agent-resolution path is invoked
many times per flow. Unknown step keys are NOT a startup-time error —
they are dropped silently after the warning and the rest of
`llm_caller.steps` is parsed normally.

**Example configuration:**
```yaml
agents:
  primary: { type: claude-code, cmd: claude,      priority: 10 }
  backup:  { type: claude-code, cmd: claude-dev,  priority: 5 }
  opus:    { type: claude-code, cmd: claude-opus, priority: 20 }

llm_caller:
  defaults: [primary, backup]
  steps:
    # Expensive step — opus first, then primary as tail
    implement: [opus, primary]
    # Cheap step — primary only; backup NOT appended
    summarize: [primary]
```

#### Scenario: Default chain from llm_caller.defaults
- **GIVEN** `llm_caller.defaults: [primary, backup]` and a registry
  containing both names
- **WHEN** `load_agents()` builds the default chain
- **THEN** the returned list contains the agent dicts for `primary`
  followed by `backup`

#### Scenario: No per-step override declared
- **WHEN** `llm_caller.steps` has no entry for the current step
- **THEN** the step uses `llm_caller.defaults` (or the built-in fallback)

#### Scenario: Per-step override used
- **GIVEN** `llm_caller.steps.implement: [opus, primary]`
- **WHEN** the implement step constructs its LLMCaller
- **THEN** the caller uses `[opus, primary]` as its complete chain
- **AND** `llm_caller.defaults` is NOT appended as a fallback

#### Scenario: Exhaustion does not fall back to default chain
- **GIVEN** `llm_caller.steps.analyze: [A, B]`
- **WHEN** both A and B fail with any error (infrastructure or otherwise)
- **THEN** the LLM call fails rather than rotating to `defaults`

#### Scenario: Rotation triggers on any error category
- **GIVEN** an LLMCaller with chain `[A, B]` and `max_retries >= 2`
- **WHEN** agent A fails with an unknown / non-classified error
  (e.g., `UNKNOWN_CERTIFICATE_VERIFICATION_ERROR` or any network
  error that is neither usage-limit nor timeout)
- **THEN** `_rotate_agent` is invoked and the next attempt runs on
  agent B — the caller does NOT retry on agent A
- **AND** the log records the failure with an `other` category label
  (alongside the existing `usage_limit` / `timeout` labels)

#### Scenario: max_retries bounds total attempts across rotations
- **GIVEN** an LLMCaller with chain `[A, B, C, D, E]` and
  `max_retries = 3`
- **WHEN** every invocation fails
- **THEN** the call performs exactly 3 attempts in total (one per
  agent in chain order, stopping when `max_retries` is reached)
- **AND** agents D and E are NOT tried in this call

#### Scenario: Tail-on-last-agent when chain shorter than max_retries
- **GIVEN** an LLMCaller with chain `[A, B]` and `max_retries = 3`
- **WHEN** every invocation fails
- **THEN** the call performs 3 attempts total: agent A, agent B,
  then agent B again for the final attempt (rotation exhaustion
  falls through and the remaining attempt runs on the last agent)

#### Scenario: Explicit agents argument wins over per-step override
- **GIVEN** `llm_caller.steps.analyze` declares an override list
- **WHEN** LLMCaller is constructed with an explicit `agents=[...]`
- **THEN** the explicit argument is used and both the per-step override
  and `defaults` are bypassed

#### Scenario: Other steps unaffected by a single override
- **GIVEN** `llm_caller.steps.implement` is declared but `plan` is not
- **WHEN** the plan step runs
- **THEN** it uses `llm_caller.defaults`, unaffected by the implement
  override

#### Scenario: Unknown name in defaults fails fast
- **GIVEN** `llm_caller.defaults: [primary, ghost]` and `ghost` is not
  in the agent registry
- **THEN** the framework raises a configuration error at startup
- **AND** the error message names the reference location
  (`llm_caller.defaults`) and lists registered agent names

#### Scenario: Unknown name in per-step override fails fast
- **GIVEN** `llm_caller.steps.implement: [nonexistent]`
- **THEN** the framework raises a configuration error at startup
- **AND** the error message names the reference location
  (`llm_caller.steps.implement`) and lists registered agent names

#### Scenario: Unknown step key in llm_caller.steps warns once and is ignored
- **GIVEN** `llm_caller.steps` declares a key that is not a valid
  `StepType` value (e.g. `implemnt` — a typo of `implement`)
- **WHEN** the framework resolves per-step agents
- **THEN** a WARNING is logged identifying the source and the unknown
  key(s), noting that the declaration is likely a typo and will be
  ignored
- **AND** the warning is emitted at most once per
  `(source, sorted-unknown-keys)` combination even if agent resolution
  runs many times during the flow
- **AND** no startup-time configuration error is raised — the unknown
  entries are silently dropped while valid `llm_caller.steps` entries
  continue to be parsed normally

#### Scenario: Global defaults replaced by project
- **GIVEN** global config declares `llm_caller.defaults: [g1, g2]`
- **AND** project config declares `llm_caller.defaults: [p1]`
- **WHEN** the framework builds the default chain
- **THEN** the chain is exactly `[p1]` — the global list is not merged

#### Scenario: Project overrides global per-step declaration
- **GIVEN** global config declares `llm_caller.steps.implement: [a, b]`
- **AND** project config declares `llm_caller.steps.implement: [c]`
- **WHEN** the implement step runs in the project
- **THEN** the chain is exactly `[c]` — the global list is not merged

#### Scenario: Nested self_check chains map per pass
- **GIVEN** `llm_caller.steps.self_check: [[agentA], [agentB, agentC]]`
  and all three names are registered
- **WHEN** the self_check step resolves the chain for pass 1
- **THEN** the chain is `[agentA]`
- **WHEN** the self_check step resolves the chain for pass 2
- **THEN** the chain is `[agentB, agentC]`, rotated in written order

#### Scenario: Flat self_check list applies to all passes
- **GIVEN** `llm_caller.steps.self_check: [agentA, agentB]` (flat form)
- **WHEN** any self_check pass resolves its chain
- **THEN** every pass uses `[agentA, agentB]`, and the pass count is
  governed solely by `workflow.self_check_passes_required`

#### Scenario: Mixed self_check form warns and falls back to defaults
- **GIVEN** `llm_caller.steps.self_check: [agentA, [agentB]]` (a list
  mixing a bare string and a sub-list)
- **WHEN** the framework resolves the self_check chain
- **THEN** a WARNING is logged identifying the malformed mixed form
- **AND** the self_check step falls back entirely to
  `llm_caller.defaults` rather than partially parsing the entry

#### Scenario: Nested form rejected for non-self_check steps
- **GIVEN** a nested list under a step other than `self_check`, e.g.
  `llm_caller.steps.implement: [[a], [b]]`
- **THEN** the nested form is not a supported schema for that step —
  only `self_check` recognizes per-pass nested chains

### Requirement: Step Sequence Configuration

The system SHALL support appending optional step types to the default step sequence via a top-level `steps` section in `se3.yaml` (or `se3.local.yaml`).

**Purpose:** Some step types (for example `summarize`) are not part of the default executed step sequence but can be opted into per project. The `steps.append` list lets a project add those steps to the end of the sequence without otherwise changing flow ordering.

**Schema:**

```yaml
steps:
  append:
    - summarize
```

**Semantics:**

- `steps.append` is a list of step-type names. Each name is validated against the framework's `StepType` enum at load time; unknown names are silently ignored (no startup error).
- Each valid name is appended to the **end** of the existing step sequence, in the order declared.
- Names already present in the existing sequence are skipped (no duplication); the existing position of the step is preserved.
- When the `steps` key is absent, is not a dict, or `steps.append` is absent or not a list, the configuration loader returns an empty append list and the step sequence is unchanged.
- The configuration honors the same project-config lookup rules as the rest of `se3.yaml` (including `se3.local.yaml` precedence and the worktree four-tier lookup).

**Example configuration:**
```yaml
steps:
  append:
    - summarize
```

#### Scenario: Default sequence when steps section absent
- **WHEN** no `steps` section exists in se3.yaml
- **THEN** the framework runs the default step sequence with no additions

#### Scenario: Append a valid step type
- **GIVEN** `steps.append: [summarize]` in se3.yaml
- **AND** `summarize` is a valid `StepType` not already present in the default sequence
- **WHEN** the framework builds the step sequence for a flow
- **THEN** `summarize` is appended to the end of the sequence

#### Scenario: Duplicate name in append is ignored
- **GIVEN** `steps.append: [<step>]` where `<step>` is already present in the default sequence
- **WHEN** the framework builds the step sequence
- **THEN** the existing occurrence is preserved
- **AND** no second copy is appended

#### Scenario: Unknown step name silently ignored
- **GIVEN** `steps.append: [not_a_real_step]`
- **WHEN** the framework builds the step sequence
- **THEN** the unknown name is dropped without raising
- **AND** other valid names in the same list are still appended

#### Scenario: Malformed steps section is treated as empty
- **GIVEN** `steps` is not a dict, or `steps.append` is not a list
- **WHEN** the framework loads the step configuration
- **THEN** the append list is treated as empty
- **AND** the step sequence is unchanged

### Requirement: Spec Loading Configuration

The system SHALL support per-step spec loading mode configuration via the `spec_loading` section in `se3.yaml`.

**Purpose:** Control whether a downstream step receives only the selected spec items (saving context window) or the full spec text (needed for cross-item consistency checks and naming collision detection).

**Schema:**
```yaml
spec_loading:
  steps:
    update_spec: full_spec    # see all specs for naming checks
    verify_spec: items        # default, only selected items
```

**Mode semantics:**
- `"items"`: Each involved spec contributes its header (Purpose, Definitions, Constraints) plus only the Requirements that were selected by the analyze step. Base spec is always included in full. This is the default for all steps except `update_spec`.
- `"full_spec"`: Each involved spec contributes its complete text. Used when the step needs to see all Requirements in a spec (e.g., `update_spec` naming a new spec must check for collisions across all existing spec names).

**Fail-fast on empty selected_items in full_spec mode:** When a step builds inputs in `full_spec` mode and `selected_items` is empty, the spec loader SHALL raise `ValueError` rather than silently degrading to a base-only spec load. An empty `selected_items` in this mode indicates the analyze step failed to pick any relevant items, and proceeding would mask that failure by producing context that *looks* valid but omits all targeted requirements.

**Default behavior:**
- Steps NOT listed in `spec_loading.steps` use the built-in default: `"items"` for most steps, `"full_spec"` for `update_spec`.
- The default cannot be changed globally — each step must be explicitly configured if the built-in default is unsuitable.

#### Scenario: Default items mode for plan step
- **GIVEN** `se3.yaml` has no `spec_loading` section
- **WHEN** the `plan` step builds its inputs
- **THEN** `spec_content` contains only the base spec full text + selected spec headers + selected items
- **AND** unselected Requirements are omitted from context

#### Scenario: YAML override switches verify_spec to full_spec
- **GIVEN** `se3.yaml` contains:
  ```yaml
  spec_loading:
    steps:
      verify_spec: full_spec
  ```
- **WHEN** the `verify_spec` step builds its inputs
- **THEN** `spec_content` contains the full text of all involved specs
- **AND** the LLM can perform cross-Requirement consistency checks

#### Scenario: update_spec defaults to full_spec without explicit config
- **GIVEN** `se3.yaml` has no `spec_loading` section
- **WHEN** the `update_spec` step builds its inputs
- **THEN** `spec_content` contains the full text of all involved specs
- **AND** the LLM can see all existing spec names to avoid naming collisions when creating a new spec

#### Scenario: full_spec mode with empty selected_items fails fast
- **GIVEN** a step is configured (or defaults) to `full_spec` loading mode
- **AND** `selected_items` from the upstream analyze step is empty
- **WHEN** the spec loader assembles `spec_content`
- **THEN** the loader raises `ValueError` with a message identifying the empty-`selected_items` condition
- **AND** the error surfaces upstream rather than silently producing a base-only or single-spec context

### Requirement: Claude Subprocess Setting Sources Isolation

When SE3 spawns Claude CLI subprocesses (for any step that drives an
LLM via `claude` — `plan`, `implement`, `verify_spec`, `update_spec`,
etc.), it sets the subprocess `cwd` to the target project root so that
the LLM can read and edit project files. The Claude CLI, by default,
also loads the project-level settings file `<cwd>/.claude/settings.json`
during that invocation. When a downstream project uses its project-root
`.claude/settings.json` to deny tools as a guardrail for its own
sub-LLMs (for example, denying `Read`, `Write`, `Edit`, `Bash`, `Glob`,
`Grep` for verifier-style sub-agents), those `permissions.deny` entries
also apply to SE3's own worker subprocess and prevent the implement
step from reading or writing files — surfacing as runtime errors like
`Read exists but is not enabled in this context`. The
`--dangerously-skip-permissions` flag does NOT override `permissions.deny`;
it only suppresses interactive permission prompts.

To structurally insulate SE3 worker subprocesses from this hazard, the
framework SHALL by default restrict Claude's settings-source loading
to user-level settings only.

**Default behavior:**

- Every Claude CLI subprocess spawned by SE3 SHALL be invoked with
  `--setting-sources user` immediately following
  `--dangerously-skip-permissions` in its argv.
- The `--setting-sources` argument value is a comma-separated list of
  source identifiers (`user`/`project`/`local`); when the configured
  list contains a single element the value is just that element with no
  trailing comma.
- The default list is `["user"]`, so the default argv injects
  `--setting-sources user`. SE3 worker subprocesses therefore consult
  `~/.claude/settings.json` only and are immune to any `permissions.deny`
  entries in the target project's `.claude/settings.json` or
  `.claude/settings.local.json`.

**Configuration:**

- The behavior is controlled by `claude_subprocess.setting_sources`
  in `se3.yaml` (or `se3.local.yaml`).
- Schema: `list[str]`. Allowed element values are `"user"`, `"project"`,
  and `"local"` — corresponding to the three settings tiers recognised
  by Claude CLI. The default value when the key is absent is
  `["user"]`.
- Validation (startup fail-fast — see Configuration File Format):
  - The value MUST be a list. A non-list value (string, dict, number,
    etc.) SHALL raise a configuration error at load time.
  - The list MUST be non-empty. An empty list SHALL raise a
    configuration error at load time.
  - Each element MUST be a string in the allowed set
    `{"user", "project", "local"}`. Any other value SHALL raise a
    configuration error at load time, and the error message SHALL list
    the allowed values.
- When the user explicitly opts back into project/local sources (for
  example `setting_sources: [user, project]`), the framework SHALL
  pass `--setting-sources user,project` to the Claude CLI subprocess.
  In this mode the operator accepts that downstream project settings
  may again constrain SE3 worker tools.

**Guidance for downstream project authors:**

Downstream projects that need to constrain sub-LLM tool access — for
example verifier-style sub-agents inside an arapa-like workflow —
SHALL NOT do so by adding broad `permissions.deny` entries to the
project-root `.claude/settings.json`, because that file is also read by
any other Claude CLI invocation rooted in the same project, including
SE3's worker subprocesses, and will silently break implement-step
file access. Recommended alternatives:

- Pass `--disallowed-tools <names>` directly to the sub-LLM
  invocation so the constraint scopes to that one subprocess.
- Place sub-LLM-specific guardrails in a dedicated settings file and
  pass it via `--settings <path>` (or via the sub-LLM's own
  `--setting-sources` flag) so that other Claude CLI invocations in
  the same `cwd` are unaffected.

#### Scenario: Default argv loads only user-level settings
- **GIVEN** the project has no `claude_subprocess.setting_sources`
  configured in `se3.yaml`
- **WHEN** SE3 spawns any Claude CLI subprocess (e.g. during the
  `implement` step)
- **THEN** the subprocess argv contains
  `--dangerously-skip-permissions --setting-sources user`
- **AND** the target project's `.claude/settings.json` is not loaded
  by that subprocess, so its `permissions.deny` entries do not apply

#### Scenario: Explicit configuration opts back into project settings
- **GIVEN** `se3.yaml` declares
  ```yaml
  claude_subprocess:
    setting_sources: [user, project]
  ```
- **WHEN** SE3 spawns any Claude CLI subprocess
- **THEN** the subprocess argv contains
  `--dangerously-skip-permissions --setting-sources user,project`
- **AND** the operator has explicitly accepted that the target
  project's `.claude/settings.json` may again constrain SE3 worker tools

#### Scenario: Target project .claude/settings.json does not affect default argv
- **GIVEN** the target project root contains a
  `.claude/settings.json` that denies `Read`, `Write`, `Edit`, `Bash`,
  `Glob`, and `Grep`
- **AND** no `claude_subprocess.setting_sources` is configured in
  `se3.yaml`
- **WHEN** SE3 spawns a Claude CLI subprocess with `cwd` set to that
  project root
- **THEN** the subprocess argv still contains
  `--setting-sources user`
- **AND** the subprocess is not affected by the project's
  `permissions.deny` entries

#### Scenario: Empty setting_sources list fails fast at startup
- **GIVEN** `se3.yaml` declares `claude_subprocess.setting_sources: []`
- **WHEN** SE3 loads project configuration
- **THEN** the framework raises a configuration error before any
  Claude CLI subprocess is spawned
- **AND** the error identifies `claude_subprocess.setting_sources`
  and states that the list must be non-empty

#### Scenario: Invalid element in setting_sources fails fast
- **GIVEN** `se3.yaml` declares
  `claude_subprocess.setting_sources: [user, system]`
- **WHEN** SE3 loads project configuration
- **THEN** the framework raises a configuration error before any
  Claude CLI subprocess is spawned
- **AND** the error message lists the allowed values
  (`user`, `project`, `local`)

### Requirement: Daemon and Server Configuration

The SE3 daemon (`se3 daemon`) and the central server (`se3-server`) each
expose a set of runtime configuration parameters. The daemon parameters
are sourced from CLI flags / environment variables and built-in
dataclass defaults. The central server's identity / auth / persistence
parameters are read from the `server:` section of `se3.yaml` (loaded via
`load_server_config` into the `ServerConfig` dataclass with the same
global→project top-level-key override as the rest of the config), with
`se3-server` CLI flags overriding a subset; they are documented here so
the configuration surface of the daemon/server feature has a single
registered home.

**Daemon parameters** (`DaemonConfig` in `src/se3/daemon/daemon.py`):

- `daemon.poll_interval` — Seconds between the daemon aggregator polls
  of `se3/state`, `se3/logs`, `se3/calls`, and `se3/issues`
  (default: `2.0`).
- `daemon.server_url` — The central-server URL the daemon dials out to.
  Supplied via `se3 daemon start --server-url <url>`; when unset
  (default: `null`/`None`) the daemon runs purely locally and does not
  open an outbound connection. When the supplied URL omits an explicit
  port, the daemon client normalizes it by completing the port with a
  *scheme-aware* default: a `ws://` URL (and `http://` normalized to
  `ws://`) is completed to the plaintext `DEFAULT_SERVER_PORT` (`8080`,
  the `se3-server --port` default), while a `wss://` URL (and `https://`
  normalized to `wss://`) is completed to `DEFAULT_SERVER_TLS_PORT`
  (`443`), because a TLS connection terminates at the reverse proxy's
  HTTPS port rather than at se3-server's plaintext default. An explicit
  port in the URL is preserved as given, and so are a custom path (e.g.
  an already-present `/ws`) and an IPv6 literal host (`[::1]`). This
  prevents a bare `ws://host` from silently falling back to the
  WebSocket-standard port 80 while the server listens on the default
  port, and prevents a bare `wss://host` (behind a reverse proxy) from
  being dialed on `8080` instead of `443` — both of which left the
  central server with no machine registration.
- `daemon.daemon_key` — The secret daemon credential the daemon presents
  in its HELLO so the multi-tenant server can resolve it to an owner
  (`key → owner_id`) and bind the reporting machine to that trust domain
  (see the `base` spec's *Server Identity, Authentication and
  Persistence* requirement). It is NOT read from `se3.yaml`; it is
  supplied via `se3 daemon start --daemon-key <key>` or the
  `SE3_DAEMON_KEY` environment variable (the flag takes precedence),
  defaults to `null`/empty (keyless = local / legacy single-tenant
  operation, no owner binding), is propagated to spawned `se3 run`
  subprocesses via the `SE3_DAEMON_KEY` child-environment variable, and
  is a secret that is never written to the daemon status file or any
  log.

**Server identity / auth / persistence parameters** (`ServerConfig` /
`AuthConfig` in `src/se3/config.py`, under the `server:` section):

- `server.db_path` — Filesystem path of the embedded sqlite store backing
  owners / identity-bindings / local credentials / daemon-key hashes /
  break-glass token hashes (default: `~/.se3/server.db`). The
  `se3-server --db-path <path>` flag overrides it for a single launch
  (an explicit `--db-path` wins over the configured value).
- `server.auth.providers` — Ordered list of auth provider type names the
  registry assembles into the provider chain (default: `["local"]`).
  Recognized names are `local`, `oidc`, and `proxy_header`; unknown or
  non-string entries are warned-and-dropped, and an empty / fully-invalid
  list falls back to the default `["local"]`. If the assembled chain ends
  up with no usable provider, the server fails closed (see the `base`
  spec) rather than serving anonymously.
- `server.auth.session.*` — UI session cookie security attributes for the
  local provider:
  `cookie_name` (default: `se3_session`),
  `cookie_secure` (default: `true`),
  `cookie_httponly` (default: `true`),
  `cookie_samesite` (default: `lax`; an invalid value warns and falls
  back to `lax`), and
  `max_age_seconds` (default: `86400`, the 24-hour session lifetime).
- `server.auth.local.*` — Brute-force defense for the local provider:
  `max_failed_attempts` consecutive failures lock an account for
  `lockout_seconds`, and independently at most `ratelimit_max_attempts`
  login attempts are accepted per `ratelimit_window_seconds` window
  (defaults: `5` / `300` / `60` / `10`; each coerced to a positive
  integer).
- `server.auth.oidc.*` — Disabled-by-default OIDC social-login seam
  (`enabled` default `false`; `issuer` / `client_id` / `client_secret` /
  `redirect_url` optional strings; `scopes` default
  `["openid", "email", "profile"]`). v1 ships only the config seam; the
  provider is not implemented, and the fields are inert while `enabled`
  is false.
- `server.auth.proxy_header.*` — Disabled-by-default reverse-proxy
  trusted-identity-header seam (`enabled` default `false`; `trust_proxy`
  default `false`; `header` default `X-Forwarded-Email`). When enabled,
  the reverse proxy MUST strip any client-supplied copy of `header` and
  the server MUST NOT be reachable bypassing the proxy, otherwise the
  injected identity is forgeable (an authz hole). v1 ships only the seam.

**Server parameters** (`se3-server` entry point in
`src/se3/server/app.py`):

- `server.host` — Bind host for the central server
  (default: `127.0.0.1`). Supplied via `se3-server --host <host>`.
- `server.port` — Bind port for the central server
  (default: `DEFAULT_SERVER_PORT`, `8080`). Supplied via
  `se3-server --port <port>`.
- `--version` / `-v` — Print the server's version string and exit
  with status `0`. The flag SHALL be intercepted in
  `src/se3/server/__init__.py:main()` **before** attempting to import
  the `[server]` optional extras (FastAPI / uvicorn), so a core-only
  install (`pip install se3` without the `[server]` extra) can still
  query the version without triggering the missing-extra hint. The
  output line is `se3-server version {__version__}`, mirroring the
  main `se3 version {__version__}` format produced by the core CLI;
  the version value is read from the single
  `se3.__version__` source (sourced from `pyproject.toml`).

**Shared default ports.** The default server ports are defined once in
`src/se3/daemon/protocol.py` — the single source of truth for the
daemon↔server protocol — so the value is not duplicated as a magic
number and the two sides cannot drift apart. `DEFAULT_SERVER_PORT`
(value `8080`) is the plaintext / `ws://` default and is referenced by
both the `se3-server` `--port` default and the daemon client's URL
normalization. `DEFAULT_SERVER_TLS_PORT` (value `443`) is the
scheme-aware `wss://` default referenced only by the daemon client's
URL normalization; introducing it does not change the value or meaning
of `DEFAULT_SERVER_PORT` and therefore does not alter `se3-server`'s
plaintext default.

#### Scenario: Daemon runs locally without a server URL
- **WHEN** the daemon is started without `--server-url`
- **THEN** `daemon.server_url` is `null` and the daemon does not open an
  outbound connection to a central server
- **AND** the daemon still supervises and aggregates local flows,
  polling at the `daemon.poll_interval` cadence

#### Scenario: Server binds to configured host and port
- **WHEN** `se3-server` is started without `--host` / `--port`
- **THEN** the server binds to `server.host` `127.0.0.1` and
  `server.port` `DEFAULT_SERVER_PORT` (`8080`)
- **AND** supplying `--host` / `--port` overrides those defaults

#### Scenario: Server URL without an explicit port is completed scheme-aware
- **WHEN** the daemon is started with `--server-url ws://host` (no port)
- **THEN** the daemon client normalizes the URL by completing the port
  to `DEFAULT_SERVER_PORT` (`8080`), matching the server's plaintext
  default
- **AND** when the daemon is started with `--server-url wss://host` (no
  port, e.g. behind a TLS reverse proxy), the port is completed to
  `DEFAULT_SERVER_TLS_PORT` (`443`) rather than to `8080`
- **AND** an `http://` URL is normalized to `ws://` and completed to
  `8080`, while an `https://` URL is normalized to `wss://` and
  completed to `443`
- **AND** when the URL already carries an explicit port, that port is
  preserved unchanged regardless of scheme
- **AND** a custom path already present in the URL (e.g. `/ws`) and an
  IPv6 literal host (`[::1]`) are preserved unchanged

#### Scenario: `se3-server --version` prints the version and exits
- **WHEN** the user runs `se3-server --version` (or `se3-server -v`)
- **THEN** the program prints a single line
  `se3-server version {__version__}` to stdout and exits with status 0
- **AND** the flag is honored even on a core-only install that has not
  installed the `[server]` optional extras (FastAPI / uvicorn), because
  `--version` is intercepted before any FastAPI / uvicorn import
- **AND** the printed version string equals `se3.__version__`, which is
  the single canonical version source (sourced from `pyproject.toml`)
  and is the same value reported by the core `se3 version` command

#### Scenario: Default server config has no se3.yaml server section
- **WHEN** `se3.yaml` has no `server:` section
- **THEN** `ServerConfig` yields `db_path = ~/.se3/server.db` and
  `auth.providers = ["local"]` (the built-in local provider), with the
  session cookie defaulting to `Secure` + `HttpOnly` + `SameSite=lax`
  and the local lockout / rate-limit defaults (`5` / `300` / `60` / `10`)

#### Scenario: Explicit --db-path overrides server.db_path
- **GIVEN** `server.db_path: /var/lib/se3/server.db` in `se3.yaml`
- **WHEN** the user runs `se3-server --db-path /tmp/test.db`
- **THEN** the server opens the sqlite store at `/tmp/test.db`
- **AND** the configured `server.db_path` is unused for that launch

#### Scenario: Disabling the only provider triggers fail-closed
- **GIVEN** `server.auth.providers` resolves to no usable provider (e.g.
  the built-in `local` provider is explicitly disabled and no other
  provider is enabled)
- **WHEN** the server application is assembled from this config
- **THEN** assembly fails closed (`AuthNotConfigured`) and the server
  refuses to serve rather than reverting to the identity-unaware bare mode

#### Scenario: Daemon key is sourced from flag or environment, never se3.yaml
- **WHEN** the daemon is started with `se3 daemon start --daemon-key K`
  (or with `SE3_DAEMON_KEY=K` in the environment)
- **THEN** `DaemonConfig.daemon_key` is `K` and the daemon presents it in
  HELLO so the server can resolve `key → owner_id`
- **AND** the flag takes precedence over the environment variable when
  both are present
- **AND** the key is not read from `se3.yaml` and never appears in the
  daemon status file or log
