# Toolize SE 3.0 Tasks

## 1. Tool Structure Setup

- [x] 1.1 Create `tools/` directory with Python package structure (`se3_tools/`)
- [x] 1.2 Create `pyproject.toml` with typer dependency and CLI entry point
- [x] 1.3 Create base module with shared utilities (file discovery, YAML parsing)

## 2. spec-lint Implementation

- [x] 2.1 Implement spec file discovery in `openspec/specs/` and `openspec/changes/*/specs/`
- [x] 2.2 Implement required field validation (Purpose, Requirements, WHEN/THEN)
- [x] 2.3 Implement scenario format validation
- [x] 2.4 Add exit code support (0=success, 1=errors, 2=runtime error)
- [x] 2.5 Test spec-lint against existing specs in this project

## 3. output-sync Implementation

- [x] 3.1 Implement source-to-output mapping detection
- [x] 3.2 Implement `--dry-run` mode to preview changes
- [x] 3.3 Implement `--apply` mode to synchronize files
- [x] 3.4 Implement orphan detection and `--prune` flag
- [x] 3.5 Test output-sync against current project state

## 4. change-verifier Implementation

- [x] 4.1 Implement scenario extraction from change specs
- [x] 4.2 Implement test marker detection (`@pytest.mark.scenario`, `# Verify:`)
- [x] 4.3 Implement coverage report generation
- [x] 4.4 Implement skip annotation support
- [x] 4.5 Test change-verifier against archived changes

## 5. status-diagnostics Implementation

- [x] 5.1 Implement status.md parsing
- [x] 5.2 Implement consistency checks (active change exists, blockers match status)
- [x] 5.3 Implement human-calls check (pending, responded, stale)
- [x] 5.4 Implement diagnostic output with `--format=text` and `--format=json`
- [x] 5.5 Test status-diagnostics against current project

## 6. Integration and Documentation

- [x] 6.1 Create unified `se3` CLI command with subcommands
- [x] 6.2 Add tool documentation to README
- [x] 6.3 Update `se3-scaffold` spec to reference tools
- [x] 6.4 Update `output/` with tool documentation
- [x] 6.5 Run all tools against SE 3.0 project itself

## 7. Verification and Archive

- [x] 7.1 Run spec-lint on all specs (should pass)
- [x] 7.2 Run output-sync --dry-run (should show no drift)
- [x] 7.3 Run change-verifier on toolize-se3 change
- [x] 7.4 Run status-diagnostics (should pass)
- [x] 7.5 Archive change with `openspec archive-change`
