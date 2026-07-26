"""Tests for prompt_dedup module."""

import hashlib

import pytest

from tianluo.engine.prompt_dedup import deduplicate_prompt_lines


def _expected_marker(block_lines: list[str]) -> str:
    """Compute the expected dedup marker for a block of lines."""
    n = len(block_lines)
    block_content = "\n".join(block_lines)
    content_hash = hashlib.sha256(block_content.encode()).hexdigest()[:8]
    first = block_lines[0].strip()[:80]
    last = block_lines[-1].strip()[:80]
    return f'[DUPLICATED CONTENT: {n} lines #{content_hash}, from "{first}" to "{last}"]'


class TestDeduplicatePromptLines:
    """Tests for deduplicate_prompt_lines()."""

    def test_no_duplicates_returns_unchanged(self):
        """Prompt with no repeated blocks should be returned unchanged."""
        prompt = "line 1\nline 2\nline 3\nline 4\nline 5"
        assert deduplicate_prompt_lines(prompt) == prompt

    def test_empty_input(self):
        """Empty string should be returned as-is."""
        assert deduplicate_prompt_lines("") == ""

    def test_single_line(self):
        """Single-line prompt should be returned unchanged."""
        assert deduplicate_prompt_lines("hello") == "hello"

    def test_two_lines(self):
        """Two-line prompt (below min_block_lines) should be returned unchanged."""
        prompt = "a\nb"
        assert deduplicate_prompt_lines(prompt) == prompt

    def test_single_duplicate_block(self):
        """A single repeated block of 3+ lines should be deduplicated."""
        prompt = "\n".join([
            "header",
            "spec line A",
            "spec line B",
            "spec line C",
            "middle",
            "spec line A",
            "spec line B",
            "spec line C",
            "footer",
        ])
        result = deduplicate_prompt_lines(prompt)
        # First occurrence kept
        assert "spec line A" in result
        assert "spec line B" in result
        assert "spec line C" in result
        # Second occurrence replaced with content-based marker (includes hash)
        assert _expected_marker(["spec line A", "spec line B", "spec line C"]) in result
        # Surrounding content preserved
        assert "header" in result
        assert "middle" in result
        assert "footer" in result

    def test_multiple_duplicate_blocks(self):
        """Multiple different repeated blocks should each be deduplicated."""
        prompt = "\n".join([
            "block A line 1",
            "block A line 2",
            "block A line 3",
            "separator 1",
            "block B line 1",
            "block B line 2",
            "block B line 3",
            "separator 2",
            "block A line 1",
            "block A line 2",
            "block A line 3",
            "separator 3",
            "block B line 1",
            "block B line 2",
            "block B line 3",
        ])
        result = deduplicate_prompt_lines(prompt)
        # Both blocks' second occurrences replaced with content-based markers (includes hash)
        assert _expected_marker(["block A line 1", "block A line 2", "block A line 3"]) in result
        assert _expected_marker(["block B line 1", "block B line 2", "block B line 3"]) in result
        # Separators preserved
        assert "separator 1" in result
        assert "separator 2" in result
        assert "separator 3" in result

    def test_short_repeat_not_deduplicated(self):
        """Blocks shorter than min_block_lines should not be deduplicated."""
        prompt = "\n".join([
            "line A",
            "line B",
            "middle",
            "line A",
            "line B",
        ])
        result = deduplicate_prompt_lines(prompt)
        # Both occurrences should remain (only 2 lines, default min is 3)
        lines = result.split("\n")
        assert lines.count("line A") == 2
        assert lines.count("line B") == 2
        assert "DUPLICATED CONTENT" not in result

    def test_custom_min_block_lines(self):
        """Custom min_block_lines=2 should dedup blocks of 2."""
        prompt = "\n".join([
            "line A",
            "line B",
            "middle",
            "line A",
            "line B",
        ])
        result = deduplicate_prompt_lines(prompt, min_block_lines=2)
        assert _expected_marker(["line A", "line B"]) in result

    def test_custom_min_block_lines_larger(self):
        """With min_block_lines=5, a 3-line repeat should not be deduplicated."""
        prompt = "\n".join([
            "a", "b", "c",
            "x",
            "a", "b", "c",
        ])
        result = deduplicate_prompt_lines(prompt, min_block_lines=5)
        assert "DUPLICATED CONTENT" not in result

    def test_triple_repeat(self):
        """A block repeated 3 times: second and third occurrences replaced."""
        block = ["spec 1", "spec 2", "spec 3"]
        prompt = "\n".join(block + ["---"] + block + ["---"] + block)
        result = deduplicate_prompt_lines(prompt)
        lines = result.split("\n")
        # First occurrence preserved
        assert lines[0] == "spec 1"
        assert lines[1] == "spec 2"
        assert lines[2] == "spec 3"
        # Second and third replaced
        marker_count = sum(1 for l in lines if "DUPLICATED CONTENT" in l)
        assert marker_count == 2

    def test_content_based_marker_correct(self):
        """Marker should reference correct first/last lines of the duplicated block."""
        prompt = "\n".join([
            "line 1",       # line 1
            "line 2",       # line 2
            "dup A",        # line 3
            "dup B",        # line 4
            "dup C",        # line 5
            "line 6",       # line 6
            "dup A",        # line 7 -> should reference 3-5
            "dup B",        # line 8
            "dup C",        # line 9
            "line 10",      # line 10
        ])
        result = deduplicate_prompt_lines(prompt)
        assert _expected_marker(["dup A", "dup B", "dup C"]) in result

    def test_adjacent_duplicate_blocks(self):
        """Adjacent duplicate blocks (no separator) should be deduplicated."""
        prompt = "\n".join([
            "aaa",
            "bbb",
            "ccc",
            "aaa",
            "bbb",
            "ccc",
        ])
        result = deduplicate_prompt_lines(prompt)
        assert _expected_marker(["aaa", "bbb", "ccc"]) in result
        # Only 4 lines: 3 original + 1 marker
        lines = result.split("\n")
        assert len(lines) == 4

    def test_preserves_blank_lines(self):
        """Blank lines within non-duplicate content should be preserved."""
        prompt = "\n".join([
            "header",
            "",
            "content",
            "",
            "footer",
        ])
        result = deduplicate_prompt_lines(prompt)
        assert result == prompt

    def test_first_call_noop(self):
        """For a prompt with no internal repetition (typical first call), no changes."""
        prompt = (
            "You are an expert software engineer.\n"
            "## Task\n"
            "Implement feature X.\n"
            "## Context\n"
            "The project uses Python 3.8+.\n"
            "## Instructions\n"
            "Follow PEP 8 conventions."
        )
        assert deduplicate_prompt_lines(prompt) == prompt

    def test_realistic_retry_prompt(self):
        """Simulate a retry prompt with repeated spec content."""
        spec = [
            "## Specification",
            "The system SHALL support user authentication.",
            "The system SHALL validate input parameters.",
            "The system SHALL log all API calls.",
        ]
        attempt1_prompt = ["Task: implement auth"] + spec + ["Begin implementation."]
        attempt2_prompt = ["Previous attempt failed."] + spec + ["Try again."]
        combined = attempt1_prompt + ["---"] + attempt2_prompt
        prompt = "\n".join(combined)
        result = deduplicate_prompt_lines(prompt)
        # The spec block should appear once fully and once as a marker
        assert result.count("The system SHALL support user authentication.") == 1
        assert "DUPLICATED CONTENT" in result

    def test_min_block_lines_one(self):
        """min_block_lines=1 should deduplicate single repeated lines."""
        prompt = "\n".join([
            "unique header",
            "repeated line",
            "unique middle",
            "repeated line",
            "unique footer",
        ])
        result = deduplicate_prompt_lines(prompt, min_block_lines=1)
        assert _expected_marker(["repeated line"]) in result
        # The standalone "repeated line" should appear only once (the marker contains it in quotes)
        lines = result.split("\n")
        standalone_count = sum(1 for l in lines if l == "repeated line")
        assert standalone_count == 1

    def test_large_prompt_performance(self):
        """Dedup on a 5000-line prompt should complete in reasonable time."""
        import time
        # Build a 5000-line prompt with a 100-line spec block repeated 5 times
        spec_block = [f"spec line {i}" for i in range(100)]
        unique_parts = []
        for attempt in range(5):
            unique_parts.append(f"=== Attempt {attempt} ===")
            unique_parts.extend(spec_block)
            unique_parts.extend([f"unique work line {attempt}-{i}" for i in range(900)])
        prompt = "\n".join(unique_parts)
        assert len(prompt.split("\n")) == 5005  # 5 * (1 + 100 + 900)

        start = time.time()
        result = deduplicate_prompt_lines(prompt)
        elapsed = time.time() - start

        # Should complete well under 5 seconds even on slow CI
        assert elapsed < 5.0, f"Dedup took {elapsed:.2f}s on 5000-line prompt"
        # First spec block kept, other 4 replaced
        markers = [l for l in result.split("\n") if "DUPLICATED CONTENT" in l]
        assert len(markers) == 4

    def test_dedup_with_retry_context_structure(self):
        """End-to-end test: dedup works correctly when format_history_for_retry
        output is combined with the original prompt (simulating _call_with_retry)."""
        spec = [
            "## Specification",
            "The system SHALL support user authentication.",
            "The system SHALL validate input parameters.",
            "The system SHALL log all API calls.",
            "The system SHALL handle errors gracefully.",
        ]
        original_prompt = "\n".join(
            ["You are an expert.", "## Task", "Implement auth."] + spec + ["Begin."]
        )
        # Simulate retry context (format_history_for_retry output)
        retry_context = "\n".join(
            ["[Previous conversation context for this step]:",
             "\n=== Attempt 1 ===",
             "\n[User Prompt]:"]
            + [original_prompt]
            + ["\n[Assistant Response]:",
               "I started implementing auth but hit an error.",
               "\n[The above attempt(s) failed.]"]
        )
        # _call_with_retry combines: retry_context + "\n" + original_prompt
        effective_prompt = f"{retry_context}\n{original_prompt}"

        result = deduplicate_prompt_lines(effective_prompt)
        # The spec block should appear once fully and once as a marker
        assert result.count("The system SHALL support user authentication.") == 1
        assert "DUPLICATED CONTENT" in result
        # The unique parts should still be present
        assert "Implement auth." in result
        assert "I started implementing auth" in result

    def test_existing_marker_not_deduplicated(self):
        """Lines that are existing dedup markers should not participate in dedup."""
        prompt = "\n".join([
            "line A",
            "line B",
            "line C",
            '[DUPLICATED CONTENT: 3 lines, from "line A" to "line C"]',
            "line A",
            "line B",
            "line C",
        ])
        result = deduplicate_prompt_lines(prompt)
        # The marker from previous dedup should be preserved as-is
        assert '[DUPLICATED CONTENT: 3 lines, from "line A" to "line C"]' in result
        # The second occurrence of the block should be deduplicated
        # Check standalone lines (not inside markers)
        lines = result.split("\n")
        standalone_a_count = sum(1 for l in lines if l == "line A")
        assert standalone_a_count == 1
        # There should be a new marker for the second block + the original marker
        markers = [l for l in lines if "DUPLICATED CONTENT" in l]
        assert len(markers) == 2  # original marker + new dedup marker

    def test_blank_line_blocks_not_deduplicated(self):
        """Blocks consisting entirely of blank lines should not be deduplicated."""
        prompt = "\n".join([
            "header",
            "",
            "",
            "",
            "middle",
            "",
            "",
            "",
            "footer",
        ])
        result = deduplicate_prompt_lines(prompt)
        assert "DUPLICATED CONTENT" not in result
        # All blank lines should be preserved
        assert result.count("") >= 6  # 6 blank lines in the original

    def test_min_block_lines_zero_returns_unchanged(self):
        """min_block_lines=0 should return the prompt unchanged (guard against IndexError)."""
        prompt = "line A\nline B\nline A\nline B"
        assert deduplicate_prompt_lines(prompt, min_block_lines=0) == prompt

    def test_min_block_lines_negative_returns_unchanged(self):
        """Negative min_block_lines should return the prompt unchanged."""
        prompt = "line A\nline B\nline A\nline B"
        assert deduplicate_prompt_lines(prompt, min_block_lines=-1) == prompt

    def test_long_line_truncated_in_marker(self):
        """When first/last lines of a block exceed 80 chars, marker truncates them."""
        long_line_a = "A" * 120
        long_line_c = "C" * 120
        prompt = "\n".join([
            long_line_a,
            "short middle",
            long_line_c,
            "separator",
            long_line_a,
            "short middle",
            long_line_c,
        ])
        result = deduplicate_prompt_lines(prompt)
        # The marker should contain truncated versions (first 80 chars) and a content hash
        expected_marker = _expected_marker([long_line_a, "short middle", long_line_c])
        assert expected_marker in result
        # Full 120-char lines should NOT appear in the marker
        assert f'from "{long_line_a}"' not in result

    def test_multi_retry_cross_pass_with_existing_markers(self):
        """Multi-retry scenario: second retry's prompt already has markers from
        the first dedup pass, and a third dedup pass must handle both markers
        and fresh duplicate content correctly."""
        spec = [
            "## Specification",
            "The system SHALL authenticate users.",
            "The system SHALL validate tokens.",
            "The system SHALL log access.",
        ]

        # --- Simulate first retry ---
        # Attempt 1 prompt + Attempt 2 prompt (both contain spec)
        attempt1_prompt = "\n".join(
            ["Task: implement auth"] + spec + ["Attempt 1 work."]
        )
        retry1_context = "\n".join(
            ["[Previous context]:", attempt1_prompt, "[Failed.]"]
        )
        effective_prompt_retry1 = f"{retry1_context}\n{attempt1_prompt}"
        # First dedup pass: spec appears twice, second occurrence becomes a marker
        after_pass1 = deduplicate_prompt_lines(effective_prompt_retry1)
        assert "DUPLICATED CONTENT" in after_pass1
        # Spec text should appear only once
        assert after_pass1.count("The system SHALL authenticate users.") == 1

        # --- Simulate second retry ---
        # The retry context now contains the already-deduped output from pass1,
        # and we append a fresh prompt that again contains the spec.
        retry2_context = "\n".join(
            ["[Previous context]:", after_pass1, "[Failed again.]"]
        )
        fresh_prompt = "\n".join(
            ["Task: implement auth"] + spec + ["Attempt 3 work."]
        )
        effective_prompt_retry2 = f"{retry2_context}\n{fresh_prompt}"

        # Second dedup pass must handle:
        # 1. Existing markers from pass1 (should be preserved, not matched)
        # 2. Fresh spec content that duplicates the original occurrence
        after_pass2 = deduplicate_prompt_lines(effective_prompt_retry2)

        # The spec text should still appear only once (the original)
        assert after_pass2.count("The system SHALL authenticate users.") == 1
        # There should be at least 2 markers (one from pass1, one new from pass2)
        markers = [l for l in after_pass2.split("\n") if "DUPLICATED CONTENT" in l]
        assert len(markers) >= 2
        # Existing marker from pass1 should be preserved intact
        assert any('from "Task: implement auth"' in m for m in markers)
        # Unique content from all attempts should be preserved
        assert "Attempt 1 work." in after_pass2
        assert "Attempt 3 work." in after_pass2
        assert "[Failed.]" in after_pass2
        assert "[Failed again.]" in after_pass2

    def test_min_block_lines_exceeds_total_lines(self):
        """When min_block_lines exceeds the total number of lines, return unchanged."""
        prompt = "line 1\nline 2\nline 3\nline 4\nline 5"
        result = deduplicate_prompt_lines(prompt, min_block_lines=10)
        assert result == prompt
        assert "DUPLICATED CONTENT" not in result

    def test_source_replaced_lines_not_extended(self):
        """Match should not extend through source lines already replaced."""
        # Construct a scenario where:
        # - Block X appears at positions 0-2 (source) and 4-6 (dup1)
        # - After replacing dup1, lines 4-6 are marked as replaced
        # - A later scan should not match through replaced source lines
        #
        # If the source-side replaced check is missing, a match starting
        # at a source position could extend into replaced territory.
        prompt = "\n".join([
            "aaa",       # 0 (source block X start)
            "bbb",       # 1
            "ccc",       # 2 (source block X end)
            "unique1",   # 3
            "aaa",       # 4 (dup1 of X)
            "bbb",       # 5
            "ccc",       # 6
            "unique2",   # 7
            "aaa",       # 8 (dup2 of X)
            "bbb",       # 9
            "ccc",       # 10
        ])
        result = deduplicate_prompt_lines(prompt)
        result_lines = result.split("\n")
        # First occurrence kept verbatim
        assert result_lines[0] == "aaa"
        assert result_lines[1] == "bbb"
        assert result_lines[2] == "ccc"
        # Both subsequent occurrences should reference the same source block
        markers = [l for l in result_lines if "DUPLICATED CONTENT" in l]
        assert len(markers) == 2
        for m in markers:
            assert 'from "aaa" to "ccc"' in m

    def test_partial_overlap_blocks_produce_distinct_markers(self):
        """Two blocks sharing the same first and last line but differing in
        the middle should produce distinct markers (disambiguated by hash)."""
        block_a = ["shared first", "middle A", "shared last"]
        block_b = ["shared first", "middle B", "shared last"]
        prompt = "\n".join(
            block_a + ["sep1"] + block_b + ["sep2"]
            + block_a + ["sep3"] + block_b
        )
        result = deduplicate_prompt_lines(prompt)
        markers = [l for l in result.split("\n") if "DUPLICATED CONTENT" in l]
        # Both second occurrences should be replaced
        assert len(markers) == 2
        # The markers should be distinct because the hashes differ
        assert markers[0] != markers[1]
        # Both reference the shared first/last lines
        for m in markers:
            assert 'from "shared first" to "shared last"' in m
        # Verify the markers match the expected hashes
        assert _expected_marker(block_a) in result
        assert _expected_marker(block_b) in result
