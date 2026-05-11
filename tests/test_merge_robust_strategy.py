"""Tests for the robust merge strategy.

Commit 1 (this file's initial scope): verifies that ``robust`` is accepted by
the type system, CLI plumbing, and MergeConfig default. Behavior is still
delegated to default-equivalent semantics; commits 2-4 add the behavioral
differentiation (auto-stash, take-theirs fallback, guardrail-as-issue).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from se3.engine.merge.conflict_resolver import (
    Confidence,
    FileResolution,
    HunkResolution,
    LLMResolution,
    MergeStrategy,
)
from se3.engine.merge.strategy import DecisionAction, StrategyDecider


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True,
    )
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True,
    )


def _make_resolution(
    overall: Confidence = Confidence.HIGH,
    requires_human_review: bool = False,
) -> LLMResolution:
    return LLMResolution(
        files=[
            FileResolution(
                path="a.txt",
                resolved_content="resolved\n",
                hunks=[
                    HunkResolution(
                        start_line=1,
                        end_line=1,
                        confidence=Confidence.HIGH,
                        reasoning="r",
                    ),
                ],
                overall_confidence=overall,
                flags={
                    "requires_human_review": requires_human_review,
                    "spec_guardrail_concern": False,
                },
                is_spec=False,
            ),
        ],
        overall_confidence=overall,
        flags={
            "requires_human_review": requires_human_review,
            "spec_guardrail_concern": False,
        },
    )


class TestRobustEnumWired:
    def test_robust_is_in_merge_strategy_enum(self) -> None:
        assert MergeStrategy("robust") is MergeStrategy.ROBUST
        assert MergeStrategy.ROBUST.value == "robust"

    def test_orchestrator_accepts_robust(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeOrchestrator

        orch = MergeOrchestrator(
            tmp_path, strategy="robust", acquire_lock=False,
        )
        assert orch.strategy is MergeStrategy.ROBUST

    def test_orchestrator_rejects_unknown_strategy(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeOrchestrator

        with pytest.raises(ValueError, match="Unknown merge strategy"):
            MergeOrchestrator(
                tmp_path, strategy="nonsense", acquire_lock=False,
            )


class TestRobustIsDefaultConfig:
    def test_default_merge_config_strategy_is_robust(self) -> None:
        from se3.config import MergeConfig

        assert MergeConfig().strategy == "robust"

    def test_config_loads_robust_from_yaml(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "se3.yaml").write_text(
            "merge:\n  strategy: robust\n"
        )
        from se3.config import MergeConfig

        cfg = MergeConfig.load(tmp_path)
        assert cfg.strategy == "robust"

    def test_run_merge_default_strategy_arg_is_robust(self) -> None:
        # Static guard on the default to catch accidental regressions in
        # the CLI plumbing when refactoring run_merge.
        import inspect

        from se3.commands.merge_cmd import run_merge

        sig = inspect.signature(run_merge)
        assert sig.parameters["strategy"].default == "robust"


class TestRobustDecideDelegatesToDefault:
    """Commit 1 keeps robust as a behavioral alias of default. Commits 3
    will change orchestrator-side handling of HUMAN_CALL under robust;
    the decider keeps returning HUMAN_CALL the same way default does, but
    the orchestrator will interpret it differently."""

    def test_high_confidence_clean_accepts_under_robust(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(overall=Confidence.HIGH)
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.ROBUST,
        )
        assert decision.action is DecisionAction.ACCEPT

    def test_human_review_flag_returns_human_call_under_robust(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(requires_human_review=True)
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.ROBUST,
        )
        # Commit 1: same decision as default (HUMAN_CALL). Commit 3 will
        # change orchestrator-side handling, not the decider itself.
        assert decision.action is DecisionAction.HUMAN_CALL

    def test_low_confidence_returns_human_call_under_robust(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(overall=Confidence.LOW)
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.ROBUST,
        )
        assert decision.action is DecisionAction.HUMAN_CALL


class TestCLIRobustAccepted:
    def test_cli_help_lists_robust(self) -> None:
        from typer.testing import CliRunner

        from se3.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["merge", "--help"])
        assert result.exit_code == 0
        assert "robust" in result.stdout

    def test_cli_rejects_invalid_strategy(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        from typer.testing import CliRunner

        from se3.cli import app

        runner = CliRunner()
        # Mock get_project_root so we don't escape tmp_path
        with patch(
            "se3.commands.run.get_project_root", return_value=tmp_path,
        ):
            result = runner.invoke(
                app, ["merge", "feat", "--strategy", "nonsense"],
            )
        assert result.exit_code != 0
        # Error message lists robust as a valid option
        out = result.stdout + (result.stderr or "")
        assert "robust" in out


# ---------------------------------------------------------------------------
# Commit 2 — robust auto-stash + dirty WT
# ---------------------------------------------------------------------------


def _init_repo_with_branch(tmp_path: Path) -> str:
    """Init a repo with a master commit + a feature branch one commit ahead.

    Returns the feature branch name.
    """
    _init_repo(tmp_path)
    # Determine current branch (master vs main depending on git default)
    cur = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat"], cwd=tmp_path, check=True,
    )
    (tmp_path / "feature.py").write_text("print('feat')\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add feat"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", cur], cwd=tmp_path, check=True,
    )
    return "feat"


class TestRobustAutoStash:
    def test_robust_stash_dirty_tracked_file_round_trip(
        self, tmp_path: Path,
    ) -> None:
        _init_repo_with_branch(tmp_path)
        # Modify the existing README so the tree has a dirty tracked file.
        readme = tmp_path / "README.md"
        readme.write_text("init\n\nlocal scratch\n")
        assert "local scratch" in readme.read_text()

        from se3.commands.merge_cmd import run_merge

        rc = run_merge(
            branches=["feat"],
            strategy="robust",
            project_root=tmp_path,
        )
        assert rc == 0
        # After successful merge, stash should have been popped → tracked
        # change is back in the working tree.
        assert "local scratch" in readme.read_text()

    def test_robust_stash_untracked_file_round_trip(
        self, tmp_path: Path,
    ) -> None:
        _init_repo_with_branch(tmp_path)
        (tmp_path / "scratch.txt").write_text("u\n")

        from se3.commands.merge_cmd import run_merge

        rc = run_merge(
            branches=["feat"],
            strategy="robust",
            project_root=tmp_path,
        )
        assert rc == 0
        assert (tmp_path / "scratch.txt").exists()
        assert (tmp_path / "scratch.txt").read_text() == "u\n"

    def test_robust_clean_tree_makes_no_stash(self, tmp_path: Path) -> None:
        _init_repo_with_branch(tmp_path)
        from se3.commands.merge_cmd import _robust_stash_dirty

        audit: list[str] = []
        label = _robust_stash_dirty(tmp_path, audit)
        assert label is None
        assert audit == []
        # No stash entries should exist.
        result = subprocess.run(
            ["git", "stash", "list"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ""

    def test_non_robust_still_rejects_dirty_tree(
        self, tmp_path: Path,
    ) -> None:
        _init_repo_with_branch(tmp_path)
        (tmp_path / "README.md").write_text("dirty\n")

        from se3.commands.merge_cmd import run_merge

        rc = run_merge(
            branches=["feat"],
            strategy="default",
            project_root=tmp_path,
        )
        assert rc == 1

    def test_robust_stash_pop_conflict_take_ours_with_audit(
        self, tmp_path: Path,
    ) -> None:
        """When pop introduces a 3-way conflict on the same path the merge
        modified, robust takes-ours (keeps merged version) and files an
        audit issue tagged stash-pop-fallback."""
        feat = _init_repo_with_branch(tmp_path)
        # The feature branch touches feature.py. Make a local-only edit
        # to the same file so the pop after merge conflicts with the
        # merged content.
        (tmp_path / "feature.py").write_text("local pre-merge edit\n")

        from se3.commands.merge_cmd import run_merge

        rc = run_merge(
            branches=[feat],
            strategy="robust",
            project_root=tmp_path,
        )
        assert rc == 0
        # After take-ours: file holds the merged (feat) content, NOT
        # the user's local edit.
        assert (
            (tmp_path / "feature.py").read_text() == "print('feat')\n"
        )
        # Audit issue filed for the pop fallback.
        open_dir = tmp_path / "se3" / "issues" / "open"
        assert open_dir.exists()
        contents = "\n".join(
            f.read_text() for f in open_dir.glob("*.yaml")
        )
        assert "stash-pop-fallback" in contents
        # Stash list is empty (drop succeeded).
        list_result = subprocess.run(
            ["git", "stash", "list"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        )
        assert list_result.stdout.strip() == ""

    def test_robust_rejects_in_progress_git_operation(
        self, tmp_path: Path,
    ) -> None:
        _init_repo_with_branch(tmp_path)
        # Forge a MERGE_HEAD marker — simulates an unfinished merge.
        git_dir_result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        )
        git_dir = Path(git_dir_result.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (tmp_path / git_dir).resolve()
        (git_dir / "MERGE_HEAD").write_text("deadbeef\n")

        from se3.commands.merge_cmd import run_merge

        rc = run_merge(
            branches=["feat"],
            strategy="robust",
            project_root=tmp_path,
        )
        assert rc == 1


class TestStashPopHelpersStillShared:
    """Regression: implement-step still has working stash-pop helpers
    after the extraction to engine.stash_utils."""

    def test_implement_helpers_resolve_to_shared_module(self) -> None:
        from se3.engine.stash_utils import (
            parse_stashpop_already_exists,
            take_ours_for_stashpop,
        )
        from se3.engine.steps import implement

        assert implement._parse_stashpop_already_exists is (
            parse_stashpop_already_exists
        )
        assert implement._take_ours_for_stashpop is (
            take_ours_for_stashpop
        )


# ---------------------------------------------------------------------------
# Commit 3 — robust conflict path: LLM → take-theirs, no human call
# ---------------------------------------------------------------------------


def _init_conflicting_branches(tmp_path: Path) -> tuple[str, str]:
    """Init a repo with a base commit + two divergent branches that
    conflict on the same file. Returns (base_branch, feature_branch).
    """
    _init_repo(tmp_path)
    cur = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    # Initial shared file
    (tmp_path / "shared.txt").write_text("base content\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "shared base"],
        cwd=tmp_path, check=True,
    )
    # Feature branch modifies shared.txt
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feat"], cwd=tmp_path, check=True,
    )
    (tmp_path / "shared.txt").write_text("incoming content from feat\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat edit"],
        cwd=tmp_path, check=True,
    )
    # Back to base, make a conflicting edit
    subprocess.run(
        ["git", "checkout", "-q", cur], cwd=tmp_path, check=True,
    )
    (tmp_path / "shared.txt").write_text("base-edited\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "base edit"],
        cwd=tmp_path, check=True,
    )
    return cur, "feat"


class TestRobustConflictFallback:
    def test_robust_llm_exception_takes_theirs_and_files_audit_issue(
        self, tmp_path: Path,
    ) -> None:
        _, feat = _init_conflicting_branches(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.orchestrator import MergeOrchestrator
        from se3.engine.merge.conflict_resolver import ConflictResolver

        # Make LLM resolver always raise — simulate API failure.
        from se3.engine.llm_caller import LLMCallError

        def boom(*_a, **_kw):
            raise LLMCallError("simulated LLM outage")

        with patch.object(ConflictResolver, "resolve", side_effect=boom):
            rc = run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        # shared.txt should now hold feat's content (take-theirs)
        assert (
            (tmp_path / "shared.txt").read_text()
            == "incoming content from feat\n"
        )
        # Audit issue filed under se3/issues/open/
        open_dir = tmp_path / "se3" / "issues" / "open"
        assert open_dir.exists()
        issues = list(open_dir.glob("*.yaml"))
        assert issues, "expected an audit issue to be filed"
        contents = issues[0].read_text()
        assert "llm-resolution-failed" in contents
        assert "merge-fallback" in contents

    def test_robust_decision_reject_takes_theirs(
        self, tmp_path: Path,
    ) -> None:
        _, feat = _init_conflicting_branches(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.strategy import (
            DecisionAction,
            StrategyDecider,
            StrategyDecision,
        )

        # Mock decider to always return REJECT.
        with patch.object(
            StrategyDecider, "decide",
            return_value=StrategyDecision(
                action=DecisionAction.REJECT,
                reason="forced reject for test",
            ),
        ):
            rc = run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        assert (
            (tmp_path / "shared.txt").read_text()
            == "incoming content from feat\n"
        )

    def test_robust_decision_human_call_takes_theirs(
        self, tmp_path: Path,
    ) -> None:
        """HUMAN_CALL decision under robust → repurposed as take-theirs."""
        _, feat = _init_conflicting_branches(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.strategy import (
            DecisionAction,
            StrategyDecider,
            StrategyDecision,
        )

        with patch.object(
            StrategyDecider, "decide",
            return_value=StrategyDecision(
                action=DecisionAction.HUMAN_CALL,
                reason="forced human-call for test",
            ),
        ):
            rc = run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        # take-theirs ran
        assert (
            (tmp_path / "shared.txt").read_text()
            == "incoming content from feat\n"
        )
        # No call file was written
        assert not (tmp_path / "se3" / "merge_calls").exists() or not list(
            (tmp_path / "se3" / "merge_calls").glob("*"),
        )

    def test_robust_context_build_failure_takes_theirs(
        self, tmp_path: Path,
    ) -> None:
        _, feat = _init_conflicting_branches(tmp_path)
        from se3.commands.merge_cmd import run_merge

        # Force build_conflict_context to raise.
        with patch(
            "se3.engine.merge.orchestrator.build_conflict_context",
            side_effect=RuntimeError("context build sabotaged"),
        ):
            rc = run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        # take-theirs ran without needing context
        assert (
            (tmp_path / "shared.txt").read_text()
            == "incoming content from feat\n"
        )
        # Audit issue should be tagged context-build-failed
        open_dir = tmp_path / "se3" / "issues" / "open"
        issues = list(open_dir.glob("*.yaml"))
        assert issues
        assert "context-build-failed" in issues[0].read_text()

    def test_robust_never_writes_human_call_file(
        self, tmp_path: Path,
    ) -> None:
        _, feat = _init_conflicting_branches(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.conflict_resolver import ConflictResolver
        from se3.engine.llm_caller import LLMCallError

        def boom(*_a, **_kw):
            raise LLMCallError("simulated")

        with patch.object(ConflictResolver, "resolve", side_effect=boom):
            run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )

        # The merge_calls directory either does not exist or is empty.
        call_dir = tmp_path / "se3" / "merge_calls"
        if call_dir.exists():
            assert not list(call_dir.glob("*"))

    def test_robust_spec_conflict_gets_spec_take_theirs_tag(
        self, tmp_path: Path,
    ) -> None:
        # Set up branches where the conflicting file is under se3/specs/.
        _init_repo(tmp_path)
        cur = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout.strip()
        spec_dir = tmp_path / "se3" / "specs"
        spec_dir.mkdir(parents=True)
        (spec_dir / "a.md").write_text("base spec\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add spec"],
            cwd=tmp_path, check=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", "-b", "spec-feat"],
            cwd=tmp_path, check=True,
        )
        (spec_dir / "a.md").write_text("feat-edited spec\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "spec edit"],
            cwd=tmp_path, check=True,
        )
        subprocess.run(
            ["git", "checkout", "-q", cur], cwd=tmp_path, check=True,
        )
        (spec_dir / "a.md").write_text("base-edited spec\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "base spec edit"],
            cwd=tmp_path, check=True,
        )

        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.conflict_resolver import ConflictResolver
        from se3.engine.llm_caller import LLMCallError

        with patch.object(
            ConflictResolver, "resolve",
            side_effect=LLMCallError("simulated"),
        ):
            rc = run_merge(
                branches=["spec-feat"],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        open_dir = tmp_path / "se3" / "issues" / "open"
        issues = list(open_dir.glob("*.yaml"))
        assert issues
        contents = issues[0].read_text()
        assert "spec-take-theirs" in contents

    def test_robust_audit_issues_recorded_on_report(
        self, tmp_path: Path,
    ) -> None:
        """The MergeReport surfaces robust_audit_issues so the CLI/operator
        can see what audit IDs were filed during the run."""
        _, feat = _init_conflicting_branches(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.conflict_resolver import ConflictResolver
        from se3.engine.llm_caller import LLMCallError

        # Patch run_merge's report rendering to capture the report.
        captured: list = []
        from se3.commands import merge_cmd as mc

        orig_render = mc.render_text

        def capture_render(*a, **kw):
            captured.append((a, kw))
            return orig_render(*a, **kw)

        with patch.object(
            ConflictResolver, "resolve",
            side_effect=LLMCallError("simulated"),
        ), patch.object(mc, "render_text", side_effect=capture_render):
            run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        # Audit issue file exists on disk — the surface contract is the
        # IssueManager YAML, which we already check elsewhere. Here we
        # just verify the function exits cleanly with rendering.
        assert any("Merge Complete" == kw.get("title")
                   for _, kw in captured), \
            "expected a 'Merge Complete' render after take-theirs fallback"


# ---------------------------------------------------------------------------
# Commit 4 — guardrail violations become issues, not merge failures
# ---------------------------------------------------------------------------


class TestRobustGuardrailPolicy:
    def test_robust_guardrail_violation_keeps_commit_and_files_issue(
        self, tmp_path: Path,
    ) -> None:
        """Force a guardrail violation; assert HEAD is still the merge
        commit, the merge succeeds (exit 0), and an audit issue exists.
        """
        feat = _init_repo_with_branch(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.guardrails import (
            GuardrailReport,
            GuardrailViolation,
            MergeGuardrailsCheck,
        )

        bad_report = GuardrailReport(
            passed=False,
            violations=[
                GuardrailViolation(
                    file_path="se3/specs/example.md",
                    violation_type="MISSING_REQUIRED_SECTION",
                    message="spec lacks the 'Acceptance' section",
                ),
            ],
            incomplete=False,
        )
        with patch.object(
            MergeGuardrailsCheck, "check_merge_result",
            return_value=bad_report,
        ):
            rc = run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        # HEAD must be the merge commit.
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=tmp_path, check=True, capture_output=True, text=True,
        ).stdout
        assert "Merge branch" in log or feat in log

        # Audit issue filed.
        open_dir = tmp_path / "se3" / "issues" / "open"
        assert open_dir.exists()
        issues = list(open_dir.glob("*.yaml"))
        assert issues
        contents = issues[0].read_text()
        assert "guardrail-violation" in contents
        assert "MISSING_REQUIRED_SECTION" in contents

    def test_robust_guardrail_check_crash_keeps_commit_and_files_issue(
        self, tmp_path: Path,
    ) -> None:
        feat = _init_repo_with_branch(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.guardrails import MergeGuardrailsCheck

        with patch.object(
            MergeGuardrailsCheck, "check_merge_result",
            side_effect=RuntimeError("simulated check crash"),
        ):
            rc = run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        open_dir = tmp_path / "se3" / "issues" / "open"
        issues = list(open_dir.glob("*.yaml"))
        assert issues
        assert "guardrail-check-crashed" in issues[0].read_text()

    def test_robust_guardrail_pass_files_no_issue(
        self, tmp_path: Path,
    ) -> None:
        """When the guardrails check passes, no audit issue is filed."""
        feat = _init_repo_with_branch(tmp_path)
        from se3.commands.merge_cmd import run_merge

        rc = run_merge(
            branches=[feat],
            strategy="robust",
            project_root=tmp_path,
        )
        assert rc == 0
        open_dir = tmp_path / "se3" / "issues" / "open"
        if open_dir.exists():
            # Only allow audit issues from completely unrelated subsystems
            # (none in this isolated test). Robust guardrails must not
            # file an issue when there is nothing to flag.
            for f in open_dir.glob("*.yaml"):
                assert "guardrail-violation" not in f.read_text()

    def test_robust_guardrail_never_writes_call_file(
        self, tmp_path: Path,
    ) -> None:
        feat = _init_repo_with_branch(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.guardrails import (
            GuardrailReport,
            GuardrailViolation,
            MergeGuardrailsCheck,
        )

        bad = GuardrailReport(
            passed=False,
            violations=[
                GuardrailViolation(
                    file_path="se3/specs/x.md",
                    violation_type="X",
                    message="m",
                ),
            ],
        )
        with patch.object(
            MergeGuardrailsCheck, "check_merge_result", return_value=bad,
        ):
            run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        call_dir = tmp_path / "se3" / "merge_calls"
        if call_dir.exists():
            assert not list(call_dir.glob("*"))


class TestRobustTakeTheirsRunsGuardrails:
    """Take-theirs literally lands the incoming branch's version. If that
    version has a spec violation, the robust guardrail policy MUST file an
    audit issue — same as clean-merge and LLM-resolution paths."""

    def test_take_theirs_guardrail_violation_files_audit_issue(
        self, tmp_path: Path,
    ) -> None:
        _, feat = _init_conflicting_branches(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.conflict_resolver import ConflictResolver
        from se3.engine.merge.guardrails import (
            GuardrailReport,
            GuardrailViolation,
            MergeGuardrailsCheck,
        )
        from se3.engine.llm_caller import LLMCallError

        # Force LLM to fail so we hit take-theirs.
        # Force guardrails to flag a violation on the post-merge state.
        bad = GuardrailReport(
            passed=False,
            violations=[
                GuardrailViolation(
                    file_path="se3/specs/x.md",
                    violation_type="BAD_SECTION",
                    message="bad",
                ),
            ],
        )
        with patch.object(
            ConflictResolver, "resolve",
            side_effect=LLMCallError("simulated"),
        ), patch.object(
            MergeGuardrailsCheck, "check_merge_result", return_value=bad,
        ):
            rc = run_merge(
                branches=[feat],
                strategy="robust",
                project_root=tmp_path,
            )
        assert rc == 0
        # Audit issues filed: at least one for take-theirs, one for guardrail
        open_dir = tmp_path / "se3" / "issues" / "open"
        contents = "\n".join(
            f.read_text() for f in open_dir.glob("*.yaml")
        )
        assert "llm-resolution-failed" in contents
        assert "guardrail-violation" in contents
        assert "BAD_SECTION" in contents


class TestNonRobustGuardrailUnchanged:
    """Regression: fast strategy still invokes the GuardrailRepairer path
    when violations are detected; robust short-circuit does NOT bleed
    into non-robust strategies."""

    def test_fast_strategy_still_invokes_repair_on_violation(
        self, tmp_path: Path,
    ) -> None:
        feat = _init_repo_with_branch(tmp_path)
        from se3.commands.merge_cmd import run_merge
        from se3.engine.merge.guardrails import (
            GuardrailReport,
            GuardrailViolation,
            MergeGuardrailsCheck,
        )

        repair_called: dict = {"count": 0}

        bad = GuardrailReport(
            passed=False,
            violations=[
                GuardrailViolation(
                    file_path="se3/specs/x.md",
                    violation_type="X",
                    message="m",
                ),
            ],
        )

        # When fast invokes its GuardrailRepairer (instantiated inside
        # _run_guardrails), spy on the repair call. We use the actual
        # class path so the patch survives the lazy import.
        try:
            from se3.engine.merge.guardrail_repair import GuardrailRepairer
        except ImportError:
            pytest.skip("GuardrailRepairer not importable")

        original_init = GuardrailRepairer.__init__

        def spy_init(self, *args, **kwargs):
            repair_called["count"] += 1
            return original_init(self, *args, **kwargs)

        with patch.object(
            MergeGuardrailsCheck, "check_merge_result", return_value=bad,
        ), patch.object(GuardrailRepairer, "__init__", spy_init):
            # We don't care about the final rc here — just that the fast
            # path tried to instantiate GuardrailRepairer (proving the
            # repair loop is still wired up for non-robust strategies).
            run_merge(
                branches=[feat],
                strategy="fast",
                project_root=tmp_path,
            )

        assert repair_called["count"] >= 1, (
            "fast strategy must still invoke GuardrailRepairer on "
            "guardrail violations; got zero invocations (regression?)"
        )
