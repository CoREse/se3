"""Tests for compose_task_description_with_interjections.

The composer renders a structured ``## Additional Instructions`` section
onto the effective task description. Used by both ``run.py`` (immediate
re-run after Ctrl-C) and ``state_machine._build_step_inputs`` (downstream
propagation), so its output must be deterministic.
"""

from __future__ import annotations

from se3.engine.task_description import compose_task_description_with_interjections


def test_empty_interjections_returns_base_unchanged():
    assert compose_task_description_with_interjections("base task", []) == "base task"


def test_empty_interjections_and_empty_base_returns_empty():
    assert compose_task_description_with_interjections("", []) == ""


def test_single_interjection_appends_section():
    base = "Original task description."
    result = compose_task_description_with_interjections(
        base,
        [{"text": "Stop touching the auth module.",
          "step_type": "implement",
          "timestamp": "2026-05-10T14:00:00"}],
    )
    assert result.startswith(base)
    assert "## Additional Instructions (added during run)" in result
    assert "Stop touching the auth module." in result
    # Step / timestamp prefix appears bracketed
    assert "[implement@2026-05-10T14:00:00]" in result


def test_multiple_interjections_listed_in_order():
    result = compose_task_description_with_interjections(
        "task",
        [
            {"text": "first instruction",
             "step_type": "analyze", "timestamp": "2026-05-10T10:00:00"},
            {"text": "second instruction",
             "step_type": "implement", "timestamp": "2026-05-10T11:00:00"},
            {"text": "third instruction",
             "step_type": "verify_spec", "timestamp": "2026-05-10T12:00:00"},
        ],
    )
    # Order preserved
    pos1 = result.find("first instruction")
    pos2 = result.find("second instruction")
    pos3 = result.find("third instruction")
    assert pos1 < pos2 < pos3


def test_base_with_trailing_whitespace_collapsed_before_separator():
    """Trailing newlines/spaces on base must not produce more than one
    blank line before the ``## Additional Instructions`` header."""
    result = compose_task_description_with_interjections(
        "base task\n\n\n   ",
        [{"text": "x", "step_type": "implement", "timestamp": "t"}],
    )
    # Exactly one blank line between base and section header.
    assert "base task\n\n## Additional Instructions" in result
    # No triple-blank artefact.
    assert "\n\n\n## Additional Instructions" not in result


def test_interjection_text_is_stripped():
    result = compose_task_description_with_interjections(
        "task",
        [{"text": "   surrounded by whitespace   \n",
          "step_type": "analyze", "timestamp": "t"}],
    )
    assert "- [analyze@t] surrounded by whitespace" in result


def test_empty_text_entries_skipped():
    """Entries whose text is empty after strip() should be silently
    dropped — they would otherwise produce orphan ``- `` bullet markers."""
    result = compose_task_description_with_interjections(
        "task",
        [
            {"text": "", "step_type": "implement", "timestamp": "t1"},
            {"text": "  \n  ", "step_type": "implement", "timestamp": "t2"},
            {"text": "real instruction",
             "step_type": "implement", "timestamp": "t3"},
        ],
    )
    assert "real instruction" in result
    # Only one bullet present (the real one).
    assert result.count("- ") == 1


def test_all_entries_empty_returns_base_unchanged():
    """If every entry has empty text, no section is appended at all."""
    result = compose_task_description_with_interjections(
        "task",
        [{"text": "", "step_type": "x", "timestamp": "y"}],
    )
    assert result == "task"


def test_missing_step_or_timestamp_omits_prefix():
    """If neither step_type nor timestamp is set, no ``[...]`` prefix
    appears — just the bullet."""
    result = compose_task_description_with_interjections(
        "task",
        [{"text": "bare instruction"}],
    )
    assert "- bare instruction" in result
    assert "[" not in result.split("Additional Instructions")[1]


def test_only_step_type_present():
    """step_type without timestamp uses the single-value form ``[step]``."""
    result = compose_task_description_with_interjections(
        "task",
        [{"text": "x", "step_type": "implement"}],
    )
    assert "[implement] x" in result


def test_only_timestamp_present():
    """timestamp without step_type uses ``[ts]``."""
    result = compose_task_description_with_interjections(
        "task",
        [{"text": "x", "timestamp": "2026-05-10T14:00:00"}],
    )
    assert "[2026-05-10T14:00:00] x" in result


def test_non_mapping_entry_skipped_silently():
    """Defensive: malformed entries (string, None) don't crash the
    composer — they're just skipped."""
    result = compose_task_description_with_interjections(
        "task",
        [None, "not a dict", {"text": "valid"}],
    )
    assert "valid" in result


def test_empty_base_with_interjections_still_renders_section():
    """A flow whose task_description is empty still gets the section
    (no leading separator before the header)."""
    result = compose_task_description_with_interjections(
        "",
        [{"text": "hi", "step_type": "implement", "timestamp": "t"}],
    )
    assert result.startswith("## Additional Instructions")
    assert "- [implement@t] hi" in result
