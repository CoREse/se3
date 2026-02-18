# Change Status: fix-openspec-init

## Summary
Fixed OpenSpec detection logic to use system-installed openspec package instead of embedded implementation.

## Implementation

### Files Deleted
- `tools/se3_tools/commands/openspec.py` - Embedded openspec implementation
- `tests/test_openspec.py` - Tests for deleted module
- `output/commands/openspec/` - Embedded command definitions

### Files Modified
- `tools/se3_tools/cli.py` - Removed openspec import and registration
- `tools/se3_tools/commands/start.py` - Fixed check_openspec() to use system openspec
- `tools/se3_tools/commands/init.py` - Call external openspec init

## Verification
- All 207 tests pass
- `se3 start` correctly detects system openspec
- `se3 init` calls external openspec command
