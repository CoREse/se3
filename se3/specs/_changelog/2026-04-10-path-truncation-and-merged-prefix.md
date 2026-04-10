# File Path Truncation Improvement + Single LLM Call Merged Prefix Removal

**Date:** 2026-04-10

## Summary

Improved file path display in LLM stream output by introducing a dedicated `truncate_path()` function that preserves filename readability. Removed redundant group prefix display when multiple groups are merged into a single LLM call.

## Changes

### 1. Updated: flow-engine

**Location:** `se3/specs/flow-engine/spec.md`

**tool_formatters module — path truncation:**
- Added `truncate_path()` function spec: converts absolute paths to project-relative, applies middle truncation preserving first directory segment and filename, default max length 160 characters
- Added `set_project_root()` / `get_project_root()` module-level management spec
- Clarified that `truncate_preview()` is for non-path text (commands, errors, JSON), while `truncate_path()` is for file paths
- All per-tool formatters (Edit, Write, Read, Grep, Glob) use `truncate_path` for file path arguments

**Multi-group stream prefix — merged group behavior:**
- Changed merged group (LOC threshold) scenario: no longer displays `[G1+G2+G3]` prefix
- Single LLM call (merged groups) now behaves consistently with single-group execution (no prefix)
