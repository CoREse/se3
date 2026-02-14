# SE 3.0 CLI Tools

SE 3.0 includes CLI tools to validate and enforce framework conventions. These tools evolve the framework from "rules as documentation" to "rules as enforceable code."

## Installation

```bash
cd tools/
pip install -e .
```

This installs the `se3` command-line tool.

## Commands

### `se3 lint` — Spec Validation

Validates that spec files conform to SE 3.0 format requirements.

```bash
# Lint all specs in openspec/
se3 lint

# Lint specific directory
se3 lint /path/to/specs
```

**Validates:**
- Spec files have title header (`# <name> Specification`)
- Purpose section exists
- Requirements section exists
- SHALL requirements have WHEN/THEN scenarios
- Scenarios have both WHEN and THEN clauses

**Exit codes:**
- `0` — All specs valid
- `1` — Validation errors found
- `2` — Runtime/configuration error

---

### `se3 sync` — Output Synchronization

Keeps `output/` directory synchronized with source files.

```bash
# Preview changes (default)
se3 sync --dry-run

# Apply changes
se3 sync --apply

# Remove orphaned output files
se3 sync --apply --prune
```

**Synchronizes:**
- `CLAUDE.md` → `output/CLAUDE.md`
- `~/.claude/CLAUDE.md` → `output/CLAUDE.global.md`
- `se3.config.yaml` → `output/se3.config.yaml`
- `status.md` → `output/status.md`

**Workflow:**
1. Always run `--dry-run` first to preview
2. Review the changes
3. Run `--apply` to synchronize

---

### `se3 verify` — Change Verification

Verifies that implementation covers all spec scenarios before archiving.

```bash
# Verify a specific change
se3 verify --change toolize-se3

# JSON output for automation
se3 verify --change toolize-se3 --format json
```

**How it works:**
1. Extracts all WHEN/THEN scenarios from change specs
2. Searches for verification markers in codebase:
   - `# Verify: <scenario-id>` in comments
   - `@pytest.mark.scenario("<id>")` in test files
3. Reports covered vs uncovered scenarios
4. Exit code 1 if gaps found

**Skip scenarios:**
Add `<!-- verify-skip: reason -->` before a scenario to exclude it from coverage requirements.

---

### `se3 status` — Session Diagnostics

Diagnoses session state and identifies potential issues.

```bash
# Human-readable diagnostics
se3 status

# JSON output
se3 status --format json
```

**Checks:**
- `status.md` Active Change exists in `openspec/changes/`
- Status field matches Blockers table
- Git working directory has uncommitted changes
- `human-calls/` has pending or unprocessed responses
- Long-pending human calls (stale detection)

**Severity levels:**
- `[ERROR]` — Must fix before proceeding
- `[WARNING]` — Should address soon
- `[INFO]` — For awareness

---

## Integration Workflow

### Pre-commit checks

```bash
se3 lint || exit 1
se3 sync --dry-run | grep -q "in sync" || exit 1
se3 status --format json | jq -e '.errors == 0' || exit 1
```

### Pre-archive verification

```bash
se3 verify --change <change-name>
# Fix any gaps, then archive
openspec archive-change <change-name>
```

---

## Tool Development

Tools are located in `tools/se3_tools/`:

```
tools/
├── pyproject.toml
└── se3_tools/
    ├── cli.py         # Main CLI entry point
    ├── utils.py       # Shared utilities
    └── commands/
        ├── lint.py    # se3 lint
        ├── sync.py    # se3 sync
        ├── verify.py  # se3 verify
        └── status.py  # se3 status
```

**Dependencies:**
- `typer` — CLI framework
- `pyyaml` — YAML parsing

---

## Design Principles

1. **Read-only by default** — Tools that modify files require explicit flags (`--apply`)
2. **Actionable output** — Error messages include file paths and suggestions
3. **Exit codes for automation** — 0=success, 1=issues found, 2=runtime error
4. **Format options** — Human-readable (default) and JSON for scripting
