"""CLI tests for ``luo review-scope diff`` (read-only review-scope inspection).

The command is the supported way to read the exact diff a SELF_CHECK round
reviews, so these cover the two things a checker depends on:

- **fidelity** — the implementation baseline rebuilds the whole task's changes
  while the fix baseline rebuilds only the latest fix delta, plus the ``--stat``
  and per-path views over the same reconstruction;
- **failure legibility** — flow-not-found / never-captured / captured-unusable /
  reclaimed-snapshot are four separately reported situations with distinct exit
  codes (the contract the snapshot lifecycle aligns to).

And the hard one: the command writes nothing at all.

Each test builds its own git repo under ``tmp_path`` and its own flow id, so the
suite stays parallel-safe.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from tianluo.cli import app
from tianluo.engine.models import FlowInstance
from tianluo.engine.persistence import PersistenceManager
from tianluo.engine.review_scope import ReviewScopeManager
from tianluo.i18n import loader
from tianluo.runtime_paths import runtime_dir

runner = CliRunner()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "review@example.com")
    _git(root, "config", "user.name", "Review Test")
    (root / ".gitignore").write_text("/tianluo/\n", encoding="utf-8")
    (root / "alpha.py").write_text("value = 1\n", encoding="utf-8")
    (root / "beta.py").write_text("other = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


def _persist(root: Path, flow_id: str, scope_context: dict) -> None:
    """Persist a flow carrying ``scope_context`` as its review-scope state."""
    flow = FlowInstance(flow_id=flow_id, task_description="review scope test")
    flow.state.context["project_root"] = str(root)
    flow.state.context["review_scope"] = scope_context
    PersistenceManager(root).save_flow(flow)


def _invoke(root: Path, args):
    with patch(
        "tianluo.commands.review_scope_cmd.get_project_root", return_value=root
    ):
        return runner.invoke(app, ["review-scope", "diff"] + list(args))


def _tree_fingerprint(directory: Path) -> list:
    """Path + content digest of every file under ``directory``."""
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            entries.append(
                (
                    str(path.relative_to(directory)),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        else:
            entries.append((str(path.relative_to(directory)), "<dir>"))
    return entries


@pytest.fixture
def flow_with_baselines(tmp_path):
    """A repo where an implementation change AND a later fix change exist.

    Returns ``(root, flow_id)``. ``alpha.py`` was touched before the fix
    baseline (implementation-era work), ``beta.py`` after it (the fix delta).
    """
    root = _repo(tmp_path)
    flow_id = f"flow-{uuid.uuid4().hex[:8]}"
    manager = ReviewScopeManager(root, flow_id)

    implementation = manager.capture("implementation")
    (root / "alpha.py").write_text("value = 1\nimplemented = 2\n", encoding="utf-8")

    fix = manager.capture("fix-1")
    (root / "beta.py").write_text("other = 1\nfixed = 3\n", encoding="utf-8")

    _persist(
        root,
        flow_id,
        {
            "implementation_baseline": implementation.to_dict(),
            "latest_fix_baseline": fix.to_dict(),
            "fix_baseline_history": [
                {
                    "fix_iteration": 1,
                    "baseline_id": fix.baseline_id,
                    "available": True,
                    "diagnostics": [],
                }
            ],
        },
    )
    return root, flow_id


class TestReconstruction:
    def test_implementation_baseline_spans_the_whole_task(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--baseline", "implementation"])

        assert result.exit_code == 0
        assert "+implemented = 2" in result.stdout
        assert "+fixed = 3" in result.stdout

    def test_fix_baseline_spans_only_the_fix_delta(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--baseline", "fix"])

        assert result.exit_code == 0
        assert "+fixed = 3" in result.stdout
        assert "implemented = 2" not in result.stdout

    def test_default_baseline_is_implementation(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 0
        assert "+implemented = 2" in result.stdout

    def test_active_flow_is_the_default_target(self, flow_with_baselines):
        root, _flow_id = flow_with_baselines

        result = _invoke(root, [])

        assert result.exit_code == 0
        assert "+implemented = 2" in result.stdout

    def test_empty_scope_reports_no_changes(self, tmp_path):
        root = _repo(tmp_path)
        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        _persist(
            root, flow_id, {"implementation_baseline": implementation.to_dict()}
        )

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 0
        assert "No changes" in result.stdout


class TestStatView:
    def test_stat_lists_per_file_counts_and_a_summary(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--stat"])

        assert result.exit_code == 0
        lines = [line.strip() for line in result.stdout.splitlines()]
        assert any(
            line.startswith("alpha.py") and line.endswith("| +1 -0")
            for line in lines
        )
        assert any(
            line.startswith("beta.py") and line.endswith("| +1 -0")
            for line in lines
        )
        assert "2 file(s) changed, 2 insertion(s)(+), 0 deletion(s)(-)" in result.stdout
        # A summary view must not smuggle the diff body in.
        assert "+implemented = 2" not in result.stdout

    def test_stat_honours_the_path_filter(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--stat", "--path", "beta.py"])

        assert result.exit_code == 0
        assert "beta.py" in result.stdout
        assert "alpha.py" not in result.stdout
        assert "1 file(s) changed" in result.stdout


class TestPathFilter:
    def test_single_file_view(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--path", "alpha.py"])

        assert result.exit_code == 0
        assert "+implemented = 2" in result.stdout
        assert "beta.py" not in result.stdout

    def test_repeated_paths_select_several_files(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(
            root,
            ["--flow", flow_id, "--path", "alpha.py", "--path", "beta.py"],
        )

        assert result.exit_code == 0
        assert "+implemented = 2" in result.stdout
        assert "+fixed = 3" in result.stdout

    def test_path_outside_the_scope_is_an_error(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--path", "nowhere.py"])

        assert result.exit_code == 6
        assert "nowhere.py" in result.output
        # The error stays actionable: it names what IS in scope.
        assert "alpha.py" in result.output


class TestErrorPaths:
    def test_unknown_flow_exits_flow_not_found(self, tmp_path):
        root = _repo(tmp_path)

        result = _invoke(root, ["--flow", "no-such-flow"])

        assert result.exit_code == 3
        assert "no-such-flow" in result.output

    def test_no_active_flow_exits_flow_not_found(self, tmp_path):
        root = _repo(tmp_path)

        result = _invoke(root, [])

        assert result.exit_code == 3
        assert "--flow" in result.output

    def test_uncaptured_fix_baseline_exits_baseline_missing(self, tmp_path):
        root = _repo(tmp_path)
        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        _persist(
            root, flow_id, {"implementation_baseline": implementation.to_dict()}
        )

        result = _invoke(root, ["--flow", flow_id, "--baseline", "fix"])

        assert result.exit_code == 4
        assert "no FIX iteration has run" in result.output

    def test_uncaptured_implementation_baseline_exits_baseline_missing(self, tmp_path):
        root = _repo(tmp_path)
        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        _persist(root, flow_id, {})

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 4
        assert "has not reached the IMPLEMENT step" in result.output

    def test_unusable_baseline_reports_its_diagnostic(self, tmp_path):
        root = _repo(tmp_path)
        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        baseline = manager.unavailable_baseline(
            "implementation", "legacy flow crossed the baseline boundary"
        )
        _persist(root, flow_id, {"implementation_baseline": baseline.to_dict()})

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 4
        assert "legacy flow crossed the baseline boundary" in result.output

    def test_reclaimed_snapshots_exit_cleaned(self, flow_with_baselines):
        root, flow_id = flow_with_baselines
        shutil.rmtree(runtime_dir(root) / "state" / "review-scopes" / flow_id)

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 5
        assert "reclaimed" in result.output

    def test_undecidable_comparison_exits_one(self, flow_with_baselines):
        """An unattributable baseline is reported, never rendered as empty."""
        root, flow_id = flow_with_baselines
        manager = ReviewScopeManager(root, flow_id)
        implementation = next(
            item for item in manager.list_baselines()
            if item.kind == "implementation"
        )
        descriptor_path = (
            manager.root / implementation.baseline_id / "descriptor.json"
        )
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        # A commit this repository has never contained: the baseline can no
        # longer be related to the current HEAD.
        descriptor["head_commit"] = "0" * 40
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 1

    def test_unknown_baseline_value_is_a_usage_error(self, flow_with_baselines):
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--baseline", "nonsense"])

        assert result.exit_code == 2
        assert "nonsense" in result.output


class TestReadOnly:
    def test_the_command_writes_nothing(self, flow_with_baselines):
        """INVARIANT: a display surface may not mutate the flow's runtime state.

        Including the diff artifact — the engine's per-round record must not
        gain entries because someone looked at the flow.
        """
        root, flow_id = flow_with_baselines
        state = runtime_dir(root)
        before = _tree_fingerprint(state)
        worktree_before = _git(root, "status", "--porcelain=v1")

        assert _invoke(root, ["--flow", flow_id]).exit_code == 0
        assert _invoke(root, ["--flow", flow_id, "--stat"]).exit_code == 0
        assert _invoke(root, ["--flow", flow_id, "--baseline", "fix"]).exit_code == 0

        assert _tree_fingerprint(state) == before
        assert _git(root, "status", "--porcelain=v1") == worktree_before


class TestLocalization:
    def test_every_key_is_present_in_both_catalogs(self):
        """zh-CN carries the whole review-scope key set, not only en-US."""
        english = loader.load_catalog("en-US")
        chinese = loader.load_catalog("zh-CN")
        keys = {
            key for key in english
            if key.startswith("review_scope.")
            or key.startswith("cli.help.review_scope")
        }
        assert keys, "the review-scope catalog entries went missing"
        assert not keys - set(chinese)

    def test_help_renders_in_chinese(self, tmp_path):
        from tianluo import i18n

        try:
            i18n.set_language("zh-CN")
            result = runner.invoke(app, ["review-scope", "diff", "--help"])
        finally:
            i18n.set_language("en-US")
        assert result.exit_code == 0
        assert "SELF_CHECK" in result.stdout
