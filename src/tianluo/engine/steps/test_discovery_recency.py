"""Tests for discovery round-0 recency-context collectors and formatter.

Colocated engine test (allowed by the charter's testing convention) covering the
read-only helpers added for round-0 recency injection:
``_collect_session_summaries`` / ``_collect_recent_commits`` /
``_gather_recency_context``. Fixtures build fake ``se3/state/summary-*.md`` files
and a throwaway git repo under ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime

import pytest

from tianluo.engine.steps.discovery import (
    _RECENCY_SUMMARY_MAX_CHARS,
    _RECENCY_TASK_MAX_CHARS,
    _collect_recent_commits,
    _collect_session_summaries,
    _gather_recency_context,
)


def _write_summary(project_root, flow_id: str, task: str, body: str) -> None:
    state_dir = project_root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "# Work Summary\n\n"
        f"**Flow ID:** {flow_id}\n"
        f"**Task:** {task}\n"
        "**Completed:** 2026-06-30T00:00:00\n\n"
        "---\n\n"
        f"{body}\n"
    )
    (state_dir / f"summary-{flow_id}.md").write_text(content, encoding="utf-8")


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(project_root) -> None:
    _git(["init"], project_root)
    _git(["config", "user.email", "test@example.com"], project_root)
    _git(["config", "user.name", "Test"], project_root)


# --- _collect_session_summaries ---------------------------------------------


def test_collect_summaries_extracts_task_and_first_paragraph(tmp_path):
    _write_summary(
        tmp_path,
        "20260101-000000_aaaa",
        "Fix the login bug",
        "# Session report heading\n\nMore body that should be ignored.",
    )

    result = _collect_session_summaries(tmp_path)

    assert len(result) == 1
    excerpt = result[0]
    assert "Task: Fix the login bug" in excerpt
    # First paragraph after --- is the heading line; later paragraphs excluded.
    assert "# Session report heading" in excerpt
    assert "should be ignored" not in excerpt


def test_collect_summaries_truncates_long_task_line(tmp_path):
    long_task = "x" * 500
    _write_summary(
        tmp_path,
        "20260101-000000_aaaa",
        long_task,
        "Body paragraph.",
    )

    result = _collect_session_summaries(tmp_path)

    assert len(result) == 1
    # Task content is truncated to the cap (+ ellipsis); the full 500 chars
    # must not survive.
    assert "x" * 500 not in result[0]
    assert "x" * _RECENCY_TASK_MAX_CHARS in result[0]
    assert "…" in result[0]


def test_collect_summaries_hard_caps_each_excerpt(tmp_path):
    # A huge first paragraph must not push the excerpt past the per-entry cap.
    _write_summary(
        tmp_path,
        "20260101-000000_aaaa",
        "short task",
        "y" * 5000,
    )

    result = _collect_session_summaries(tmp_path)

    assert len(result) == 1
    assert len(result[0]) <= _RECENCY_SUMMARY_MAX_CHARS + 1  # +1 for ellipsis


def test_collect_summaries_returns_most_recent_three(tmp_path):
    for i in range(5):
        # Day component is i + 1 (01..05) so every filename is a VALID calendar
        # timestamp — day 00 would be rejected by the recency key's strptime and
        # silently fall back to mtime, masking the filename-ordering this asserts.
        _write_summary(
            tmp_path,
            f"2026010{i + 1}-000000_id{i}",
            f"task {i}",
            f"body {i}",
        )

    result = _collect_session_summaries(tmp_path)

    assert len(result) == 3
    # Filename timestamps sort by recency desc → ids 4, 3, 2 are most recent.
    assert "task 4" in result[0]
    assert "task 3" in result[1]
    assert "task 2" in result[2]


def test_collect_summaries_timestamped_outranks_nonconforming_name(tmp_path):
    # Regression: a non-timestamped name (summary-e752...) must not lexically
    # leapfrog a genuinely recent timestamped summary. Under a plain reverse
    # filename sort, 'e' (0x65) > '2' (0x32) would wrongly rank the stale,
    # oddly-named file ahead of the recent summary-2026*.md. The oddly-named
    # file's mtime is pinned to an old epoch so the timestamped file is, by
    # actual recency, the newer of the two — the recency key must reflect that.
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_summary(
        tmp_path, "20260630-120000_x", "recent timestamped task", "recent body"
    )
    stale = state_dir / "summary-e752e110-9bd.md"
    stale.write_text(
        "# Work Summary\n\n**Task:** stale oddly named task\n\n---\n\nstale body\n",
        encoding="utf-8",
    )
    old_epoch = datetime(2020, 1, 1).timestamp()
    os.utime(stale, (old_epoch, old_epoch))

    result = _collect_session_summaries(tmp_path, limit=1)

    assert len(result) == 1
    assert "recent timestamped task" in result[0]
    assert "stale oddly named task" not in result[0]


def test_collect_summaries_recent_nonconforming_outranks_old_timestamped(tmp_path):
    # The fix: filename timestamps and the mtime fallback must live on the same
    # epoch scale. A freshly-written non-conforming summary (newest mtime) must
    # NOT be forced behind older timestamped summaries. Before the fix, a packed
    # 14-digit filename integer dwarfed any 10-digit mtime, so the new file was
    # always dropped from the top-N regardless of its real recency.
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    for day in (1, 2, 3):
        _write_summary(
            tmp_path,
            f"2026060{day}-000000_old",
            f"old timestamped task {day}",
            "old body",
        )
    fresh = state_dir / "summary-latest.md"
    fresh.write_text(
        "# Work Summary\n\n**Task:** fresh non-conforming task\n\n---\n\nfresh body\n",
        encoding="utf-8",
    )
    # Pin the non-conforming file's mtime well past every filename timestamp.
    future_epoch = datetime(2030, 1, 1).timestamp()
    os.utime(fresh, (future_epoch, future_epoch))

    result = _collect_session_summaries(tmp_path, limit=3)

    assert len(result) == 3
    # The fresh non-conforming file is genuinely the most recent → ranks first.
    assert "fresh non-conforming task" in result[0]


def test_collect_summaries_fewer_than_three(tmp_path):
    _write_summary(tmp_path, "20260101-000000_a", "only task", "only body")

    result = _collect_session_summaries(tmp_path)

    assert len(result) == 1


def test_collect_summaries_ignores_json_siblings(tmp_path):
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "summary-20260101-000000_a.json").write_text("{}", encoding="utf-8")

    result = _collect_session_summaries(tmp_path)

    assert result == []


def test_collect_summaries_no_files_returns_empty(tmp_path):
    # No se3/state dir at all — must not raise.
    assert _collect_session_summaries(tmp_path) == []


# --- _collect_recent_commits ------------------------------------------------


def test_collect_commits_filters_merges_and_returns_subjects_only(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "first commit\n\nbody line should not appear"], tmp_path)

    # Resolve the default branch name (master vs. main varies by git config).
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Create a branch, commit, then merge with --no-ff to force a merge commit.
    _git(["checkout", "-b", "feature"], tmp_path)
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "feature commit"], tmp_path)
    _git(["checkout", base_branch], tmp_path)
    _git(["merge", "--no-ff", "-m", "Merge feature branch", "feature"], tmp_path)

    result = _collect_recent_commits(tmp_path)

    assert "first commit" in result
    assert "feature commit" in result
    # Merge commit subject is filtered out by --no-merges.
    assert "Merge feature branch" not in result
    # Body lines never appear (subject %s only).
    assert all("body line should not appear" not in s for s in result)


def test_collect_commits_respects_limit(tmp_path):
    _init_repo(tmp_path)
    for i in range(15):
        (tmp_path / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        _git(["add", "."], tmp_path)
        _git(["commit", "-m", f"commit {i}"], tmp_path)

    result = _collect_recent_commits(tmp_path, limit=10)

    assert len(result) == 10


def test_collect_commits_no_commits_returns_empty(tmp_path):
    _init_repo(tmp_path)
    assert _collect_recent_commits(tmp_path) == []


def test_collect_commits_non_git_returns_empty(tmp_path):
    # No repo initialized — git log exits non-zero, helper must not raise.
    assert _collect_recent_commits(tmp_path) == []


# --- _gather_recency_context ------------------------------------------------


def test_gather_recency_both_sections(tmp_path):
    _write_summary(tmp_path, "20260101-000000_a", "do the thing", "report lede")
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "initial commit"], tmp_path)

    block = _gather_recency_context(tmp_path)

    assert "## Recent Activity Context" in block
    assert "### Recent session summaries" in block
    assert "### Recent commits" in block
    assert "do the thing" in block
    assert "initial commit" in block


def test_gather_recency_only_summaries(tmp_path):
    _write_summary(tmp_path, "20260101-000000_a", "summary only", "body")

    block = _gather_recency_context(tmp_path)

    assert "### Recent session summaries" in block
    assert "### Recent commits" not in block


def test_gather_recency_only_commits(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    _git(["add", "."], tmp_path)
    _git(["commit", "-m", "commit only"], tmp_path)

    block = _gather_recency_context(tmp_path)

    assert "### Recent commits" in block
    assert "### Recent session summaries" not in block
    assert "commit only" in block


def test_gather_recency_empty_returns_empty_string(tmp_path):
    # No summaries and no git repo → empty block.
    assert _gather_recency_context(tmp_path) == ""


# --- round-0-only injection invariant ---------------------------------------

_RECENCY_SENTINEL = "<<RECENCY-SENTINEL-BLOCK>>"


class _FakeCaller:
    """Stand-in for LLMCaller that records the prompt and skips the real call.

    Records every prompt it is asked to send so the test can assert whether the
    recency sentinel was injected, and returns a minimal valid discovery JSON so
    ``_run_discovery_round`` parses and returns without a real LLM call.
    """

    prompts: list[str] = []

    def __init__(self, *args, **kwargs):
        self.last_raw_result = ""

    def call(self, prompt, **kwargs):
        type(self).prompts.append(prompt)
        self.last_raw_result = '{"mode": "question", "content": "hi"}'
        return self.last_raw_result


def _make_flow_and_step():
    import types

    from tianluo.engine.models import StepType

    flow = types.SimpleNamespace(flow_id="flow-test")
    step = types.SimpleNamespace(
        step_id="step-test",
        step_type=types.SimpleNamespace(value=StepType.DISCOVERY.value),
        inputs={},
    )
    return flow, step


def _run_round(monkeypatch, tmp_path, round_number):
    """Drive _run_discovery_round with the recency collector and LLM mocked."""
    from tianluo.engine.steps import discovery

    # Sentinel replaces the real recency block so the assertion is independent of
    # whether tmp_path actually has summaries/commits.
    monkeypatch.setattr(
        discovery, "_gather_recency_context", lambda project_root: _RECENCY_SENTINEL
    )
    # Mock LLMCaller so no real LLM call happens; it records the prompt instead.
    _FakeCaller.prompts = []
    monkeypatch.setattr(discovery, "LLMCaller", _FakeCaller)

    flow, step = _make_flow_and_step()
    discovery._run_discovery_round(
        project_root=tmp_path,
        flow=flow,
        step=step,
        prompt_template="initial {initial_description} {round_number} "
        "{conversation_history} {user_response} {project_context}",
        initial_description="do the thing",
        round_number=round_number,
        conversation_history=[],
        user_response="",
        project_context="proj-ctx",
    )
    assert _FakeCaller.prompts, "LLM was never called"
    return _FakeCaller.prompts[-1]


def test_recency_injected_on_round_zero(monkeypatch, tmp_path):
    prompt = _run_round(monkeypatch, tmp_path, round_number=0)
    assert _RECENCY_SENTINEL in prompt
    # project_context is the per-round injection and must still be present.
    assert "proj-ctx" in prompt


def test_recency_not_injected_after_round_zero(monkeypatch, tmp_path):
    for round_number in (1, 2):
        prompt = _run_round(monkeypatch, tmp_path, round_number=round_number)
        assert _RECENCY_SENTINEL not in prompt
        # project_context is injected every round, including round >= 1.
        assert "proj-ctx" in prompt
