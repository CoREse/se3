# spec-lint Specification

## Purpose
TBD - created by archiving change toolize-se3. Update Purpose after archive.
## Requirements
### Requirement: Spec File Discovery
The system SHALL discover all spec files in `openspec/specs/` and `openspec/changes/*/specs/`.

#### Scenario: Discover specs in standard location
- **WHEN** `se3 lint` is run
- **THEN** it finds all `spec.md` files under `openspec/specs/*/` and `openspec/changes/*/specs/*/`

### Requirement: Required Field Validation
The system SHALL validate that each spec file contains required fields.

Required fields:
- `# <name> Specification` - Title header
- `## Purpose` - Purpose section explaining what this spec governs
- `## Requirements` - Requirements section with at least one requirement
- Each requirement MUST have scenarios (WHEN/THEN) or be marked as `**SHOULD**`/`**MAY**`

#### Scenario: Spec with missing Purpose
- **WHEN** a spec file lacks a Purpose section
- **THEN** `se3 lint` reports an error with file path and line number

#### Scenario: Spec with untestable requirement
- **WHEN** a requirement uses SHALL but has no WHEN/THEN scenario
- **THEN** `se3 lint` warns that the requirement may not be verifiable

### Requirement: Scenario Format Validation
The system SHALL validate WHEN/THEN scenario format.

Valid format:
```
#### Scenario: <name>
- **WHEN** <condition>
- **THEN** <expected outcome>
```

#### Scenario: Malformed scenario
- **WHEN** a scenario is missing THEN clause
- **THEN** `se3 lint` reports the error with context

### Requirement: Exit Codes
The system SHALL return appropriate exit codes.

Exit codes:
- `0` - All specs valid
- `1` - One or more errors found
- `2` - Configuration or runtime error

#### Scenario: CI integration
- **WHEN** `se3 lint` runs in CI pipeline
- **THEN** exit code 1 causes pipeline failure on spec violations

