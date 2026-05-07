"""G4 regression tests for ``version_aggregator`` hardening.

Covers tasks C1–C7 in the G4 group:

  * **C1** — ``current_version >= new_version`` no longer silently
    succeeds; it returns ``success=False`` with
    ``version_already_at_target=True`` so callers can distinguish a
    genuine bump from a degenerate "merge already brought the version"
    state.
  * **C2** — TOML version regex tolerates 0..N whitespace around the
    ``=`` sign.
  * **C3** — ``Version.parse`` failure on the on-disk current version
    is fail-loud (no silent fall-through).
  * **C4** — ``write_version`` is atomic: a partial write does not
    leave pyproject.toml unparseable.
  * **C5** — ``git add`` failure restores pyproject.toml AND clears
    any partially-staged change.
  * **C6** — no bare ``except Exception`` paths remain in
    ``version_aggregator.py``.
  * **C7** — ``write_version`` exceptions restore the original file
    contents.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import se3.engine.merge.version_aggregator as vagg
from se3.engine.merge.version_aggregator import (
    AggregateResult,
    VersionNotAdvanced,
    _atomic_write_text,
    _parse_pyproject_version,
    _safe_write_version,
    aggregate_and_apply,
)
from se3.engine.version_bumper import BumpType


# ---------- helpers ---------- #


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("# Test\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


def _write_pyproject(path: Path, version: str) -> None:
    content = (
        '[build-system]\nrequires = ["setuptools"]\n\n'
        '[project]\n'
        'name = "test-pkg"\n'
        f'version = "{version}"\n'
    )
    (path / "pyproject.toml").write_text(content)


def _commit(path: Path, message: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", message],
        check=True, capture_output=True,
    )


def _setup_with_pyproject(path: Path, version: str) -> None:
    """Init repo, write pyproject, and commit so HEAD is amendable."""
    _init_repo(path)
    _write_pyproject(path, version)
    _commit(path, "Add pyproject")
    # Stand-in for a merge commit so amend has something to overwrite
    (path / "marker.txt").write_text("merged\n")
    _commit(path, "Merge stand-in")


def _read_disk_version(path: Path) -> str | None:
    content = (path / "pyproject.toml").read_text(encoding="utf-8")
    return _parse_pyproject_version(content)


# ---------- C1: VersionNotAdvanced fail-loud ---------- #


class TestC1VersionNotAdvanced:
    """Acceptance: 4.7.0→4.7.0 must fail-loud, 4.6.1→4.7.0 misreport gone."""

    def test_current_equal_to_target_returns_failure(self, tmp_path: Path) -> None:
        """When current == new_version, return success=False."""
        _setup_with_pyproject(tmp_path, "4.7.0")
        # Force aggregator to compute new = 4.7.0 + bump != 4.7.0
        # but mock disk read to return 4.7.0 ahead of the comparison.
        # Actually a cleaner test: pre-merge=4.6.1, bump=MINOR → new=4.7.0.
        # Disk currently has 4.7.0 → current >= new → fail-loud.
        _write_pyproject(tmp_path, "4.7.0")  # disk = 4.7.0
        result = aggregate_and_apply(
            tmp_path, [BumpType.MINOR], "4.6.1"
        )
        assert result.success is False
        assert result.version_already_at_target is True
        assert result.error is not None
        assert "VersionNotAdvanced" in result.error
        # new_version is set to the on-disk value (4.7.0), not the
        # computed target — callers should see what's actually there.
        assert result.new_version == "4.7.0"

    def test_current_higher_than_target_returns_failure(self, tmp_path: Path) -> None:
        """When current > new_version, fail-loud with explicit reason."""
        _setup_with_pyproject(tmp_path, "5.0.0")
        # pre-merge=4.4.0, bump=PATCH → target 4.4.1; disk=5.0.0 (way ahead)
        _write_pyproject(tmp_path, "5.0.0")
        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        assert result.success is False
        assert result.version_already_at_target is True
        assert result.error is not None
        assert "VersionNotAdvanced" in result.error
        assert "higher" in result.error
        assert result.new_version == "5.0.0"

    def test_4_7_0_to_4_7_0_fails_loud(self, tmp_path: Path) -> None:
        """Spec acceptance: 4.7.0→4.7.0 must fail-loud."""
        _setup_with_pyproject(tmp_path, "4.7.0")
        # pre = 4.7.0, bump = PATCH → target 4.7.1.  Force disk to
        # already be 4.7.0 (matches pre AND is below target normally,
        # so this exercises pre==current<target).  But to specifically
        # land on "4.7.0→4.7.0" the on-disk version must equal the
        # target.  We use bump=PATCH for unconditional advance and
        # then write the disk to target:
        _write_pyproject(tmp_path, "4.7.1")  # disk = target
        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.7.0"
        )
        # current 4.7.1 == new 4.7.1 → fail-loud
        assert result.success is False
        assert result.version_already_at_target is True
        assert "VersionNotAdvanced" in (result.error or "")

    def test_misreport_4_6_1_to_4_7_0_no_longer_happens(self, tmp_path: Path) -> None:
        """Spec acceptance: 误报 4.6.1→4.7.0 不再发生.

        With the C1 fix, calling aggregate with pre=4.6.1 against a
        disk that's already at 4.7.0 returns version_already_at_target
        instead of silently reporting success.
        """
        _setup_with_pyproject(tmp_path, "4.7.0")
        _write_pyproject(tmp_path, "4.7.0")
        result = aggregate_and_apply(
            tmp_path, [BumpType.MINOR], "4.6.1"
        )
        assert result.success is False
        assert result.version_already_at_target is True
        # The function did NOT modify pyproject.toml — disk still 4.7.0
        assert _read_disk_version(tmp_path) == "4.7.0"
        # The function did NOT amend the commit either
        # (no new commit since the marker commit)
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--format=%s"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()
        # initial / Add pyproject / Merge stand-in
        assert len(log) == 3

    def test_normal_bump_still_succeeds(self, tmp_path: Path) -> None:
        """C1 fix does not regress the normal happy-path."""
        _setup_with_pyproject(tmp_path, "4.4.0")
        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        assert result.success is True
        assert result.version_already_at_target is False
        assert result.new_version == "4.4.1"
        assert _read_disk_version(tmp_path) == "4.4.1"

    def test_version_not_advanced_class_constructor(self) -> None:
        """The VersionNotAdvanced exception carries structured fields."""
        exc = VersionNotAdvanced("4.6.1", "4.7.0", "4.7.0")
        assert exc.pre_version == "4.6.1"
        assert exc.current_version == "4.7.0"
        assert exc.target_version == "4.7.0"
        assert "VersionNotAdvanced" in str(exc)
        assert "4.7.0" in str(exc)
        assert "4.6.1" in str(exc)


# ---------- C2: TOML regex tolerates 0..N whitespace ---------- #


class TestC2RegexTolerance:
    """The version field regex must tolerate any whitespace around `=`."""

    def test_no_space_double_quote(self) -> None:
        content = '[project]\nname = "x"\nversion="1.2.3"\n'
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_no_space_single_quote(self) -> None:
        content = "[project]\nname = 'x'\nversion='1.2.3'\n"
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_one_space_each_side(self) -> None:
        content = '[project]\nversion = "1.2.3"\n'
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_many_spaces_each_side(self) -> None:
        content = '[project]\nversion    =    "1.2.3"\n'
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_tab_around_equals(self) -> None:
        content = '[project]\nversion\t=\t"1.2.3"\n'
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_mixed_spaces_and_tabs(self) -> None:
        content = '[project]\nversion \t = \t "1.2.3"\n'
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_indented_version_still_parses(self) -> None:
        """Indented version lines (rare but valid) still parse."""
        content = '[project]\n    version = "1.2.3"\n'
        assert _parse_pyproject_version(content) == "1.2.3"


# ---------- C3: Version.parse fail-loud ---------- #


class TestC3VersionParseFailLoud:
    """When Version.parse on current_version fails, fail-loud."""

    def test_unparseable_current_version_returns_failure(
        self, tmp_path: Path
    ) -> None:
        """If pyproject.toml has an unparseable version, refuse to write."""
        _init_repo(tmp_path)
        # Write pyproject with an obviously bad version string
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "not-a-semver"\n'
        )
        _commit(tmp_path, "Add unparseable pyproject")
        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        # Function refuses to overwrite — fail-loud
        assert result.success is False
        assert result.error is not None
        assert "unparseable" in result.error.lower()
        # Disk content is unchanged — file still has "not-a-semver"
        content = (tmp_path / "pyproject.toml").read_text()
        assert "not-a-semver" in content

    def test_unparseable_pre_merge_version_returns_failure(
        self, tmp_path: Path
    ) -> None:
        """When pre_merge_version itself is unparseable, fail-loud."""
        _setup_with_pyproject(tmp_path, "4.4.0")
        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "garbage"
        )
        assert result.success is False
        assert result.error is not None
        assert "pre_merge_version" in result.error
        # Disk version unchanged
        assert _read_disk_version(tmp_path) == "4.4.0"


# ---------- C4: atomic write ---------- #


class TestC4AtomicWrite:
    """Partial writes must NEVER leave pyproject.toml unparseable."""

    def test_atomic_write_replaces_file(self, tmp_path: Path) -> None:
        """_atomic_write_text writes content via temp+rename."""
        target = tmp_path / "demo.txt"
        target.write_text("original content")
        _atomic_write_text(target, "new content")
        assert target.read_text() == "new content"

    def test_atomic_write_no_temp_files_remain(self, tmp_path: Path) -> None:
        """After successful write, no .tmp files are left in the directory."""
        target = tmp_path / "demo.txt"
        target.write_text("original")
        _atomic_write_text(target, "updated")
        # Only the target file should exist (and any pre-existing files),
        # no leftover .tmp files
        leftover = [
            p for p in tmp_path.iterdir()
            if p.name.startswith("demo.txt.") and ".tmp" in p.name
        ]
        assert leftover == []

    def test_safe_write_version_uses_atomic_write(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """_safe_write_version goes through _atomic_write_text."""
        target = tmp_path / "pyproject.toml"
        _write_pyproject(tmp_path, "1.0.0")
        captured = []

        def fake_atomic(path, content):
            captured.append((path, content))
            # Actually do the write so subsequent steps see it
            path.write_text(content)

        monkeypatch.setattr(vagg, "_atomic_write_text", fake_atomic)
        _safe_write_version(target, "2.0.0")
        assert len(captured) == 1
        assert captured[0][0] == target
        assert 'version = "2.0.0"' in captured[0][1]

    def test_safe_write_version_handles_no_space_format(
        self, tmp_path: Path
    ) -> None:
        """_safe_write_version works on no-space format (C2 + C4 combo)."""
        target = tmp_path / "pyproject.toml"
        target.write_text(
            '[project]\nname="x"\nversion="1.0.0"\n'
        )
        _safe_write_version(target, "2.0.0")
        content = target.read_text()
        assert 'version="2.0.0"' in content

    def test_safe_write_version_preserves_inline_array(
        self, tmp_path: Path
    ) -> None:
        """A pyproject with `keywords = ["py"]` before version still parses."""
        target = tmp_path / "pyproject.toml"
        target.write_text(
            '[project]\n'
            'name = "x"\n'
            'keywords = ["py"]\n'
            'version = "1.0.0"\n'
        )
        _safe_write_version(target, "2.0.0")
        content = target.read_text()
        assert 'version = "2.0.0"' in content
        assert 'keywords = ["py"]' in content

    def test_safe_write_version_raises_on_missing_field(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "pyproject.toml"
        target.write_text('[project]\nname = "x"\n')
        with pytest.raises(ValueError, match="version field"):
            _safe_write_version(target, "2.0.0")

    def test_disk_still_parseable_after_write_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """C4 acceptance: simulated write failure → file stays parseable."""
        _setup_with_pyproject(tmp_path, "4.4.0")
        original_disk = _read_disk_version(tmp_path)
        assert original_disk == "4.4.0"

        def boom(path, content):
            raise OSError("simulated disk error")

        monkeypatch.setattr(vagg, "_atomic_write_text", boom)

        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        assert result.success is False
        assert "simulated disk error" in (result.error or "")
        # File MUST still be parseable AND at the original version
        assert _read_disk_version(tmp_path) == "4.4.0"


# ---------- C5: git add failure → no staged residue ---------- #


class TestC5GitAddFailureCleanup:
    """A git-add failure must restore the file AND clear any staged residue."""

    def test_no_staged_residue_after_add_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """git add returncode != 0 → pyproject.toml is restored AND
        not left in staged state."""
        _setup_with_pyproject(tmp_path, "4.4.0")
        orig_run_git = vagg._run_git

        def fake_run_git(project_root, *args, **kwargs):
            if args == ("add", "pyproject.toml"):
                return subprocess.CompletedProcess(
                    args=args, returncode=1,
                    stdout="", stderr="permission denied",
                )
            return orig_run_git(project_root, *args, **kwargs)

        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        assert result.success is False
        assert "permission denied" in (result.error or "")
        # Disk version restored
        assert _read_disk_version(tmp_path) == "4.4.0"
        # No staged changes
        status = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert "pyproject.toml" not in status

    def test_git_add_oserror_resets_staged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """git add raising OSError → restore + reset staged."""
        _setup_with_pyproject(tmp_path, "4.4.0")
        orig_run_git = vagg._run_git

        def fake_run_git(project_root, *args, **kwargs):
            if args == ("add", "pyproject.toml"):
                raise OSError("subprocess crashed")
            return orig_run_git(project_root, *args, **kwargs)

        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        assert result.success is False
        assert "OSError" in (result.error or "")
        assert _read_disk_version(tmp_path) == "4.4.0"
        # No staged changes
        status = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert "pyproject.toml" not in status


# ---------- C6: no bare except Exception ---------- #


class TestC6NoBareExcept:
    """Acceptance: grep 'except Exception:' in version_aggregator returns 0 hits."""

    def test_no_bare_except_in_module(self) -> None:
        """version_aggregator.py contains no bare ``except Exception:``.

        Bare ``except Exception:`` swallows information that a more
        specific class plus ``logger.exception`` would preserve.
        """
        module_file = Path(vagg.__file__)
        content = module_file.read_text(encoding="utf-8")
        # Match bare `except Exception:` (no `as` clause for at least
        # the legacy unbound form) — but allow `except Exception as exc`
        # because that DOES bind the exception for logging.
        bare_except_re = re.compile(r"^\s*except\s+Exception\s*:", re.MULTILINE)
        matches = bare_except_re.findall(content)
        assert matches == [], (
            f"Found bare `except Exception:` in {module_file}: "
            f"{len(matches)} occurrence(s)"
        )


# ---------- C7: write_version exception → restore ---------- #


class TestC7AmendExceptionRestore:
    """Amend exception must restore pyproject.toml and unstage."""

    def test_amend_oserror_restores_and_resets(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """git commit --amend raising OSError → restore + reset."""
        _setup_with_pyproject(tmp_path, "4.4.0")
        orig_run_git = vagg._run_git

        def fake_run_git(project_root, *args, **kwargs):
            if args[:2] == ("commit", "--amend"):
                raise OSError("amend crashed")
            return orig_run_git(project_root, *args, **kwargs)

        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        assert result.success is False
        assert "OSError" in (result.error or "")
        # Disk restored
        assert _read_disk_version(tmp_path) == "4.4.0"
        # Index restored — no staged pyproject.toml
        status = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert "pyproject.toml" not in status

    def test_write_version_exception_restores(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When _safe_write_version raises, pyproject.toml is restored."""
        _setup_with_pyproject(tmp_path, "4.4.0")
        # Save original content for comparison
        original = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

        def boom(path, content):
            # Write something garbage to the file BEFORE raising,
            # to simulate a partial write
            path.write_text("garbage\nnot a TOML\n")
            raise OSError("simulated")

        monkeypatch.setattr(vagg, "_safe_write_version", boom)

        result = aggregate_and_apply(
            tmp_path, [BumpType.PATCH], "4.4.0"
        )
        assert result.success is False
        # The aggregator must restore the original content
        restored = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert restored == original
        # Version is parseable
        assert _read_disk_version(tmp_path) == "4.4.0"


# ---------- AggregateResult dataclass extension ---------- #


class TestAggregateResultDefaults:
    def test_default_values_include_version_already_at_target(self) -> None:
        r = AggregateResult()
        assert r.success is False
        assert r.pre_version is None
        assert r.new_version is None
        assert r.bump_type is None
        assert r.error is None
        assert r.version_already_at_target is False
