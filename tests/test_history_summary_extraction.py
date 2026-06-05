"""Tests for history-title extraction alignment with the web chat display.

Covers:

* :func:`se3.engine.prompt_markers.extract_user_content` — the Python-side
  equivalent of the web console's ``splitUserPromptByMarker``, cutting the
  user's literal input out of a step-prompt body by the ``USER_CONTENT``
  markers.
* The CLI title extractor
  :meth:`se3.engine.persistence.PersistenceManager.extract_history_summary`
  (100-char clip contract).
* The daemon title extractor
  :func:`se3.daemon.history._extract_history_summary` (untruncated contract).

Both extractors share the same three-tier priority — USER_CONTENT markers →
``Task description:`` regex → raw-content fallback — and this module asserts
that their *extraction source* is identical while each keeps its own
truncation contract.
"""

from __future__ import annotations

import json

import pytest

from se3.engine.prompt_markers import (
    TEMPLATE_PREFIX_END,
    USER_CONTENT_BEGIN,
    USER_CONTENT_END,
    extract_user_content,
)
from se3.engine.persistence import PersistenceManager
from se3.daemon.history import _extract_history_summary


# ---------------------------------------------------------------------------
# Prompt builders mirroring the engine's marker layout
# ---------------------------------------------------------------------------


def _three_segment(prefix: str, user: str, suffix: str) -> str:
    """Build a canonical three-segment prompt body (matches wrap_user_section)."""
    return (
        f"{prefix}{TEMPLATE_PREFIX_END}\n"
        f"{USER_CONTENT_BEGIN}\n{user}"
        f"\n{USER_CONTENT_END}\n{suffix}"
    )


def _two_segment(prefix: str, tail: str) -> str:
    """Build a legacy two-segment prompt body (BEGIN with no END)."""
    return f"{prefix}{TEMPLATE_PREFIX_END}\n{USER_CONTENT_BEGIN}\n{tail}"


# ---------------------------------------------------------------------------
# G1: extract_user_content pure-function unit tests
# ---------------------------------------------------------------------------


def test_extract_user_content_three_segment_hit():
    body = _three_segment("system boilerplate\n", "Fix the login bug", "Available Specs: ...")
    assert extract_user_content(body) == "Fix the login bug"


def test_extract_user_content_strips_edge_newlines():
    body = _three_segment("prefix\n", "\n\n  real input  \n\n", "suffix")
    # Only newlines are stripped; surrounding spaces of the middle are kept.
    assert extract_user_content(body) == "  real input  "


def test_extract_user_content_missing_markers():
    assert extract_user_content("just some plain text, no markers") is None


def test_extract_user_content_two_segment_returns_none():
    # BEGIN without END: the tail is framework-injected, not user input.
    body = _two_segment("prefix\n", "## Task Description\nframework tail text")
    assert extract_user_content(body) is None


def test_extract_user_content_empty_middle_returns_none():
    body = _three_segment("prefix\n", "\n\n", "suffix")
    assert extract_user_content(body) is None


def test_extract_user_content_non_string():
    assert extract_user_content(None) is None  # type: ignore[arg-type]
    assert extract_user_content("") is None


def test_extract_user_content_begin_before_template_end_is_none():
    # Markers out of canonical order: USER_CONTENT_BEGIN before TEMPLATE_PREFIX_END.
    body = f"{USER_CONTENT_BEGIN}\nx\n{USER_CONTENT_END}"
    assert extract_user_content(body) is None


# ---------------------------------------------------------------------------
# Fixtures: write a first-line jsonl into a flow dir
# ---------------------------------------------------------------------------


def _write_first_line(flow_dir, content, *, filename="01_discovery_abc123.jsonl"):
    flow_dir.mkdir(parents=True, exist_ok=True)
    record = {"role": "user", "content": content}
    (flow_dir / filename).write_text(json.dumps(record) + "\n", encoding="utf-8")
    return flow_dir


# Matrix of (label, content, expected_extracted_source). The expected value is
# the *untruncated* extraction source shared by both extractors.
USER_INPUT = "Implement the new history title extraction feature end to end"


def _build_cases():
    long_user = "X" * 250
    return {
        "three_segment_marker": (
            _three_segment("boilerplate\n", USER_INPUT, "Available Specs"),
            USER_INPUT,
        ),
        "three_segment_long": (
            _three_segment("boilerplate\n", long_user, "suffix"),
            long_user,
        ),
        "task_description_regex": (
            "Some preamble\nTask description:\n---\nFix the parser crash\n---\nmore",
            "Fix the parser crash",
        ),
        "plain_text_fallback": (
            "just plain content with no markers and no task description header",
            "just plain content with no markers and no task description header",
        ),
        "two_segment_falls_through": (
            _two_segment("boilerplate\n", "Task description:\n---\nTwo seg task\n---\n"),
            "Two seg task",
        ),
    }


@pytest.mark.parametrize("label", list(_build_cases().keys()))
def test_cli_and_daemon_share_extraction_source(tmp_path, label):
    content, expected = _build_cases()[label]
    flow_dir = _write_first_line(tmp_path / label, content)

    cli = PersistenceManager.extract_history_summary(flow_dir)
    daemon = _extract_history_summary(flow_dir)

    # Daemon returns untruncated source; CLI applies the 100-char clip.
    assert daemon == expected
    if len(expected) > 100:
        assert cli == expected[:100] + "..."
    else:
        assert cli == expected


def test_three_segment_marker_takes_priority_over_regex(tmp_path):
    # A body that has BOTH the user markers and a Task description block: the
    # marker-level user content must win.
    inner = "User real intent here"
    body = _three_segment(
        "boilerplate\nTask description:\n---\nshould be ignored\n---\n",
        inner,
        "suffix",
    )
    flow_dir = _write_first_line(tmp_path / "both", body)
    assert _extract_history_summary(flow_dir) == inner
    assert PersistenceManager.extract_history_summary(flow_dir) == inner


def test_content_as_text_block_list(tmp_path):
    body = _three_segment("boilerplate\n", USER_INPUT, "suffix")
    content_list = [{"type": "text", "text": body}]
    flow_dir = _write_first_line(tmp_path / "list", content_list)
    assert _extract_history_summary(flow_dir) == USER_INPUT
    assert PersistenceManager.extract_history_summary(flow_dir) == USER_INPUT


def test_no_jsonl_returns_no_history_data(tmp_path):
    flow_dir = tmp_path / "empty"
    flow_dir.mkdir()
    assert _extract_history_summary(flow_dir) == "(no history data)"
    assert PersistenceManager.extract_history_summary(flow_dir) == "(no history data)"


def test_corrupt_json_returns_no_state_data(tmp_path):
    flow_dir = tmp_path / "corrupt"
    flow_dir.mkdir()
    (flow_dir / "01_discovery_abc.jsonl").write_text("{not valid json\n", encoding="utf-8")
    assert _extract_history_summary(flow_dir) == "(no state data)"
    assert PersistenceManager.extract_history_summary(flow_dir) == "(no state data)"


def test_plain_text_fallback_matches_pre_change_behavior(tmp_path):
    # Regression guard: a plain-text first line with no markers and no Task
    # description header still yields the raw content (clipped for CLI).
    content = "Y" * 130
    flow_dir = _write_first_line(tmp_path / "plain_long", content)
    assert _extract_history_summary(flow_dir) == content
    assert PersistenceManager.extract_history_summary(flow_dir) == content[:100] + "..."
