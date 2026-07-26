"""Tests for version_intent — intent metadata model + collection + idempotency.

Covers the G1 acceptance criteria: serialization round-trip (including the
custom-rules case where bump_type is absent but versions_changes still carries
the intent), cross-branch collection from a merged tree, consumed-marker
idempotency, and git-durable reconcile-commit detection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .version_intent import (
    RECONCILE_TRAILER,
    VERSION_INTENT_DIR_RELPATH,
    VersionIntent,
    VersionIntentIgnoredError,
    collect_intents,
    intent_path,
    is_consumed,
    mark_consumed,
    read_intent,
    reconcile_commit_exists,
    write_intent,
)


def _make_intent(flow_id: str, **overrides) -> VersionIntent:
    base = dict(
        flow_id=flow_id,
        change_summary=f"summary for {flow_id}",
        versions_changes=[f"Add thing in {flow_id}", "Fix another thing"],
        bump_type="minor",
        is_tag=True,
        pre_session_baseline="1.2.3",
        provisional_suggested_version="1.3.0",
    )
    base.update(overrides)
    return VersionIntent(**base)


class TestSerialization:
    def test_round_trip_preserves_all_fields(self):
        intent = _make_intent("20260706-010101_aaaa1111")
        restored = VersionIntent.from_dict(intent.to_dict())
        assert restored == intent

    def test_write_and_read_from_disk(self, tmp_path: Path):
        intent = _make_intent("20260706-010101_aaaa1111")
        path = write_intent(tmp_path, intent)
        assert path == intent_path(tmp_path, intent.flow_id)
        assert path.is_file()
        assert VERSION_INTENT_DIR_RELPATH in str(path)

        loaded = read_intent(tmp_path, intent.flow_id)
        assert loaded == intent

    def test_custom_rules_intent_without_bump_type(self, tmp_path: Path):
        """versions_changes remains the sole intent carrier when bump_type is absent.

        Mirrors a date-version / build-number custom rule where the SemVer
        bump hint is meaningless or lossy — the intent must still round-trip
        with its substance intact.
        """
        intent = _make_intent(
            "20260706-020202_bbbb2222",
            bump_type=None,
            provisional_suggested_version=None,
        )
        write_intent(tmp_path, intent)
        loaded = read_intent(tmp_path, intent.flow_id)
        assert loaded is not None
        assert loaded.bump_type is None
        assert loaded.versions_changes == intent.versions_changes
        assert loaded.change_summary == intent.change_summary

    def test_from_dict_requires_flow_id(self):
        with pytest.raises(ValueError):
            VersionIntent.from_dict({"change_summary": "x"})

    def test_from_dict_ignores_unknown_keys(self):
        intent = VersionIntent.from_dict(
            {"flow_id": "f1", "future_field": "ignored", "versions_changes": ["a"]}
        )
        assert intent.flow_id == "f1"
        assert intent.versions_changes == ["a"]

    def test_from_dict_drops_non_string_changes(self):
        intent = VersionIntent.from_dict(
            {"flow_id": "f1", "versions_changes": ["keep", "", 42, None, "  also  "]}
        )
        assert intent.versions_changes == ["keep", "also"]

    def test_is_tag_round_trip(self):
        intent = _make_intent("20260709-tag_0001", is_tag=False)
        restored = VersionIntent.from_dict(intent.to_dict())
        assert restored.is_tag is False

    def test_from_dict_missing_is_tag_defaults_none(self):
        intent = VersionIntent.from_dict({"flow_id": "f1"})
        assert intent.is_tag is None

    def test_read_missing_returns_none(self, tmp_path: Path):
        assert read_intent(tmp_path, "nope") is None

    def test_read_corrupt_returns_none(self, tmp_path: Path):
        path = intent_path(tmp_path, "corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        assert read_intent(tmp_path, "corrupt") is None


class TestCollectIntents:
    def test_collect_multiple_branches(self, tmp_path: Path):
        """Multiple merged-in branches each contribute a distinct intent."""
        for fid in ("20260706-a_1111", "20260706-b_2222", "20260706-c_3333"):
            write_intent(tmp_path, _make_intent(fid))

        collected = collect_intents(tmp_path)
        assert [i.flow_id for i in collected] == [
            "20260706-a_1111",
            "20260706-b_2222",
            "20260706-c_3333",
        ]

    def test_collect_empty_when_dir_absent(self, tmp_path: Path):
        assert collect_intents(tmp_path) == []

    def test_collect_skips_corrupt_file(self, tmp_path: Path):
        write_intent(tmp_path, _make_intent("good_1111"))
        bad = intent_path(tmp_path, "bad_2222")
        bad.write_text("garbage", encoding="utf-8")
        collected = collect_intents(tmp_path)
        assert [i.flow_id for i in collected] == ["good_1111"]

    def test_collect_excludes_consumed_by_default(self, tmp_path: Path):
        write_intent(tmp_path, _make_intent("open_1111"))
        write_intent(tmp_path, _make_intent("done_2222"))
        mark_consumed(tmp_path, "done_2222", reconcile_commit="deadbeef")

        assert [i.flow_id for i in collect_intents(tmp_path)] == ["open_1111"]
        assert [i.flow_id for i in collect_intents(tmp_path, include_consumed=True)] == [
            "done_2222",
            "open_1111",
        ]


class TestIdempotency:
    def test_mark_and_is_consumed(self, tmp_path: Path):
        write_intent(tmp_path, _make_intent("f_1111"))
        assert is_consumed(tmp_path, "f_1111", check_reconcile_commit=False) is False

        assert mark_consumed(tmp_path, "f_1111", reconcile_commit="abc123") is True
        assert is_consumed(tmp_path, "f_1111", check_reconcile_commit=False) is True

        loaded = read_intent(tmp_path, "f_1111")
        assert loaded is not None
        assert loaded.consumed is True
        assert loaded.consumed_by == "abc123"

    def test_mark_consumed_is_idempotent(self, tmp_path: Path):
        write_intent(tmp_path, _make_intent("f_1111"))
        assert mark_consumed(tmp_path, "f_1111") is True
        # Second call is a no-op (already consumed) — no double bump.
        assert mark_consumed(tmp_path, "f_1111") is False

    def test_mark_consumed_missing_intent(self, tmp_path: Path):
        assert mark_consumed(tmp_path, "ghost") is False

    def test_is_consumed_missing_intent_is_false(self, tmp_path: Path):
        assert is_consumed(tmp_path, "ghost", check_reconcile_commit=False) is False


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


def _check_ignored(repo: Path, rel: str) -> bool:
    """Return True iff git reports *rel* (repo-relative) as ignored."""
    result = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", "--", rel],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


class TestWriteIntentGitignoreGuard:
    """write_intent self-heals a stale .gitignore, and fails loud only if it can't.

    On an existing project whose committed .gitignore predates the
    ``!/tianluo/version-intents/`` whitelist, the intent JSON would be silently
    skipped by the commit step's ``git add -A`` and the flow would only fail
    much later at version_reconcile with a misleading "restore the intent file".
    The guard now first minimally, idempotently patches the (worktree's own)
    .gitignore so the whitelist takes effect and the write proceeds; it raises
    :class:`VersionIntentIgnoredError` only when that self-heal cannot clear the
    ignore rule.
    """

    def test_ignored_error_is_oserror_subclass(self):
        # version_analyze's intent-emit path catches OSError to FAIL the step;
        # the ignored-path error must ride that same channel.
        assert issubclass(VersionIntentIgnoredError, OSError)

    def test_whitelisted_path_writes_normally(self, git_repo: Path):
        # The current template shape: ``/tianluo/*`` ignores se3 CONTENTS one level
        # down (not the directory itself, so the whitelist below can re-include —
        # git cannot re-include under a fully-excluded parent dir), and
        # ``!/tianluo/version-intents/`` tracks the intents.
        (git_repo / ".gitignore").write_text(
            f"/tianluo/*\n!/{VERSION_INTENT_DIR_RELPATH}/\n", encoding="utf-8"
        )
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "whitelist intents")

        path = write_intent(git_repo, _make_intent("20260707-ok_0002"))
        assert path.is_file()
        # An already-whitelisted .gitignore is left untouched (no self-heal churn).
        assert (git_repo / ".gitignore").read_text(encoding="utf-8") == (
            f"/tianluo/*\n!/{VERSION_INTENT_DIR_RELPATH}/\n"
        )

    def test_non_repo_does_not_block_write(self, tmp_path: Path):
        # No git repo -> check-ignore probe faults -> best-effort, write proceeds.
        path = write_intent(tmp_path, _make_intent("20260707-nr_0003"))
        assert path.is_file()

    def test_heals_gitignore_with_se3_contents_anchor(self, git_repo: Path):
        # Shape (a): ``/tianluo/*`` present but no version-intents whitelist. The
        # whitelist must be inserted right AFTER the ``/tianluo/*`` anchor so the
        # negation takes effect, and the intent must then write without error.
        (git_repo / ".gitignore").write_text(
            "*.log\n/tianluo/*\n!/tianluo/charter.md\n", encoding="utf-8"
        )
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "ignore se3 contents, no intents whitelist")

        path = write_intent(git_repo, _make_intent("20260707-heal_0001"))

        assert path.is_file()
        rel = f"{VERSION_INTENT_DIR_RELPATH}/20260707-heal_0001.json"
        assert not _check_ignored(git_repo, rel)

        new_lines = (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        anchor_idx = new_lines.index("/tianluo/*")
        assert new_lines[anchor_idx + 1] == f"!/{VERSION_INTENT_DIR_RELPATH}/"

    def test_heals_gitignore_with_whole_se3_dir_ignored(self, git_repo: Path):
        # Shape (b): the entire ``tianluo/`` tree ignored (the exact pre-migration
        # shape). A lone whitelist is void under a fully-excluded parent, so the
        # heal must append the re-include / re-exclude / whitelist trio.
        (git_repo / ".gitignore").write_text("/tianluo/\n", encoding="utf-8")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "ignore whole se3 tree")

        path = write_intent(git_repo, _make_intent("20260707-heal_0002"))

        assert path.is_file()
        rel = f"{VERSION_INTENT_DIR_RELPATH}/20260707-heal_0002.json"
        assert not _check_ignored(git_repo, rel)
        # read_intent round-trips the healed write.
        loaded = read_intent(git_repo, "20260707-heal_0002")
        assert loaded is not None
        assert loaded.flow_id == "20260707-heal_0002"

    def test_self_heal_is_idempotent(self, git_repo: Path):
        # Two flows writing intents into the same stale repo must not accumulate
        # duplicate whitelist lines.
        (git_repo / ".gitignore").write_text("/tianluo/\n", encoding="utf-8")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "ignore whole se3 tree")

        write_intent(git_repo, _make_intent("20260707-idem_0001"))
        after_first = (git_repo / ".gitignore").read_text(encoding="utf-8")
        write_intent(git_repo, _make_intent("20260707-idem_0002"))
        after_second = (git_repo / ".gitignore").read_text(encoding="utf-8")

        # The second write triggered no further .gitignore change.
        assert after_first == after_second
        lines = after_second.splitlines()
        assert lines.count(f"!/{VERSION_INTENT_DIR_RELPATH}/") == 1
        assert lines.count("!/tianluo/") == 1
        assert lines.count("/tianluo/*") == 1

    def test_heals_whole_dir_with_preexisting_void_whitelist(self, git_repo: Path):
        # The exact state a user reaches by following the OLD error text: tianluo/ is
        # ignored as a whole dir AND a bare ``!/tianluo/version-intents/`` was appended
        # under that still-excluded parent (so it is void). A set-membership dedup
        # would leave that stale whitelist stranded BEFORE the ``/tianluo/*`` re-exclude
        # the heal appends, keeping ``/tianluo/*`` the last match and the path ignored —
        # hard-failing exactly the users who tried the previous guidance. The heal
        # must reposition the whitelist AFTER the re-exclude so the negation wins.
        (git_repo / ".gitignore").write_text(
            f"/tianluo/\n!/{VERSION_INTENT_DIR_RELPATH}/\n", encoding="utf-8"
        )
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "whole-dir ignore plus void whitelist")

        rel = f"{VERSION_INTENT_DIR_RELPATH}/20260707-void_0001.json"
        assert _check_ignored(git_repo, rel)

        path = write_intent(git_repo, _make_intent("20260707-void_0001"))

        assert path.is_file()
        assert not _check_ignored(git_repo, rel)
        lines = (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        # Exactly one whitelist line, and it sits AFTER the ``/tianluo/*`` re-exclude,
        # which in turn follows the ``!/tianluo/`` re-include.
        assert lines.count(f"!/{VERSION_INTENT_DIR_RELPATH}/") == 1
        assert lines.index("!/tianluo/") < lines.index("/tianluo/*")
        assert lines.index("/tianluo/*") < lines.index(f"!/{VERSION_INTENT_DIR_RELPATH}/")
        # read_intent round-trips the healed write.
        loaded = read_intent(git_repo, "20260707-void_0001")
        assert loaded is not None and loaded.flow_id == "20260707-void_0001"

    def test_unrepairable_gitignore_raises_with_worktree_guidance(
        self, git_repo: Path
    ):
        # Self-heal cannot proceed (read-only .gitignore) → fail loud with the
        # updated, truthful guidance and no intent file written.
        gi = git_repo / ".gitignore"
        gi.write_text("/tianluo/\n", encoding="utf-8")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "ignore whole se3 tree")
        gi.chmod(0o444)
        try:
            with pytest.raises(VersionIntentIgnoredError) as exc_info:
                write_intent(git_repo, _make_intent("20260707-ro_0001"))
        finally:
            gi.chmod(0o644)

        msg = str(exc_info.value)
        assert VERSION_INTENT_DIR_RELPATH in msg
        assert ".gitignore" in msg
        assert f"!/{VERSION_INTENT_DIR_RELPATH}/" in msg
        # New, truthful guidance: point at the worktree, drop the false init hint.
        assert "se3 init" not in msg
        assert "worktree" in msg.lower()
        # The intent file must NOT have been written (it could never be staged).
        assert not intent_path(git_repo, "20260707-ro_0001").exists()

    def test_non_utf8_gitignore_raises_not_crashes(self, git_repo: Path):
        # A legacy .gitignore with non-UTF-8 bytes (e.g. a latin-1/GBK comment)
        # that ALSO ignores tianluo/: check-ignore matches on raw bytes so the probe
        # reports ignored, but the heal's UTF-8 read raises UnicodeDecodeError.
        # That is an "unrepairable" case and must degrade to the fail-loud
        # VersionIntentIgnoredError (an OSError subclass the version_analyze step
        # rides), NOT escape as a raw UnicodeDecodeError traceback.
        gi = git_repo / ".gitignore"
        gi.write_bytes(b"# comment \xe9\xe8\n/tianluo/\n")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "non-utf8 gitignore ignoring se3")

        with pytest.raises(VersionIntentIgnoredError) as exc_info:
            write_intent(git_repo, _make_intent("20260707-utf8_0001"))

        assert "se3 init" not in str(exc_info.value)
        assert not intent_path(git_repo, "20260707-utf8_0001").exists()
        # The undecodable file must be left byte-for-byte intact.
        assert gi.read_bytes() == b"# comment \xe9\xe8\n/tianluo/\n"

    def test_unrelated_ignore_rule_rolls_back_mutation(self, git_repo: Path):
        # The intent JSON is ignored by an UNRELATED rule (``*.json``), while
        # tianluo/ itself is tracked. The heal's appended trio cannot clear the ignore
        # (``*.json`` re-matches), so it must fail loud AND roll back — leaving no
        # stray ``/tianluo/*`` line that would newly untrack other tianluo/ files once the
        # user fixes the real rule and resumes.
        gi = git_repo / ".gitignore"
        original = "*.json\n"
        gi.write_text(original, encoding="utf-8")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "ignore all json")

        rel = f"{VERSION_INTENT_DIR_RELPATH}/20260707-unrel_0001.json"
        assert _check_ignored(git_repo, rel)

        with pytest.raises(VersionIntentIgnoredError):
            write_intent(git_repo, _make_intent("20260707-unrel_0001"))

        # Mutation rolled back: no leftover tianluo/ re-exclude lines.
        assert gi.read_text(encoding="utf-8") == original
        assert not intent_path(git_repo, "20260707-unrel_0001").exists()

    def test_heals_gitignore_with_whole_dir_and_anchor_coexisting(
        self, git_repo: Path
    ):
        # A hand-edited legacy .gitignore carrying BOTH a whole-dir ignore
        # (``/tianluo/``) AND a ``/tianluo/*`` anchor. Inserting only the whitelist after
        # the anchor is void — ``/tianluo/`` still excludes the parent so git won't
        # recurse. The heal must ALSO add the ``!/tianluo/`` re-include so the
        # whitelist genuinely takes effect and the worktree flow is not
        # hard-stopped at version_analyze.
        (git_repo / ".gitignore").write_text("/tianluo/\n/tianluo/*\n", encoding="utf-8")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "whole-dir plus anchor, no whitelist")

        rel = f"{VERSION_INTENT_DIR_RELPATH}/20260707-both_0001.json"
        assert _check_ignored(git_repo, rel)

        path = write_intent(git_repo, _make_intent("20260707-both_0001"))

        assert path.is_file()
        assert not _check_ignored(git_repo, rel)
        lines = (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        # Both the re-include and the whitelist are present; the re-include sits
        # before the ``/tianluo/*`` anchor so the directory is re-included first.
        assert "!/tianluo/" in lines
        assert f"!/{VERSION_INTENT_DIR_RELPATH}/" in lines
        assert lines.index("!/tianluo/") < lines.index("/tianluo/*")
        # Idempotent: a second flow's write adds no duplicate re-include line.
        write_intent(git_repo, _make_intent("20260707-both_0002"))
        lines2 = (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert lines2.count("!/tianluo/") == 1

    def test_unreadable_gitignore_is_not_deleted_on_fail_loud(self, git_repo: Path):
        # A pre-existing .gitignore EXISTS but is unreadable (mode 000), while the
        # ignore verdict originates from .git/info/exclude — not this file. The
        # heal cannot read it, so the guard fails loud; but rollback must NEVER
        # destroy a file the heal did not create. The user's .gitignore must
        # survive the raise intact.
        (git_repo / ".git" / "info" / "exclude").write_text(
            "/tianluo/\n", encoding="utf-8"
        )
        gi = git_repo / ".gitignore"
        gi.write_text("# user rules\n*.tmp\n", encoding="utf-8")
        _git(git_repo, "add", ".gitignore")
        _git(git_repo, "commit", "-m", "user gitignore")

        rel = f"{VERSION_INTENT_DIR_RELPATH}/20260707-unread_0001.json"
        assert _check_ignored(git_repo, rel)

        gi.chmod(0o000)
        try:
            with pytest.raises(VersionIntentIgnoredError):
                write_intent(git_repo, _make_intent("20260707-unread_0001"))
            # The user's pre-existing .gitignore must NOT have been unlinked.
            assert gi.exists()
        finally:
            gi.chmod(0o644)

        # And its contents are byte-for-byte intact.
        assert gi.read_text(encoding="utf-8") == "# user rules\n*.tmp\n"
        assert not intent_path(git_repo, "20260707-unread_0001").exists()


class TestReconcileCommitDetection:
    def test_detects_reconcile_commit_by_trailer(self, git_repo: Path):
        flow_id = "20260706-x_9999"
        assert reconcile_commit_exists(git_repo, flow_id) is False

        (git_repo / "pyproject.toml").write_text('version = "1.3.0"\n', encoding="utf-8")
        _git(git_repo, "add", "pyproject.toml")
        _git(
            git_repo,
            "commit",
            "-m",
            f"reconcile: bump to 1.3.0\n\n{RECONCILE_TRAILER}: {flow_id}",
        )

        assert reconcile_commit_exists(git_repo, flow_id) is True
        # A different session must not match this commit's trailer.
        assert reconcile_commit_exists(git_repo, "other_0000") is False

    def test_is_consumed_via_reconcile_commit(self, git_repo: Path):
        """A committed reconcile trailer alone marks the session consumed,

        even when the on-disk intent file's consumed flag was never set.
        """
        flow_id = "20260706-y_8888"
        write_intent(git_repo, _make_intent(flow_id))
        # File flag is still False, but the commit trailer exists.
        assert is_consumed(git_repo, flow_id, check_reconcile_commit=False) is False

        _git(
            git_repo,
            "commit",
            "--allow-empty",
            "-m",
            f"reconcile\n\n{RECONCILE_TRAILER}: {flow_id}",
        )
        assert is_consumed(git_repo, flow_id) is True

    def test_reconcile_commit_exists_non_git_dir(self, tmp_path: Path):
        # No git repo → treated as "not found", never raises.
        assert reconcile_commit_exists(tmp_path, "anything") is False
