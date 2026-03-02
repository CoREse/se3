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

### Requirement: Version File Detection

SE3 SHALL automatically detect project type and locate the version file with the following priority:

**Detection Priority:**
1. `pyproject.toml` - Python project (PEP 518/621)
2. `package.json` - Node.js project
3. `se3.yaml` - Custom configuration for other project types

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

### Requirement: Automatic Version Bumping

SE3 SHALL provide automatic version bumping integrated into the commit workflow.

**Bump Rules:**
Map task types to version bump types via configuration:

| Task Type | Bump Type | Version Change |
|-----------|-----------|----------------|
| `feature` | minor | X.Y.Z → X.Y+1.0 |
| `feat` | minor | X.Y.Z → X.Y+1.0 |
| `bugfix` | patch | X.Y.Z → X.Y.Z+1 |
| `fix` | patch | X.Y.Z → X.Y.Z+1 |
| `breaking` | major | X.Y.Z → X+1.0.0 |
| `docs` | none | No version change |
| `test` | none | No version change |
| `chore` | none | No version change |

**Bump Process:**
1. Detect current version from version file
2. Determine task type from flow context
3. Look up bump type from configuration
4. Calculate new version following SemVer rules
5. Update version file atomically
6. Create backup for potential rollback
7. Stage version file for commit

**Configuration (se3.yaml):**
```yaml
version:
  enabled: true                       # Enable automatic version bumping
  file_path: null                     # Explicit version file path (null = auto-detect)
  include_in_commit_message: true     # Include version in commit message
  bump_rules:                         # Task type to bump type mapping
    feature: minor
    feat: minor
    bugfix: patch
    fix: patch
    breaking: major
    docs: none
    test: none
    chore: none
```

#### Scenario: Feature Task Version Bump
- **GIVEN** current version is `1.2.3`
- **WHEN** commit step executes for a `feature` task
- **THEN** version bumps to `1.3.0`
- **AND** version file is staged for commit

#### Scenario: Bugfix Task Version Bump
- **GIVEN** current version is `1.2.3`
- **WHEN** commit step executes for a `bugfix` task
- **THEN** version bumps to `1.2.4`
- **AND** commit message includes new version

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
┌─────────────────────────────────────────────────────────────┐
│                    Version Management                        │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Config    │  │   Bumper    │  │      Updater        │  │
│  │   Loader    │→ │             │→ │                     │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│         │                │                      │           │
│         ▼                ▼                      ▼           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │se3.yaml     │  │Version File │  │  README.md          │  │
│  │bump_rules   │  │Read/Write   │  │  VERSIONS.md        │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### Integration Points

1. **Commit Step**: Triggers version bump before git commit
2. **Config System**: Loads version settings from se3.yaml
3. **Engine**: Provides task type context for bump decisions
4. **Git**: Stages version file changes with code changes

## References

- [Semantic Versioning 2.0.0](https://semver.org/)
- [PEP 518](https://peps.python.org/pep-0518/) - Python Project Metadata
- [npm package.json](https://docs.npmjs.com/cli/v10/configuring-npm/package-json) - Node.js Version Field
