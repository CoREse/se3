"""Unit tests for _salvage_history_from_worktree and related helpers.

Tests verify:
- Non-group files (discovery.jsonl, analyze.jsonl, etc.) are skipped
- Group-specific files (_G1.jsonl, _G2.jsonl, etc.) are copied correctly
- Append path works correctly when a target file already exists
- Empty / missing worktree history directories are handled gracefully
- _salvage_results_history deduplicates by worktree_path (relay-chain guard)
- _restore_history_to_worktree skips _G*.jsonl files (resume-path guard)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tianluo.engine.steps.implement import (
    _restore_history_to_worktree,
    _salvage_history_from_worktree,
    _salvage_results_history,
)


class TestSalvageHistoryFromWorktree:
    """Filesystem-level tests for _salvage_history_from_worktree."""

    def _make_history_file(self, flow_dir: Path, name: str, content: str) -> Path:
        """Create a history file with the given content."""
        f = flow_dir / name
        f.write_text(content, encoding="utf-8")
        return f

    def test_skips_non_group_files(self, tmp_path: Path):
        """Files like discovery.jsonl, analyze.jsonl are NOT copied."""
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        for name in ("discovery.jsonl", "analyze.jsonl", "plan.jsonl", "confirm.jsonl"):
            self._make_history_file(flow_dir, name, '{"step": "' + name + '"}\n')

        _salvage_history_from_worktree(worktree, main_repo)

        target_flow_dir = main_repo / "tianluo" / "history" / "20260413-flow1"
        # None of the non-group files should have been copied
        for name in ("discovery.jsonl", "analyze.jsonl", "plan.jsonl", "confirm.jsonl"):
            assert not (target_flow_dir / name).exists(), f"{name} should not be copied"

    def test_copies_group_files(self, tmp_path: Path):
        """Files matching _G<n>.jsonl are copied to the main repo."""
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        content_g1 = '{"group": "G1", "line": 1}\n'
        content_g2 = '{"group": "G2", "line": 1}\n'
        self._make_history_file(flow_dir, "_G1.jsonl", content_g1)
        self._make_history_file(flow_dir, "_G2.jsonl", content_g2)

        _salvage_history_from_worktree(worktree, main_repo)

        target_flow_dir = main_repo / "tianluo" / "history" / "20260413-flow1"
        assert (target_flow_dir / "_G1.jsonl").read_text() == content_g1
        assert (target_flow_dir / "_G2.jsonl").read_text() == content_g2

    def test_appends_when_target_exists(self, tmp_path: Path):
        """If the target file already exists, new content is appended (not overwritten)."""
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        main_repo = tmp_path / "main"
        target_flow_dir = main_repo / "tianluo" / "history" / "20260413-flow1"
        target_flow_dir.mkdir(parents=True)

        existing_content = '{"existing": true}\n'
        new_content = '{"new": true}\n'

        target_file = target_flow_dir / "_G1.jsonl"
        target_file.write_text(existing_content, encoding="utf-8")
        self._make_history_file(flow_dir, "_G1.jsonl", new_content)

        _salvage_history_from_worktree(worktree, main_repo)

        result = target_file.read_text(encoding="utf-8")
        assert result == existing_content + new_content

    def test_no_history_dir_is_noop(self, tmp_path: Path):
        """If the worktree has no tianluo/history directory, function returns silently."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        # Should not raise
        _salvage_history_from_worktree(worktree, main_repo)

        # Main repo should be untouched
        assert not (main_repo / "tianluo" / "history").exists()

    def test_mixed_files_only_group_copied(self, tmp_path: Path):
        """A flow dir with both shared and group files: only group files are copied."""
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        self._make_history_file(flow_dir, "discovery.jsonl", '{"step":"discovery"}\n')
        self._make_history_file(flow_dir, "_G1.jsonl", '{"group":"G1"}\n')
        self._make_history_file(flow_dir, "analyze.jsonl", '{"step":"analyze"}\n')
        self._make_history_file(flow_dir, "_G3.jsonl", '{"group":"G3"}\n')

        _salvage_history_from_worktree(worktree, main_repo)

        target_flow_dir = main_repo / "tianluo" / "history" / "20260413-flow1"
        assert not (target_flow_dir / "discovery.jsonl").exists()
        assert not (target_flow_dir / "analyze.jsonl").exists()
        assert (target_flow_dir / "_G1.jsonl").exists()
        assert (target_flow_dir / "_G3.jsonl").exists()

    def test_relay_chain_deduplication(self, tmp_path: Path):
        """Salvaging the same worktree twice leaves exactly one copy of each turn.

        The caller (``_run_dag_parallel``) deduplicates worktree paths within a
        single step, but that alone cannot cover an interrupted run: it salvages
        the group file and then KEEPS the worktree, so the continuation salvages
        the very same file again in a *later process*. The high-water mark makes
        the copy idempotent, so a group interrupted N times still renders once
        in history display and in the rebuilt retry context.
        """
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        content = '{"group": "G1"}\n'
        self._make_history_file(flow_dir, "_G1.jsonl", content)

        # First call: copies the file
        _salvage_history_from_worktree(worktree, main_repo)
        target = main_repo / "tianluo" / "history" / "20260413-flow1" / "_G1.jsonl"
        assert target.read_text() == content

        # Second call with nothing new written: no-op.
        _salvage_history_from_worktree(worktree, main_repo)
        assert target.read_text() == content

        # Only turns produced SINCE the previous salvage are appended.
        more = '{"group": "G1", "turn": 2}\n'
        with open(flow_dir / "_G1.jsonl", "a", encoding="utf-8") as fh:
            fh.write(more)
        _salvage_history_from_worktree(worktree, main_repo)
        assert target.read_text() == content + more

    def test_unrelated_target_is_not_mistaken_for_a_prefix(self, tmp_path: Path):
        """Main's copy counts as already-salvaged only when it really is a prefix.

        A retry whose fresh worktree was never seeded with the restored history
        starts its own file at zero while main still holds a longer earlier
        attempt. Treating main's length as the high-water mark there would read
        "already salvaged" for bytes main has never seen and drop the entire new
        attempt, so a non-prefix overlap must re-copy instead.
        """
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        main_repo = tmp_path / "main"
        target_flow_dir = main_repo / "tianluo" / "history" / "20260413-flow1"
        target_flow_dir.mkdir(parents=True)

        # Main holds a LONGER, unrelated earlier attempt; the worktree file is
        # this attempt's own shorter, independent conversation.
        earlier = '{"attempt": 1, "padding": "aaaaaaaaaaaaaaaaaaaaaaaa"}\n'
        fresh = '{"attempt": 2}\n'
        (target_flow_dir / "_G1.jsonl").write_text(earlier, encoding="utf-8")
        self._make_history_file(flow_dir, "_G1.jsonl", fresh)

        _salvage_history_from_worktree(worktree, main_repo)

        assert (target_flow_dir / "_G1.jsonl").read_text() == earlier + fresh


