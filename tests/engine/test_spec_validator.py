"""Unit tests for ``tianluo.engine.spec_validator.validate_spec_structure``.

Covers each of the five v1 structural rules (positive + negative case
each), plus the high-priority documentation-updater meta-summary
fixture that motivated the validator.
"""

from __future__ import annotations

from tianluo.engine.spec_validator import (
    V1_MARKER,
    ValidationResult,
    extract_spec_body,
    validate_spec_structure,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _good_spec(name: str = "auth") -> str:
    return (
        f"{V1_MARKER}\n"
        f"# {name} Specification\n"
        "\n"
        "## Purpose\n"
        "Handles authentication.\n"
        "\n"
        "## Requirements\n"
        "\n"
        "### Requirement: Login\n"
        "Users SHALL be able to log in.\n"
        "\n"
        "#### Scenario: Valid credentials\n"
        "- **WHEN** valid credentials are submitted\n"
        "- **THEN** the user is logged in\n"
    )


# Real-world meta-summary string that triggered the spec — this MUST
# be detected as invalid.
_DOCUMENTATION_UPDATER_META = (
    "I have explored the documentation-updater module. The main file is "
    "src/se3/engine/docs_updater.py. The class DocumentationUpdater "
    "manages README.md and VERSIONS.md updates. It has methods for "
    "updating version badges and inserting changelog entries. The "
    "module exposes a Template helper and three regexes for badge "
    "matching. I will now write the spec for this module."
)


# ---------------------------------------------------------------------------
# Rule 1: v1 marker
# ---------------------------------------------------------------------------

class TestRule1V1Marker:
    def test_valid_marker(self):
        result = validate_spec_structure(_good_spec("auth"), "auth")
        assert result.passed, result.errors

    def test_missing_marker(self):
        spec = _good_spec("auth").replace(V1_MARKER + "\n", "", 1)
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        assert any("v1 marker" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Rule 2: '# <name> Specification' heading
# ---------------------------------------------------------------------------

class TestRule2Heading:
    def test_valid_heading(self):
        result = validate_spec_structure(_good_spec("payment"), "payment")
        assert result.passed, result.errors

    def test_case_insensitive_name(self):
        spec = _good_spec("Documentation-Updater")
        result = validate_spec_structure(spec, "documentation-updater")
        assert result.passed, result.errors

    def test_wrong_heading(self):
        spec = (
            f"{V1_MARKER}\n"
            "# Some Other Title\n\n"
            "## Purpose\n"
            "x\n\n"
            "### Requirement: a\n"
            "y\n"
        )
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        assert any("Specification" in e or "heading" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Rule 3: ## Purpose
# ---------------------------------------------------------------------------

class TestRule3Purpose:
    def test_valid_purpose(self):
        result = validate_spec_structure(_good_spec(), "auth")
        assert result.passed, result.errors

    def test_missing_purpose(self):
        spec = (
            f"{V1_MARKER}\n"
            "# auth Specification\n\n"
            "### Requirement: a\n"
            "y\n"
        )
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        assert any("Purpose" in e for e in result.errors)

    def test_empty_purpose(self):
        spec = (
            f"{V1_MARKER}\n"
            "# auth Specification\n\n"
            "## Purpose\n\n"
            "## Requirements\n\n"
            "### Requirement: a\n"
            "y\n"
        )
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        assert any("Purpose" in e and "empty" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Rule 4: ### Requirement:
# ---------------------------------------------------------------------------

class TestRule4Requirement:
    def test_at_least_one_requirement(self):
        result = validate_spec_structure(_good_spec(), "auth")
        assert result.passed, result.errors

    def test_zero_requirements(self):
        spec = (
            f"{V1_MARKER}\n"
            "# auth Specification\n\n"
            "## Purpose\n"
            "Authentication.\n"
        )
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        assert any("Requirement" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Rule 5: no narrative-prose first line
# ---------------------------------------------------------------------------

class TestRule5Narrative:
    def test_structured_first_line_passes(self):
        result = validate_spec_structure(_good_spec(), "auth")
        assert result.passed, result.errors

    def test_english_i_have_enough_context(self):
        spec = (
            f"{V1_MARKER}\n"
            "# auth Specification\n\n"
            "## Purpose\n"
            "I have enough context from the source code and usage sites "
            "to write the spec. Let me produce it now.\n\n"
            "### Requirement: a\n"
            "y\n"
        )
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        assert any("narrative" in e for e in result.errors)

    def test_chinese_narrative_first_line(self):
        spec = (
            f"{V1_MARKER}\n"
            "# auth Specification\n\n"
            "## Purpose\n"
            "我已经收集了足够的信息，下面输出 spec。\n\n"
            "### Requirement: a\n"
            "y\n"
        )
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        assert any("narrative" in e for e in result.errors)

    def test_let_me_first_line(self):
        spec = (
            f"{V1_MARKER}\n"
            "# auth Specification\n\n"
            "## Purpose\n"
            "Let me describe the auth subsystem in detail.\n\n"
            "### Requirement: a\n"
            "y\n"
        )
        result = validate_spec_structure(spec, "auth")
        assert not result.passed


# ---------------------------------------------------------------------------
# Composite real-world fixtures
# ---------------------------------------------------------------------------

class TestRealWorldFixtures:
    def test_documentation_updater_meta_summary_rejected(self):
        """The exact-style meta summary that motivated this validator
        MUST be flagged with at least one error."""
        result = validate_spec_structure(
            _DOCUMENTATION_UPDATER_META,
            "documentation-updater",
        )
        assert not result.passed
        # Should fire on the v1 marker AND on missing structure.
        assert len(result.errors) >= 2

    def test_returns_validation_result(self):
        result = validate_spec_structure(_good_spec(), "auth")
        assert isinstance(result, ValidationResult)
        assert result.passed is True
        assert result.errors == []

    def test_empty_string(self):
        result = validate_spec_structure("", "auth")
        assert not result.passed
        assert "empty" in " ".join(result.errors).lower()

    def test_non_string_input(self):
        # Defensive: don't raise if caller passes None
        result = validate_spec_structure(None, "auth")  # type: ignore[arg-type]
        assert not result.passed

    def test_multiple_errors_collected(self):
        """Validator does not short-circuit — it reports every failure
        so callers can present a complete punch list."""
        spec = "this is just plain text\n"
        result = validate_spec_structure(spec, "auth")
        assert not result.passed
        # No marker, no title, no Purpose, no Requirement, narrative.
        assert len(result.errors) >= 3


# ---------------------------------------------------------------------------
# extract_spec_body — purify agentic output to a clean spec body
# ---------------------------------------------------------------------------

class TestExtractSpecBody:
    def test_narrative_then_v1_marker_slices_from_marker(self):
        text = (
            "I have enough context from the source code. Let me produce it now.\n"
            "Here is the spec:\n"
            "\n"
            f"{V1_MARKER}\n"
            "# my-feature Specification\n"
            "## Purpose\nDoes things.\n"
            "### Requirement: Thing\nDetails.\n"
        )
        body = extract_spec_body(text, "my-feature")
        assert body.startswith(V1_MARKER)

    def test_narrative_then_title_heading_slices_from_heading(self):
        # No v1 marker present — slice from the '# <name> Specification' heading.
        text = (
            "I explored the code. The module does X, Y, Z.\n"
            "I'll write the spec now.\n"
            "\n"
            "# data-pipeline Specification\n"
            "## Purpose\nProcesses data.\n"
            "### Requirement: Run\nRuns.\n"
        )
        body = extract_spec_body(text, "data-pipeline")
        assert body.startswith("# data-pipeline Specification")

    def test_narrative_with_tool_process_then_body(self):
        text = (
            "Let me read the files.\n"
            "[tool_use] Read src/foo.py\n"
            "[tool_result] (contents...)\n"
            "Now I understand the module. Here's the spec:\n"
            "\n"
            f"{V1_MARKER}\n"
            "# foo Specification\n"
            "## Purpose\nFoo.\n"
            "### Requirement: Bar\nBar.\n"
        )
        body = extract_spec_body(text, "foo")
        assert body.startswith(V1_MARKER)
        # Tool process / narrative is dropped.
        assert "tool_use" not in body
        assert "Let me read" not in body

    def test_already_pure_marker_returned_unchanged(self):
        text = (
            f"{V1_MARKER}\n"
            "# foo Specification\n"
            "## Purpose\nFoo.\n"
            "### Requirement: Bar\nBar.\n"
        )
        assert extract_spec_body(text, "foo") == text

    def test_already_pure_heading_returned_from_heading(self):
        text = "# foo Specification\n## Purpose\nFoo.\n### Requirement: Bar\nBar.\n"
        assert extract_spec_body(text, "foo") == text

    def test_pure_narrative_no_anchor_returned_unchanged(self):
        text = "I have enough context to write the spec. Let me produce it now.\n"
        assert extract_spec_body(text, "foo") == text

    def test_fallback_to_first_level1_heading(self):
        # No v1 marker and the title token doesn't match spec_name, but a
        # level-1 heading exists — fall back to it.
        text = (
            "Some narrative preamble here.\n"
            "\n"
            "# Completely Different Specification\n"
            "## Purpose\nX.\n"
            "### Requirement: Y\nY.\n"
        )
        body = extract_spec_body(text, "foo")
        assert body.startswith("# Completely Different Specification")

    def test_empty_and_non_string_inputs_do_not_raise(self):
        assert extract_spec_body("", "foo") == ""
        assert extract_spec_body(None, "foo") is None  # type: ignore[arg-type]

    def test_purified_narrative_output_passes_validation(self):
        # End-to-end: agentic narrative + body, slice, then validate.
        text = (
            "I explored the subsystem and understand it well.\n"
            "Here is the complete spec:\n"
            "\n"
            f"{V1_MARKER}\n"
            "# widget Specification\n"
            "\n## Purpose\nManages widgets.\n"
            "\n## Requirements\n"
            "\n### Requirement: Create\nCreates a widget.\n"
        )
        body = extract_spec_body(text, "widget")
        result = validate_spec_structure(body, "widget")
        assert result.passed, result.errors
