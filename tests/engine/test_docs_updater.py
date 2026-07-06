"""Tests for VERSIONS.md changelog merge and head-blank hygiene.

Covers the docs_updater changes that stop changelog entries from being
silently swallowed on a version collision, and that drain / prevent the
head-blank accumulation in VERSIONS.md:

- an already-present version has its block *merged* into, not discarded;
- a verbatim re-write is a no-op (idempotent merge);
- repeated inserts never accumulate blank lines at the head;
- head cleanup collapses a large blank run to a single separator and is
  stable under repeated runs (idempotent);
- pre-existing changelog history is preserved across all of the above.
"""

from __future__ import annotations

from se3.engine import docs_updater
from se3.engine.docs_updater import DocumentationUpdater


def _default_updater(tmp_path, monkeypatch):
    """Updater forced onto the built-in DEFAULT entry template.

    Pins the ``## {{version}} - {{date}}`` template so these tests exercise
    insertion/merge mechanics independently of whatever packaged
    versions_md.md happens to be on disk.
    """
    monkeypatch.setattr(
        docs_updater,
        "_versions_md_template_path",
        lambda: tmp_path / "no-such-template.md",
    )
    return DocumentationUpdater(tmp_path)


# ---------------------------------------------------------------------------
# Merge instead of silent discard
# ---------------------------------------------------------------------------

class TestMergeIntoExistingVersion:
    def test_new_entry_merged_not_swallowed(self, tmp_path, monkeypatch):
        versions = tmp_path / "VERSIONS.md"
        versions.write_text(
            "# Version History\n\n"
            "## 11.12.0 - 2026-07-06\n\n"
            "- feature A change\n",
            encoding="utf-8",
        )
        updater = _default_updater(tmp_path, monkeypatch)

        # A second concurrent flow lands on the same version — its bullet
        # must survive rather than be dropped.
        updater.update_versions_md("11.12.0", ["feature B change"])

        content = versions.read_text(encoding="utf-8")
        assert content.count("## 11.12.0") == 1
        assert "- feature A change" in content
        assert "- feature B change" in content
        # B is appended after A within the same block.
        assert content.index("feature A change") < content.index("feature B change")

    def test_merge_preserves_older_history(self, tmp_path, monkeypatch):
        versions = tmp_path / "VERSIONS.md"
        versions.write_text(
            "# Version History\n\n"
            "## 2.0.0 - 2026-02-01\n\n"
            "- newest\n"
            "## 1.0.0 - 2026-01-01\n\n"
            "- oldest\n",
            encoding="utf-8",
        )
        updater = _default_updater(tmp_path, monkeypatch)
        updater.update_versions_md("2.0.0", ["merged bullet"])

        content = versions.read_text(encoding="utf-8")
        # Older block untouched; the merged bullet stays inside 2.0.0's block.
        assert "## 1.0.0 - 2026-01-01" in content
        assert "- oldest" in content
        assert content.index("merged bullet") < content.index("## 1.0.0")

    def test_verbatim_rewrite_idempotent(self, tmp_path, monkeypatch):
        versions = tmp_path / "VERSIONS.md"
        versions.write_text(
            "# Version History\n\n"
            "## 3.1.0 - 2026-03-01\n\n"
            "- only change\n",
            encoding="utf-8",
        )
        updater = _default_updater(tmp_path, monkeypatch)
        updater.update_versions_md("3.1.0", ["only change"])
        first = versions.read_text(encoding="utf-8")
        updater.update_versions_md("3.1.0", ["only change"])
        second = versions.read_text(encoding="utf-8")

        assert first.count("- only change") == 1
        assert first == second


# ---------------------------------------------------------------------------
# Head-blank hygiene
# ---------------------------------------------------------------------------

class TestNoBlankAccumulation:
    def test_repeated_inserts_do_not_grow_head(self, tmp_path, monkeypatch):
        versions = tmp_path / "VERSIONS.md"
        versions.write_text("# Version History\n\n", encoding="utf-8")
        updater = _default_updater(tmp_path, monkeypatch)

        for i in range(5):
            updater.update_versions_md(f"1.0.{i}", [f"change {i}"])

        content = versions.read_text(encoding="utf-8")
        lines = content.split("\n")
        # Exactly one blank line between the title and the first entry.
        assert lines[0] == "# Version History"
        assert lines[1] == ""
        assert lines[2].startswith("## ")
        # No run of consecutive blank lines anywhere in the head region.
        assert "\n\n\n" not in content

    def test_existing_head_accumulation_is_drained(self, tmp_path, monkeypatch):
        versions = tmp_path / "VERSIONS.md"
        # Simulate the ~89-blank-line head accumulation.
        versions.write_text(
            "# Version History\n" + ("\n" * 89) + "## 1.0.0 - 2026-01-01\n\n- old\n",
            encoding="utf-8",
        )
        updater = _default_updater(tmp_path, monkeypatch)
        updater.update_versions_md("2.0.0", ["new"])

        content = versions.read_text(encoding="utf-8")
        lines = content.split("\n")
        assert lines[0] == "# Version History"
        assert lines[1] == ""
        assert lines[2].startswith("## 2.0.0")
        assert "- old" in content
        assert "\n\n\n" not in content


class TestHeadCleanupIdempotent:
    def test_cleanup_is_stable(self, tmp_path, monkeypatch):
        versions = tmp_path / "VERSIONS.md"
        versions.write_text(
            "# Version History\n" + ("\n" * 50) + "## 1.0.0 - 2026-01-01\n\n- old\n",
            encoding="utf-8",
        )
        updater = _default_updater(tmp_path, monkeypatch)

        # First insert drains the head; a second (no-op merge on the same
        # version and bullet) must leave the file byte-stable.
        updater.update_versions_md("1.0.0", ["extra"])
        after_first = versions.read_text(encoding="utf-8")
        updater.update_versions_md("1.0.0", ["extra"])
        after_second = versions.read_text(encoding="utf-8")

        assert after_first == after_second
        assert after_first.split("\n")[1] == ""
        assert "\n\n\n" not in after_first

    def test_normalize_head_blanks_directly_idempotent(self, tmp_path):
        updater = DocumentationUpdater(tmp_path)
        raw = "# Version History\n" + ("\n" * 40) + "## 1.0.0 - 2026-01-01\n\n- x\n"
        once = updater._normalize_head_blanks(raw)
        twice = updater._normalize_head_blanks(once)
        assert once == twice
        lines = once.split("\n")
        assert lines[0] == "# Version History"
        assert lines[1] == ""
        assert lines[2].startswith("## 1.0.0")
