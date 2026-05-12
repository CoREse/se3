<!-- spec-format: v1 -->
# SE3 Version Management Specification

## Purpose

Define version management standards for SE3 projects, including Semantic Versioning adoption, version file detection rules, automatic version bumping mechanisms, and documentation update conventions.

## Requirements

### Requirement: Semantic Versioning 2.0.0 Adoption

SE3 SHALL adopt Semantic Versioning 2.0.0 as the core version control standard.

**Version Format:**
```
version ::= major "." minor "." patch ("-" pre-release)? ("+" build-metadata)?
major ::= numeric identifier
minor ::= numeric identifier  
patch ::= numeric identifier
pre-release ::= dot-separated identifiers
build-metadata ::= dot-separated identifiers
identifier ::= alphanumeric | digits
```

**Version Increment Rules:**
- **MAJOR**: Increment when making incompatible API changes
- **MINOR**: Increment when adding backward-compatible functionality  
- **PATCH**: Increment when making backward-compatible bug fixes

**Pre-release Versions:**
- Use `-` suffix for pre-releases: `1.0.0-alpha`, `1.0.0-alpha.1`, `1.0.0-0.3.7`
- Pre-release versions have lower precedence than normal versions

**Build Metadata:**
- Use `+` suffix for build info: `1.0.0+build.1`, `1.0.0+20130313144700`
- Build metadata is ignored in version precedence

**Reference:** https://semver.org/

#### Scenario: Standard Version Release
- **GIVEN** current version is `1.2.3`
- **WHEN** releasing backward-compatible new feature
- **THEN** new version is `1.3.0`

#### Scenario: Bug Fix Release
- **GIVEN** current version is `1.2.3`
- **WHEN** fixing a bug and releasing
- **THEN** new version is `1.2.4`

#### Scenario: Breaking Change Release
- **GIVEN** current version is `1.2.3`
- **WHEN** introducing incompatible API changes
- **THEN** new version is `2.0.0`

### Requirement: Single Version Source

SE3 projects SHALL maintain a single authoritative source for the version number.

**Rules:**
- There MUST be exactly one canonical location where the version is defined
- All other references to the version (badges, documentation, generated files) MUST derive from this single source
- The version script interface (`se3/scripts/version.py`) serves as the unified access point to this source
- Projects MUST NOT maintain independent version numbers in multiple files (e.g., both `pyproject.toml` and `__init__.py` with separate version values)

**Rationale:** Multiple version sources lead to version drift, where different parts of the project report different versions. A single source of truth eliminates this class of errors.

#### Scenario: Single Source Enforcement
- **GIVEN** a project with version defined in `pyproject.toml`
- **WHEN** the version script reads and writes versions
- **THEN** it operates exclusively on `pyproject.toml` as the single source
- **AND** any other version references are derived, not independently maintained

### Requirement: Version Script Interface

SE3 SHALL support a script-based interface for version management, providing a universal contract that works across any project type.

**Script Contract:**

The version script (`se3/scripts/version.py` or `.sh` by default) MUST support three subcommands:

| Command | Input | Output (stdout) | Description |
|---------|-------|-----------------|-------------|
| `get` | (none) | Current version (e.g., `1.2.3`) | Read current version |
| `bump --type <type>` | `major`, `minor`, or `patch` | New version after bump | Increment and write version |
| `set --version <ver>` | Version string | The set version | Write explicit version |

**Output Contract:**
- Success: print version string to stdout (clean semver, e.g., `1.2.3`), exit code 0
- Failure: print error message to stderr, exit code 1

**Script Discovery Priority:**
1. Configured `version.script_path` in `se3.yaml`
2. `se3/scripts/version.py` (default)
3. `se3/scripts/version.sh` (default)
4. Fall back to built-in handler detection (pyproject.toml, package.json, etc.)

**Auto-Generation:**
When no version script exists and `version.auto_generate_script` is `true` (default), SE3 SHALL:
1. Scan project structure to identify the version file type
2. Use LLM to generate a project-specific script implementation
3. Write the script to the default path (`se3/scripts/version.py`)
4. Validate by running the `get` command
5. Fall back to built-in handlers if generation fails

