"""Tests for ``SyncEngine._update_spec_via_llm`` dual-path behavior.

The G3 refactor split the LLM update flow into three branches:

* **Way A** — the sub-agent edited the spec file directly via the
  ``Edit`` tool, so the disk SHA-256 changed between the pre-call
  snapshot and the post-call snapshot. The engine re-reads the new
  content, validates it with :func:`spec_validator.validate_spec_structure`,
  refreshes the in-memory cache, and counts it as an update. If
  validation fails the engine runs ``git checkout HEAD -- <path>`` to
  roll back the half-baked sub-agent write.
* **Way B** — disk is unchanged but stdout contains the complete new
  spec body. The engine writes stdout to disk after fence-stripping
  and validates it.
* **Way C** — neither happened. The engine logs an error and skips
  this update without touching disk or the cache.

These tests construct a real git repository under ``tmp_path`` so the
Way-A rollback path can exercise the actual ``git checkout`` call.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from se3.engine.sync_engine import (
    DiffType,
    SpecDiff,
    SyncEngine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_SPEC_BODY = (
    "<!-- spec-format: v1 -->\n"
    "# auth Specification\n\n"
    "## Purpose\n\n"
    "Auth spec body that is long enough to clear the 50% length safety "
    "guard used by the sync engine's LLM update path. "
    * 4
    + "\n\n### Requirement: Sample\n\n"
    "- The system SHALL behave correctly under unit tests.\n"
)

META_SUMMARY = (
    "I have enough context from the source code and usage sites to "
    "write the spec. Let me produce it now.\n\n"
    "Summary: created the spec under se3/specs/auth/spec.md.\n"
)


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True)


def _init_git_repo(tmp_path: Path) -> Path:
    """Create a tmp git repo containing a single committed spec file."""
    _run(["git", "init", "-q"], tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], tmp_path)
    _run(["git", "config", "user.name", "Test"], tmp_path)
    _run(["git", "config", "commit.gpgsign", "false"], tmp_path)
    spec_dir = tmp_path / "se3" / "specs" / "auth"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.md"
    spec_path.write_text(VALID_SPEC_BODY, encoding="utf-8")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-q", "-m", "init"], tmp_path)
    return spec_path


def _engine_with_loaded_specs(tmp_path: Path) -> SyncEngine:
    engine = SyncEngine(tmp_path)
    engine._load_specs()
    return engine


def _gap_diff() -> SpecDiff:
    return SpecDiff(
        DiffType.GAP, "auth",
        "Spec describes obsolete behavior", "src/auth.py",
    )


# ---------------------------------------------------------------------------
# Way A — sub-agent edits the spec file directly
# ---------------------------------------------------------------------------

class TestWayAEdit:
    def test_way_a_disk_change_refreshes_cache(self, tmp_path):
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        new_body = VALID_SPEC_BODY.replace("Sample", "Sample-Edited")

        llm = MagicMock()

        def _llm_call(prompt, json_mode):
            # Simulate the sub-agent calling Edit during the LLM call.
            spec_path.write_text(new_body, encoding="utf-8")
            return "I used Edit to update se3/specs/auth/spec.md."

        llm.call.side_effect = _llm_call

        applied, label = engine._apply_spec_drift_update(_gap_diff(), llm)

        assert applied is True
        assert "removed" in label  # GAP → "removed: ..."

        # Disk reflects the sub-agent's edit.
        assert spec_path.read_text(encoding="utf-8") == new_body
        # In-memory cache is refreshed to match disk.
        assert engine._specs["auth"]["content"] == new_body


# ---------------------------------------------------------------------------
# Way B — full rewrite via stdout
# ---------------------------------------------------------------------------

class TestWayBRewrite:
    def test_way_b_disk_unchanged_stdout_carries_full_spec(self, tmp_path):
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        rewrite = VALID_SPEC_BODY.replace("Sample", "Rewritten")

        llm = MagicMock()
        llm.call.return_value = rewrite

        diff = SpecDiff(DiffType.EXTENSION, "auth", "Helper added", "src/u.py:1")
        applied, label = engine._apply_spec_drift_update(diff, llm)

        assert applied is True
        assert "added" in label
        # The engine strips outer whitespace before writing, so compare
        # against the stripped form.
        expected = rewrite.strip()
        assert spec_path.read_text(encoding="utf-8") == expected
        assert engine._specs["auth"]["content"] == expected

    def test_way_b_narrative_preamble_is_purified_before_write(self, tmp_path):
        """Way-B stdout that leads with agentic narrative + tool process is
        purified (sliced to the spec body) before validation and write, so a
        valid body at the tail is not rejected as a narrative first line."""
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        rewrite_body = VALID_SPEC_BODY.replace("Sample", "Purified")
        noisy_stdout = (
            "I have enough context to rewrite the spec. Let me do it now.\n"
            "[tool_use] Read src/auth.py\n"
            "[tool_result] (contents...)\n"
            "Here is the complete updated spec:\n"
            "\n" + rewrite_body
        )

        llm = MagicMock()
        llm.call.return_value = noisy_stdout

        diff = SpecDiff(DiffType.EXTENSION, "auth", "Helper added", "src/u.py:1")
        applied, label = engine._apply_spec_drift_update(diff, llm)

        assert applied is True
        written = spec_path.read_text(encoding="utf-8")
        # Narrative / tool process is gone; spec body persisted to se3/specs.
        assert written.startswith("<!-- spec-format: v1 -->")
        assert "I have enough context" not in written
        assert "tool_use" not in written
        assert spec_path == tmp_path / "se3" / "specs" / "auth" / "spec.md"
        # No flat top-level specs/ file created.
        assert not (tmp_path / "specs").exists()
        assert engine._specs["auth"]["content"] == written


# ---------------------------------------------------------------------------
# Way C — no disk change, stdout carries no spec body
# ---------------------------------------------------------------------------

class TestWayCNoOp:
    def test_way_c_disk_unchanged_stdout_empty_logs_error_and_skips(
        self, tmp_path, caplog
    ):
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        llm = MagicMock()
        llm.call.return_value = "OK done."

        with caplog.at_level(logging.ERROR, logger="se3.engine.sync_engine"):
            applied, label = engine._apply_spec_drift_update(_gap_diff(), llm)

        assert applied is False
        assert label == ""

        # Disk unchanged.
        assert spec_path.read_text(encoding="utf-8") == VALID_SPEC_BODY
        # Cache unchanged.
        assert engine._specs["auth"]["content"] == VALID_SPEC_BODY
        # Error logged.
        assert any(
            "neither a disk edit nor a complete spec body" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Way A — invalid edit triggers git checkout rollback
# ---------------------------------------------------------------------------

class TestWayARollback:
    def test_way_a_meta_summary_triggers_git_checkout(self, tmp_path):
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        llm = MagicMock()

        def _llm_call(prompt, json_mode):
            # Sub-agent writes a 3-line meta summary — fails validation.
            spec_path.write_text(META_SUMMARY, encoding="utf-8")
            return "I wrote a summary of my work."

        llm.call.side_effect = _llm_call

        applied, label = engine._apply_spec_drift_update(_gap_diff(), llm)

        assert applied is False
        assert label == ""

        # The git checkout must have restored the committed content.
        restored = spec_path.read_text(encoding="utf-8")
        assert restored == VALID_SPEC_BODY

        # Cache must reflect the restored disk state, NOT the meta-summary.
        assert engine._specs["auth"]["content"] == VALID_SPEC_BODY


# ---------------------------------------------------------------------------
# Way B — invalid stdout content is rejected without writing disk
# ---------------------------------------------------------------------------

class TestWayBValidation:
    def test_way_b_meta_summary_in_stdout_rejected(self, tmp_path):
        """A meta-summary in stdout that does NOT carry a v1 marker or
        Specification heading is classified as Way-C (skip), so disk
        stays untouched even though stdout was non-empty."""
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        llm = MagicMock()
        llm.call.return_value = META_SUMMARY

        applied, _ = engine._apply_spec_drift_update(_gap_diff(), llm)

        assert applied is False
        assert spec_path.read_text(encoding="utf-8") == VALID_SPEC_BODY


# ---------------------------------------------------------------------------
# LLM raises exception BUT disk was already mutated (Way-A edit mid-flight)
# ---------------------------------------------------------------------------

class TestExceptionWithWayAEdit:
    """The sub-agent may use ``Edit`` to write the spec file, then the
    network drops and the LLM call raises an exception. The engine must
    detect the disk change, validate the new content, and either accept
    it (validation passes) or roll it back (validation fails)."""

    def test_exception_after_way_a_edit_accepted_when_valid(self, tmp_path):
        """LLM raises after writing valid spec → engine accepts the edit."""
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        new_body = VALID_SPEC_BODY.replace("Sample", "Edited-Mid-Crash")

        llm = MagicMock()

        def _llm_call(prompt, json_mode):
            # Sub-agent writes a valid spec body to disk via Edit...
            spec_path.write_text(new_body, encoding="utf-8")
            # ...then the network drops.
            raise RuntimeError("Connection reset by peer")

        llm.call.side_effect = _llm_call

        applied, _ = engine._apply_spec_drift_update(_gap_diff(), llm)

        assert applied is True

        # Disk holds the sub-agent's valid edit.
        assert spec_path.read_text(encoding="utf-8") == new_body
        # Cache is refreshed to match the disk.
        assert engine._specs["auth"]["content"] == new_body

    def test_exception_after_way_a_edit_rolled_back_when_invalid(
        self, tmp_path, caplog
    ):
        """LLM raises after writing a meta-summary → engine rolls back."""
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        llm = MagicMock()

        def _llm_call(prompt, json_mode):
            # Sub-agent writes a meta-summary (invalid spec) to disk...
            spec_path.write_text(META_SUMMARY, encoding="utf-8")
            # ...then the network drops.
            raise RuntimeError("Connection reset by peer")

        llm.call.side_effect = _llm_call

        with caplog.at_level(logging.ERROR, logger="se3.engine.sync_engine"):
            applied, _ = engine._apply_spec_drift_update(_gap_diff(), llm)

        assert applied is False

        # git checkout must have restored the committed content.
        restored = spec_path.read_text(encoding="utf-8")
        assert restored == VALID_SPEC_BODY

        # Cache must reflect the restored disk state, NOT the meta-summary.
        assert engine._specs["auth"]["content"] == VALID_SPEC_BODY

        # Error logged about validation failure.
        assert any(
            "failed validation" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Length guard demotion: 50% shorter accepted with warning, not rejected
# ---------------------------------------------------------------------------

class TestLengthGuardWarning:
    def test_short_but_valid_rewrite_accepted_with_warning(
        self, tmp_path, caplog
    ):
        spec_path = _init_git_repo(tmp_path)
        engine = _engine_with_loaded_specs(tmp_path)

        # A valid but much shorter rewrite (under 50% of original).
        short_valid = (
            "<!-- spec-format: v1 -->\n"
            "# auth Specification\n\n"
            "## Purpose\n\nSlim.\n\n"
            "### Requirement: Tiny\n\n- SHALL be tiny.\n"
        )
        assert len(short_valid) < len(VALID_SPEC_BODY) * 0.5

        llm = MagicMock()
        llm.call.return_value = short_valid

        with caplog.at_level(logging.WARNING, logger="se3.engine.sync_engine"):
            applied, _ = engine._apply_spec_drift_update(_gap_diff(), llm)

        assert applied is True
        assert spec_path.read_text(encoding="utf-8") == short_valid.strip()
        # Warning was emitted but did not block the update.
        assert any(
            "much shorter than original" in rec.message
            for rec in caplog.records
            if rec.levelname == "WARNING"
        )
