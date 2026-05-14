"""Unit tests for ``se3.engine.spec_validator.validate_spec_structure``.

Covers each of the five v1 structural rules (positive + negative case
each), plus the high-priority documentation-updater meta-summary
fixture that motivated the validator.
"""

from __future__ import annotations

from se3.engine.spec_validator import (
    V1_MARKER,
    ValidationResult,
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