**Customization:**
Users MAY create or modify the version script at any time. The script can be implemented in any language (Python, Bash, etc.) as long as it follows the command contract above.

**Configuration (se3.yaml):**
```yaml
version:
  script_path: null          # Custom script path (null = default se3/scripts/version.py)
  auto_generate_script: true # Auto-generate via LLM if script not found
```

#### Scenario: Script Mode Version Bump
- **GIVEN** `se3/scripts/version.py` exists and implements the contract
- **WHEN** commit step triggers a version bump
- **THEN** SE3 calls the script's `bump --type minor` command
- **AND** uses the stdout output as the new version

#### Scenario: Script Auto-Generation
- **GIVEN** no version script exists
- **AND** `auto_generate_script: true`
- **WHEN** version system is initialized
- **THEN** LLM generates a script based on detected project structure
- **AND** script is validated by running `get` command

#### Scenario: Script Rollback
- **GIVEN** version was bumped via script from `1.2.3` to `1.3.0`
- **WHEN** git commit fails
- **THEN** SE3 calls script's `set --version 1.2.3` to restore

### Requirement: Version File Detection

SE3 SHALL automatically detect project type and locate the version file with the following priority:

**Detection Priority (when no version script exists):**
1. `pyproject.toml` - Python project (PEP 518/621)
2. `package.json` - Node.js project
3. `se3.yaml` - Custom configuration for other project types

Note: When a version script is present, it takes priority over file detection.

**Version Storage Location:**

| Project Type | File Path | Field Path |
|-------------|-----------|------------|
| Python (PEP 621) | `pyproject.toml` | `project.version` |
| Python (Poetry) | `pyproject.toml` | `tool.poetry.version` |
| Node.js | `package.json` | `version` |
| Custom | `se3.yaml` configured | `version.file_path` |

**Auto-detection Logic:**
1. Check for `pyproject.toml` - if exists, read version from it
2. Check for `package.json` - if exists, read version from it
3. Check `se3.yaml` for explicit `version.file_path`
4. If none found, skip version bumping

#### Scenario: Python Project Detection
- **GIVEN** project has `pyproject.toml` with `project.version = "1.0.0"`
- **WHEN** SE3 detects version file
- **THEN** returns `pyproject.toml` as version file
- **AND** extracts version `1.0.0`

#### Scenario: Node.js Project Detection
- **GIVEN** project has `package.json` with `"version": "2.1.0"`
- **WHEN** SE3 detects version file
- **THEN** returns `package.json` as version file
- **AND** extracts version `2.1.0`

### Requirement: LLM-Based Version Analysis

SE3 SHALL determine the new version number via LLM analysis of actual changes. The LLM's `suggested_version` is the **single authoritative version field** consumed by the commit step.

**Version Analyze Step:**
A dedicated `version_analyze` step SHALL run after `update_spec` and before `commit` to compute the new version number, based on:
- **Spec changes (updated_specs)**: API contract changes — PRIMARY indicator for breaking/non-breaking
- **Files changed (changes_made)**: Implementation details and scope
- **Verification results**: Consistency checks against specs

Spec changes are prioritized as they directly reflect API contract modifications.

**LLM Analysis Output:**
```json
{
  "bump_type": "major|minor|patch|none",
  "reasoning": "Explanation based on SemVer 2.0.0 rules and specific changes",
  "confidence": "high|medium|low",
  "suggested_version": "X.Y.Z",
  "commit_message": "Concise imperative commit summary (max 72 chars)"
}
```

**Authoritative field:** `suggested_version` is the single authoritative version number consumed downstream. The commit step writes this value verbatim into the project's version file. `bump_type`, `reasoning`, and `confidence` are supplementary fields used only for human-facing display (renderers, commit-message metadata) and are NOT used to mechanically recompute the new version from `current_version`.

The `commit_message` field is generated alongside version analysis. It uses imperative mood, starts with a verb, and does not include task type prefixes. The commit step consumes this field as the primary source for the git commit message subject line.

