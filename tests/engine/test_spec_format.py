"""Tests for spec_format.py parser and validator.

Covers parse_spec boundary handling, tags/keywords/refs extraction,
validate issue detection, and snapshot tests against real spec files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.spec_format import (
    SPEC_FORMAT_VERSION_MARKER,
    Issue,
    ParsedSpec,
    Requirement,
    parse_spec,
    validate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(text: str) -> str:
    """Wrap raw requirement body text in a minimal spec structure."""
    return f"# Test Spec\n\n## Purpose\nTest purpose.\n\n## Requirements\n\n{text}"


# ---------------------------------------------------------------------------
# parse_spec — basic behaviour
# ---------------------------------------------------------------------------

class TestParseSpecBasic:
    """Smoke tests for parse_spec core behaviour."""

    def test_empty_spec_no_requirements(self):
        parsed = parse_spec("# Title\n\n## Purpose\n\nSome text.")
        assert parsed.requirements == []
        assert "## Purpose" in parsed.header_text
        assert parsed.has_v1_marker is False

    def test_v1_marker_detected(self):
        text = f"{SPEC_FORMAT_VERSION_MARKER}\n\n# Title\n\n## Purpose\n\n### Requirement: Foo\nBody.\n"
        parsed = parse_spec(text)
        assert parsed.has_v1_marker is True
        assert len(parsed.requirements) == 1
        assert parsed.requirements[0].name == "Foo"

    def test_no_v1_marker(self):
        text = "# Title\n\n## Purpose\n\n### Requirement: Bar\nBody.\n"
        parsed = parse_spec(text)
        assert parsed.has_v1_marker is False
        assert len(parsed.requirements) == 1

    def test_header_excludes_requirements(self):
        text = (
            "# Spec\n\n## Purpose\nPurpose text.\n\n"
            "### Requirement: One\nOne body.\n\n"
            "### Requirement: Two\nTwo body.\n"
        )
        parsed = parse_spec(text)
        assert "### Requirement: One" not in parsed.header_text
        assert "One body" not in parsed.header_text
        assert "Purpose text" in parsed.header_text
        assert len(parsed.requirements) == 2

    def test_multiple_requirements(self):
        text = _make_spec(
            "### Requirement: First\nFirst body.\n\n"
            "### Requirement: Second\nSecond body.\n\n"
            "### Requirement: Third\nThird body.\n"
        )
        parsed = parse_spec(text)
        assert len(parsed.requirements) == 3
        names = [r.name for r in parsed.requirements]
        assert names == ["First", "Second", "Third"]

    def test_requirement_body_excludes_next_boundary(self):
        text = _make_spec(
            "### Requirement: A\nA body.\n\n"
            "### Requirement: B\nB body.\n"
        )
        parsed = parse_spec(text)
        assert "### Requirement: B" not in parsed.requirements[0].body
        assert parsed.requirements[0].body == "A body."

    def test_line_start_tracking(self):
        text = (
            "# Spec\n\n## Purpose\nP.\n\n"
            "### Requirement: First\nBody 1.\n\n"
            "### Requirement: Second\nBody 2.\n"
        )
        parsed = parse_spec(text)
        # line 1: # Spec
        # line 2: (empty)
        # line 3: ## Purpose
        # line 4: P.
        # line 5: (empty)
        # line 6: ### Requirement: First
        assert parsed.requirements[0].line_start == 6
        # line 7: Body 1.
        # line 8: (empty)
        # line 9: ### Requirement: Second
        assert parsed.requirements[1].line_start == 9


# ---------------------------------------------------------------------------
# parse_spec — code-block filtering
# ---------------------------------------------------------------------------

class TestParseSpecCodeBlocks:
    """Requirement headers inside fenced code blocks must be ignored."""

    def test_requirement_in_code_block_ignored(self):
        text = _make_spec(
            "### Requirement: Real\nReal body.\n\n"
            "```markdown\n"
            "### Requirement: Fake\nFake body.\n"
            "```\n\n"
            "### Requirement: AlsoReal\nAlso real body.\n"
        )
        parsed = parse_spec(text)
        names = [r.name for r in parsed.requirements]
        assert names == ["Real", "AlsoReal"]

    def test_code_block_with_language_tag(self):
        text = _make_spec(
            "### Requirement: Alpha\nAlpha body.\n\n"
            "```python\n"
            "### Requirement: Beta\nBeta body.\n"
            "```\n\n"
            "### Requirement: Gamma\nGamma body.\n"
        )
        parsed = parse_spec(text)
        names = [r.name for r in parsed.requirements]
        assert "Beta" not in names
        assert names == ["Alpha", "Gamma"]


# ---------------------------------------------------------------------------
# parse_spec — tags and keywords
# ---------------------------------------------------------------------------

class TestParseSpecTagsKeywords:
    """Extraction of **tags** and **keywords** lines."""

    def test_tags_extracted(self):
        text = _make_spec(
            "### Requirement: Auth\n"
            "Auth body.\n\n"
            "**tags**: security, api, auth\n"
        )
        parsed = parse_spec(text)
        assert parsed.requirements[0].tags == ["security", "api", "auth"]

    def test_keywords_extracted(self):
        text = _make_spec(
            "### Requirement: Auth\n"
            "Auth body.\n\n"
            "**keywords**: OAuth2, bearer token\n"
        )
        parsed = parse_spec(text)
        assert parsed.requirements[0].keywords == ["OAuth2", "bearer token"]

    def test_tags_and_keywords_case_insensitive(self):
        text = _make_spec(
            "### Requirement: Auth\n"
            "Auth body.\n\n"
            "**TAGS**: A, B\n"
            "**Keywords**: X, Y\n"
        )
        parsed = parse_spec(text)
        assert parsed.requirements[0].tags == ["A", "B"]
        assert parsed.requirements[0].keywords == ["X", "Y"]

    def test_missing_tags_defaults_empty(self):
        text = _make_spec("### Requirement: Plain\nPlain body.\n")
        parsed = parse_spec(text)
        assert parsed.requirements[0].tags == []
        assert parsed.requirements[0].keywords == []

    def test_whitespace_around_values_trimmed(self):
        text = _make_spec(
            "### Requirement: R\n"
            "Body.\n\n"
            "**tags**:  foo ,  bar  \n"
        )
        parsed = parse_spec(text)
        assert parsed.requirements[0].tags == ["foo", "bar"]

    def test_empty_tags_line(self):
        text = _make_spec(
            "### Requirement: R\n"
            "Body.\n\n"
            "**tags**: \n"
        )
        parsed = parse_spec(text)
        assert parsed.requirements[0].tags == []

    def test_multiple_tags_lines_all_captured(self):
        """Multiple **tags** lines (merge artifact) are all captured."""
        text = _make_spec(
            "### Requirement: R\n"
            "Body.\n\n"
            "**tags**: foo\n"
            "**tags**: bar, baz\n"
        )
        parsed = parse_spec(text)
        assert parsed.requirements[0].tags == ["foo", "bar", "baz"]


# ---------------------------------------------------------------------------
# parse_spec — refs extraction
# ---------------------------------------------------------------------------

class TestParseSpecRefs:
    """Extraction of intra-spec and inter-spec references."""

    def test_intra_spec_ref_detected(self):
        text = _make_spec(
            "### Requirement: A\n"
            "See also Requirement: B for details.\n"
        )
        parsed = parse_spec(text)
        # Stop-word truncation prevents trailing prose capture
        assert "B" in parsed.requirements[0].refs
        assert "B for details" not in parsed.requirements[0].refs

    def test_inter_spec_ref_detected(self):
        text = _make_spec(
            "### Requirement: A\n"
            "As defined in flow-engine::State Machine.\n"
        )
        parsed = parse_spec(text)
        assert "flow-engine::State Machine" in parsed.requirements[0].refs

    def test_multiple_refs_deduplicated(self):
        text = _make_spec(
            "### Requirement: A\n"
            "See Requirement: B.\n"
            "Also see Requirement: B.\n"
            "Also see spec::Other Thing.\n"
        )
        parsed = parse_spec(text)
        refs = parsed.requirements[0].refs
        # Tightened regex strips the trailing period; "B" appears once
        assert refs.count("B") == 1
        assert "spec::Other Thing" in refs

    def test_ref_in_code_block_skipped(self):
        """Refs inside fenced code blocks must not be extracted."""
        text = _make_spec(
            "### Requirement: A\n"
            "Example template:\n"
            "```markdown\n"
            "### Requirement: FakeRef\n"
            "Body.\n"
            "```\n"
            "See Requirement: RealRef.\n"
        )
        parsed = parse_spec(text)
        refs = parsed.requirements[0].refs
        assert "FakeRef" not in refs
        assert "RealRef" in refs

    def test_no_refs_empty_list(self):
        text = _make_spec("### Requirement: Lone\nNo references here.\n")
        parsed = parse_spec(text)
        assert parsed.requirements[0].refs == []

    def test_ref_in_table_row_skipped(self):
        """Refs inside markdown table rows must not be extracted."""
        text = _make_spec(
            "### Requirement: A\n"
            "| Col1 | Col2 |\n"
            "|------|------|\n"
            "| Requirement: Fake | value |\n"
            "\n"
            "See Requirement: RealRef.\n"
        )
        parsed = parse_spec(text)
        refs = parsed.requirements[0].refs
        assert "Fake" not in refs
        assert "RealRef" in refs


# ---------------------------------------------------------------------------
# validate — issue detection
# ---------------------------------------------------------------------------

class TestValidateIssues:
    """Constructive tests covering each Issue type."""

    def test_duplicate_requirement_names_error(self):
        text = _make_spec(
            "### Requirement: Dup\nFirst.\n\n"
            "### Requirement: Dup\nSecond.\n"
        )
        parsed = parse_spec(text)
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        assert any("Duplicate Requirement name" in i.message for i in errors)

    def test_illegal_chars_in_name_error(self):
        text = _make_spec("### Requirement: Bad\x01Name\nBody.\n")
        parsed = parse_spec(text)
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        assert any("illegal characters" in i.message for i in errors)

    def test_empty_requirement_name_error(self):
        # The parser regex already rejects empty names (.+ requires at
        # least one char), so test the validator directly.
        parsed = parse_spec("# Title\n\n## Purpose\nP.\n")
        parsed.requirements.append(Requirement(name="", body="Body."))
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        assert any("empty" in i.message.lower() for i in errors)

    def test_deep_heading_error_reports_line_number(self):
        text = _make_spec(
            "### Requirement: R\n"
            "Body.\n\n"
            "###### Too Deep\n"
            "Content.\n"
        )
        parsed = parse_spec(text)
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        deep_error = next((i for i in errors if "exceeds v1 allowed range" in i.message), None)
        assert deep_error is not None
        # Location must be a line number, not a byte offset
        assert deep_error.location.startswith("line ")
        loc_val = deep_error.location.replace("line ", "")
        assert loc_val.isdigit()

    def test_deep_heading_line_number_is_accurate(self):
        """Deep heading line number must reflect the actual file line, not the fragment offset."""
        # Build a spec where the deep heading is at a known file line.
        # Lines:
        #  1: # Spec
        #  2: (empty)
        #  3: ## Purpose
        #  4: P.
        #  5: (empty)
        #  6: ### Requirement: R
        #  7: Body.
        #  8: (empty)
        #  9: ###### Too Deep
        # 10: Content.
        text = (
            "# Spec\n\n"
            "## Purpose\n"
            "P.\n\n"
            "### Requirement: R\n"
            "Body.\n\n"
            "###### Too Deep\n"
            "Content.\n"
        )
        parsed = parse_spec(text)
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        deep_error = next((i for i in errors if "exceeds v1 allowed range" in i.message), None)
        assert deep_error is not None
        # The ###### heading is on line 9, not line 3 (which would be the fragment offset)
        assert deep_error.location == "line 9"

    def test_deep_heading_line_number_with_v1_marker(self):
        """Line numbers remain accurate even when a v1 marker shifts the text."""
        from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER
        # Line 1: v1 marker, line 2: empty, then the rest starts at line 3
        text = (
            f"{SPEC_FORMAT_VERSION_MARKER}\n\n"
            "# Spec\n\n"
            "## Purpose\n"
            "P.\n\n"
            "### Requirement: R\n"
            "Body.\n\n"
            "###### Too Deep\n"
            "Content.\n"
        )
        parsed = parse_spec(text)
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        deep_error = next((i for i in errors if "exceeds v1 allowed range" in i.message), None)
        assert deep_error is not None
        # With the v1 marker on line 1, ###### is on line 11
        assert deep_error.location == "line 11"

    def test_five_level_heading_is_allowed(self):
        """##### is used for Scenarios and must NOT trigger an error."""
        text = _make_spec(
            "### Requirement: R\n"
            "Body.\n\n"
            "##### Scenario: Something\n"
            "Content.\n"
        )
        parsed = parse_spec(text)
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        assert not any("exceeds v1" in i.message for i in errors)

    def test_missing_purpose_warning(self):
        text = "# No Purpose\n\n### Requirement: R\nBody.\n"
        parsed = parse_spec(text)
        issues = validate(parsed)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("missing a ## Purpose" in i.message for i in warnings)

    def test_missing_v1_marker_warning(self):
        text = "# Title\n\n## Purpose\n\n### Requirement: R\nBody.\n"
        parsed = parse_spec(text)
        issues = validate(parsed)
        warnings = [i for i in issues if i.severity == "warning"]
        assert any("does not declare a format version" in i.message for i in warnings)

    def test_v1_marker_present_no_version_warning(self):
        text = (
            f"{SPEC_FORMAT_VERSION_MARKER}\n\n"
            "# Title\n\n## Purpose\n\n### Requirement: R\nBody.\n"
        )
        parsed = parse_spec(text)
        issues = validate(parsed)
        warnings = [i for i in issues if i.severity == "warning"]
        assert not any("format version" in i.message for i in warnings)

    def test_purpose_present_no_purpose_warning(self):
        text = "# Title\n\n## Purpose\nP.\n\n### Requirement: R\nBody.\n"
        parsed = parse_spec(text)
        issues = validate(parsed)
        warnings = [i for i in issues if i.severity == "warning"]
        assert not any("## Purpose" in i.message for i in warnings)

    def test_compliant_spec_no_errors(self):
        text = (
            f"{SPEC_FORMAT_VERSION_MARKER}\n\n"
            "# Title\n\n## Purpose\nP.\n\n"
            "### Requirement: One\nOne body.\n\n"
            "### Requirement: Two\nTwo body.\n"
        )
        parsed = parse_spec(text)
        issues = validate(parsed)
        errors = [i for i in issues if i.severity == "error"]
        assert errors == []

    def test_issue_dataclass(self):
        issue = Issue(severity="error", message="bad", location="line 1")
        assert issue.severity == "error"
        assert issue.message == "bad"
        assert issue.location == "line 1"


