"""Boundary tests for DocumentationUpdater (subtask 1.4 cases g-l).

Covers:
- (g) VERSIONS.md is an empty file
- (h) VERSIONS.md has no `# Version History` title
- (i) duplicate version is not re-inserted (content byte-identical)
- (j) README.md with leading YAML front-matter — badge placement
- (k) config-supplied versions_entry_template takes effect
- (l) config missing but versions_md.md template exists — fallback

These exercise Template rendering and update_both's independent error
handling alongside the explicit boundary cases. All filesystem state is
created under tmp_path; the only external dependency (the packaged
versions_md.md template) is monkeypatched where its content matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine import docs_updater
from se3.engine.docs_updater import (
    DocumentationUpdater,
    Template,
    _versions_entry_template_from_file,
)


# ---------------------------------------------------------------------------
# (g) empty VERSIONS.md file
# ---------------------------------------------------------------------------

class TestEmptyVersionsFile:
    def test_empty_file_gets_entry(self, tmp_path, monkeypatch):
        # Force the built-in DEFAULT entry template so this exercises the
        # empty-file insertion mechanics, not the versions_md.md fallback.
        monkeypatch.setattr(
            docs_updater, "_versions_md_template_path",
            lambda: tmp_path / "no-such-template.md",
        )
        versions = tmp_path / "VERSIONS.md"
        versions.write_text("", encoding="utf-8")

        updater = DocumentationUpdater(tmp_path)
        updater.update_versions_md("1.0.0", ["initial entry"])

        content = versions.read_text(encoding="utf-8")
        assert "## 1.0.0" in content
        assert "- initial entry" in content


# ---------------------------------------------------------------------------
# (h) VERSIONS.md without a `# Version History` title
# ---------------------------------------------------------------------------

class TestVersionsWithoutTitle:
    def test_no_title_inserts_before_first_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            docs_updater, "_versions_md_template_path",
            lambda: tmp_path / "no-such-template.md",
        )
        # A file with no markdown heading at all (no `# Version History`
        # title): the new entry is inserted before the first non-header
        # content line.
        versions = tmp_path / "VERSIONS.md"
        versions.write_text(
            "Release notes follow.\n",
            encoding="utf-8",
        )

        updater = DocumentationUpdater(tmp_path)
        updater.update_versions_md("1.1.0", ["new change"])

        content = versions.read_text(encoding="utf-8")
        # New entry present, prior content preserved.
        assert "## 1.1.0" in content
        assert "- new change" in content
        assert "Release notes follow." in content
        # The new entry is inserted before the pre-existing prose.
        assert content.index("## 1.1.0") < content.index("Release notes follow.")


# ---------------------------------------------------------------------------
# (i) duplicate version is not re-inserted
# ---------------------------------------------------------------------------

class TestDuplicateVersionDedup:
    def test_existing_version_not_duplicated(self, tmp_path):
        versions = tmp_path / "VERSIONS.md"
        original = (
            "# Version History\n\n"
            "## 1.2.3 - 2026-01-01\n\n"
            "- already here\n"
        )
        versions.write_text(original, encoding="utf-8")

        updater = DocumentationUpdater(tmp_path)
        updater.update_versions_md("1.2.3", ["should not appear"])

        content = versions.read_text(encoding="utf-8")
        assert content == original
        assert "should not appear" not in content
        assert content.count("## 1.2.3") == 1


# ---------------------------------------------------------------------------
# (j) README.md with leading YAML front-matter
# ---------------------------------------------------------------------------

class TestReadmeFrontMatter:
    def test_badge_inserted_after_heading_not_breaking_front_matter(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text(
            "---\n"
            "title: My Project\n"
            "layout: default\n"
            "---\n"
            "\n"
            "# My Project\n"
            "\n"
            "Some intro content.\n",
            encoding="utf-8",
        )

        updater = DocumentationUpdater(tmp_path)
        updater.update_readme("1.0.0")

        content = readme.read_text(encoding="utf-8")
        # Front-matter intact at the very top.
        assert content.startswith("---\ntitle: My Project\nlayout: default\n---\n")
        # Badge rendered and placed after the heading, after the front-matter.
        badge = "![Version](https://img.shields.io/badge/version-1.0.0-blue)"
        assert badge in content
        assert content.index("# My Project") < content.index(badge)
        assert content.index(badge) < content.index("Some intro content.")

    def test_html_comment_header_is_skipped(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text(
            "<!-- This file is auto-generated. Do not edit. -->\n"
            "# My Project\n"
            "\n"
            "Body.\n",
            encoding="utf-8",
        )

        updater = DocumentationUpdater(tmp_path)
        updater.update_readme("2.0.0")

        content = readme.read_text(encoding="utf-8")
        badge = "![Version](https://img.shields.io/badge/version-2.0.0-blue)"
        assert content.startswith("<!-- This file is auto-generated. Do not edit. -->\n")
        assert content.index("# My Project") < content.index(badge)

    def test_no_heading_prepends_badge(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("Just prose, no heading at all.\n", encoding="utf-8")

        updater = DocumentationUpdater(tmp_path)
        updater.update_readme("3.0.0")

        content = readme.read_text(encoding="utf-8")
        badge = "![Version](https://img.shields.io/badge/version-3.0.0-blue)"
        assert content.startswith(badge)


# ---------------------------------------------------------------------------
# (k) config-supplied versions_entry_template takes effect
# ---------------------------------------------------------------------------

class TestConfigCustomEntryTemplate:
    def test_custom_entry_template_used(self, tmp_path):
        config = {"versions_entry_template": "ENTRY {{version}} | {{changes}}\n"}
        updater = DocumentationUpdater(tmp_path, config=config)

        assert updater.templates["versions_entry"].content == (
            "ENTRY {{version}} | {{changes}}\n"
        )

        updater.update_versions_md("4.5.6", ["custom rendered"])
        content = (tmp_path / "VERSIONS.md").read_text(encoding="utf-8")
        assert "ENTRY 4.5.6 | - custom rendered" in content


# ---------------------------------------------------------------------------
# (l) config missing but versions_md.md template exists — fallback
# ---------------------------------------------------------------------------

class TestVersionsTemplateFileFallback:
    def test_fallback_reads_first_section_block(self, tmp_path, monkeypatch):
        # A versions_md.md whose first ## block is a genuine entry template
        # (carries {{version}}/{{date}}/{{changes}}) is adopted as the
        # versions_entry fallback when config supplies no override.
        template_file = tmp_path / "versions_md.md"
        template_file.write_text(
            "# {project_name} Version History\n"
            "\n"
            "## {{version}} - {{date}}\n"
            "\n"
            "{{changes}}\n"
            "\n"
            "## Older\n"
            "\n"
            "history...\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            docs_updater, "_versions_md_template_path", lambda: template_file
        )

        # No versions_entry_template in config -> fall back to file's
        # first ## block.
        updater = DocumentationUpdater(tmp_path, config={})
        assert updater.templates["versions_entry"].content == (
            "## {{version}} - {{date}}\n\n{{changes}}"
        )

        # End-to-end: the fallback template must actually substitute the
        # real version and the forwarded changes into VERSIONS.md.
        updater.update_versions_md("0.2.0", ["Real change A", "Real change B"])
        content = (tmp_path / "VERSIONS.md").read_text(encoding="utf-8")
        assert "## 0.2.0 - " in content
        assert "- Real change A" in content
        assert "- Real change B" in content
        # The literal template tokens must not leak into the output.
        assert "{{version}}" not in content
        assert "{{changes}}" not in content

    def test_concrete_first_block_rejected_falls_back_to_default(
        self, tmp_path, monkeypatch
    ):
        # The packaged init template's first ## block is a CONCRETE entry
        # (single-brace {date}, hardcoded version, no {{changes}}). It must
        # NOT be adopted as an entry template — doing so would discard the
        # new version/date/changes. Instead, fall back to the DEFAULT
        # placeholder template so rendering works.
        template_file = tmp_path / "versions_md.md"
        template_file.write_text(
            "# {project_name} Version History\n"
            "\n"
            "## 0.1.0 - {date}\n"
            "\n"
            "- Initial release.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            docs_updater, "_versions_md_template_path", lambda: template_file
        )

        assert _versions_entry_template_from_file() is None

        updater = DocumentationUpdater(tmp_path, config={})
        assert updater.templates["versions_entry"].content == (
            DocumentationUpdater.DEFAULT_VERSIONS_ENTRY_TEMPLATE
        )

        # End-to-end: the new version's changelog bullets are written, not
        # the template's hardcoded "0.1.0 / Initial release." entry.
        updater.update_versions_md("0.3.0", ["Synthesized bullet"])
        content = (tmp_path / "VERSIONS.md").read_text(encoding="utf-8")
        assert "## 0.3.0 - " in content
        assert "- Synthesized bullet" in content
        assert "Initial release." not in content
        assert "{date}" not in content

    def test_fallback_to_default_when_file_absent(self, tmp_path, monkeypatch):
        missing = tmp_path / "does-not-exist.md"
        monkeypatch.setattr(
            docs_updater, "_versions_md_template_path", lambda: missing
        )

        assert _versions_entry_template_from_file() is None

        updater = DocumentationUpdater(tmp_path, config={})
        assert updater.templates["versions_entry"].content == (
            DocumentationUpdater.DEFAULT_VERSIONS_ENTRY_TEMPLATE
        )

    def test_config_template_wins_over_file(self, tmp_path, monkeypatch):
        template_file = tmp_path / "versions_md.md"
        template_file.write_text(
            "## From File\n\nshould not be used\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            docs_updater, "_versions_md_template_path", lambda: template_file
        )

        updater = DocumentationUpdater(
            tmp_path, config={"versions_entry_template": "## From Config {{version}}"}
        )
        assert updater.templates["versions_entry"].content == (
            "## From Config {{version}}"
        )


# ---------------------------------------------------------------------------
# update_both independent error handling (cross-check)
# ---------------------------------------------------------------------------

class TestUpdateBothIndependentErrors:
    def test_missing_readme_does_not_block_versions(self, tmp_path):
        updater = DocumentationUpdater(tmp_path)
        results = updater.update_both("1.0.0", ["x"])
        assert results == {"readme": False, "versions": True}
        assert (tmp_path / "VERSIONS.md").exists()

    def test_both_sides_succeed(self, tmp_path):
        (tmp_path / "README.md").write_text(
            "# Proj\n\n"
            "![Version](https://img.shields.io/badge/version-0.1.0-blue)\n",
            encoding="utf-8",
        )
        (tmp_path / "VERSIONS.md").write_text(
            "# Version History\n\n## 0.1.0 - 2026-01-01\n\n- start\n",
            encoding="utf-8",
        )
        updater = DocumentationUpdater(tmp_path)
        results = updater.update_both("1.0.0", ["x"])
        assert results == {"readme": True, "versions": True}


# ---------------------------------------------------------------------------
# Template rendering sanity (used by all of the above)
# ---------------------------------------------------------------------------

class TestTemplateRendering:
    def test_context_overrides_defaults(self):
        t = Template("hello {{name}}", placeholders={"name": "World"})
        assert t.render({"name": "Alice"}) == "hello Alice"

    def test_unknown_placeholder_preserved(self):
        t = Template("{{a}} {{b}}")
        assert t.render({"a": "x"}) == "x {{b}}"