**Semantic Versioning 2.0.0 Decision Criteria (default rules):**
- **MAJOR**: Incompatible API changes, removed functionality, breaking behavioral changes
- **MINOR**: New backward-compatible functionality, new features, new optional parameters
- **PATCH**: Backward-compatible bug fixes, performance improvements, internal refactoring
- **NONE**: No version-worthy changes (formatting, comments only)

The LLM applies these defaults to compute `suggested_version` from `current_version` when no project-level custom rules file is present. When a custom rules file exists (see Custom Version Rules File requirement), the LLM applies the custom rules in preference to the defaults.

**Confidence Levels:**
- `high`: Clear change type (e.g., obvious breaking change or simple bugfix)
- `medium`: Some ambiguity but reasonable determination possible
- `low`: Complex changes with unclear impact, borderline cases

#### Scenario: Version analysis identifies breaking change
- **GIVEN** a `small` task that removed a public function parameter
- **WHEN** the `version_analyze` step runs
- **THEN** the LLM identifies this as a breaking change
- **AND** `suggested_version` reflects a major bump regardless of task type

#### Scenario: suggested_version is the authoritative value
- **GIVEN** `version_analyze` returns `suggested_version: 1.3.0` and `bump_type: minor`
- **WHEN** the commit step writes the new version to the version file
- **THEN** it writes `1.3.0` directly (the value from `suggested_version`)
- **AND** it does NOT recompute the version by applying `bump_type` to `current_version`

### Requirement: Custom Version Rules File

SE3 SHALL support an optional, project-level natural-language version rules file at the conventional path `se3/version-rules.md`.

**Purpose:** Allow each project to declare its own version-numbering policy in plain Markdown / prose. The LLM running the `version_analyze` step reads this file (when present) and uses it as the authoritative rule set when computing `suggested_version`.

**Conventional path and discovery:**
- Path: `<project_root>/se3/version-rules.md`.
- No configuration field overrides this path — there is no `se3.yaml` `version.rules_file` key.
- Discovery is purely path-based: if the file exists and is non-empty, it is read; otherwise the framework falls back to the default SemVer 2.0.0 rules built into the `version_analyze` prompt.
- The file SHALL be excluded from the default `.gitignore` mask (alongside `se3/specs/`, `se3/issues/`, `se3/scripts/`) so that the rules are committed with the project.

**File format:**
- Pure natural language / Markdown. No DSL, no schema, no code blocks interpreted as executable rules.
- No syntax validation, no parsing beyond reading the text. The framework MUST NOT execute, evaluate, or interpret the file beyond passing its contents into the LLM prompt.
- Practical size cap: when the file exceeds ~64 KB the framework SHALL truncate the content (preserving the head) and log a warning, to keep the LLM prompt within budget.

**Prompt injection:**
- When the file exists, its content is injected into the `version_analyze` prompt as a dedicated "Project-Specific Version Rules" section. The default SemVer 2.0.0 description remains in the prompt as a baseline; the LLM is instructed to prefer the custom rules whenever they conflict with the defaults.
- When the file does NOT exist, no custom-rules section is emitted and the LLM uses only the default SemVer 2.0.0 rules to compute `suggested_version`.

**Out of scope (explicit non-goals):**
- No DSL or structured rule schema.
- No code execution from the rules file under any circumstances.
- No syntax validation or auto-fix of malformed content.
- No CLI override or alternate file path.

#### Scenario: Custom rules file present is injected into prompt
- **GIVEN** `se3/version-rules.md` exists with project-specific version guidance (e.g., "Bugfix only bumps patch when the user-visible behavior changes; otherwise stay on the same version.")
- **WHEN** the `version_analyze` step runs
- **THEN** the file's content is included in the LLM prompt under a "Project-Specific Version Rules" section
- **AND** the LLM is instructed to prefer the custom rules over the default SemVer 2.0.0 rules where they differ
- **AND** the resulting `suggested_version` reflects the custom rules