class TestMarklessPrefixRecovery:
    """The sidecar-less path: an upgraded interrupted flow, or a sidecar whose
    write failed. Main's copy is then recognised as already-salvaged only by
    comparing the bytes."""

    def _run(self, tmp_path: Path, main_text: str, worktree_text: str) -> str:
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        target_flow_dir = tmp_path / "main" / "tianluo" / "history" / "20260413-flow1"
        target_flow_dir.mkdir(parents=True)
        (target_flow_dir / "_G1.jsonl").write_text(main_text, encoding="utf-8")
        (flow_dir / "_G1.jsonl").write_text(worktree_text, encoding="utf-8")
        _salvage_history_from_worktree(worktree, tmp_path / "main")
        return (target_flow_dir / "_G1.jsonl").read_text()

    def test_a_shorter_exact_prefix_appends_only_the_new_bytes(self, tmp_path):
        """Reads of the two files are compared over their OVERLAP: a short read
        means EOF, not divergence. Comparing the raw buffers made a genuine
        prefix read as "unrelated" and duplicated every pre-interruption turn."""
        already = '{"turn": 1}\n{"turn": 2}\n'
        new = '{"turn": 3}\n'
        assert self._run(tmp_path, already, already + new) == already + new

    def test_a_large_shorter_prefix_is_recognised_across_chunks(self, tmp_path):
        """The failure needed the divergence to land inside one 64 KiB read."""
        already = "".join('{"turn": %d}\n' % i for i in range(6000))
        new = '{"turn": "last"}\n'
        assert len(already) > 65536
        assert self._run(tmp_path, already, already + new) == already + new

    def test_a_longer_main_copy_appends_nothing(self, tmp_path):
        worktree_text = '{"turn": 1}\n'
        main_text = worktree_text + '{"turn": 2}\n'
        assert self._run(tmp_path, main_text, worktree_text) == main_text

    def test_divergent_content_is_still_re_copied(self, tmp_path):
        """Any divergence answers zero: re-copying is recoverable, skipping
        unsalvaged turns is not."""
        main_text = '{"turn": 1, "x": 1}\n'
        worktree_text = '{"turn": 1, "y": 2}\n{"turn": 2}\n'
        assert self._run(tmp_path, main_text, worktree_text) == (
            main_text + worktree_text
        )


