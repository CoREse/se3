The file on disk already contains the fully resolved content with no conflict markers. Here it is:

```python
"""Tests for the code-index + charter system (config knobs, charter subsystem,
and the context-injection surface).

This module is the shared home for the new code-index / charter surface. It
covers three layers:

* ``CodeIndexConfig`` — the ``se3 config`` knobs the code-index subsystem
  consumes (degrade thresholds, chunk granularity, explicit-exclude list).
* the charter subsystem (``src/se3/engine/charter.py``) — whole-text loading
  (incl. the missing-file degrade and sandbox conventions-channel role),
  template rendering with placeholder substitution, and the altitude gate's
  monitoring-light (warn-not-block) byte threshold.
* the context-injection surface (``get_charter_injection`` /
  ``get_code_index_injection``) — the charter full-text + code-index top-map
  injection that replaced the retired spec-name list.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from se3.config import (
    DEFAULT_CODE_INDEX_CHUNK_BYTES,
    DEFAULT_CODE_INDEX_CHUNK_LINES,
    DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_BYTES,
    DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES,
    CodeIndexConfig,
    load_code_index_config,
)
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
from se3.engine.context_builder import (
    get_charter_injection,
    get_code_index_injection,
)


# ===========================================================================
# CodeIndexConfig — the se3 config knobs (G8)
# ===========================================================================
class TestCodeIndexConfigDefaults:
    """Default values when nothing is configured (built-in defaults)."""

    def test_dataclass_defaults(self):
        cfg = CodeIndexConfig()
        assert cfg.degrade_trigger_lines == 2000
        assert cfg.degrade_trigger_bytes == 256 * 1024
        assert cfg.chunk_lines == 200
        assert cfg.chunk_bytes == 16 * 1024
        assert cfg.exclude == []

    def test_module_constants_match_dataclass_defaults(self):
        cfg = CodeIndexConfig()
        assert cfg.degrade_trigger_lines == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES
        assert cfg.degrade_trigger_bytes == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_BYTES
        assert cfg.chunk_lines == DEFAULT_CODE_INDEX_CHUNK_LINES
        assert cfg.chunk_bytes == DEFAULT_CODE_INDEX_CHUNK_BYTES

    def test_exclude_default_is_independent_list(self):
        # field(default_factory=list) — each instance gets its own list.
        a = CodeIndexConfig()
        b = CodeIndexConfig()
        a.exclude.append("foo")
        assert b.exclude == []

    def test_from_dict_empty(self):
        assert CodeIndexConfig.from_dict({}) == CodeIndexConfig()

    def test_from_dict_none(self):
        assert CodeIndexConfig.from_dict(None) == CodeIndexConfig()

    def test_from_dict_non_dict(self):
        assert CodeIndexConfig.from_dict("nonsense") == CodeIndexConfig()


class TestCodeIndexConfigOverride:
    """``se3.yaml`` overrides and ``load()`` — code_index.* takes effect."""

    def test_load_defaults_when_no_file(self, tmp_path):
        # Acceptance: absent config returns built-in defaults.
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()

    def test_load_defaults_when_no_section(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("version:\n  enabled: true\n")
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()

    def test_yaml_override_takes_effect(self, tmp_path):
        # Acceptance: se3.yaml code_index.* override is honored.
        (tmp_path / "se3.yaml").write_text(
            "code_index:\n"
            "  degrade_trigger_lines: 5000\n"
            "  degrade_trigger_bytes: 524288\n"
            "  chunk_lines: 100\n"
            "  chunk_bytes: 8192\n"
            "  exclude:\n"
            "    - vendor/\n"
            "    - generated/big.json\n"
        )
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg.degrade_trigger_lines == 5000
        assert cfg.degrade_trigger_bytes == 524288
        assert cfg.chunk_lines == 100
        assert cfg.chunk_bytes == 8192
        assert cfg.exclude == ["vendor/", "generated/big.json"]

    def test_partial_override_keeps_other_defaults(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "code_index:\n  chunk_lines: 50\n"
        )
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg.chunk_lines == 50
        assert cfg.degrade_trigger_lines == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES
        assert cfg.chunk_bytes == DEFAULT_CODE_INDEX_CHUNK_BYTES
        assert cfg.exclude == []

    def test_load_code_index_config_helper(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "code_index:\n  degrade_trigger_bytes: 1234\n"
        )
        cfg = load_code_index_config(tmp_path)
        assert cfg.degrade_trigger_bytes == 1234


class TestCodeIndexConfigExclude:
    """``exclude`` is the explicit-exclude (project-specific) list."""

    def test_exclude_is_explicit_list(self):
        cfg = CodeIndexConfig.from_dict({"exclude": ["a.py", "dir/"]})
        assert cfg.exclude == ["a.py", "dir/"]

    def test_exclude_entries_are_stripped(self):
        cfg = CodeIndexConfig.from_dict({"exclude": ["  spaced/  "]})
        assert cfg.exclude == ["spaced/"]

    def test_exclude_non_list_falls_back_to_empty(self):
        cfg = CodeIndexConfig.from_dict({"exclude": "not-a-list"})
        assert cfg.exclude == []

    def test_exclude_drops_non_string_and_blank_entries(self):
        cfg = CodeIndexConfig.from_dict(
            {"exclude": ["keep.py", "", "   ", 42, None, "also-keep/"]}
        )
        assert cfg.exclude == ["keep.py", "also-keep/"]

    def test_exclude_absent_defaults_empty(self):
        cfg = CodeIndexConfig.from_dict({"chunk_lines": 10})
        assert cfg.exclude == []


class TestCodeIndexConfigFaultTolerance:
    """Illegal values fall back to defaults and never raise."""

    def test_negative_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"degrade_trigger_lines": -5})
        assert cfg.degrade_trigger_lines == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_LINES

    def test_zero_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"chunk_lines": 0})
        assert cfg.chunk_lines == DEFAULT_CODE_INDEX_CHUNK_LINES

    def test_non_integer_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"chunk_bytes": "big"})
        assert cfg.chunk_bytes == DEFAULT_CODE_INDEX_CHUNK_BYTES

    def test_bool_falls_back(self):
        # bool is an int subclass — must be rejected explicitly.
        cfg = CodeIndexConfig.from_dict({"degrade_trigger_bytes": True})
        assert cfg.degrade_trigger_bytes == DEFAULT_CODE_INDEX_DEGRADE_TRIGGER_BYTES

    def test_float_falls_back(self):
        cfg = CodeIndexConfig.from_dict({"chunk_lines": 200.0})
        assert cfg.chunk_lines == DEFAULT_CODE_INDEX_CHUNK_LINES

    def test_invalid_yaml_falls_back_to_defaults(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("{{invalid yaml")
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()

    def test_non_dict_section_falls_back(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("code_index: true\n")
        cfg = CodeIndexConfig.load(tmp_path)
        assert cfg == CodeIndexConfig()


# ===========================================================================
# charter subsystem (G3)
# ===========================================================================
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


# ===========================================================================
# context-injection surface — get_charter_injection / get_code_index_injection (G7)
# ===========================================================================
class TestCharterInjection:
    """The charter is injected in full, unconditionally, into every step."""

    def test_injects_full_charter_text(self, tmp_path: Path):
        se3_dir = tmp_path / "se3"
        se3_dir.mkdir()
        body = "# Demo — Charter\n\n## Purpose\nproject conventions live here\n"
        (se3_dir / "charter.md").write_text(body, encoding="utf-8")

        out = get_charter_injection(tmp_path)
        assert "## Project Charter" in out
        # The full charter body is present verbatim.
        assert "project conventions live here" in out
        # Labelled as authoritative project-level context.
        assert "authoritative" in out.lower()

    def test_missing_charter_degrades_to_empty(self, tmp_path: Path):
        """No charter on disk -> no injection (degrade, never raise)."""
        assert get_charter_injection(tmp_path) == ""

    def test_blank_charter_degrades_to_empty(self, tmp_path: Path):
        se3_dir = tmp_path / "se3"
        se3_dir.mkdir()
        (se3_dir / "charter.md").write_text("   \n\n", encoding="utf-8")
        assert get_charter_injection(tmp_path) == ""

    def test_does_not_inject_spec_name_list(self, tmp_path: Path):
        """Acceptance: the retired spec-name list is no longer injected."""
        se3_dir = tmp_path / "se3"
        se3_dir.mkdir()
        (se3_dir / "charter.md").write_text("# C\n\n## Purpose\nx\n", encoding="utf-8")
        out = get_charter_injection(tmp_path)
        assert "Available Specifications" not in out
        assert "se3 spec index" not in out


class TestCodeIndexInjection:
    """The code-index top map is injected; deeper detail is on-demand."""

    def _write_md(self, tmp_path: Path) -> None:
        se3_dir = tmp_path / "se3"
        se3_dir.mkdir()
        # Minimal authoritative md parseable by CodeIndex.from_md: file headings
        # plus one symbol bullet (drill-in detail, NOT shown in the top map).
        md = (
            "# Code Index\n\n"
            "### `src/se3/engine/spec_index.py` (module) — builds item-level spec index\n"
            "  - `load_or_build` (function) — incremental rebuild entry point\n"
            "\n"
            "### `src/se3/engine/charter.py` (module) — charter load + altitude gate\n"
        )
        (se3_dir / "code-index.md").write_text(md, encoding="utf-8")

    def test_injects_top_map_when_built(self, tmp_path: Path):
        self._write_md(tmp_path)
        out = get_code_index_injection(tmp_path)
        # File-level one-liners (the top map) are present...
        assert "src/se3/engine/spec_index.py" in out
        assert "builds item-level spec index" in out
        assert "src/se3/engine/charter.py" in out
        # ...and the drill-down convention is always present.
        assert "se3 code-index show" in out
        assert "Before reading source code" in out

    def test_top_map_omits_symbol_detail(self, tmp_path: Path):
        """Acceptance: deep (function-level) detail is on-demand, not in the map."""
        self._write_md(tmp_path)
        out = get_code_index_injection(tmp_path)
        # The function-level symbol is drill-in detail, not injected in the top map.
        assert "load_or_build" not in out

    def test_unbuilt_index_still_injects_convention_and_note(self, tmp_path: Path):
        """No md yet -> still injects the drill-down protocol + a build note."""
        out = get_code_index_injection(tmp_path)
        assert "se3 code-index show" in out
        assert "se3 code-index rebuild" in out
        assert "Before reading source code" in out

    def test_does_not_inject_spec_name_list(self, tmp_path: Path):
        self._write_md(tmp_path)
        out = get_code_index_injection(tmp_path)
        assert "Available Specifications" not in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
```