#### Scenario: No custom rules file falls back to default SemVer
- **GIVEN** `se3/version-rules.md` does NOT exist
- **WHEN** the `version_analyze` step runs
- **THEN** the LLM prompt contains only the default SemVer 2.0.0 description
- **AND** `suggested_version` is computed using SemVer 2.0.0 rules

#### Scenario: Oversized rules file is truncated with warning
- **GIVEN** `se3/version-rules.md` is larger than ~64 KB
- **WHEN** the `version_analyze` step reads the file
- **THEN** the content is truncated to a safe budget (head preserved)
- **AND** a warning is logged identifying the truncation

### Requirement: Automatic Version Bumping

SE3 SHALL provide automatic version bumping integrated into the commit workflow, driven exclusively by `suggested_version` from the `version_analyze` step.

**Bump Process:**
1. Detect current version from the version file (or version script).
2. Read `suggested_version` from the completed `version_analyze` step.
3. If `version_analyze` is missing or did not produce a `suggested_version`, the commit step SHALL fail with a descriptive error (see Missing Version Handling requirement).
4. Write `suggested_version` verbatim into the version file (atomic write + backup for rollback).
5. Stage the version file for the upcoming commit.

`bump_type` is NOT used to compute the new version. There is no static task-type-to-bump-type lookup table — that mechanism has been removed. `bump_type` survives only as a display hint and a commit-message metadata field.

**Configuration (se3.yaml):**
```yaml
version:
  enabled: true                       # Enable automatic version bumping
  file_path: null                     # Explicit version file path (null = auto-detect)
  include_in_commit_message: true     # Include version in commit message
  auto_bump: true                     # Auto-apply suggested_version without confirmation
  confidence_threshold: null          # Threshold for human confirmation (null = never)
  script_path: null                   # Custom version script path (null = default)
  auto_generate_script: true          # Auto-generate version script if absent
```

Legacy `version` keys that previously controlled a static bump-rules table or a smart-analysis toggle are no longer recognized. When present in a legacy `se3.yaml`, they are silently ignored at load time (a deprecation note may be logged once). They have no effect on the new flow.

#### Scenario: Feature task applies suggested_version
- **GIVEN** current version is `1.2.3`
- **AND** `version_analyze` returns `suggested_version: 1.3.0`
- **WHEN** the commit step runs
- **THEN** the version file is updated to `1.3.0` (the value from `suggested_version`)
- **AND** the commit includes the version-file change

#### Scenario: Bugfix task applies suggested_version
- **GIVEN** current version is `1.2.3`
- **AND** `version_analyze` returns `suggested_version: 1.2.4`
- **WHEN** the commit step runs
- **THEN** the version file is updated to `1.2.4`
- **AND** the commit message includes the new version

#### Scenario: Disabled Version Bumping
- **GIVEN** `version.enabled: false` in se3.yaml
- **WHEN** commit step executes
- **THEN** no version bumping occurs
- **AND** existing version is preserved

### Requirement: Missing Version Handling

When the `version_analyze` step fails or completes without a usable `suggested_version`, the `commit` step SHALL halt the flow rather than silently fall back to a default bump.

**Behavior:**
- The commit step raises a runtime error describing: (a) the current version on disk, (b) the reason `suggested_version` is unavailable (e.g., `version_analyze` failed, missing field, empty string), and (c) guidance on human intervention — re-run `version_analyze`, edit `se3/version-rules.md` to clarify the policy, or apply an explicit override through existing manual mechanisms.
- The flow is left in a state where the user can resume after intervention; no partial version-file write is committed.
- Silent fallback to a default patch bump (the previous behavior) is explicitly REMOVED.

#### Scenario: Commit step halts when suggested_version missing
- **GIVEN** the `version_analyze` step completed but `suggested_version` is absent from its outputs
- **WHEN** the commit step runs
- **THEN** the commit step raises an error identifying the current version and the missing-version reason
- **AND** the flow is halted for human intervention
- **AND** no version-file write is committed

#### Scenario: Commit step halts when version_analyze failed
- **GIVEN** the `version_analyze` step has status FAILED
- **WHEN** the commit step is reached
- **THEN** the commit step raises an error with current version and intervention guidance
- **AND** the flow is halted