# ---------------------------------------------------------------------------
# Real-file snapshot tests
# ---------------------------------------------------------------------------

class TestRealSpecFiles:
    """Parse actual spec files from se3/specs/ and assert counts."""

    @pytest.fixture(scope="class")
    def specs_dir(self) -> Path:
        return Path(__file__).parents[2] / "se3" / "specs"

    def test_base_spec_requirement_count(self, specs_dir: Path):
        text = (specs_dir / "base" / "spec.md").read_text()
        parsed = parse_spec(text)
        assert len(parsed.requirements) == 18
        names = [r.name for r in parsed.requirements]
        assert "Project Identity" in names
        assert "Directory Structure" in names
        assert "Coding Conventions" in names
        assert "Key Constraints" in names
        assert "Workflow Conventions" in names

    def test_flow_engine_spec_requirement_count(self, specs_dir: Path):
        text = (specs_dir / "flow-engine" / "spec.md").read_text()
        parsed = parse_spec(text)
        assert len(parsed.requirements) == 43

    def test_spec_guardrails_requirement_count(self, specs_dir: Path):
        text = (specs_dir / "spec-guardrails" / "spec.md").read_text()
        parsed = parse_spec(text)
        assert len(parsed.requirements) == 13

    def test_all_specs_parse_without_exception(self, specs_dir: Path):
        """Every spec file must be parseable without raising."""
        for spec_path in specs_dir.glob("*/spec.md"):
            text = spec_path.read_text()
            parsed = parse_spec(text)
            # Basic sanity: at least one Requirement or a non-empty header
            assert parsed.requirements or parsed.header_text

    def test_all_specs_have_zero_validation_errors(self, specs_dir: Path):
        """No existing spec should produce error-level issues."""
        for spec_path in specs_dir.glob("*/spec.md"):
            text = spec_path.read_text()
            parsed = parse_spec(text)
            issues = validate(parsed)
            errors = [i for i in issues if i.severity == "error"]
            assert errors == [], f"{spec_path.parent.name} has validation errors: {errors}"

    def test_spec_format_self_describing(self, specs_dir: Path):
        """The spec-format spec itself must declare v1 and parse correctly."""
        text = (specs_dir / "spec-format" / "spec.md").read_text()
        parsed = parse_spec(text)
        assert parsed.has_v1_marker is True
        assert len(parsed.requirements) == 11
        names = [r.name for r in parsed.requirements]
        assert "Spec Format Version" in names
        assert "Requirement Boundary" in names
        assert "Shared Sections" in names
        assert "Tags and Keywords" in names
        assert "Cross-Item References" in names

    def test_base_spec_line_numbers(self, specs_dir: Path):
        text = (specs_dir / "base" / "spec.md").read_text()
        parsed = parse_spec(text)
        # First requirement "Project Identity" starts at line 9
        assert parsed.requirements[0].line_start == 9
        # Second requirement "Directory Structure" starts at line 14
        assert parsed.requirements[1].line_start == 14
