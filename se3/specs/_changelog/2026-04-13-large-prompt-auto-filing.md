# Large Prompt Auto-Filing to Avoid execve() E2BIG

**Date:** 2026-04-13

## Summary

Added automatic filing of large prompt arguments in `ClaudeCodeRunner._resolve_args()` to prevent `OSError: [Errno 7] Argument list too long` when prompt UTF-8 byte length exceeds Linux's `MAX_ARG_STRLEN` (128 KB) limit. Prompts exceeding 100 KB are transparently written to temp files in `se3/tmp/` and passed via `@file` syntax.

## Changes

### 1. Updated: flow-engine

**Location:** `se3/specs/flow-engine/spec.md`

**Large Prompt Auto-Filing (under "步骤内 LLM 调用" requirement):**
- Added "Large Prompt Auto-Filing" specification block documenting the threshold (100 KB / 102,400 bytes), mechanism (`@file` substitution via `_resolve_args()`), and cleanup behavior across all three execution paths (`run()`, `popen()`, `run_with_monitor()`)
- Added scenario: "Large prompt auto-filed to temp file" — prompt exceeds 100 KB, written to `se3/tmp/*.prompt`, argument replaced with `@{path}`
- Added scenario: "Prompt below auto-filing threshold" — prompt passed directly on command line
- Added scenario: "Auto-filing temp file write failure" — orphan cleanup on disk errors