#### Scenario: Aggregated SemVer bump after `se3 merge`
- **GIVEN** the current branch is at version `4.4.0`
- **AND** the user runs `se3 merge feat/a feat/b feat/c`
- **AND** `feat/a` represents a `patch` bump relative to its merge base, `feat/b` a `patch`, and `feat/c` a `minor`
- **WHEN** all three branches have been sequentially merged
- **THEN** the per-branch bump types are reduced via SemVer's max rule (`max(patch, patch, minor) = minor`)
- **AND** a single `pyproject.toml` update writes the new version `4.5.0`
- **AND** that update is amended onto the last merge commit (not added as a separate commit)
- **AND** historical commits on `feat/a`, `feat/b`, `feat/c` are NOT rewritten — SemVer uniqueness is enforced by tags, not by retroactive bumps on the merged branches

#### Scenario: Per-branch bump uses end-to-end merge-base diff, not pre-merge HEAD
- **GIVEN** the current branch's pre-merge version is `4.6.0`
- **AND** branch `B` diverged from the current branch at version `4.4.0` and its tip is `4.4.1`
- **AND** branch `C` diverged at `4.4.0` and its tip is `4.6.0`, with intermediate commits walking through `4.5.0` → `4.5.1` → `4.6.0`
- **WHEN** the user runs `se3 merge B C`
- **THEN** branch `B`'s bump is computed as the end-to-end diff `4.4.0 → 4.4.1` = PATCH
- **AND** branch `C`'s bump is computed as the end-to-end diff `4.4.0 → 4.6.0` = MINOR (intra-branch intermediate bumps are NOT accumulated)
- **AND** the comparison base is each branch's git merge-base with the pre-merge HEAD, NOT the pre-merge HEAD itself — so a branch tip that is below the pre-merge version still produces a valid bump rather than being skipped
- **AND** the aggregated `max(PATCH, MINOR) = MINOR` is applied to the pre-merge version `4.6.0`, producing `4.7.0`

#### Scenario: Aggregator fails loud when on-disk version already at or above target
- **GIVEN** the version aggregator computes a target version `X.Y.Z` for the current merge
- **AND** the on-disk `pyproject.toml` already records a version equal to or greater than `X.Y.Z`
- **WHEN** the aggregator attempts to apply the bump
- **THEN** the aggregator does NOT silently no-op as success — instead it returns `success=False` with `version_already_at_target=True` (when equal) or `version_higher_than_target=True` (when strictly greater)
- **AND** the merge orchestrator surfaces the typed failure reasons `version_already_at_target` or `version_higher_than_target`
- **AND** a partial pyproject.toml write that fails midway (or whose `git add` fails) restores the original file content and clears any staged change before the failure is reported

#### Scenario: Aggregator tolerates flexible TOML version formatting
- **GIVEN** `pyproject.toml` records the version as `version="1.2.3"` (no spaces around `=`)
- **WHEN** the aggregator parses the version field
- **THEN** the field is recognised and the version is read correctly
- **AND** tools that emit either `version = "1.2.3"` or `version="1.2.3"` are accepted equivalently

### Requirement: Documentation Updates

SE3 SHALL automatically update README.md and VERSIONS.md when version changes.

**README.md Updates:**
- Insert/update version badge near the top of the file
- Use configurable template with placeholder replacement
- Default badge template: `![Version](https://img.shields.io/badge/version-{version}-blue)`
- Preserve existing content, only update badge

**VERSIONS.md Updates:**
- Create file if it doesn't exist with standard header
- Insert new version entry at the top (newest first)
- Use configurable template for entry format
- Preserve existing entries

**Template Placeholders:**
- `{version}` - The new version string
- `{date}` - Current date (YYYY-MM-DD)
- `{changes}` - Summary of changes (from commit message)

**Configuration:**
```yaml
version:
  templates:
    readme_badge: "![Version](https://img.shields.io/badge/version-{version}-blue)"
    versions_entry: |
      ## {version} - {date}
      
      {changes}
      
  readme_marker: "<!-- SE3-VERSION -->"  # Marker for badge insertion point
  versions_header: "# Version History\n\n"
```

