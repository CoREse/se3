# Fix-Loop Implement Iterations in History Display + Self-Check Task Groups Scope Reference

**Date:** 2026-04-17

## Summary

Addressed two orthogonal fix-iteration issues in a single bundle:

1. **History display**: `se3 history show --detailed` was silently hiding multi-iteration implement prompts because `_transition_to_fix` re-uses a single implement Step object (and thus a single `04_implement_{id}.jsonl` file), while test/self_check each iteration gets its own file. The timeline appeared as `test → self_check → test → self_check` with implement invisible.
2. **Self-check scope**: `self_check` had no access to the plan's task deliverables, so its "Functional Gaps" review dimension was judged purely from the original free-text task description.

Both fixes are minimal, reversible, and additive — no persistence, state-machine, file-naming, or step-sequence changes.

## Changes

### 1. Updated: flow-engine

**Location:** `se3/specs/flow-engine/spec.md`

**13 步流程池 step pool table (self_check row):**
- Added `task_groups` to the `self_check` step's input column.

**步骤间输入传递 (input propagation rules):**
- Documented that `self_check` now also receives `task_groups` from `plan` for use as a scope reference in the Functional Gaps dimension.

**New scenario — SELF_CHECK prompt injects plan task_groups as scope reference:**
- When `step.inputs["task_groups"]` is non-empty, the prompt gains a `## Plan Task Groups (Scope Reference)` section summarizing each group's tasks and acceptance criteria.
- Section body is capped at `SELF_CHECK_TASK_GROUPS_MAX_CHARS` (default 2000) via the shared `truncation.py` module.
- Prompt wording explicitly positions task_groups as a **scope reference, NOT a strict specification**; reasonable deviations are NOT issues; self_check MUST NOT flag plan-compliance gaps.
- When `task_groups` is missing/empty/None (e.g., `small`/`bugfix` flows without a plan), the section is omitted entirely — no orphan heading.
- Explicitly locks in that `proposal` and full `design` are intentionally NOT injected (avoid redundancy with `design` itself and preserve the `verify_spec` boundary).

**LLM Content Truncation table:**
- Added row for `self_check prompt task_groups summary` (2000 chars, head).
- Added `SELF_CHECK_TASK_GROUPS_MAX_CHARS` to the list of constants in `truncation.py`.

**Chat history core functions:**
- Added `split_implement_session_by_iterations()` and `interleave_sessions_for_display()` to the `chat_history` public API list.

**New scenario — Fix-loop implement iterations rendered as virtual per-iteration sessions:**
- Documents the render-layer transformation: a single `04_implement_{id}.jsonl` file is partitioned at display time into virtual `-iter{N}` ChatSessions using `test` session first-message timestamps as fences.
- All sessions are stable-sorted by first-message timestamp so the timeline reads `implement-iter1 → test-1 → self_check-1 → implement-iter2 → …`.
- Persistence and file naming are explicitly unchanged — the split lives entirely in `chat_history` and is consumed by both the Rich display (`history_cmd._show_detailed_sessions`) and the programmatic JSON export (`chat_history.get_detailed_json`), keeping `--detailed` and `--detailed --json` consistent.
- Single-iteration implement sessions are returned unchanged (no `-iter1` suffix injected), preserving non-fix-loop rendering exactly as before.

### 2. Updated: se3-commands

**Location:** `se3/specs/se3-commands/spec.md`

**Show flow details with LLM call details scenario:**
- Added clause: sessions are run through `interleave_sessions_for_display()` before rendering so fix-loop implement sessions are split into virtual `-iter{N}` sub-sessions and chronologically interleaved with test/self_check sessions.

**Detailed JSON output scenario:**
- Added clause: the `chat_history` array ordering matches the Rich display path — fix-loop implement sessions appear as `-iter{N}` entries interleaved with test/self_check entries.

## Motivation

### Problem 1 — Invisible implement in fix loops
`_transition_to_fix` (src/se3/engine/state_machine.py:611-613) resets the implement Step's status rather than allocating a new step so that downstream state (changes_made, inputs, fix_history) stays consistent across iterations. The side-effect is that `chat_history.record_prompt()` appends every retry's prompt to the same jsonl file. Users debugging fix loops via `se3 history show --detailed` could not tell whether implement actually ran again, or what the follow-up prompt looked like.

Three classes of fix were considered: (C) new Step per iteration [breaks step-reuse semantics], (C'') split the jsonl at write time [changes persistence, breaks existing histories]. **(C') render-layer virtual split** was chosen as the only non-invasive option — zero migration, backward compatible with already-persisted flows, and equally effective for the Rich view and JSON export.

### Problem 2 — Functional gaps judged without a checklist
The "Functional Gaps" self_check dimension previously had only the free-text `task_description` to cross-reference, so missing deliverables from a multi-task plan could slip through. Plan already produces a structured task_groups list with per-task acceptance criteria; surfacing that as a **scope reference** (explicitly not as a strict spec) lets the LLM check deliverables task-by-task without self_check sliding into plan-conformance auditing — which is `verify_spec`'s job, not `self_check`'s.

`proposal` and full `design` were deliberately excluded: the former is redundant with `design`, and the latter would both explode prompt size and blur the responsibility boundary with `verify_spec`. A 2000-char head-truncation bounds worst-case prompt growth.
