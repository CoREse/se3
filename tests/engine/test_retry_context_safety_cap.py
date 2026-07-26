"""Tests for retry-context safety cap behavior.

Regression coverage for the bug where format_history_for_retry() applied a
per-user-prompt 50K hard truncation *before* deduplicate_prompt_lines() had a
chance to eliminate repeated spec text across attempts.  The fix moves the
cap to a post-dedup whole-prompt fallback.

Scenarios:
(a) Old prompt repeats spec text that also appears in the new prompt — dedup
    should collapse the repetition, and no per-prompt truncation warning
    should fire.
(b) Old prompt contains unique (non-dedup-able) bulk text that exceeds the
    post-dedup safety limit — the fallback cap truncates the history head
    while preserving the new prompt tail, and a warning is emitted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.engine.chat_history import (
    ChatMessage,
    ChatSession,
    format_history_for_retry,
)
from tianluo.engine.llm_caller import _post_dedup_safety_cap
from tianluo.engine.prompt_dedup import deduplicate_prompt_lines
from tianluo.engine.retry_context import (
    POST_DEDUP_SAFETY_LIMIT as _POST_DEDUP_SAFETY_LIMIT,
    RETRY_HISTORY_MARKER as _RETRY_HISTORY_MARKER,
    RETRY_HISTORY_SEPARATOR as _RETRY_HISTORY_SEPARATOR,
)


def _make_session(messages):
    return ChatSession(
        flow_id="test-flow",
        step_id="test-step",
        step_type="implement",
        messages=messages,
    )


def _big_spec_block(tag: str = "shared-spec", n_lines: int = 2000) -> str:
    """Build a deterministic multi-line spec text roughly 130K chars long."""
    lines = [
        f"{tag}: line {i:04d} — this line is long enough to contribute meaningful "
        f"character weight to the total prompt size so dedup has something to chew on."
        for i in range(n_lines)
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scenario (a): dedup eliminates repeated spec text, no per-prompt truncation
# ---------------------------------------------------------------------------

class TestRepeatedSpecNoTruncation:

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_repeated_spec_not_truncated_by_format_history(self, mock_get, caplog):
        spec_text = _big_spec_block("shared-spec")
        assert len(spec_text) > 130_000, "spec fixture must exceed old 50K cap"

        old_prompt = (
            "Please implement feature X.\n\n"
            "## Spec\n"
            f"{spec_text}\n"
            "## End spec\n"
        )
        user_msg = ChatMessage(
            role="user",
            content=old_prompt,
            raw_json=[],
            timestamp="2026-04-20T10:00:00",
            step_type="implement",
            attempt=0,
        )
        mock_get.return_value = _make_session([user_msg])

        with caplog.at_level(logging.WARNING, logger="tianluo.engine.chat_history"):
            retry_context = format_history_for_retry(
                Path("/tmp"), "flow", "step", mode="retry"
            )

        assert retry_context is not None
        # The entire original spec body must still be present — format_history_for_retry
        # no longer truncates individual user prompts.
        assert spec_text in retry_context
        # The old truncation warning must not be emitted.
        assert not any(
            "hit safety limit" in r.message for r in caplog.records
        )

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_dedup_eliminates_repetition_below_safety_limit(self, mock_get, caplog):
        spec_text = _big_spec_block("shared-spec")
        old_prompt = (
            "Please implement feature X.\n\n"
            "## Spec\n"
            f"{spec_text}\n"
            "## End spec\n"
        )
        new_prompt = (
            "Retry with the same task.\n\n"
            "## Spec\n"
            f"{spec_text}\n"
            "## End spec\n"
            "NEW_PROMPT_TAIL_SENTINEL\n"
        )

        user_msg = ChatMessage(
            role="user",
            content=old_prompt,
            raw_json=[],
            timestamp="2026-04-20T10:00:00",
            step_type="implement",
            attempt=0,
        )
        mock_get.return_value = _make_session([user_msg])

        with caplog.at_level(logging.WARNING):
            retry_context = format_history_for_retry(
                Path("/tmp"), "flow", "step", mode="retry"
            )
            assert retry_context is not None
            effective_prompt = f"{retry_context}\n{new_prompt}"
            before_len = len(effective_prompt)
            deduped = deduplicate_prompt_lines(effective_prompt)
            capped = _post_dedup_safety_cap(deduped)

        # Dedup should shrink the prompt substantially (spec text appeared twice,
        # so a large block collapses to a marker line).
        assert len(deduped) < before_len * 0.55
        # Post-dedup length should be well under the safety limit, so the cap
        # should leave the prompt untouched.
        assert len(deduped) < _POST_DEDUP_SAFETY_LIMIT
        assert capped == deduped
        # No per-prompt truncation warning, no post-dedup cap warning.
        assert not any("hit safety limit" in r.message for r in caplog.records)
        assert not any(
            "Post-dedup safety cap triggered" in r.message for r in caplog.records
        )
        # New-prompt tail sentinel survives intact.
        assert "NEW_PROMPT_TAIL_SENTINEL" in capped


# ---------------------------------------------------------------------------
# Scenario (b): dedup cannot collapse; post-dedup cap truncates history head
# ---------------------------------------------------------------------------

class TestPostDedupSafetyCap:

    def test_cap_noop_when_under_limit(self):
        prompt = f"{_RETRY_HISTORY_MARKER}\nsome history\n{_RETRY_HISTORY_SEPARATOR}\ntail"
        assert _post_dedup_safety_cap(prompt) == prompt

    def test_cap_noop_when_no_history_marker(self):
        # Build a prompt larger than the limit but without the retry marker.
        # The cap should refuse to truncate (safety: it's not a retry prompt).
        body = "x" * (_POST_DEDUP_SAFETY_LIMIT + 1000)
        assert _post_dedup_safety_cap(body) == body

    def test_cap_truncates_history_head_preserves_tail(self, caplog):
        # Unique (non-dedup-able) history content — simulate the degenerate
        # case where every old prompt is distinct.  We bypass dedup here and
        # invoke the cap directly, which is what llm_caller does after dedup
        # has had its chance.
        history_body_lines = [
            f"unique-history-line {i:08d} {hex(i * 2654435761 & 0xFFFFFFFF)}"
            for i in range(20_000)
        ]
        history_body = "\n".join(history_body_lines)
        tail = (
            f"{_RETRY_HISTORY_SEPARATOR}\n"
            "[The above attempt(s) failed. Please try again with the same task.]\n\n"
            "## New prompt\n"
            "Please implement feature X.\n"
            "NEW_PROMPT_TAIL_SENTINEL\n"
        )
        effective_prompt = (
            f"{_RETRY_HISTORY_MARKER}\n"
            f"{history_body}\n"
            f"{tail}"
        )
        assert len(effective_prompt) > _POST_DEDUP_SAFETY_LIMIT

        with caplog.at_level(logging.WARNING, logger="tianluo.engine.llm_caller"):
            capped = _post_dedup_safety_cap(effective_prompt)

        # Length is strictly bounded by the cap — the arithmetic is exact
        # (truncated = header + kept_body + tail with
        #  len(kept_body) <= budget = limit - len(header) - len(tail)).
        # Any slack here would mask off-by-N regressions in budget math.
        assert len(capped) <= _POST_DEDUP_SAFETY_LIMIT
        # The new-prompt tail is preserved verbatim.
        assert capped.endswith(tail.split(_RETRY_HISTORY_SEPARATOR, 1)[-1]) or \
            "NEW_PROMPT_TAIL_SENTINEL" in capped
        assert "NEW_PROMPT_TAIL_SENTINEL" in capped
        # The separator — which lives inside the preserved tail — survives.
        assert _RETRY_HISTORY_SEPARATOR in capped
        # Cap emitted a warning with before/after sizes.
        cap_records = [
            r for r in caplog.records
            if "Post-dedup safety cap triggered" in r.message
        ]
        assert cap_records, "expected a post-dedup safety cap warning"

    def test_cap_preserves_tail_even_when_tail_exceeds_limit(self, caplog):
        # Pathological case: the tail itself is bigger than the limit.  Semantic
        # priority: keep the tail intact, accept temporary over-limit size, log.
        tail_body = "T" * (_POST_DEDUP_SAFETY_LIMIT + 2000)
        tail = f"{_RETRY_HISTORY_SEPARATOR}\n{tail_body}\nNEW_PROMPT_TAIL_SENTINEL\n"
        effective_prompt = f"{_RETRY_HISTORY_MARKER}\nshort history\n{tail}"

        with caplog.at_level(logging.WARNING, logger="tianluo.engine.llm_caller"):
            capped = _post_dedup_safety_cap(effective_prompt)

        assert "NEW_PROMPT_TAIL_SENTINEL" in capped
        assert tail_body in capped
        # A distinguishing warning (not the generic "triggered" one) should fire
        # so log-scanners can tell the cap intentionally failed to bound size.
        distinguishing = [
            r for r in caplog.records
            if "could not bound size" in r.message and "tail exceeds limit" in r.message
        ]
        assert distinguishing, "expected a distinguishing tail-exceeds-limit warning"
        # The generic "triggered" message must NOT also fire in this branch —
        # a single, unambiguous log line per cap invocation.
        assert not any(
            "Post-dedup safety cap triggered" in r.message for r in caplog.records
        )

    def test_cap_warns_when_separator_missing(self, caplog):
        """Marker present but separator missing → cap cannot act; must log."""
        # Oversized body with the marker but no separator.
        body = "x" * (_POST_DEDUP_SAFETY_LIMIT + 1000)
        effective_prompt = f"{_RETRY_HISTORY_MARKER}\n{body}"
        assert _RETRY_HISTORY_SEPARATOR not in effective_prompt

        with caplog.at_level(logging.WARNING, logger="tianluo.engine.llm_caller"):
            capped = _post_dedup_safety_cap(effective_prompt)

        # Returns unchanged (cannot act without knowing where tail starts).
        assert capped == effective_prompt
        # But the silent-failure is now observable.
        assert any(
            "marker found but separator missing" in r.message
            for r in caplog.records
        )

    def test_cap_kept_body_starts_at_line_boundary(self):
        """kept_body must start at a newline boundary, not mid-line."""
        # Construct a history whose last `budget` chars would fall mid-line.
        # Each line is long enough that a byte-slice from the end will split
        # one. The slice should round forward to the next '\n'.
        line = "X" * 200  # long lines; any budget < 200 forces a mid-line cut
        n_lines = max(10_000, (_POST_DEDUP_SAFETY_LIMIT // 200) + 500)
        history_body = "\n".join(f"{line}-{i:06d}" for i in range(n_lines))
        tail = (
            f"{_RETRY_HISTORY_SEPARATOR}\n"
            "tail content NEW_PROMPT_TAIL_SENTINEL\n"
        )
        effective_prompt = (
            f"{_RETRY_HISTORY_MARKER}\n"
            f"{history_body}\n"
            f"{tail}"
        )
        assert len(effective_prompt) > _POST_DEDUP_SAFETY_LIMIT

        capped = _post_dedup_safety_cap(effective_prompt)

        # Find where kept_body actually starts (right after the header line).
        header_tail = "[... retry history truncated (head) to stay under safety limit ...]\n"
        idx = capped.find(header_tail)
        assert idx >= 0
        kept_start = idx + len(header_tail)
        # The first character of kept_body must be the beginning of a line —
        # i.e. the prior character is '\n' — not a truncated fragment like
        # the middle of an 'XXXX-000123' token.
        first_line = capped[kept_start:].split("\n", 1)[0]
        # A full line always starts with 'X' * 200 followed by '-NNNNNN',
        # never a partial segment of X's.
        assert first_line.startswith("X" * 200) or first_line == "", \
            f"kept_body started mid-line: {first_line[:60]!r}"


# ---------------------------------------------------------------------------
# Scenario (b2): retry-of-retry chain — cap anchors on OUTER separator
# ---------------------------------------------------------------------------

class TestRetryOfRetryAnchoring:
    """When a prior retry's full effective_prompt (which itself contained a
    marker+separator block) is stored as a user message and replayed verbatim
    by format_history_for_retry() on the next retry, the post-dedup safety
    cap MUST anchor on the OUTER separator — the inner one must not outrank
    it.  Regression guard for the ``find`` → ``rfind`` fix and for any future
    change to the sentinel.  Without this coverage, a regression in either
    dimension would pass CI silently.
    """

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_cap_anchors_outer_separator_on_retry_of_retry(self, mock_get, caplog):
        # (i) simulate attempt 0 producing an effective_prompt that already
        #     contains a marker+separator pair (as would happen on any retry
        #     from attempt ≥ 2 in the real flow). Include large non-dedup-able
        #     bulk BETWEEN the inner marker and inner separator so the outer
        #     retry-context eventually exceeds the safety limit when the whole
        #     block is replayed verbatim under ``[User Prompt]:``.
        bulk_lines = [
            f"BULK-UNIQUE-LINE-{i:08d}-" + ("Z" * 50)
            for i in range(8_000)
        ]
        attempt0_effective = (
            f"{_RETRY_HISTORY_MARKER}\n"
            "=== Attempt 1 ===\n"
            "[User Prompt]:\n"
            "earlier attempt content\n"
            + "\n".join(bulk_lines) + "\n"
            + f"{_RETRY_HISTORY_SEPARATOR}\n"
            "[The above attempt(s) failed. Please try again with the same task.]\n"
            "original user prompt body\n"
        )
        # (ii) record it as a user ChatMessage for attempt 0
        user_msg = ChatMessage(
            role="user",
            content=attempt0_effective,
            raw_json=[],
            timestamp="2026-04-20T10:00:00",
            step_type="implement",
            attempt=0,
        )
        mock_get.return_value = _make_session([user_msg])

        # (iii) invoke format_history_for_retry for the next attempt
        retry_context = format_history_for_retry(
            Path("/tmp"), "flow", "step", mode="retry"
        )
        assert retry_context is not None
        # Sanity: both the inner (from stored user msg) AND the outer sentinel
        # exist in the retry_context.
        assert retry_context.count(_RETRY_HISTORY_SEPARATOR) >= 2, \
            "fixture must contain both inner and outer separators"

        # Build effective_prompt with a tail marker we can check for survival.
        new_prompt = "## New retry prompt\nNEW_PROMPT_TAIL_SENTINEL\n"
        effective_prompt = f"{retry_context}\n{new_prompt}"
        assert len(effective_prompt) > _POST_DEDUP_SAFETY_LIMIT, \
            "fixture must exceed the cap to actually exercise it"

        # (iv) run dedup + cap
        with caplog.at_level(logging.WARNING, logger="tianluo.engine.llm_caller"):
            deduped = deduplicate_prompt_lines(effective_prompt)
            capped = _post_dedup_safety_cap(deduped)

        # The cap must resolve to the OUTER separator, so the new-prompt tail
        # (which lives AFTER the outer separator in the deduped prompt)
        # survives intact.
        assert "NEW_PROMPT_TAIL_SENTINEL" in capped, \
            "new prompt tail must survive — cap anchored on wrong separator"

        # The cap output must still satisfy the marker/separator invariant so
        # that a subsequent retry-of-retry-of-retry can anchor correctly.
        assert _RETRY_HISTORY_MARKER in capped
        assert _RETRY_HISTORY_SEPARATOR in capped

        # Length is bounded by the cap (budget > 0 path).
        assert len(capped) <= _POST_DEDUP_SAFETY_LIMIT


# ---------------------------------------------------------------------------
# Scenario (c): integration — _call_with_retry wiring actually invokes the cap
# ---------------------------------------------------------------------------

class TestCallWithRetryInvokesSafetyCap:
    """End-to-end: on a retry path, _post_dedup_safety_cap is called after
    deduplicate_prompt_lines, so the wiring at llm_caller.py's retry branch
    cannot be silently removed without breaking this test.
    """

    def test_safety_cap_called_on_retry_path(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from tianluo.engine.llm_caller import LLMCaller

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        # Force retry path: external_attempt > 0 triggers is_retry = True.
        caller.external_attempt = 1

        spy = MagicMock(side_effect=lambda p, limit=_POST_DEDUP_SAFETY_LIMIT: p)

        # Realistic retry context: contains both the marker AND the separator,
        # matching what format_history_for_retry() actually emits. Using the
        # literal 'fake retry context' would let a regression that drops the
        # retry-history block or violates the marker/separator contract pass.
        realistic_retry_context = (
            f"{_RETRY_HISTORY_MARKER}\n"
            "=== Attempt 1 ===\n"
            "[User Prompt]:\n"
            "please implement feature X\n"
            "[Assistant Response]:\n"
            "partial work\n"
            f"{_RETRY_HISTORY_SEPARATOR}\n"
            "[The above attempt(s) failed. Please try again with the same task.]\n"
        )

        with patch("tianluo.engine.llm_caller._post_dedup_safety_cap", spy), \
             patch.object(caller, "_get_current_runner") as mock_get_runner, \
             patch.object(caller, "_get_retry_context", return_value=realistic_retry_context):
            runner_inst = MagicMock()
            result_obj = MagicMock()
            result_obj.success = True
            result_obj.output = "ok"
            result_obj.interrupted = False
            runner_inst.run_with_monitor.return_value = result_obj
            mock_get_runner.return_value = runner_inst

            caller._call_with_retry(
                prompt="retry prompt",
                timeout=30,
                context_files=None,
                on_output=lambda line: None,
                require_json=False,
                json_retry_count=0,
            )

        spy.assert_called_once()
        # The cap must see an effective_prompt that carries BOTH the marker
        # and the separator — asserting this pins the marker/separator
        # contract at the _call_with_retry → cap boundary.
        passed_prompt = spy.call_args[0][0]
        assert _RETRY_HISTORY_MARKER in passed_prompt
        assert _RETRY_HISTORY_SEPARATOR in passed_prompt

    def test_safety_cap_not_called_on_first_call(self, tmp_path):
        """No retry → cap is not invoked (dedup also skipped)."""
        from unittest.mock import MagicMock, patch

        from tianluo.engine.llm_caller import LLMCaller

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        # external_attempt == 0, internal_attempt == 0 → first call

        spy = MagicMock(side_effect=lambda p, limit=_POST_DEDUP_SAFETY_LIMIT: p)

        with patch("tianluo.engine.llm_caller._post_dedup_safety_cap", spy), \
             patch.object(caller, "_get_current_runner") as mock_get_runner:
            runner_inst = MagicMock()
            result_obj = MagicMock()
            result_obj.success = True
            result_obj.output = "ok"
            result_obj.interrupted = False
            runner_inst.run_with_monitor.return_value = result_obj
            mock_get_runner.return_value = runner_inst

            caller._call_with_retry(
                prompt="first call",
                timeout=30,
                context_files=None,
                on_output=lambda line: None,
                require_json=False,
                json_retry_count=0,
            )

        spy.assert_not_called()


# ---------------------------------------------------------------------------
# Dedup-time literal \n preprocessing
# ---------------------------------------------------------------------------

class TestDedupLiteralNewlinePreprocessing:
    """In retry-context, JSON-encoded tool_result previews stash multi-line
    file content as single huge "lines" with embedded literal ``\\n`` (two
    chars: backslash + n). Without preprocessing, ``str.split("\\n")`` in
    deduplicate_prompt_lines treats each as one line and dedup misses
    massive repetition. The fix in ``LLMCaller._call_with_retry`` does
    ``effective_prompt.replace('\\n', '\n')`` before dedup so the embedded
    content gets sliced into real lines first.
    """

    def test_replace_lets_dedup_collapse_embedded_blocks(self):
        """Two JSON-encoded tool_results carrying identical embedded multi-line
        content should collapse to a single dedup marker after the
        backslash-n preprocessing."""
        # Embedded payload: ten identical-looking lines (sufficient to clear
        # min_block_lines=3 with margin).
        embedded = "\\n".join([f"line{i}: shared embedded content" for i in range(10)])
        prompt = (
            "## Header\n"
            "first\n"
            f'tool_result_a: {{"content":"{embedded}"}}\n'
            "second\n"
            f'tool_result_b: {{"content":"{embedded}"}}\n'
            "third\n"
        )
        # Without preprocessing, dedup sees two single huge "lines" — no
        # match (min_block_lines == 3 requires multi-line repetition).
        before = deduplicate_prompt_lines(prompt)
        assert "DUPLICATED CONTENT" not in before, (
            "expected dedup to MISS without \\n preprocessing"
        )

        # After preprocessing, each tool_result's payload becomes ten real
        # lines, and the second occurrence collapses to a marker.
        preprocessed = prompt.replace('\\n', '\n')
        after = deduplicate_prompt_lines(preprocessed)
        assert "DUPLICATED CONTENT" in after, (
            "expected dedup to HIT after \\n preprocessing"
        )

    def test_replace_does_not_touch_real_newlines(self):
        """Real newlines must remain real newlines — the replace is idempotent
        on a string that has no literal \\n escape."""
        s = "line1\nline2\nline3"
        assert s.replace('\\n', '\n') == s

    def test_replace_does_not_touch_other_escapes(self):
        """Only ``\\n`` is converted; ``\\t`` / ``\\\\`` / ``\\\"`` left alone
        so legitimate code samples in retry-context (e.g. a Python string
        literal containing ``\\t``) are not garbled."""
        s = r"keep \t and \\ and \" intact"
        result = s.replace('\\n', '\n')
        assert result == s