#### Scenario: README.md Badge Update
- **GIVEN** README.md exists without version badge
- **WHEN** version bumps to `1.3.0`
- **THEN** badge `![Version](https://img.shields.io/badge/version-1.3.0-blue)` is inserted after the first heading

#### Scenario: VERSIONS.md Entry Creation
- **GIVEN** VERSIONS.md exists with previous entries
- **WHEN** version bumps to `1.3.0`
- **THEN** new entry inserted at the top with current date and change summary

### Requirement: Version Rollback

SE3 SHALL support rollback of version changes if commit fails.

**Rollback Mechanism:**
1. Before bumping, create backup of original version
2. If commit fails or is interrupted, restore original version
3. Clear backup after successful commit
4. Rollback includes both version file and documentation changes

**Error Handling:**
- Log rollback attempts and results
- If rollback fails, log error for manual intervention
- Preserve backup files until explicit clear or next bump

#### Scenario: Commit Failure Rollback
- **GIVEN** version was bumped from `1.2.3` to `1.3.0`
- **WHEN** git commit fails (network error, rejected, etc.)
- **THEN** version file is restored to `1.2.3`
- **AND** README.md/VERSIONS.md changes are reverted

#### Scenario: Successful Commit
- **GIVEN** version was bumped and committed successfully
- **WHEN** commit completes
- **THEN** backup is cleared
- **AND** version change is permanent

### Requirement: CLI Integration

SE3 SHALL provide CLI commands for manual version management.

**`se3 commit --bump <type>`:**
- Allow manual version bump during commit
- Override automatic bump detection
- Supported types: `major`, `minor`, `patch`

**Version Display:**
- `se3 status` SHALL display current version
- Commit output SHALL include version change info

#### Scenario: Manual Version Bump
- **GIVEN** current version is `1.2.3`
- **WHEN** user runs `se3 commit --bump minor`
- **THEN** version bumps to `1.3.0` regardless of task type
- **AND** commit proceeds with new version

## Architecture

### Version Management Components

```
┌──────────────────────────────────────────────────────────────────┐
│                      Version Management                           │
├──────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────────┐  ┌─────────────────────┐  │
│  │   Config    │  │  VersionBumper   │  │      Updater        │  │
│  │   Loader    │→ │                  │→ │                     │  │
│  └─────────────┘  └────────┬────────┘  └─────────────────────┘  │
│         │                  │                      │              │
│         │          ┌───────┴───────┐              │              │
│         │          │               │              │              │
│         ▼          ▼               ▼              ▼              │
│  ┌───────────┐ ┌────────────┐ ┌────────────┐ ┌────────────────┐ │
│  │ se3.yaml  │ │  Script    │ │  Built-in  │ │  README.md     │ │
│  │script_path│ │  Interface │ │  Handlers  │ │  VERSIONS.md   │ │
│  │auto_bump  │ │(subprocess)│ │ (fallback) │ │                │ │
│  └───────────┘ └─────┬──────┘ └────────────┘ └────────────────┘ │
│                      │                                           │
│                      ▼                                           │
│              ┌──────────────┐                                    │
│              │ Version      │  ← Single Source of Truth          │
│              │ Source File  │                                    │
│              └──────────────┘                                    │
└──────────────────────────────────────────────────────────────────┘
```

### Integration Points

1. **Commit Step**: Triggers version bump before git commit
2. **Config System**: Loads version settings from se3.yaml
3. **Version Script**: Script-based interface (priority over built-in handlers)
4. **Built-in Handlers**: Fallback for pyproject.toml, package.json, etc.
5. **Engine**: Provides task type context for bump decisions
6. **Git**: Stages version file changes with code changes

## References

- [Semantic Versioning 2.0.0](https://semver.org/)
- [PEP 518](https://peps.python.org/pep-0518/) - Python Project Metadata
- [npm package.json](https://docs.npmjs.com/cli/v10/configuring-npm/package-json) - Node.js Version Field
