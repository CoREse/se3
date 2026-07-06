"""``se3 code-index search`` — grep-style search over the code-index item lines.

These tests drive the CLI through Typer's ``CliRunner`` against a hand-authored
``se3/code-index.md``. Writing the md by hand (rather than running a real build)
keeps the item set — directory / file / symbol lines, their kinds and summaries,
and deliberately embedded ``<!--#...-->`` fingerprint comments — fully under the
test's control, so each grep-semantics assertion is exact and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from se3.cli import app
from se3.commands import code_index_cmd
from se3.engine import code_index

runner = CliRunner()


# The `a.*b` in the regex_meta summary is a literal substring that also happens
# to be a valid regex; the `alpha value builds` summary matches the *regex*
# `a.*b` but does NOT contain the literal `a.*b` — the pair lets a -F test prove
# metacharacters are not interpreted. Every line carries a fingerprint comment so
# the "output strips <!--#...-->" assertion is meaningful.
_MD = """\
# Code Index (map)

## `(root)` — the project root holding source and docs <!--#0000000000000001-->
### `README.md` (markdown) — the readme with setup notes <!--#0000000000000002-->

## `src/` — the source package directory <!--#0000000000000003-->
### `src/mod.py` (python) — module with alpha and Greeter helpers <!--#0000000000000004|0000000000000005-->
  - `alpha` (function) — computes the alpha value builds output <!--#0000000000000006-->
  - `Greeter` (class) — Greeter greets people <!--#0000000000000007-->
  - `Greeter.hello` (method) — say hello to a person <!--#0000000000000008-->
  - `regex_meta` (function) — matches a.*b literally in text <!--#0000000000000009-->
"""


@pytest.fixture
def built_project(tmp_path: Path, monkeypatch) -> Path:
    """A project whose se3/code-index.md is the hand-authored map above, with
    ``get_project_root`` pointed at it so the CLI resolves here."""
    root = tmp_path / "proj"
    md = code_index.md_path(root)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_MD, encoding="utf-8")
    monkeypatch.setattr(code_index_cmd, "get_project_root", lambda: root)
    return root


def _match_lines(output: str) -> list[str]:
    """The emitted match lines only (item bullets), excluding any diagnostics."""
    return [ln for ln in output.splitlines() if ln.startswith("- `")]


def test_regex_match_hits_and_exit_zero(built_project: Path):
    # `.` is a regex wildcard, so `Greet.r` matches the `Greeter` lines — proving
    # the pattern is interpreted as a regex by default.
    result = runner.invoke(app, ["code-index", "search", "Greet.r"])
    assert result.exit_code == 0, result.output
    lines = _match_lines(result.output)
    assert lines, result.output
    assert all("Greeter" in ln for ln in lines)
    assert "`src/mod.py::Greeter`" in result.output


def test_fixed_strings_literal_not_regex(built_project: Path):
    # As a regex, `a.*b` matches two lines (alpha…builds and the regex_meta line);
    # with -F it matches only the line literally containing `a.*b`.
    as_regex = runner.invoke(app, ["code-index", "search", "a.*b"])
    assert as_regex.exit_code == 0, as_regex.output
    assert len(_match_lines(as_regex.output)) == 2

    literal = runner.invoke(app, ["code-index", "search", "-F", "a.*b"])
    assert literal.exit_code == 0, literal.output
    lit_lines = _match_lines(literal.output)
    assert len(lit_lines) == 1
    assert "regex_meta" in lit_lines[0]


def test_case_sensitive_by_default_and_ignore_case(built_project: Path):
    # Lowercase `greeter` does not match the capitalised `Greeter` by default.
    sensitive = runner.invoke(app, ["code-index", "search", "greeter"])
    assert sensitive.exit_code == 1
    assert not _match_lines(sensitive.output)

    insensitive = runner.invoke(app, ["code-index", "search", "-i", "greeter"])
    assert insensitive.exit_code == 0, insensitive.output
    assert _match_lines(insensitive.output)
    assert all("reeter" in ln.lower() for ln in _match_lines(insensitive.output))


def test_symbol_match_carries_owning_file_path(built_project: Path):
    # The context a raw grep of the md cannot give: a symbol hit shows its full
    # `relpath::local_id` path, not a bare bullet detached from its file heading.
    result = runner.invoke(app, ["code-index", "search", "hello to a person"])
    assert result.exit_code == 0, result.output
    assert "`src/mod.py::Greeter.hello`" in result.output


def test_output_has_no_fingerprint_comments(built_project: Path):
    # Lines are rebuilt from the structured index, so the md's embedded
    # <!--#...--> fingerprints never leak into search output.
    result = runner.invoke(app, ["code-index", "search", "alpha"])
    assert result.exit_code == 0, result.output
    assert "<!--#" not in result.output


def test_max_count_truncates(built_project: Path):
    # `mod.py` appears on the file line plus all four symbol lines (5 total);
    # -m 2 caps the output at two matches.
    result = runner.invoke(app, ["code-index", "search", "-m", "2", "mod.py"])
    assert result.exit_code == 0, result.output
    assert len(_match_lines(result.output)) == 2


def test_no_match_exits_one_with_message(built_project: Path):
    result = runner.invoke(app, ["code-index", "search", "zzz-no-such-item"])
    assert result.exit_code == 1
    assert "No code-index item matched" in result.output
    assert not _match_lines(result.output)


def test_invalid_regex_exits_two(built_project: Path):
    # An unbalanced `[` is a bad pattern, not a "no match": grep-style exit 2.
    result = runner.invoke(app, ["code-index", "search", "[unterminated"])
    assert result.exit_code == 2
    assert "Invalid regular expression" in result.output


def test_unbuilt_map_hints_rebuild(tmp_path: Path, monkeypatch):
    # No md on disk -> the not-built hint + exit 1, same as index/show/inspect.
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(code_index_cmd, "get_project_root", lambda: root)
    result = runner.invoke(app, ["code-index", "search", "anything"])
    assert result.exit_code == 1
    assert "code-index rebuild" in result.output
