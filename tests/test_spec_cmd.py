"""G4 tests: the read-only ``se3 spec`` CLI (index / show).

Covers the exposure layer added by group G4 (``src/se3/commands/spec_cmd.py``):

- ``se3 spec index`` (root view), ``se3 spec index <spec>`` (item index), and
  ``se3 spec index <spec> <group>...`` (drill) are available and produce a
  self-describing, item-addressed view.
- Output is always the latest on-disk state: an edit to a spec between two
  invocations is reflected without an explicit rebuild (load_or_build's
  incremental reconciliation).
- A view that exceeds the configured ``index_render_threshold`` is folded into a
  size-bounded mixed view (navigation handles appear).
- ``se3 spec show <spec>::<requirement>`` prints the body plus the physical
  location (file path + line interval) and the two are consistent.
- ``se3 spec show`` rejects a group name / non-item address / address missing
  ``::`` with a non-zero exit (the interface-rejection invariant).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from se3.cli import app
from se3.commands import spec_cmd
from se3.engine.spec_format import SPEC_FORMAT_VERSION_MARKER

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _write_spec(project_root: Path, name: str, content: str) -> Path:
    spec_dir = project_root / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_file = spec_dir / "spec.md"
    spec_file.write_text(content, encoding="utf-8")
    return spec_file


def _simple_spec(title: str, domain: str | None, *reqs: tuple[str, str]) -> str:
    head = [SPEC_FORMAT_VERSION_MARKER]
    if domain is not None:
        head.append(f"<!-- domain: {domain} -->")
    head += [
        "",
        f"# {title} Specification",
        "",
        "## Purpose",
        "",
        f"{title} governs the example subsystem in one sentence.",
        "",
    ]
    body = []
    for rname, rbody in reqs:
        body += [f"### Requirement: {rname}", "", rbody, ""]
    return "\n".join(head + body)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A temp project whose specs dir holds a couple of small specs.

    ``get_project_root`` is monkeypatched to point at the temp project so the
    CLI commands resolve here regardless of the test's cwd.
    """
    _write_spec(
        tmp_path,
        "alpha",
        _simple_spec(
            "alpha",
            "engine/steps",
            ("First Req", "First requirement opening summary sentence."),
            ("Second Req", "Second requirement opening summary sentence."),
        ),
    )
    _write_spec(
        tmp_path,
        "beta",
        _simple_spec(
            "beta",
            None,  # no domain marker -> (未分类) bucket
            ("Only Req", "The only beta requirement summary."),
        ),
    )
    monkeypatch.setattr(spec_cmd, "get_project_root", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# index: availability + the three view modes
# ---------------------------------------------------------------------------

def test_index_root_view(project: Path) -> None:
    result = runner.invoke(app, ["spec", "index"])
    assert result.exit_code == 0, result.output
    # Self-describing header is present.
    assert "se3 spec show <spec>::<requirement>" in result.output
    # Both specs appear with their drill commands.
    assert "se3 spec index alpha" in result.output
    assert "se3 spec index beta" in result.output


def test_index_spec_view_lists_items(project: Path) -> None:
    result = runner.invoke(app, ["spec", "index", "alpha"])
    assert result.exit_code == 0, result.output
    # Items carry their full <spec>::<requirement> address.
    assert "alpha::First Req" in result.output
    assert "alpha::Second Req" in result.output


def test_index_group_drill(project: Path) -> None:
    # alpha's domain is engine/steps; drilling the root by domain path should
    # surface alpha's spec entry under that group.
    result = runner.invoke(app, ["spec", "index", "engine", "steps"])
    assert result.exit_code == 0, result.output
    assert "se3 spec index alpha" in result.output


# ---------------------------------------------------------------------------
# index: always latest (incremental reconciliation)
# ---------------------------------------------------------------------------

def test_index_reflects_latest_after_edit(project: Path) -> None:
    first = runner.invoke(app, ["spec", "index", "alpha"])
    assert first.exit_code == 0
    assert "alpha::Third Req" not in first.output

    # Append a new Requirement and re-run: the new item must appear without an
    # explicit rebuild (load_or_build detects the mtime/size/sha change).
    spec_file = project / "se3" / "specs" / "alpha" / "spec.md"
    spec_file.write_text(
        spec_file.read_text(encoding="utf-8")
        + "\n### Requirement: Third Req\n\nA freshly added requirement.\n",
        encoding="utf-8",
    )
    second = runner.invoke(app, ["spec", "index", "alpha"])
    assert second.exit_code == 0, second.output
    assert "alpha::Third Req" in second.output


# ---------------------------------------------------------------------------
# index: over-threshold folding (mixed view with handles)
# ---------------------------------------------------------------------------

def test_index_folds_over_threshold(tmp_path: Path, monkeypatch) -> None:
    # Many specs in distinct domains so the root view exceeds a tiny threshold
    # and must fold the largest domain groups into [group] handles.
    for i in range(12):
        _write_spec(
            tmp_path,
            f"spec{i:02d}",
            _simple_spec(
                f"spec{i:02d}",
                f"dom{i % 3}/sub{i}",
                ("R", "x" * 120),
            ),
        )
    monkeypatch.setattr(spec_cmd, "get_project_root", lambda: tmp_path)

    # Force a small threshold (above the ~300B header so folding is feasible)
    # via the config loader so folding is guaranteed. ``spec_cmd`` imports the
    # loader inside the function body, so the patch targets ``se3.config``.
    from se3 import config

    threshold = 500
    monkeypatch.setattr(
        config,
        "load_spec_governance_config",
        lambda root: config.SpecGovernanceConfig(index_render_threshold=threshold),
    )
    result = runner.invoke(app, ["spec", "index"])
    assert result.exit_code == 0, result.output
    # The view folds into navigation handles ([group] / [page]) ...
    assert ("[group]" in result.output) or ("[page]" in result.output)
    # ... and the whole rendered output stays within the threshold.
    assert len(result.output.encode("utf-8")) <= threshold


# ---------------------------------------------------------------------------
# show: body + physical location consistency
# ---------------------------------------------------------------------------

def test_show_outputs_body_and_location(project: Path) -> None:
    result = runner.invoke(app, ["spec", "show", "alpha::First Req"])
    assert result.exit_code == 0, result.output
    assert "### Requirement: First Req" in result.output
    assert "First requirement opening summary sentence." in result.output

    # The location line carries the file path + a 1-based inclusive interval,
    # and the printed body equals exactly those lines of the file.
    loc_line = next(
        ln for ln in result.output.splitlines() if ln.startswith("# location:")
    )
    path_part, _, span = loc_line[len("# location:"):].strip().rpartition(":")
    start_s, _, end_s = span.partition("-")
    start, end = int(start_s), int(end_s)
    file_lines = Path(path_part).read_text(encoding="utf-8").splitlines()
    expected_body = "\n".join(file_lines[start - 1:end])
    assert expected_body in result.output
    assert file_lines[start - 1] == "### Requirement: First Req"


# ---------------------------------------------------------------------------
# show: interface rejection (item-identity invariant, guarantee b)
# ---------------------------------------------------------------------------

def test_show_rejects_group_name(project: Path) -> None:
    # A bare spec/group name has no '::' -> rejected.
    result = runner.invoke(app, ["spec", "show", "alpha"])
    assert result.exit_code == 1
    assert "not an item address" in result.output


def test_show_rejects_domain_group_path(project: Path) -> None:
    # A domain group handle like 'engine/steps' is navigation, not an item.
    result = runner.invoke(app, ["spec", "show", "engine/steps"])
    assert result.exit_code == 1
    assert "not an item address" in result.output


def test_show_rejects_empty_halves(project: Path) -> None:
    for addr in ["::Foo", "alpha::", "::"]:
        result = runner.invoke(app, ["spec", "show", addr])
        assert result.exit_code == 1, addr


def test_show_rejects_unknown_spec(project: Path) -> None:
    result = runner.invoke(app, ["spec", "show", "nope::Foo"])
    assert result.exit_code == 1
    assert "no such spec" in result.output


def test_show_rejects_unknown_item(project: Path) -> None:
    result = runner.invoke(app, ["spec", "show", "alpha::Missing"])
    assert result.exit_code == 1
    assert "no such item" in result.output


# ---------------------------------------------------------------------------
# Read-only guarantee: no LLM call from the command path
# ---------------------------------------------------------------------------

def test_spec_commands_do_not_invoke_llm(project: Path, monkeypatch) -> None:
    calls = {"n": 0}

    try:
        from se3.engine import llm_caller

        def _boom(*a, **k):  # pragma: no cover - must never run
            calls["n"] += 1
            raise AssertionError("spec commands must not invoke the LLM")

        if hasattr(llm_caller, "LLMCaller"):
            monkeypatch.setattr(
                llm_caller.LLMCaller, "call", _boom, raising=False
            )
    except Exception:
        pass

    assert runner.invoke(app, ["spec", "index"]).exit_code == 0
    assert runner.invoke(app, ["spec", "index", "alpha"]).exit_code == 0
    assert runner.invoke(app, ["spec", "show", "alpha::First Req"]).exit_code == 0
    assert calls["n"] == 0
