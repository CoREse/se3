## Tasks

- [x] 1. Analyze the bug: Understand collab mode detection logic
- [x] 2. Identify root cause: .collab/config.json check is wrong
- [x] 3. Fix done.py: Remove config.json check from is_in_collab_mode
- [x] 4. Fix handoff.py: Remove config.json check from is_in_collab_mode
- [x] 5. Run tests to verify fix (219 passed)
- [x] 6. Archive change

## Summary

Fixed `is_in_collab_mode()` in both `done.py` and `handoff.py`:

**Before:**
```python
def is_in_collab_mode(project_root: Path) -> bool:
    if os.environ.get("SE3_AGENT_ROLE"):
        return True
    # WRONG: This makes interactive sessions act as collab agents
    if (project_root / ".collab" / "config.json").exists():
        return True
    return False
```

**After:**
```python
def is_in_collab_mode(project_root: Path) -> bool:
    """Only True if SE3_AGENT_ROLE is set (process spawned by orchestrator)."""
    return bool(os.environ.get("SE3_AGENT_ROLE"))
```

**Impact:**
- Interactive Claude Code sessions now correctly use standard shutdown protocol
- Only actual collab work agents (with SE3_AGENT_ROLE) use collab handoff
