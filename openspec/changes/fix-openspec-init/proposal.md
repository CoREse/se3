# Fix OpenSpec Init and Detection Logic

## Problem

The OpenSpec initialization and detection logic has several issues:

1. **Incorrect detection**: `check_openspec()` in `start.py` checks for standalone `openspec` CLI via `which openspec`, but OpenSpec is actually a subcommand of `se3` (`se3 openspec`)

2. **Missing `.claude/commands/openspec/`**: After `se3 init` or `openspec init`, the `.claude/commands/openspec/` directory is not created, so there are no OpenSpec commands available

3. **Inconsistent initialization**: `openspec init` only creates `openspec/` directory structure but doesn't set up the `.claude/` framework files

## Solution

1. Fix `check_openspec()` to detect `se3 openspec` subcommand availability instead of standalone `openspec` CLI

2. Create `output/commands/openspec/` directory with command definitions:
   - `init.md` - OpenSpec init command
   - `list.md` - OpenSpec list command
   - `archive.md` - OpenSpec archive command

3. Update `openspec init` to also install OpenSpec commands to `.claude/commands/openspec/`

4. Update `se3 init` to also install OpenSpec commands alongside SE3 commands

## Acceptance Criteria

- [ ] `se3 start` correctly detects OpenSpec availability via `se3 openspec --help`
- [ ] `se3 init` creates `.claude/commands/openspec/` with command files
- [ ] `openspec init` creates both `openspec/` directory and `.claude/commands/openspec/`
- [ ] All OpenSpec commands (init, list, archive) are available after initialization
