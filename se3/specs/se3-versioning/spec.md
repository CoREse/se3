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

### Requirement: Smart Version Analysis

SE3 SHALL provide intelligent version bumping using LLM analysis of actual changes, rather than relying solely on task type classification.

**Version Analyze Step:**
A dedicated `version_analyze` step SHALL run after `update_spec` and before `commit` to determine the appropriate SemVer bump type and generate the commit message, based on:
- **Spec changes (updated_specs)**: API contract changes - PRIMARY indicator for breaking/non-breaking
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

The `commit_message` field is generated alongside version analysis. It uses imperative mood, starts with a verb, and does not include task type prefixes. The commit step consumes this field as the primary source for the git commit message subject line.

**Semantic Versioning 2.0.0 Decision Criteria:**
- **MAJOR**: Incompatible API changes, removed functionality, breaking behavioral changes
- **MINOR**: New backward-compatible functionality, new features, new optional parameters
- **PATCH**: Backward-compatible bug fixes, performance improvements, internal refactoring
- **NONE**: No version-worthy changes (formatting, comments only)

**Confidence Levels:**
- `high`: Clear change type (e.g., obvious breaking change or simple bugfix)
- `medium`: Some ambiguity but reasonable determination possible
- `low`: Complex changes with unclear impact, borderline cases

#### Scenario: Smart Version Analysis for Breaking Change
- **GIVEN** a `small` task that removed a public function parameter
- **WHEN** the `version_analyze` step runs
- **THEN** LLM identifies this as a breaking change
- **AND** recommends `bump_type: major` despite task type being `small`

#### Scenario: Confidence-Based Fallback
- **GIVEN** LLM analysis returns `confidence: low`
- **WHEN** auto_bump is enabled (default)
- **THEN** system applies the suggested bump type anyway
- **AND** logs a warning about low confidence

### Requirement: Automatic Version Bumping

SE3 SHALL provide automatic version bumping integrated into the commit workflow.

**Bump Process with Smart Analysis:**
1. Detect current version from version file
2. Run `version_analyze` step to determine bump type via LLM analysis
3. If smart analysis is disabled or fails, fall back to task type based rules
4. Calculate new version following SemVer rules
5. Update version file atomically
6. Create backup for potential rollback
7. Stage version file for commit

**Fallback Bump Rules (when smart analysis is disabled):**
| Task Type | Bump Type | Version Change |
|-----------|-----------|----------------|
| `feature` | minor | X.Y.Z → X.Y+1.0 |
| `feat` | minor | X.Y.Z → X.Y+1.0 |
| `bugfix` | patch | X.Y.Z → X.Y.Z+1 |
| `fix` | patch | X.Y.Z → X.Y.Z+1 |
| `breaking` | major | X.Y.Z → X+1.0.0 |
| `small` | patch | X.Y.Z → X.Y.Z+1 |
| `docs` | patch | X.Y.Z → X.Y.Z+1 |
| `refactor` | patch | X.Y.Z → X.Y.Z+1 |

**Configuration (se3.yaml):**
```yaml
version:
  enabled: true                       # Enable automatic version bumping
  file_path: null                     # Explicit version file path (null = auto-detect)
  include_in_commit_message: true     # Include version in commit message
  
  # Smart Version Analysis
  smart_version_analysis: true        # Enable LLM-based version analysis
  auto_bump: true                     # Auto-apply bump without confirmation
  confidence_threshold: null          # Threshold for human confirmation (null=never)
  
  # Fallback bump rules (used when smart analysis is disabled)
  bump_rules:
    feature: minor
    bugfix: patch
    breaking: major
    small: patch
```

#### Scenario: Feature Task Version Bump with Smart Analysis
- **GIVEN** current version is `1.2.3`
- **AND** `smart_version_analysis: true`
- **WHEN** `version_analyze` step executes for a `feature` task
- **THEN** LLM analyzes the actual changes
- **AND** version bumps according to analysis result (typically `1.3.0`)

#### Scenario: Bugfix Task Version Bump
- **GIVEN** current version is `1.2.3`
- **WHEN** commit step executes for a `bugfix` task
- **THEN** version bumps to `1.2.4`
- **AND** commit message includes new version

#### Scenario: Disabled Smart Analysis
- **GIVEN** `smart_version_analysis: false` in se3.yaml
- **WHEN** commit step executes
- **THEN** system uses task type based bump rules from configuration

#### Scenario: Disabled Version Bumping
- **GIVEN** `version.enabled: false` in se3.yaml
- **WHEN** commit step executes
- **THEN** no version bumping occurs
- **AND** existing version is preserved

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
│  │bump_rules │ │  Interface │ │  Handlers  │ │  VERSIONS.md   │ │
│  │script_path│ │(subprocess)│ │ (fallback) │ │                │ │
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
