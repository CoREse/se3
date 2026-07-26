"""Tests for ``_read_version_rules_file`` — project-level version-rules.md loader.

Covers the four branches of the loader:
- (a) file absent → None
- (b) normal-sized file → full content returned
- (c) oversized file → truncated content + warning
- (d) unreadable file (OSError) → None + warning
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.engine.steps.version_analyze import (
    VERSION_RULES_FILE_RELPATH,
    VERSION_RULES_MAX_BYTES,
    _read_version_rules_file,
)


class TestVersionRulesFileLoader:
    """Cover every branch of _read_version_rules_file."""

    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        """No se3/version-rules.md file → loader returns None silently."""
        assert _read_version_rules_file(tmp_path) is None

    def test_returns_none_when_se3_dir_missing(self, tmp_path: Path) -> None:
        """Project root without an se3/ dir at all → loader returns None."""
        # tmp_path is intentionally empty
        assert _read_version_rules_file(tmp_path) is None

    def test_returns_full_content_for_normal_file(self, tmp_path: Path) -> None:
        """A normal Markdown rules file is returned verbatim."""
        rules_dir = tmp_path / "se3"
        rules_dir.mkdir()
        content = (
            "# Project Version Rules\n\n"
            "- docs-only commits → no version bump\n"
            "- security patches always bump minor\n"
        )
        (rules_dir / "version-rules.md").write_text(content, encoding="utf-8")

        result = _read_version_rules_file(tmp_path)

        assert result is not None
        # Content is returned verbatim and untruncated
        assert result == content
        assert "Truncated by SE3" not in result

    def test_truncates_oversized_file_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """File over the byte cap → truncated payload + truncation notice + warning."""
        rules_dir = tmp_path / "se3"
        rules_dir.mkdir()
        # 1 KB past the cap to comfortably exceed it
        oversized = "x" * (VERSION_RULES_MAX_BYTES + 1024)
        (rules_dir / "version-rules.md").write_text(oversized, encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            result = _read_version_rules_file(tmp_path)

        assert result is not None
        # The bulk of the input is preserved up to the cap (notice is appended)
        assert result.startswith("x" * 1024)
        # An explicit truncation notice was appended
        assert "Truncated by SE3" in result
        # A warning was logged that names the cap-exceeded condition
        assert any("exceeds" in rec.message for rec in caplog.records)

    def test_returns_none_when_file_unreadable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An OSError raised while reading the file degrades to None + warning."""
        rules_dir = tmp_path / "se3"
        rules_dir.mkdir()
        rules_path = rules_dir / "version-rules.md"
        rules_path.write_text("rules go here", encoding="utf-8")

        original_read_bytes = Path.read_bytes

        def _raising_read_bytes(self: Path) -> bytes:
            # Only break the version-rules.md read, leave others alone
            if self == rules_path:
                raise OSError("permission denied")
            return original_read_bytes(self)

        with caplog.at_level(logging.WARNING):
            with patch.object(Path, "read_bytes", _raising_read_bytes):
                result = _read_version_rules_file(tmp_path)

        assert result is None
        assert any("Could not read" in rec.message for rec in caplog.records)


class TestVersionRulesFileRelPath:
    """The relpath constant is the contract — pin it explicitly."""

    def test_relpath_is_the_documented_location(self) -> None:
        """The loader looks at se3/version-rules.md inside project_root."""
        assert VERSION_RULES_FILE_RELPATH == "se3/version-rules.md"
