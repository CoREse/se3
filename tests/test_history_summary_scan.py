"""Regression tests for history-only title extraction (backward scan).

Both the daemon (`history._extract_history_summary`) and the CLI
(`PersistenceManager.extract_history_summary`) recover a history-only flow's
title from its first per-step jsonl. The first line is frequently a
``step_started`` *event* record that carries no user content, with the real
user prompt on a later line (the on-disk shape of interrupted discovery
sessions such as 6bd01377 / 960518b3 / d8e33322). These tests assert both
extractors scan forward past such event records to the first record carrying
user content, while preserving the first-line-is-prompt behaviour (9b0897c1)
and the bounded-scan / fallback guarantees.
"""

from __future__ import annotations

import json

import pytest

from tianluo.daemon.history import _extract_history_summary
from tianluo.engine.persistence import PersistenceManager
from tianluo.engine.prompt_markers import (
    SUMMARY_MAX_SCAN_LINES,
    TEMPLATE_PREFIX_END,
    USER_CONTENT_BEGIN,
    USER_CONTENT_END,
)


def _wrap(user_text: str) -> str:
    """Build a step-prompt body with the three USER_CONTENT marker segments."""
    return (
        "You are an expert software engineering assistant in DISCOVERY mode.\n"
        f"{TEMPLATE_PREFIX_END}\n{USER_CONTENT_BEGIN}\n"
        f"{user_text}\n"
        f"{USER_CONTENT_END}\n## Available Specifications\n(framework suffix)"
    )


def _step_started() -> str:
    return json.dumps({"type": "step_started", "step_id": "01_discovery_x"})


def _stream_progress(text: str = "") -> str:
    return json.dumps(
        {"type": "stream_progress", "role": "assistant", "content": text,
         "partial": True}
    )


def _user_record(content) -> str:
    return json.dumps({"role": "user", "content": content})


def _write_flow(tmp_path, lines):
    flow_dir = tmp_path / "20260618-101832_6bd01377"
    flow_dir.mkdir()
    (flow_dir / "01_discovery_16835223.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return flow_dir


def _both(flow_dir):
    """Return (daemon_title, cli_title) for the same flow dir."""
    return (
        _extract_history_summary(flow_dir),
        PersistenceManager.extract_history_summary(flow_dir),
    )


def test_first_line_step_started_recovers_user_prompt(tmp_path):
    """6bd01377-shape: step_started first, user prompt on line 1."""
    flow_dir = _write_flow(
        tmp_path,
        [
            _step_started(),
            _user_record(_wrap("加个 terminal 好不好？")),
            _stream_progress("[Bash: se3 spec index]"),
            _stream_progress(),
        ],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "加个 terminal 好不好？"
    assert cli_t == "加个 terminal 好不好？"


def test_first_line_is_user_prompt_unchanged(tmp_path):
    """9b0897c1-shape: user prompt is already the first line."""
    flow_dir = _write_flow(
        tmp_path,
        [
            _user_record(_wrap("标注一下属于哪个项目")),
            _stream_progress(),
        ],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "标注一下属于哪个项目"
    assert cli_t == "标注一下属于哪个项目"


def test_task_description_block_tier(tmp_path):
    """Second-tier extraction when no USER_CONTENT markers are present."""
    body = "System preamble\nTask description:\n---\nFix the parser bug\n---\nmore"
    flow_dir = _write_flow(
        tmp_path,
        [_step_started(), _user_record(body), _stream_progress()],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "Fix the parser bug"
    assert cli_t == "Fix the parser bug"


def test_raw_content_fallback_tier(tmp_path):
    """Third-tier fallback: raw content when no markers / task block."""
    flow_dir = _write_flow(
        tmp_path,
        [_step_started(), _user_record("just a plain prompt"), _stream_progress()],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "just a plain prompt"
    assert cli_t == "just a plain prompt"


def test_list_content_first_text_block(tmp_path):
    """User content given as a block array reduces to its first text block."""
    flow_dir = _write_flow(
        tmp_path,
        [
            _step_started(),
            _user_record([{"type": "text", "text": _wrap("block-array prompt")}]),
        ],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "block-array prompt"
    assert cli_t == "block-array prompt"


def test_no_user_record_returns_fallback(tmp_path):
    """No user-content record anywhere -> fallback string, not empty."""
    flow_dir = _write_flow(
        tmp_path,
        [_step_started(), _stream_progress("a"), _stream_progress("b")],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "(no state data)"
    assert cli_t == "(no state data)"
    # Neither returns an empty title (the bug under repair).
    assert daemon_t and cli_t


def test_empty_user_record_skipped(tmp_path):
    """A role=user record with empty content is skipped for the next one."""
    flow_dir = _write_flow(
        tmp_path,
        [
            _step_started(),
            _user_record(""),
            _user_record(_wrap("the real prompt")),
        ],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "the real prompt"
    assert cli_t == "the real prompt"


def test_cli_clips_to_100_chars(tmp_path):
    """CLI clips to 100 chars + ellipsis; daemon stays untruncated."""
    long_text = "x" * 250
    flow_dir = _write_flow(
        tmp_path,
        [_step_started(), _user_record(_wrap(long_text))],
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == long_text  # untruncated
    assert cli_t == "x" * 100 + "..."
    assert len(cli_t) == 103


def test_bounded_scan_does_not_read_whole_file(tmp_path):
    """A user record beyond the scan bound is NOT found (file not fully read).

    Many event lines precede the user record so that it sits past
    ``SUMMARY_MAX_SCAN_LINES``; the bounded scan stops first and returns the
    fallback rather than walking the entire (potentially huge) file.
    """
    filler = [_stream_progress(f"line {i}") for i in range(SUMMARY_MAX_SCAN_LINES + 50)]
    lines = [_step_started(), *filler, _user_record(_wrap("too far away"))]
    flow_dir = _write_flow(tmp_path, lines)
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "(no state data)"
    assert cli_t == "(no state data)"


def test_unparseable_lines_skipped(tmp_path):
    """Malformed jsonl lines are skipped without raising."""
    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    (flow_dir / "01_discovery_x.jsonl").write_text(
        "not json at all\n"
        + _step_started() + "\n"
        + _user_record(_wrap("recovered after junk")) + "\n",
        encoding="utf-8",
    )
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "recovered after junk"
    assert cli_t == "recovered after junk"


def test_no_jsonl_files_returns_no_history(tmp_path):
    flow_dir = tmp_path / "empty"
    flow_dir.mkdir()
    daemon_t, cli_t = _both(flow_dir)
    assert daemon_t == "(no history data)"
    assert cli_t == "(no history data)"
