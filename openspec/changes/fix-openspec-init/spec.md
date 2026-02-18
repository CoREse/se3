# Fix OpenSpec Init and Detection Logic

## Problem

The OpenSpec initialization and detection logic had several issues:

1. **Incorrect detection**: `check_openspec()` in `start.py` was checking for a standalone `openspec` CLI but had inconsistent behavior

2. **Embedded openspec implementation**: The project had its own `tools/se3_tools/commands/openspec.py` implementing openspec functionality, instead of using the system-installed `openspec` package

3. **Unnecessary duplication**: The system already has `openspec` installed with full functionality, but the project was duplicating this functionality

## Solution

1. **Deleted embedded openspec module**:
   - Removed `tools/se3_tools/commands/openspec.py`
   - Removed `tests/test_openspec.py`
   - Removed `output/commands/openspec/` directory

2. **Updated detection logic** in `start.py`:
   - `check_openspec()` now uses `which openspec` to detect system openspec
   - Initialization check only looks at `openspec/` directory structure

3. **Updated `se3 init` command**:
   - Now calls external `openspec init` command instead of using embedded implementation
   - Removed creation of `openspec/` directories (handled by external openspec)

4. **Updated CLI** (`cli.py`):
   - Removed `app.add_typer(openspec.app, ...)` line
   - Removed import of deleted module

## Acceptance Criteria

- [x] `se3 start` correctly detects system openspec availability via `which openspec`
- [x] `se3 init` calls external `openspec init` command
- [x] All tests pass (207 passed)
- [x] No embedded openspec implementation remains
