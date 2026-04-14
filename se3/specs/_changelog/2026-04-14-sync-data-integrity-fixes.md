# Sync Engine Data Integrity and Idempotency Fixes

**Date:** 2026-04-14

## Summary

Updated specifications for `se3 sync` to reflect fixes for 3 high-severity, 3 medium-severity, and 2 low-severity issues around data integrity, idempotency, and input sanitization in the sync engine.

## Changes

### 1. Updated: se3-commands

**Location:** `se3/specs/se3-commands/spec.md`

**Gap detection idempotency (under "se3 sync" requirement):**
- Updated "Gap detected" scenario to specify normalized matching semantics (lowercase, article removal, punctuation stripping, whitespace collapsing) instead of simple exact title match

**Extension spec update safety:**
- Added markdown fence stripping for LLM responses before writing to spec files

**Issue lifecycle auto-close:**
- Updated "Issue lifecycle" scenario to specify the three-layer matching strategy (normalized match, prefix fallback, close) that prevents false closures
- Added that only gap issues are processed (conflict issues excluded)

**Conflict spec update safety:**
- Added "Conflict spec update safety guards" scenario specifying the < 50% length guard and markdown fence stripping for conflict resolution spec updates

**MCP call file content:**
- Updated "MCP call file generation" scenario to include `spec_content` (truncated to 2000 chars) in the call file output

**Call response validation:**
- Updated "Process call response" scenario to specify that invalid decisions and unknown conflict IDs are skipped

### 2. Updated: issue-discovery

**Location:** `se3/specs/issue-discovery/spec.md`

**Sync gap detection idempotency:**
- Updated "A-class trigger on sync gap detection" scenario to specify normalized matching semantics and exact case-insensitive matching in `find_open_by_title`

**Sync gap auto-close:**
- Updated "A-class trigger on sync gap resolution" scenario to specify the three-layer matching strategy, conflict-tag exclusion, and `close_issue` OSError propagation
