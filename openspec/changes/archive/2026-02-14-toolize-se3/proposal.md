# Toolize SE 3.0 Framework

## Why

SE 3.0 is currently a "paper framework" — rules exist in specs and CLAUDE.md, but compliance depends entirely on Claude manually following them. This creates drift risk: specs can become outdated, status.md can be forgotten, and verification can be skipped. We need to evolve from "rules as documentation" to "rules as enforceable tools" that actively check and validate compliance.

## What Changes

1. **Spec Lint Tool** (`se3-lint`): Validates spec file format, required fields, and scenario completeness
2. **Output Sync Tool** (`se3-sync`): Ensures `output/` directory stays synchronized with source specs and CLAUDE.md
3. **Change Verification Tool** (`se3-verify`): Validates that implementation covers all spec scenarios before archiving
4. **Status Check Tool** (`se3-status`): Diagnoses session state by reading status.md and cross-checking with git/openspec state

## Capabilities

### New Capabilities
- `spec-lint`: Validates spec files have required fields (Purpose, Requirements with WHEN/THEN scenarios)
- `output-sync`: Automated synchronization between openspec/specs/ + CLAUDE.md and output/ directory
- `change-verifier`: Pre-archive verification that all spec scenarios have corresponding tests/implementation
- `status-diagnostics`: Automated status.md validation and diagnostic reporting

### Modified Capabilities
- None (this is purely additive tooling)

## Impact

- New `tools/` directory containing Python-based CLI tools
- Adds `pyproject.toml` for tool packaging
- Updates `se3-scaffold` spec to include tool installation in new projects
- No breaking changes to existing workflows
