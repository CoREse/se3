"""Tests for the ``DaemonAggregator._collect_issues`` stat-signature cache.

The idle-CPU hotspot being closed: every 5s status tick used to
``yaml.safe_load`` every issue YAML under every root (~307 files → 0.3–0.6s
of pure-Python parsing per snapshot) even when nothing changed. The cache
keys the parsed snapshots on the directory stat signature — the ordered
``(relative name, st_mtime_ns, st_size)`` of every ``*.yaml`` under ``open/``
and ``closed/`` — so an unchanged tree costs only stats, while any add /
remove / rewrite re-parses the root in full.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from se3.daemon.aggregator import DaemonAggregator


def _write_issue(
    root: Path,
    issue_id: str,
    *,
    subdir: str = "open",
    title: str = "an issue",
    description: str = "body",
) -> Path:
    issues_dir = root / "se3" / "issues" / subdir
    issues_dir.mkdir(parents=True, exist_ok=True)
    path = issues_dir / f"{issue_id}_x.yaml"
    path.write_text(
        "\n".join(
            [
                f"id: '{issue_id}'",
                f"title: {title}",
                f"description: {description}",
                "status: open" if subdir == "open" else "status: closed",
                "source: system",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def parse_calls(monkeypatch):
    """Count every ``yaml.load`` call and record the Loader it was given."""
    calls = []
    real_load = yaml.load

    def counting_load(stream, Loader=None, **kwargs):
        calls.append(Loader)
        return real_load(stream, Loader=Loader, **kwargs)

    monkeypatch.setattr(yaml, "load", counting_load)
    return calls


def test_unchanged_tree_second_call_zero_parses(tmp_path, parse_calls):
    """Two calls over an untouched tree: the second one parses nothing."""
    for i in range(3):
        _write_issue(tmp_path, f"00{i}")
    _write_issue(tmp_path, "010", subdir="closed")

    agg = DaemonAggregator(machine_id="m1")
    first = agg._collect_issues(tmp_path)
    assert {i.id for i in first} == {"000", "001", "002", "010"}
    assert len(parse_calls) == 4

    second = agg._collect_issues(tmp_path)
    assert len(parse_calls) == 4  # cache hit — zero additional parses
    assert second == first


def test_mtime_only_change_invalidates_cache(tmp_path, parse_calls):
    """A pure mtime bump (same content/size) moves the signature → re-parse."""
    path = _write_issue(tmp_path, "001")
    agg = DaemonAggregator(machine_id="m1")
    agg._collect_issues(tmp_path)
    baseline = len(parse_calls)

    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    agg._collect_issues(tmp_path)
    assert len(parse_calls) == baseline + 1


def test_size_only_change_invalidates_cache(tmp_path, parse_calls):
    """A size change with the mtime pinned back still moves the signature."""
    path = _write_issue(tmp_path, "001", title="Old")
    agg = DaemonAggregator(machine_id="m1")
    assert agg._collect_issues(tmp_path)[0].title == "Old"

    st = path.stat()
    _write_issue(tmp_path, "001", title="Old but longer now")
    # Pin the mtime back so ONLY the byte size differs.
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    snaps = agg._collect_issues(tmp_path)
    assert snaps[0].title == "Old but longer now"


def test_rewrite_reflected_in_snapshot(tmp_path):
    """Rewriting an issue's content is reflected after the cache busts."""
    _write_issue(tmp_path, "001", title="Before")
    agg = DaemonAggregator(machine_id="m1")
    assert agg._collect_issues(tmp_path)[0].title == "Before"

    _write_issue(tmp_path, "001", title="After")
    assert agg._collect_issues(tmp_path)[0].title == "After"


def test_added_and_deleted_files_reflected(tmp_path):
    """File adds and removals move the signature and the snapshot set."""
    _write_issue(tmp_path, "001")
    agg = DaemonAggregator(machine_id="m1")
    assert {i.id for i in agg._collect_issues(tmp_path)} == {"001"}

    added = _write_issue(tmp_path, "002")
    assert {i.id for i in agg._collect_issues(tmp_path)} == {"001", "002"}

    added.unlink()
    assert {i.id for i in agg._collect_issues(tmp_path)} == {"001"}


