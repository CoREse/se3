# output-sync Specification

## Purpose
TBD - created by archiving change toolize-se3. Update Purpose after archive.
## Requirements
### Requirement: Source Detection
The system SHALL identify source files that generate output artifacts.

Source-to-output mappings:
- `CLAUDE.md` → `output/CLAUDE.md`
- `~/.claude/CLAUDE.md` (global) → `output/CLAUDE.global.md`
- `se3.config.yaml` → `output/se3.config.yaml`
- `status.md` (template) → `output/status.md`

#### Scenario: Detect modified source
- **WHEN** `CLAUDE.md` has newer mtime than `output/CLAUDE.md`
- **THEN** `se3 sync --dry-run` reports that output needs update

### Requirement: Dry-Run Mode
The system SHALL support dry-run mode that shows changes without applying them.

#### Scenario: Preview changes
- **WHEN** `se3 sync --dry-run` is executed
- **THEN** it lists which output files would be created, updated, or deleted
- **AND** no files are actually modified

### Requirement: Apply Mode
The system SHALL apply changes when explicitly requested.

#### Scenario: Apply synchronization
- **WHEN** `se3 sync --apply` is executed
- **THEN** output files are updated to match source files
- **AND** a summary of changes is displayed

### Requirement: Delete Detection
The system SHALL detect when output files no longer have corresponding sources.

#### Scenario: Orphaned output file
- **WHEN** an output file exists with no corresponding source
- **THEN** `se3 sync` warns about the orphaned file
- **AND** `--prune` flag removes orphaned files

### Requirement: Content Validation
The system SHALL validate that output files match expected templates.

#### Scenario: Manual edit to output
- **WHEN** an output file was manually edited (differs from source)
- **THEN** `se3 sync` shows the diff and warns about drift

