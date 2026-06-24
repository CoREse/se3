"""Tests for the charter subsystem (src/se3/engine/charter.py).

Covers whole-text loading (incl. the missing-file degrade and sandbox
conventions-channel role), template rendering with placeholder substitution,
and the altitude gate's monitoring-light (warn-not-block) byte threshold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine import charter
from se3.engine.charter import (
    CHARTER_ADMISSION_STANDARD,
    DEFAULT_CHARTER_MAX_BYTES,
    AdmissionResult,
    charter_path,
    check_admission,
    load_charter,
    load_charter_template,
    render_charter_template,
)


# --------------------------------------------------------------------------
# load_charter
# --------------------------------------------------------------------------
def test_load_charter_returns_full_text(tmp_path: Path):
    """Charter full text is returned verbatim for injection into any step."""
    se3_dir = tmp_path / "se3"
    se3_dir.mkdir()
    content = "# Demo — Charter\n\n## Purpose\nfull text here\n"
    (se3_dir / "charter.md").write_text(content, encoding="utf-8")

    assert load_charter(tmp_path) == content


def test_load_charter_accepts_str_project_root(tmp_path: Path):
    se3_dir = tmp_path / "se3"
    se3_dir.mkdir()
    (se3_dir / "charter.md").write_text("x", encoding="utf-8")
    # str path, not Path, must also work (subprocess conventions channel)
    assert load_charter(str(tmp_path)) == "x"


def test_load_charter_missing_file_degrades_to_empty(tmp_path: Path):
    """A missing charter degrades to '' rather than raising."""
    assert load_charter(tmp_path) == ""


def test_charter_path_points_at_se3_charter_md(tmp_path: Path):
    assert charter_path(tmp_path) == tmp_path / "se3" / "charter.md"


# --------------------------------------------------------------------------
# template rendering
# --------------------------------------------------------------------------
def test_load_charter_template_has_placeholders():
    """The packaged template ships with the project-init placeholders."""
    text = load_charter_template()
    assert "{project_name}" in text
    assert "{project_description}" in text
    assert "{languages_and_frameworks}" in text


def test_charter_template_drops_per_module_locator():
    """Charter must NOT carry a per-module locator index (delegated to code-index)."""
    text = load_charter_template().lower()
    # The high-altitude sections are present...
    assert "project identity" in text
    assert "version management" in text
    # ...and the template explicitly hands per-module/per-symbol locating to
    # code-index rather than enumerating it inline.
    assert "code-index" in text


def test_render_charter_template_substitutes_placeholders():
    rendered = render_charter_template(
        project_name="MyProj",
        project_description="a demo",
        languages_and_frameworks="Python",
    )
    assert "MyProj" in rendered
    assert "{project_name}" not in rendered
    assert "{project_description}" not in rendered


def test_render_charter_template_leaves_unknown_braces_untouched():
    """Literal-replace rendering never raises on incidental braces (code fences)."""
    rendered = render_charter_template(project_name="X")
    # the version-management code fence contains YAML-ish braces / other tokens;
    # rendering must not raise and must leave un-provided placeholders intact.
    assert "{languages_and_frameworks}" in rendered


# --------------------------------------------------------------------------
# check_admission — altitude gate / monitoring light
# --------------------------------------------------------------------------
def test_check_admission_under_threshold_no_warning():
    result = check_admission("small charter")
    assert isinstance(result, AdmissionResult)
    assert result.over_threshold is False
    assert result.warning is None
    assert result.threshold_bytes == DEFAULT_CHARTER_MAX_BYTES
    assert result.size_bytes == len(b"small charter")


def test_check_admission_carries_admission_standard():
    """The gate always surfaces the normative altitude standard for the LLM gate."""
    result = check_admission("anything")
    assert result.admission_standard == CHARTER_ADMISSION_STANDARD
    assert "code-index" in result.admission_standard
    assert "MUST NOT" in result.admission_standard


def test_check_admission_over_threshold_warns_but_does_not_block():
    """Over-threshold is advisory: a warning is set, nothing is raised/rejected."""
    big = "x" * 100
    result = check_admission(big, threshold_bytes=10)
    assert result.over_threshold is True
    assert result.warning is not None
    assert "monitoring threshold" in result.warning
    assert "does not block" in result.warning
    # the admission standard is still provided regardless of size
    assert result.admission_standard == CHARTER_ADMISSION_STANDARD


def test_check_admission_byte_size_counts_utf8():
    """Size is measured in UTF-8 bytes, not characters."""
    text = "héllo"  # 'é' is 2 bytes in UTF-8 -> 6 bytes total
    result = check_admission(text)
    assert result.size_bytes == len(text.encode("utf-8")) == 6


def test_check_admission_boundary_equal_is_not_over():
    """Exactly-at-threshold is within budget (strict greater-than triggers)."""
    text = "abcde"  # 5 bytes
    result = check_admission(text, threshold_bytes=5)
    assert result.over_threshold is False
    assert result.warning is None


def test_check_admission_empty_charter():
    result = check_admission("")
    assert result.size_bytes == 0
    assert result.over_threshold is False


def test_check_admission_handles_none_text():
    """A defensive None text is treated as empty, never raises."""
    result = check_admission(None)  # type: ignore[arg-type]
    assert result.size_bytes == 0
    assert result.over_threshold is False


# --------------------------------------------------------------------------
# module hygiene
# --------------------------------------------------------------------------
def test_module_has_no_heavy_import_side_effects():
    """charter.py is stdlib-only and import-safe (mirrors spec_role discipline)."""
    # re-import is a no-op; just assert the public surface is present
    assert hasattr(charter, "load_charter")
    assert hasattr(charter, "check_admission")
    assert hasattr(charter, "render_charter_template")