class TestSalvageResultsHistory:
    """Tests for the _salvage_results_history deduplication guard."""

    def test_dedup_same_worktree_path(self, tmp_path: Path):
        """Two GroupResult-like objects sharing the same worktree_path must only
        trigger one salvage call, so group files are not doubled in main."""
        worktree = tmp_path / "worktree"
        flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        content = '{"group": "G1"}\n'
        (flow_dir / "_G1.jsonl").write_text(content, encoding="utf-8")

        # Two fake GroupResult objects pointing at the same worktree
        r1 = MagicMock()
        r1.worktree_path = worktree
        r2 = MagicMock()
        r2.worktree_path = worktree  # same path — relay chain scenario

        _salvage_results_history([r1, r2], main_repo)

        target = main_repo / "tianluo" / "history" / "20260413-flow1" / "_G1.jsonl"
        assert target.exists(), "_G1.jsonl should have been salvaged"
        assert target.read_text(encoding="utf-8") == content, (
            "content should appear exactly once — dedup guard fired"
        )

    def test_different_worktree_paths_both_salvaged(self, tmp_path: Path):
        """Two results with distinct worktree_paths both get salvaged."""
        wt1 = tmp_path / "wt1"
        wt2 = tmp_path / "wt2"
        for wt, gname in ((wt1, "_G1.jsonl"), (wt2, "_G2.jsonl")):
            fd = wt / "tianluo" / "history" / "20260413-flow1"
            fd.mkdir(parents=True)
            (fd / gname).write_text(f'{{"g":"{gname}"}}\n', encoding="utf-8")

        main_repo = tmp_path / "main"
        main_repo.mkdir()

        r1 = MagicMock()
        r1.worktree_path = wt1
        r2 = MagicMock()
        r2.worktree_path = wt2

        _salvage_results_history([r1, r2], main_repo)

        target_dir = main_repo / "tianluo" / "history" / "20260413-flow1"
        assert (target_dir / "_G1.jsonl").exists()
        assert (target_dir / "_G2.jsonl").exists()

    def test_none_worktree_path_skipped(self, tmp_path: Path):
        """Results with worktree_path=None (failed/skipped groups) are silently ignored."""
        main_repo = tmp_path / "main"
        main_repo.mkdir()

        r = MagicMock()
        r.worktree_path = None

        # Should not raise
        _salvage_results_history([r], main_repo)
        assert not (main_repo / "tianluo" / "history").exists()


class TestRestoreHistoryToWorktree:
    """Tests for the resume-path guard in _restore_history_to_worktree."""

    def test_skips_group_files(self, tmp_path: Path):
        """_G*.jsonl files must NOT be copied into a new worktree.

        If they were copied, the subsequent salvage would append them a second
        time to the already-existing files in main, doubling their content.
        """
        main_repo = tmp_path / "main"
        flow_dir = main_repo / "tianluo" / "history" / "20260413-flow1"
        flow_dir.mkdir(parents=True)

        (flow_dir / "discovery.jsonl").write_text('{"step":"discovery"}\n', encoding="utf-8")
        (flow_dir / "analyze.jsonl").write_text('{"step":"analyze"}\n', encoding="utf-8")
        (flow_dir / "_G1.jsonl").write_text('{"group":"G1"}\n', encoding="utf-8")
        (flow_dir / "_G2.jsonl").write_text('{"group":"G2"}\n', encoding="utf-8")

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        _restore_history_to_worktree(main_repo, worktree, "20260413-flow1")

        wt_flow_dir = worktree / "tianluo" / "history" / "20260413-flow1"
        # Shared context files should be restored
        assert (wt_flow_dir / "discovery.jsonl").exists()
        assert (wt_flow_dir / "analyze.jsonl").exists()
        # Group-specific files must NOT be restored
        assert not (wt_flow_dir / "_G1.jsonl").exists(), "_G1.jsonl must not be copied to worktree"
        assert not (wt_flow_dir / "_G2.jsonl").exists(), "_G2.jsonl must not be copied to worktree"

    def test_no_main_history_is_noop(self, tmp_path: Path):
        """If main repo has no history for the flow, function returns silently."""
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        _restore_history_to_worktree(main_repo, worktree, "20260413-missing")

        assert not (worktree / "tianluo" / "history").exists()
