"""Unit tests for the shared renumber primitives (``issue_renumber``).

These primitives are the geometry both ``se3 merge`` channels (git three-way
merge of committed issues, and runtime-sync of uncommitted worktree issues)
reuse to renumber a colliding issue. The two easiest-to-get-wrong parts are
token-precise reference rewriting (must not clobber ``#1234`` / ``abc#123``,
must treat ``#14`` and ``#014`` as the same reference) and pushing ``.next_id``
to ``max(ID) + 1`` regardless of the counter's prior state — so those are
covered directly here rather than only through the two channels.
"""

from __future__ import annotations

from pathlib import Path

from se3.engine.issue_manager import Issue, IssueManager, IssueStatus
from se3.engine.merge.issue_renumber import (
    advance_next_id_to_max,
    format_renumber_trace,
    rewrite_issue_references,
)


def _write_issue(
    mgr: IssueManager,
    issue_id: str,
    description: str,
    status: IssueStatus = IssueStatus.OPEN,
) -> Path:
    """Write a minimal issue YAML with an explicit ID into open/ or closed/."""
    mgr._ensure_dirs()
    issue = Issue(id=issue_id, description=description, status=status)
    target = mgr.closed_dir if status != IssueStatus.OPEN else mgr.open_dir
    path = target / f"{issue_id}_fixture.yaml"
    mgr._write_issue(path, issue)
    return path


def _read_next_id(project_root: Path) -> int:
    return int((project_root / "se3" / "issues" / ".next_id").read_text().strip())


# --------------------------------------------------------------------------
# rewrite_issue_references — token precision
# --------------------------------------------------------------------------

def test_rewrite_leaves_non_reference_hashnumbers_untouched(tmp_path: Path) -> None:
    """``#1234`` and ``abc#123`` are prose, not a ``#123`` reference."""
    mgr = IssueManager(tmp_path)
    # A ``#1234`` (superstring), an ``abc#123`` (leading alnum), and a genuine
    # standalone ``#123`` all in one body — only the last must change.
    p = _write_issue(mgr, "050", "see #1234 and abc#123 but fix #123 now")

    n = rewrite_issue_references(tmp_path, "123", "240")

    assert n == 1
    body = p.read_text()
    assert "#1234" in body          # superstring untouched
    assert "abc#123" in body        # leading-alnum token untouched
    assert "#240" in body           # genuine reference rewritten
    # The only bare "#123" left must be the one inside "#1234"; no standalone.
    assert " #123 " not in body and not body.endswith(" #123")


def test_rewrite_treats_zero_padding_as_equivalent(tmp_path: Path) -> None:
    """``#14`` and ``#014`` denote the same issue and both get rewritten."""
    mgr = IssueManager(tmp_path)
    p = _write_issue(mgr, "060", "blocks #14 and also #014 and #0014")

    n = rewrite_issue_references(tmp_path, "014", "240")

    assert n == 3
    body = p.read_text()
    assert "#14" not in body.replace("#240", "")  # every variant became #240
    assert body.count("#240") == 3


def test_rewrite_counts_across_open_and_closed(tmp_path: Path) -> None:
    """A rewrite spans both open/ and closed/ and returns the total count."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "070", "refs #93 and #093", status=IssueStatus.OPEN)
    _write_issue(mgr, "071", "closed but mentions #93", status=IssueStatus.RESOLVED)

    n = rewrite_issue_references(tmp_path, "093", "300")

    assert n == 3  # two in open (#93, #093) + one in closed (#93)


def test_rewrite_confined_to_issues_dir(tmp_path: Path) -> None:
    """References outside se3/issues/ are never touched."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "080", "fix #55")
    stray = tmp_path / "se3" / "logs"
    stray.mkdir(parents=True)
    (stray / "note.txt").write_text("unrelated #55 mention")

    rewrite_issue_references(tmp_path, "055", "240")

    assert (stray / "note.txt").read_text() == "unrelated #55 mention"


def test_rewrite_new_id_is_zero_padded(tmp_path: Path) -> None:
    """The replacement token is zero-padded to at least three digits."""
    mgr = IssueManager(tmp_path)
    p = _write_issue(mgr, "090", "depends on #55")

    rewrite_issue_references(tmp_path, "55", "7")

    assert "#007" in p.read_text()


# --------------------------------------------------------------------------
# advance_next_id_to_max — counter recovery
# --------------------------------------------------------------------------

def test_advance_from_missing_counter(tmp_path: Path) -> None:
    """A missing .next_id is created at max(ID)+1."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "005", "a")
    _write_issue(mgr, "012", "b", status=IssueStatus.RESOLVED)
    counter = tmp_path / "se3" / "issues" / ".next_id"
    assert not counter.exists()

    result = advance_next_id_to_max(tmp_path)

    assert result == 13
    assert _read_next_id(tmp_path) == 13


def test_advance_from_lagging_counter(tmp_path: Path) -> None:
    """A counter that lags the true max is pushed forward to max+1."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "005", "a")
    _write_issue(mgr, "040", "b")
    counter = tmp_path / "se3" / "issues" / ".next_id"
    counter.write_text("6")  # stale: behind the real max of 40

    result = advance_next_id_to_max(tmp_path)

    assert result == 41
    assert _read_next_id(tmp_path) == 41


def test_advance_from_garbage_counter(tmp_path: Path) -> None:
    """A corrupt counter value is discarded and recomputed from the files."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "022", "a")
    counter = tmp_path / "se3" / "issues" / ".next_id"
    counter.write_text("not-a-number")

    result = advance_next_id_to_max(tmp_path)

    assert result == 23
    assert _read_next_id(tmp_path) == 23


def test_advance_with_no_issues(tmp_path: Path) -> None:
    """With an empty store the counter starts at 1 (max of 0, +1)."""
    result = advance_next_id_to_max(tmp_path)
    assert result == 1
    assert _read_next_id(tmp_path) == 1


# --------------------------------------------------------------------------
# format_renumber_trace
# --------------------------------------------------------------------------

def test_trace_contains_old_and_new_ids() -> None:
    trace = format_renumber_trace("014", "240")
    assert "#014" in trace
    assert "#240" in trace


def test_trace_normalizes_and_zero_pads() -> None:
    """Trace zero-pads both IDs regardless of input padding/type."""
    assert format_renumber_trace(14, 7) == "旧号 #014 → 新号 #007 (se3 merge)"


def test_trace_appended_preserves_display_title(tmp_path: Path) -> None:
    """Appending the trace at the tail leaves display_title/slug derivation intact."""
    desc = "Fix the merge bug\n\nmore details"
    trace = format_renumber_trace("014", "240")
    issue = Issue(id="240", description=f"{desc}\n\n{trace}")
    # display_title takes the first non-empty line — appending at the end keeps it.
    assert issue.display_title == "Fix the merge bug"
    assert trace in issue.description