def test_open_to_closed_move_reflected(tmp_path):
    """Closing an issue (open/ → closed/) is reflected via the signature."""
    open_path = _write_issue(tmp_path, "001")
    agg = DaemonAggregator(machine_id="m1")
    assert agg._collect_issues(tmp_path)[0].status == "open"

    open_path.unlink()
    _write_issue(tmp_path, "001", subdir="closed")
    assert agg._collect_issues(tmp_path)[0].status == "closed"


def test_worktree_copy_root_empty_and_uncached(tmp_path):
    """A worktree copy root yields no issues and never enters the cache."""
    main = tmp_path / "main"
    (main / "se3" / "state").mkdir(parents=True)
    wt = main / "se3" / "worktrees" / "wt-1"
    (wt / "se3" / "state").mkdir(parents=True)
    _write_issue(main, "001")
    _write_issue(wt, "001")  # the worktree's clone of the same issue

    agg = DaemonAggregator(machine_id="m1")
    assert len(agg._collect_issues(main)) == 1
    assert agg._collect_issues(wt) == []
    assert str(wt) not in agg._issue_cache


def test_missing_issues_dir_returns_empty_and_drops_cache(tmp_path):
    """Removing the whole issues tree clears the root's cached snapshots."""
    _write_issue(tmp_path, "001")
    agg = DaemonAggregator(machine_id="m1")
    assert len(agg._collect_issues(tmp_path)) == 1
    assert str(tmp_path) in agg._issue_cache

    import shutil

    shutil.rmtree(tmp_path / "se3" / "issues")
    assert agg._collect_issues(tmp_path) == []
    assert str(tmp_path) not in agg._issue_cache


def test_csafeloader_used_when_available(tmp_path, parse_calls):
    """The C-accelerated loader is preferred whenever libyaml is present."""
    _write_issue(tmp_path, "001")
    DaemonAggregator(machine_id="m1")._collect_issues(tmp_path)
    expected = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert parse_calls == [expected]


def test_safeloader_fallback_behaves_identically(tmp_path, monkeypatch, parse_calls):
    """Without the C extension the pure-Python SafeLoader yields the same data."""
    _write_issue(tmp_path, "001", title="Same either way")
    with_c = DaemonAggregator(machine_id="m1")._collect_issues(tmp_path)

    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    without_c = DaemonAggregator(machine_id="m2")._collect_issues(tmp_path)
    assert parse_calls[-1] is yaml.SafeLoader
    assert without_c == with_c


def test_malformed_file_tolerated_and_cached(tmp_path, parse_calls):
    """A corrupt file is skipped, and the skip result is cached like any other."""
    bad_dir = tmp_path / "se3" / "issues" / "open"
    bad_dir.mkdir(parents=True)
    (bad_dir / "bad.yaml").write_text("not: [valid: yaml: {", encoding="utf-8")
    _write_issue(tmp_path, "001")

    agg = DaemonAggregator(machine_id="m1")
    snaps = agg._collect_issues(tmp_path)
    assert [i.id for i in snaps] == ["001"]
    baseline = len(parse_calls)

    # The bad file participates in the signature; nothing changed → no parses.
    assert agg._collect_issues(tmp_path) == snaps
    assert len(parse_calls) == baseline


def test_caches_are_per_root(tmp_path):
    """Two roots keep independent cache entries."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_issue(root_a, "001", title="A")
    _write_issue(root_b, "001", title="B")

    agg = DaemonAggregator(machine_id="m1")
    assert agg._collect_issues(root_a)[0].title == "A"
    assert agg._collect_issues(root_b)[0].title == "B"

    _write_issue(root_a, "002", title="A2")
    assert {i.id for i in agg._collect_issues(root_a)} == {"001", "002"}
    assert {i.id for i in agg._collect_issues(root_b)} == {"001"}
