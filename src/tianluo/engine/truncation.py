"""Shared truncation constants for LLM-consumed content.

Centralizes the truncation limits used across step handlers (test,
self_check, verify_spec) so they stay consistent and are easy to
adjust in one place.  Values meet or exceed the minimums defined in
the flow-engine spec's "LLM Content Truncation Strategy" table.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-phase stdout / stderr tail-truncation for LLM prompts
# Used by: self_check._format_test_results, verify_spec._format_test_results
# Spec minimums: stdout 1000, stderr 1500  →  using 2000 for both
# ---------------------------------------------------------------------------
PHASE_STDOUT_TAIL_CHARS = 2000
PHASE_STDERR_TAIL_CHARS = 2000

# ---------------------------------------------------------------------------
# Test history recording — truncation for persisted phase output
# Used by: test._record_test_history
# Spec minimums: stdout 1000, stderr 1000  →  using 2000 for both
# ---------------------------------------------------------------------------
TEST_HISTORY_STDOUT_TAIL_CHARS = 2000
TEST_HISTORY_STDERR_TAIL_CHARS = 2000

# ---------------------------------------------------------------------------
# Test history archive slimming — passed-phase stored stdout/stderr tail
# Used by: test.run_and_classify_tests (archive slimming of the stored
#   test_results copy for PASSED phases)
# A passed phase's full ``pytest -v`` stdout is pure noise in the archived
# history jsonl (every line is a PASSED line). The STORED copy is replaced with
# a compact pass/fail count summary plus this tail of the output (enough to keep
# the final ``=== N passed in Ts ===`` summary line); failed phases keep their
# full stdout. Shares the same numeric floor as the other test-history limits.
# ---------------------------------------------------------------------------
TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS = 2000

# ---------------------------------------------------------------------------
# Fix instructions — stderr tail-truncation
# Used by: test.test_handler, verify_spec.verify_spec_handler
# Spec minimum: 2000
# ---------------------------------------------------------------------------
FIX_STDERR_TAIL_CHARS = 2000

# ---------------------------------------------------------------------------
# Failures section smart extraction max chars
# Used by: test._extract_failures_section, verify_spec default fix instructions
# Spec minimum: 3000
# ---------------------------------------------------------------------------
FAILURES_SECTION_MAX_CHARS = 3000

# ---------------------------------------------------------------------------
# self_check — task_groups summary max chars
# Used by: self_check._format_task_groups
# Bounds the plan task_groups reference section injected into the LLM prompt
# so long plans don't balloon prompt size.
# ---------------------------------------------------------------------------
SELF_CHECK_TASK_GROUPS_MAX_CHARS = 2000

# ---------------------------------------------------------------------------
# self_check — baseline-to-current scope diff max chars injected in prompt
# Used by: self_check._format_review_scope
# Bounds the reconstructed review-scope diff so a large vendored/generated
# file cannot blow the model context window; the complete diff stays
# available at the persisted artifact referenced by the prompt.
# ---------------------------------------------------------------------------
SELF_CHECK_SCOPE_DIFF_MAX_CHARS = 20000

# ---------------------------------------------------------------------------
# Web tool-chip detail payload max chars
# Used by: tool_formatters.build_tool_detail_payload
# Bounds the structured detail body (diff / read text / bash output / matches)
# shipped via stream_progress to the web console; oversized bodies are
# tail-truncated and flagged with `truncated: true` so the frontend can render
# a "... more truncated" hint instead of dropping the chip.
# ---------------------------------------------------------------------------
TOOL_DETAIL_PAYLOAD_MAX_CHARS = 20000
