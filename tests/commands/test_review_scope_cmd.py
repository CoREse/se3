"""CLI tests for ``luo review-scope diff`` (read-only review-scope inspection).

The command is the supported way to read the exact diff a SELF_CHECK round
reviews, so these cover the two things a checker depends on:

- **fidelity** — the implementation baseline rebuilds the whole task's changes
  while the fix baseline rebuilds only the delta of the fixes no round has
  reviewed yet, plus the ``--stat`` and per-path views over the same
  reconstruction;
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


@pytest.fixture
def flow_with_a_changed_directory(tmp_path):
    """A repo whose changes sit under ``pkg/``, plus one change outside it.

    Returns ``(root, flow_id)``. Covers the filter shape a checker pulling one
    subsystem at a time uses: ``--path <directory>``.
    """
    root = _repo(tmp_path)
    package = root / "pkg"
    package.mkdir()
    (package / "mod_a.py").write_text("a = 1\n", encoding="utf-8")
    (package / "mod_b.py").write_text("b = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "package")

    flow_id = f"flow-{uuid.uuid4().hex[:8]}"
    manager = ReviewScopeManager(root, flow_id)
    implementation = manager.capture("implementation")
    (package / "mod_a.py").write_text("a = 1\nchanged = 2\n", encoding="utf-8")
    (package / "mod_b.py").write_text("b = 1\nchanged = 3\n", encoding="utf-8")
    (root / "alpha.py").write_text("value = 1\noutside = 4\n", encoding="utf-8")

    _persist(root, flow_id, {"implementation_baseline": implementation.to_dict()})
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

    def test_path_below_a_changed_file_is_an_error(self, flow_with_baselines):
        """A filter beneath a regular file names nothing, so it is refused.

        Containment runs one way: ``--path <dir>`` selects the changed files
        under it, but ``alpha.py/not-real`` is not a subtree of the changed
        file ``alpha.py`` — answering it with that file's diff would show
        changes under a name this scope never held.
        """
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--path", "alpha.py/not-real"])

        assert result.exit_code == 6
        assert "alpha.py/not-real" in result.output
        assert "+implemented = 2" not in result.stdout

    def test_path_below_a_changed_file_is_refused_by_the_stat_view_too(
        self, flow_with_baselines
    ):
        root, flow_id = flow_with_baselines

        result = _invoke(
            root, ["--flow", flow_id, "--stat", "--path", "alpha.py/not-real"]
        )

        assert result.exit_code == 6

    def test_directory_filter_selects_the_files_under_it(
        self, flow_with_a_changed_directory
    ):
        root, flow_id = flow_with_a_changed_directory

        result = _invoke(root, ["--flow", flow_id, "--path", "pkg"])

        assert result.exit_code == 0
        assert "+changed = 2" in result.stdout
        assert "+changed = 3" in result.stdout
        # The filter is a restriction, not a widening.
        assert "outside = 4" not in result.stdout
        # A directory whose files changed must never be reported as unchanged.
        assert "No changes" not in result.stdout

    def test_directory_filter_agrees_between_the_diff_and_stat_views(
        self, flow_with_a_changed_directory
    ):
        root, flow_id = flow_with_a_changed_directory

        diff = _invoke(root, ["--flow", flow_id, "--path", "pkg"])
        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", "pkg"])

        assert diff.exit_code == 0
        assert stat.exit_code == 0
        assert "2 file(s) changed" in stat.stdout
        # Same filter, same files: whatever --stat counts, the diff view shows.
        counted = {
            line.strip().split(" ")[0]
            for line in stat.stdout.splitlines()
            if "|" in line
        }
        shown = {
            line.split(" b/")[-1].strip()
            for line in diff.stdout.splitlines()
            if line.startswith("diff --git ")
        }
        assert counted == shown == {"pkg/mod_a.py", "pkg/mod_b.py"}

    @pytest.mark.parametrize("spelling", ["pkg/", "./pkg", "pkg//"])
    def test_equivalent_directory_spellings_select_the_same_files(
        self, flow_with_a_changed_directory, spelling
    ):
        """A trailing slash is how shells complete a directory name.

        Comparing the filter raw made ``--path pkg/`` look for the prefix
        ``pkg//`` and exit 6 on a directory whose files did change.
        """
        root, flow_id = flow_with_a_changed_directory

        diff = _invoke(root, ["--flow", flow_id, "--path", spelling])
        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", spelling])

        assert diff.exit_code == 0
        assert stat.exit_code == 0
        assert "+changed = 2" in diff.stdout
        assert "+changed = 3" in diff.stdout
        assert "outside = 4" not in diff.stdout
        assert "No changes" not in diff.stdout
        assert "2 file(s) changed" in stat.stdout

    @pytest.mark.parametrize("spelling", ["/pkg", "../pkg", "pkg/mod_a.py/not-real/"])
    def test_normalization_admits_no_filter_that_names_nothing(
        self, flow_with_a_changed_directory, spelling
    ):
        """Collapsing spellings must not re-root a filter into the scope.

        ``/pkg`` and ``../pkg`` are not repository-relative, and containment
        still runs one way, so a trailing slash cannot rescue a path below a
        changed file either.
        """
        root, flow_id = flow_with_a_changed_directory

        result = _invoke(root, ["--flow", flow_id, "--path", spelling])

        assert result.exit_code == 6

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_path_is_refused_rather_than_silently_dropped(
        self, flow_with_baselines, value
    ):
        """`--path "$TARGET"` with an unset variable must not widen the view.

        An empty filter names nothing in this scope, so it takes the same
        out-of-scope rejection any other unsupported filter takes. Dropping it
        would answer a single-file question with the entire diff.
        """
        root, flow_id = flow_with_baselines

        result = _invoke(root, ["--flow", flow_id, "--path", value])

        assert result.exit_code == 6
        # The whole diff must NOT have been printed.
        assert "+implemented = 2" not in result.stdout
        # It still names what IS in scope.
        assert "alpha.py" in result.output

    def test_a_blank_path_alongside_a_real_one_still_fails(
        self, flow_with_baselines
    ):
        root, flow_id = flow_with_baselines

        result = _invoke(
            root, ["--flow", flow_id, "--path", "alpha.py", "--path", ""]
        )

        assert result.exit_code == 6
        assert "+implemented = 2" not in result.stdout

    def test_directory_outside_the_scope_is_still_an_error(
        self, flow_with_a_changed_directory
    ):
        root, flow_id = flow_with_a_changed_directory

        result = _invoke(root, ["--flow", flow_id, "--path", "elsewhere"])

        assert result.exit_code == 6
        assert "elsewhere" in result.output

    def test_surrounding_whitespace_is_part_of_the_name(self, tmp_path):
        """Trimming the filter admitted a path the repository does not hold.

        With the changed file spelled ``" pkg/mod.py"``, ``--path pkg`` names
        nothing — it must take the same exit 6 any other unsupported filter
        takes, and only the exact spelling may select the file.
        """
        root = _repo(tmp_path)
        package = root / " pkg"
        package.mkdir()
        (package / "mod.py").write_text("a = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "spaced package")

        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (package / "mod.py").write_text("a = 1\nchanged = 2\n", encoding="utf-8")
        _persist(
            root, flow_id, {"implementation_baseline": implementation.to_dict()}
        )

        trimmed = _invoke(root, ["--flow", flow_id, "--path", "pkg"])
        assert trimmed.exit_code == 6
        assert "+changed = 2" not in trimmed.stdout

        trimmed_stat = _invoke(
            root, ["--flow", flow_id, "--stat", "--path", "pkg"]
        )
        assert trimmed_stat.exit_code == 6

        exact = _invoke(root, ["--flow", flow_id, "--path", " pkg"])
        assert exact.exit_code == 0
        assert "+changed = 2" in exact.stdout
        assert "No changes" not in exact.stdout

    def test_quoted_token_is_a_usable_filter_in_both_views(self, tmp_path):
        """The only spelling such a file is ever SHOWN in must also filter.

        A pathname carrying edge whitespace (or a line break, or a byte that is
        not valid UTF-8) appears in the prompt manifest, in the diff headers
        and in the ``--stat`` column solely as a C-quoted token, so that token
        is what a checker copies and pastes back here. Refusing it would leave
        exactly the files whose names cannot be rendered raw unreachable
        through the command the prompt sends the checker to — and both views
        have to resolve the token to the same file.
        """
        root = _repo(tmp_path)
        package = root / " pkg"
        package.mkdir()
        (package / "mod.py").write_text("a = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "spaced package")

        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (package / "mod.py").write_text("a = 1\nchanged = 2\n", encoding="utf-8")
        _persist(
            root, flow_id, {"implementation_baseline": implementation.to_dict()}
        )

        token = '" pkg/mod.py"'
        view = _invoke(root, ["--flow", flow_id, "--path", token])
        assert view.exit_code == 0
        assert "+changed = 2" in view.stdout
        assert "No changes" not in view.stdout

        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", token])
        assert stat.exit_code == 0
        assert token in stat.stdout

        # A malformed token still names nothing: only a well-formed one
        # decodes, so a lenient reading can never ground a filter on a path no
        # surface presented.
        bogus = _invoke(root, ["--flow", flow_id, "--path", '" pkg\\q.py"'])
        assert bogus.exit_code == 6
        # The hint names the file in a spelling that both survives this line
        # and is accepted back by --path.
        assert token in bogus.stderr

    def test_a_copied_token_selects_the_file_it_was_shown_for(self, tmp_path):
        """The displayed token must not be captured by a literal namesake.

        Every surface escapes a pathname that itself contains a quote, so a
        bare ``" pkg/mod.py"`` is the spelling shown FOR `` pkg/mod.py`` and
        never for the (equally legal) path literally named ``" pkg/mod.py"`` —
        that one is shown as ``"\\" pkg/mod.py\\""``. Resolving the copied token
        by its raw reading first handed it to the literal namesake whenever
        both changed, answering a single-file question with a different file.
        """
        root = _repo(tmp_path)
        spaced = root / " pkg"
        spaced.mkdir()
        (spaced / "mod.py").write_text("a = 1\n", encoding="utf-8")
        literal = root / '" pkg'
        literal.mkdir()
        (literal / 'mod.py"').write_text("b = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "both spellings")

        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (spaced / "mod.py").write_text("a = 1\nspaced = 2\n", encoding="utf-8")
        (literal / 'mod.py"').write_text("b = 1\nquoted = 3\n", encoding="utf-8")
        _persist(
            root, flow_id, {"implementation_baseline": implementation.to_dict()}
        )

        shown = '" pkg/mod.py"'
        view = _invoke(root, ["--flow", flow_id, "--path", shown])
        assert view.exit_code == 0
        assert "+spaced = 2" in view.stdout
        assert "+quoted = 3" not in view.stdout

        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", shown])
        assert stat.exit_code == 0
        assert "+quoted = 3" not in stat.stdout

        # The literal name keeps its own reachable spelling: its escaped token
        # decodes straight back to it.
        escaped = '"\\" pkg/mod.py\\""'
        other = _invoke(root, ["--flow", flow_id, "--path", escaped])
        assert other.exit_code == 0
        assert "+quoted = 3" in other.stdout
        assert "+spaced = 2" not in other.stdout


class TestSubmoduleInnerPathFilter:
    """A submodule's inner files must be filterable without dragging siblings.

    A gitlink diff renders the parent entry and its inner files together. The
    diff view used to reach an inner file by selecting the whole parent
    section, so ``--path vendor/inner.py`` also printed everything else that
    section happened to hold — while ``--stat`` over the same filter listed the
    one file. Both views resolve the filter through the same containment
    relation now, and every inner label carries its own header.
    """

    @pytest.fixture
    def flow_with_submodule(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        _git(sub, "init")
        _git(sub, "config", "user.email", "sub@example.com")
        _git(sub, "config", "user.name", "Sub Test")
        (sub / "inner.py").write_text("x = 1\n", encoding="utf-8")
        (sub / "other.py").write_text("keep = 1\n", encoding="utf-8")
        _git(sub, "add", "-A")
        _git(sub, "commit", "-m", "sub initial")

        root = _repo(tmp_path)
        _git(
            root,
            "-c", "protocol.file.allow=always",
            "submodule", "add", str(sub), "vendor",
        )
        _git(root, "commit", "-m", "add submodule")

        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (root / "vendor" / "inner.py").write_text("x = 2\n", encoding="utf-8")
        _persist(
            root, flow_id, {"implementation_baseline": implementation.to_dict()}
        )
        return root, flow_id

    def test_inner_path_view_agrees_with_the_stat_view(
        self, flow_with_submodule
    ):
        root, flow_id = flow_with_submodule

        stat = _invoke(
            root, ["--flow", flow_id, "--stat", "--path", "vendor/inner.py"]
        )
        diff = _invoke(root, ["--flow", flow_id, "--path", "vendor/inner.py"])

        assert stat.exit_code == 0
        assert diff.exit_code == 0
        counted = {
            line.strip().split(" ")[0]
            for line in stat.stdout.splitlines()
            if "|" in line
        }
        shown = {
            line.split(" b/")[-1].strip()
            for line in diff.stdout.splitlines()
            if line.startswith("diff --git ")
        }
        assert counted == shown == {"vendor/inner.py"}
        assert "+x = 2" in diff.stdout
        # A file whose changes exist is never reported as unchanged.
        assert "No changes" not in diff.stdout

    def test_the_submodule_filter_still_selects_everything_under_it(
        self, flow_with_submodule
    ):
        root, flow_id = flow_with_submodule

        diff = _invoke(root, ["--flow", flow_id, "--path", "vendor"])
        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", "vendor"])

        assert diff.exit_code == 0
        assert stat.exit_code == 0
        counted = {
            line.strip().split(" ")[0]
            for line in stat.stdout.splitlines()
            if "|" in line
        }
        shown = {
            line.split(" b/")[-1].strip()
            for line in diff.stdout.splitlines()
            if line.startswith("diff --git ")
        }
        assert counted == shown
        assert "vendor" in counted and "vendor/inner.py" in counted


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


@pytest.fixture
def flow_with_a_gitignored_change(tmp_path):
    """A flow whose only change is a file ``.gitignore`` hides from git.

    Returns ``(root, flow_id)``. Baseline capture enumerates with
    ``--exclude-standard`` and provably cannot hold such a file, so the round
    only sees it through the implement step's self-reported paths — which the
    engine persists under ``review_scope.declared_changed_paths``.
    """
    root = _repo(tmp_path)
    with (root / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("/generated/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")

    flow_id = f"flow-{uuid.uuid4().hex[:8]}"
    manager = ReviewScopeManager(root, flow_id)
    implementation = manager.capture("implementation")
    generated = root / "generated"
    generated.mkdir()
    (generated / "out.js").write_text("console.log(1);\n", encoding="utf-8")

    _persist(
        root,
        flow_id,
        {
            "implementation_baseline": implementation.to_dict(),
            "declared_changed_paths": ["generated/out.js"],
        },
    )
    return root, flow_id


class TestDeclaredIgnoredPaths:
    """A baseline view carries only what its snapshot comparison can place.

    A git-ignored file exists in NO snapshot on either side, so no persisted
    baseline comparison attributes it to the implementation view rather than
    the fix one — only execution-side bookkeeping of who declared what could,
    and the flow deliberately keeps none. The command therefore leaves such a
    path out of both views instead of listing it in both under a membership
    neither baseline supports; the round's prompt manifest is where it is
    advertised, by path alone and with no domain mark.
    """

    def test_gitignored_declared_path_is_not_claimed_by_a_baseline_view(
        self, flow_with_a_gitignored_change
    ):
        root, flow_id = flow_with_a_gitignored_change

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 0
        assert "generated/out.js" not in result.stdout

    def test_gitignored_declared_path_is_not_admitted_as_a_filter(
        self, flow_with_a_gitignored_change
    ):
        root, flow_id = flow_with_a_gitignored_change

        result = _invoke(root, ["--flow", flow_id, "--path", "generated/out.js"])

        assert result.exit_code == 6

    def test_the_stat_view_does_not_count_it_either(
        self, flow_with_a_gitignored_change
    ):
        root, flow_id = flow_with_a_gitignored_change

        result = _invoke(root, ["--flow", flow_id, "--stat"])

        assert result.exit_code == 0
        assert "generated/out.js" not in result.stdout

    def test_undeclared_ignored_file_stays_out_of_scope(
        self, flow_with_a_gitignored_change
    ):
        """No ignored file enters a view, declared or not."""
        root, flow_id = flow_with_a_gitignored_change
        (root / "generated" / "stray.js").write_text("stray\n", encoding="utf-8")

        result = _invoke(root, ["--flow", flow_id, "--path", "generated/stray.js"])

        assert result.exit_code == 6


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
        """zh-CN renders the Chinese help, not a silent en-US fallback.

        WHY the module is re-imported instead of just switching the language:
        Typer freezes every ``help=`` string when the defining module is
        imported, so the active language at THAT moment is what the rendered
        help speaks — which is exactly the path a real zh-CN project takes, its
        process starting inside the project so the language resolves from
        config before the CLI modules load. The suite pins en-US at conftest
        import time (see ``tests/conftest.py``), so the ``app`` this module
        holds froze the English help and no runtime switch can move it.
        """
        import importlib

        from tianluo import i18n
        from tianluo.commands import review_scope_cmd

        try:
            i18n.set_language("zh-CN")
            localized = importlib.reload(review_scope_cmd)
            result = runner.invoke(
                localized.review_scope_app, ["diff", "--help"]
            )
        finally:
            # The module object is process-wide and the rest of the suite
            # asserts English help, so the en-US freeze is restored here.
            i18n.set_language("en-US")
            importlib.reload(review_scope_cmd)
        assert result.exit_code == 0
        # WHY zh-only tokens rather than a bare "it did not crash" check:
        # "SELF_CHECK" is verbatim in BOTH catalogs, so asserting it alone
        # passes even when zh-CN silently renders the en-US fallback — exactly
        # the regression this is the only test guarding. Short tokens keep the
        # assertion independent of where Rich wraps a CJK line.
        assert "SELF_CHECK" in result.stdout
        assert "重建" in result.stdout
        assert "基线" in result.stdout
        assert "Rebuild" not in result.stdout


@pytest.fixture
def flow_with_declared_paths(tmp_path):
    """A flow whose ignored files were reported across two stages.

    ``generated/early.js`` comes from the first IMPLEMENT, ``generated/late.js``
    from the fix; the round persists them as ONE flat union. A tracked file is
    edited on each side too, so a view that carries nothing is distinguishable
    from a view that carries only what its own baseline can compare. Returns
    ``(root, flow_id)``.
    """
    root = _repo(tmp_path)
    with (root / ".gitignore").open("a", encoding="utf-8") as handle:
        handle.write("/generated/\n")
    (root / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")

    flow_id = f"flow-{uuid.uuid4().hex[:8]}"
    manager = ReviewScopeManager(root, flow_id)
    implementation = manager.capture("implementation")
    generated = root / "generated"
    generated.mkdir()
    (generated / "early.js").write_text("console.log(1);\n", encoding="utf-8")
    (root / "tracked.py").write_text("value = 1\nimplemented = 2\n", encoding="utf-8")

    fix = manager.capture("fix-1")
    (generated / "late.js").write_text("console.log(2);\n", encoding="utf-8")
    (root / "tracked.py").write_text(
        "value = 1\nimplemented = 2\nfixed = 3\n", encoding="utf-8"
    )

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
            "declared_changed_paths": [
                "generated/early.js",
                "generated/late.js",
            ],
        },
    )
    return root, flow_id


class TestDeclaredPaths:
    """A view never inherits the flow-wide union of declared paths.

    The engine keeps the implement steps' self-reports as ONE flat, flow-wide
    list because no persisted baseline comparison can attribute such a path to
    a domain. Injecting that union into a baseline view would do exactly that
    attribution by the back door: the fix baseline holds no fact placing an
    ignored file an earlier IMPLEMENT created after it, so the fix view must
    not present it as one of its own changes.
    """

    def test_implementation_view_shows_only_what_its_baseline_compares(
        self, flow_with_declared_paths
    ):
        root, flow_id = flow_with_declared_paths

        result = _invoke(root, ["--flow", flow_id, "--baseline", "implementation"])

        assert result.exit_code == 0
        assert "+implemented = 2" in result.stdout
        assert "generated/early.js" not in result.stdout
        assert "generated/late.js" not in result.stdout

    def test_fix_view_does_not_inherit_the_earlier_stage_report(
        self, flow_with_declared_paths
    ):
        root, flow_id = flow_with_declared_paths

        result = _invoke(root, ["--flow", flow_id, "--baseline", "fix"])

        assert result.exit_code == 0
        assert "+fixed = 3" in result.stdout
        assert "generated/early.js" not in result.stdout
        assert "generated/late.js" not in result.stdout

    def test_declared_path_is_not_a_usable_filter_in_either_view(
        self, flow_with_declared_paths
    ):
        root, flow_id = flow_with_declared_paths

        for selector in ("implementation", "fix"):
            result = _invoke(
                root,
                [
                    "--flow", flow_id,
                    "--baseline", selector,
                    "--path", "generated/early.js",
                ],
            )
            assert result.exit_code == 6, selector


class TestAmbiguousHeaderPaths:
    """A filename containing the header's own separator must still resolve.

    ``diff --git a/<old> b/<new>`` packs both paths onto one line, so a file
    named ``pkg b/generated.py`` produces a header with three ``" b/"``
    sequences. An end-anchored scan cuts inside the filename, and the diff view
    then selects no section for a filter ``--stat`` resolves correctly —
    reporting "no changes" over a file that really did change.
    """

    def _flow(self, tmp_path):
        root = _repo(tmp_path)
        target = root / "pkg b"
        target.mkdir()
        (target / "generated.py").write_text("value = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "add ambiguous path")

        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (target / "generated.py").write_text("value = 2\n", encoding="utf-8")
        _persist(
            root,
            flow_id,
            {"implementation_baseline": implementation.to_dict()},
        )
        return root, flow_id

    def test_header_paths_survive_the_split(self):
        from tianluo.engine.review_scope import split_diff_sections

        text = (
            "diff --git a/pkg b/generated.py b/pkg b/generated.py\n"
            "@@ -1 +1 @@\n"
            "-value = 1\n"
            "+value = 2\n"
        )
        sections = split_diff_sections(text)
        assert [s.path for s in sections] == ["pkg b/generated.py"]
        assert [s.old_path for s in sections] == ["pkg b/generated.py"]

    def test_rename_paths_come_from_the_rename_lines(self):
        from tianluo.engine.review_scope import split_diff_sections

        text = (
            "diff --git a/old b/one.py b/new b/two.py\n"
            "similarity index 100%\n"
            "rename from old b/one.py\n"
            "rename to new b/two.py\n"
        )
        sections = split_diff_sections(text)
        assert sections[0].old_path == "old b/one.py"
        assert sections[0].path == "new b/two.py"

    def test_diff_and_stat_views_select_the_same_file(self, tmp_path):
        root, flow_id = self._flow(tmp_path)

        stat = _invoke(
            root,
            ["--flow", flow_id, "--stat", "--path", "pkg b/generated.py"],
        )
        assert stat.exit_code == 0
        assert "pkg b/generated.py" in stat.stdout

        diff = _invoke(
            root, ["--flow", flow_id, "--path", "pkg b/generated.py"]
        )
        assert diff.exit_code == 0
        assert "No changes" not in diff.stdout
        assert "+value = 2" in diff.stdout


class TestLineBreakInAPathname:
    """A pathname carrying a line break must still name its own section.

    The rendered diff is split back into per-file sections line by line, so an
    unescaped pathname holding a newline tears its own ``diff --git`` header in
    two and the section loses the path it names — while the ``--stat`` table,
    built from the anchors, keeps the exact pathname. The filter is then
    admitted, ``--stat`` lists the file, and the diff view reports "no changes"
    over a file that really did change.
    """

    #: A legal (if hostile) tracked filename on every platform this runs on.
    NAME = "line\nbreak.py"

    def _flow(self, tmp_path):
        root = _repo(tmp_path)
        (root / self.NAME).write_text("value = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "add line-break path")

        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (root / self.NAME).write_text("value = 2\n", encoding="utf-8")
        _persist(
            root,
            flow_id,
            {"implementation_baseline": implementation.to_dict()},
        )
        return root, flow_id

    def test_the_header_stays_one_line(self, tmp_path):
        from tianluo.engine.review_scope import split_diff_sections

        root, flow_id = self._flow(tmp_path)
        manager = ReviewScopeManager(root, flow_id)
        baseline = manager.lookup_baseline("implementation").baseline
        scope = manager.reconstruct("full", baseline, write_artifact=False)

        sections = split_diff_sections(scope.unified_diff)
        assert [section.path for section in sections] == [self.NAME]
        assert [section.old_path for section in sections] == [self.NAME]

    def test_diff_and_stat_views_select_the_same_file(self, tmp_path):
        root, flow_id = self._flow(tmp_path)

        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", self.NAME])
        assert stat.exit_code == 0
        assert "+1 -1" in stat.stdout

        diff = _invoke(root, ["--flow", flow_id, "--path", self.NAME])
        assert diff.exit_code == 0
        assert "No changes" not in diff.stdout
        assert "+value = 2" in diff.stdout


class TestExactRenameFilter:
    """An exact rename names two paths but is ONE change.

    The renderer preserves it as a single ``similarity index 100%`` section
    rather than an unrelated delete/add pair, so its text cannot be cut in
    half — selecting it by either side selects the whole change. The ``--stat``
    table, meanwhile, carries one row per changed path. Resolving the filter
    per view therefore used to give two different file sets: ``--path
    alpha.py`` kept only the ``alpha.py`` row while the diff showed both sides,
    and filtering by the destination produced the inverse mismatch.
    """

    def _flow(self, tmp_path):
        root = _repo(tmp_path)
        flow_id = f"flow-{uuid.uuid4().hex[:8]}"
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (root / "alpha.py").rename(root / "gamma.py")
        _persist(
            root,
            flow_id,
            {"implementation_baseline": implementation.to_dict()},
        )
        return root, flow_id

    @staticmethod
    def _stat_rows(stdout):
        return {
            line.strip().split(" ")[0]
            for line in stdout.splitlines()
            if "|" in line
        }

    @staticmethod
    def _diff_paths(stdout):
        paths = set()
        for line in stdout.splitlines():
            if line.startswith("rename from "):
                paths.add(line[len("rename from "):].strip())
            elif line.startswith("rename to "):
                paths.add(line[len("rename to "):].strip())
        return paths

    def test_the_rename_renders_as_one_section(self, tmp_path):
        root, flow_id = self._flow(tmp_path)

        result = _invoke(root, ["--flow", flow_id])

        assert result.exit_code == 0
        assert "rename from alpha.py" in result.stdout
        assert "rename to gamma.py" in result.stdout

    @pytest.mark.parametrize("side", ["alpha.py", "gamma.py"])
    def test_either_side_selects_the_same_files_in_both_views(
        self, tmp_path, side
    ):
        root, flow_id = self._flow(tmp_path)

        diff = _invoke(root, ["--flow", flow_id, "--path", side])
        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", side])

        assert diff.exit_code == 0
        assert stat.exit_code == 0
        # Both sides of the rename, in both views, for either filter.
        assert self._diff_paths(diff.stdout) == {"alpha.py", "gamma.py"}
        assert self._stat_rows(stat.stdout) == {"alpha.py", "gamma.py"}
        assert "2 file(s) changed" in stat.stdout

    def test_the_filter_is_still_a_restriction(self, tmp_path):
        root, flow_id = self._flow(tmp_path)
        # A file changed outside the rename must not ride along with it.
        (root / "beta.py").write_text("other = 2\n", encoding="utf-8")

        diff = _invoke(root, ["--flow", flow_id, "--path", "alpha.py"])
        stat = _invoke(root, ["--flow", flow_id, "--stat", "--path", "alpha.py"])

        assert "beta.py" not in diff.stdout
        assert "beta.py" not in self._stat_rows(stat.stdout)

    def test_a_path_below_a_rename_side_is_still_refused(self, tmp_path):
        root, flow_id = self._flow(tmp_path)

        for side in ("alpha.py/not-real", "gamma.py/not-real"):
            result = _invoke(root, ["--flow", flow_id, "--path", side])
            assert result.exit_code == 6, side
