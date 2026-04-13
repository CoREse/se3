# LLM Content Truncation Fixes and Fix History Restructure

**Date:** 2026-04-13

## Summary

Fixed multiple truncation limits that impaired LLM diagnostic capability, restructured fix_history to use structured issues instead of truncated text, centralized truncation constants into a shared module, and fixed a field name mismatch in version_analyze.

## Changes

### 1. Updated: flow-engine

**Location:** `se3/specs/flow-engine/spec.md`

**LLM Content Truncation Strategy table:**
- Raised `LLM prompt test results (verify_spec, self_check) | stderr per phase` from 1500 to 2000
- Raised `LLM prompt test results (verify_spec, self_check) | stdout per phase` from 1000 to 2000
- Raised `Test history record | stderr per phase` from 1000 to 2000
- Raised `Test history record | stdout per phase` from 1000 to 2000
- Raised `Loop iteration summaries | accumulated total` from 4000 to 8000

**Shared Truncation Constants Module:**
- Added specification for `truncation.py` module that centralizes truncation limits as named constants (`PHASE_STDOUT_TAIL_CHARS`, `PHASE_STDERR_TAIL_CHARS`, `TEST_HISTORY_STDOUT_TAIL_CHARS`, `TEST_HISTORY_STDERR_TAIL_CHARS`, `FIX_STDERR_TAIL_CHARS`, `FAILURES_SECTION_MAX_CHARS`)
- Added scenario: "Truncation constants are centralized"

**Fix History Structure (under Test step configuration):**
- Added "Fix History Structure" subsection documenting the new structured fix_history schema
- Documented `_normalize_issue_fields` function that normalizes `severity`/`priority` across self_check and verify_spec issues
- Documented source-aware storage policy: test.py raw output is not stored in fix_history (only trigger reason), verify_spec LLM analysis is preserved via structured issues
- Documented `_format_fix_history` rendering: shows up to 5 issues per iteration with severity/description/location
- Added scenario: "Fix history stores structured issues"
- Added scenario: "Fix history prev_issues cap aligned at 20"

**Version Analyze step:**
- Added "Verification result formatting" section specifying that all issues are included (no display cap) and the `priority` field is used (not `severity`)
- Added scenario: "Version analyze shows all verification issues"
