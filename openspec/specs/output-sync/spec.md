# output-sync Specification

## Purpose
Manages the `output/` directory which contains templates for `se3 init`. With the SE3 module system (v7.0), runtime file sync was removed — `output/` only holds templates and documentation.

## Requirements
### Requirement: Template Management
The system SHALL manage template files in `output/` for use by `se3 init`.

Templates:
- `output/SE3.md.template` → `.claude/SE3.md`
- `output/CLAUDE.minimal.md.template` → `.claude/CLAUDE.md`
- `output/status.md.template` → `status.md`

#### Scenario: List templates
- **WHEN** `se3 sync --dry-run` is executed
- **THEN** it lists template files and their status

### Requirement: Orphan Detection
The system SHALL detect output files that are not recognized templates.

#### Scenario: Orphaned output file
- **WHEN** an output file exists that is not a recognized template
- **THEN** `se3 sync` warns about the orphaned file
- **AND** `--prune` flag removes orphaned files
