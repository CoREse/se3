# History Spec Folding and Flow Detail Lookup Fixes

**Date:** 2026-04-16

## Summary

Updated se3-commands spec to reflect two fixes in `se3 history show`: (1) "Project Conventions" segments are now folded like other spec segments, and (2) `show` command performs three-source lookup with prefix matching fallback.

## Changes

### 1. Updated: se3-commands

**Location:** `se3/specs/se3-commands/spec.md`

**Project Conventions spec folding:**
- Added "Project Conventions" to the list of segment titles that trigger spec subsection folding in the `--detailed` view
- Previously only "Relevant Specifications" and "Specifications (for context only)" were listed; implement steps and DAG merge conflict contexts use "Project Conventions" as the heading for embedded spec content

**Flow detail three-source lookup:**
- Updated "Show flow details" scenario to specify that the system searches across three data sources in order: active flow, archived flows, then history-only flows
- Added specification that prefix matching falls back to all flows from all three sources when exact match is not found
- This was already implied by the top-level requirement but not explicitly stated in the show scenario
