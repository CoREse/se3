# TWO_PHASE required_keys Validation and Self Check Renderer Fix

**Date:** 2026-04-16

## Summary

Updated flow-engine spec to reflect two bug fixes: (1) TWO_PHASE fast path now validates `required_keys` before skipping Phase 2, and (2) self_check renderer correctly displays FAILED when the step failed before producing outputs.

## Changes

### 1. Updated: flow-engine

**Location:** `se3/specs/flow-engine/spec.md`

**TWO_PHASE fast path required_keys validation:**
- Added new scenario "TWO_PHASE fast path with required_keys validation" under JSON mode section
- Documents that `LLMCaller.call()` and `_call_two_phase()` accept an optional `required_keys` parameter
- Specifies that the fast path validates parsed JSON against `required_keys` and falls back to Phase 2 if any key is missing
- Specifies that Phase 2 extraction also receives `required_keys` for end-to-end consistency

**Self check renderer FAILED status handling:**
- Updated status line rules to prioritize `step.status == FAILED` over `actionable_count`-based logic
- Added new scenario "Self check step failed rendering" specifying that FAILED steps display `✗ FAILED` regardless of output defaults
