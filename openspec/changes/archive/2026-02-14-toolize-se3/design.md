# Toolize SE 3.0 Design

## Context

SE 3.0 currently relies on manual compliance:
- Specs are markdown files that may drift from implementation
- `output/` directory must be manually kept in sync with source
- No automated verification that spec scenarios are tested
- `status.md` format is conventional, not enforced

We need to introduce tooling that validates and enforces SE 3.0 conventions without adding significant overhead.

## Goals / Non-Goals

**Goals:**
- Provide CLI tools for spec validation, sync, and verification
- Keep tools lightweight and standalone (no heavy dependencies)
- Integrate naturally into existing SE 3.0 workflows
- Support both programmatic and human-readable output

**Non-Goals:**
- Replace openspec CLI (complement it)
- Add CI/CD integration (future enhancement)
- Support non-SE-3.0 projects

## Decisions

### D1: Python-based CLI with typer
- **Choice**: Use Python with `typer` for CLI framework
- **Rationale**: Python is universally available, rich ecosystem for YAML/markdown parsing, typer provides clean CLI syntax with type hints
- **Alternative**: Shell scripts (rejected: harder to maintain, test, and extend)

### D2: Tools as separate commands under unified `se3` namespace
- **Choice**: `se3 lint`, `se3 sync`, `se3 verify`, `se3 status`
- **Rationale**: Discoverable, consistent interface, easy to extend
- **Alternative**: Separate binaries (rejected: more complex packaging)

### D3: Read-only by default, write with explicit flags
- **Choice**: `se3 sync --dry-run` shows what would change; `se3 sync --apply` applies changes
- **Rationale**: Safe by default, prevents accidental overwrites

### D4: Spec scenario verification via marker comments
- **Choice**: Look for `# Verify: <scenario-id>` or `@pytest.mark.scenario("<id>")` markers to map tests to spec scenarios
- **Rationale**: Explicit linkage between spec and implementation without rigid structure

## Risks / Trade-offs

- [Risk] Adding tools creates new maintenance burden → Mitigation: Keep tools simple, focused on SE 3.0 core
- [Risk] Tools may become out of sync with spec format → Mitigation: Tools validate against openspec schema if available
- [Risk] False positives in verification → Mitigation: Clear error messages, easy override mechanisms

## Migration Plan

1. Create `tools/` directory with Python package structure
2. Implement `se3-lint` first (read-only, safest)
3. Implement `se3-sync` with `--dry-run` default
4. Implement `se3-verify` for pre-archive checks
5. Implement `se3-status` for diagnostics
6. Update `se3-scaffold` spec to include tool reference
7. Update output/ with tool documentation
