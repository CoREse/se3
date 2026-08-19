"""Regression tests for the removal of the merge spec-guardrails chain.

The ``tianluo/specs/`` mirror was retired, so everything the merge subsystem
built on top of it — the post-merge spec diff/size checks, the LLM repair loop,
the ``luo guardrails`` command — is gone. These tests are negative by design:
they fail if any part of that chain is reintroduced, and they pin the read-path
tolerance that lets call files and reports written before the removal still be
answered instead of crashing.
"""

from __future__ import annotations

import importlib
import inspect
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tianluo.engine.merge.orchestrator import MergeOrchestrator


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    )


def _init_repo(path: Path) -> str:
    """Init a repo carrying a spec-shaped file. Returns the default branch."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / ".gitignore").write_text("/tianluo/*\n!/tianluo/specs/\n")
    spec_dir = path / "tianluo" / "specs" / "base"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(
        "## Requirement: Auth\n\n"
        "The system SHALL validate all user inputs.\n\n"
        "## Requirement: Audit\n\n"
        "The system SHALL record every write.\n"
    )
    (path / "README.md").write_text("# Test\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


# --------------------------------------------------------------------------
# the modules and the orchestrator surface are gone
# --------------------------------------------------------------------------

class TestGuardrailModulesRemoved:
    @pytest.mark.parametrize(
        "module",
        [
            "tianluo.engine.merge.guardrails",
            "tianluo.engine.merge.guardrail_repair",
        ],
    )
    def test_module_not_importable(self, module: str) -> None:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)

    def test_merge_package_exports_no_guardrail_symbols(self) -> None:
        import tianluo.engine.merge as merge_pkg

        exported = set(merge_pkg.__all__)
        assert not any("uardrail" in name for name in exported)
        assert "check_spec_diff" not in exported

    @pytest.mark.parametrize(
        "attr",
        [
            "_run_guardrails",
            "_violations_to_dicts",
            "_guardrails",
            "_repairer",
            "_max_repair_iterations",
            "_last_branch_repair_ran",
        ],
    )
    def test_orchestrator_has_no_guardrail_attribute(
        self, attr: str, tmp_path: Path,
    ) -> None:
        _init_repo(tmp_path)
        orch = MergeOrchestrator(project_root=tmp_path)
        assert not hasattr(orch, attr)

    def test_human_call_writer_has_no_guardrail_call(self) -> None:
        from tianluo.engine.merge.human_call import HumanCallWriter

        assert not hasattr(HumanCallWriter, "write_guardrail_call")


class TestMergeFlowHasNoGuardrailsPhase:
    """The merge path itself must carry no guardrails stage."""

    @pytest.mark.parametrize(
        "method", ["_merge_single_branch", "_apply_resolution", "_execute_inner"],
    )
    def test_merge_path_source_mentions_no_guardrails(self, method: str) -> None:
        source = inspect.getsource(getattr(MergeOrchestrator, method))
        assert "guardrail" not in source.lower()

    def test_spec_weakening_merge_is_not_blocked(self, tmp_path: Path) -> None:
        """A merge that rewrites SHALL→SHOULD and drops a Requirement lands.

        This is exactly the diff the retired guardrails rolled back. It must now
        complete as an ordinary merge — no violation, no rollback, no call file.
        """
        default_branch = _init_repo(tmp_path)
        _git(tmp_path, "checkout", "-b", "feature")
        (tmp_path / "tianluo" / "specs" / "base" / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate some user inputs.\n"
        )
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "weaken spec")
        _git(tmp_path, "checkout", default_branch)

        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
        report = orch.execute(["feature"])

        assert report.success is True, report.failure_reason
        assert report.merged_branches == ["feature"]
        assert report.pending_human is False
        assert report.human_call_file is None
        merged = (tmp_path / "tianluo" / "specs" / "base" / "spec.md").read_text()
        assert "SHOULD validate some user inputs" in merged
        assert "Requirement: Audit" not in merged


# --------------------------------------------------------------------------
# `luo guardrails` is no longer a command
# --------------------------------------------------------------------------

class TestGuardrailsCommandRemoved:
    def test_not_listed_in_help(self) -> None:
        from tianluo.cli import app

        result = CliRunner().invoke(app, ["--help"])
        assert "guardrails" not in result.output

    def test_invoking_it_fails(self) -> None:
        from tianluo.cli import app

        result = CliRunner().invoke(app, ["guardrails", "--sizes"])
        assert result.exit_code != 0

    def test_cli_module_defines_no_guardrails_command(self) -> None:
        import tianluo.cli as cli_mod

        assert not hasattr(cli_mod, "guardrails_cmd")
        assert not hasattr(cli_mod, "_run_spec_size_guardrails")

    @pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
    def test_locale_has_no_guardrail_keys(self, locale: str) -> None:
        import tianluo.i18n as i18n_pkg

        locale_file = Path(i18n_pkg.__file__).parent / "locales" / f"{locale}.json"
        data = json.loads(locale_file.read_text(encoding="utf-8"))
        assert not any("guardrail" in key for key in data)


# --------------------------------------------------------------------------
# read-path tolerance for artefacts written before the removal
# --------------------------------------------------------------------------

class TestLegacyArtefactTolerance:
    def _repo(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)

    def test_legacy_guardrail_call_file_aborts_cleanly(self, tmp_path: Path) -> None:
        """An old ``guardrail_violation`` call file still answers with exit 0.

        Its merge was already rolled back when it was written, so ``abort``
        must report success instead of failing on ``git merge --abort``.
        """
        from tianluo.commands.merge_respond import process_merge_response

        self._repo(tmp_path)
        calls_dir = tmp_path / "tianluo" / "calls"
        calls_dir.mkdir(parents=True)
        call_file = calls_dir / "merge_legacy_guardrail.json"
        call_file.write_text(json.dumps({
            "type": "guardrail_violation",
            "branch": "feature",
            "pre_merge_sha": "deadbeef",
            "violations": [
                {
                    "file_path": "tianluo/specs/base/spec.md",
                    "violation_type": "WEAKENING",
                    "message": "SHALL -> SHOULD",
                },
            ],
        }), encoding="utf-8")
        Path(str(call_file) + ".response").write_text(
            json.dumps({"choice": "abort", "feedback": "ok"}), encoding="utf-8",
        )

        assert process_merge_response(call_file, project_root=tmp_path) == 0

    def test_legacy_orphan_violation_field_is_ignored_on_accept(
        self, tmp_path: Path,
    ) -> None:
        """``orphan_guardrails_violations`` in an old call file no longer blocks.

        The field used to refuse the accept outright; it is now simply not read,
        and the recorded resolution is written back as usual.
        """
        from tianluo.commands.merge_respond import process_merge_response

        default_branch = _init_repo(tmp_path)
        _git(tmp_path, "checkout", "-b", "feature")
        (tmp_path / "note.txt").write_text("theirs\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "theirs")
        _git(tmp_path, "checkout", default_branch)
        (tmp_path / "note.txt").write_text("ours\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", "ours")
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True,
        )

        calls_dir = tmp_path / "tianluo" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        call_file = calls_dir / "merge_legacy_orphan.json"
        call_file.write_text(json.dumps({
            "type": "merge_conflict",
            "theirs_branch": "feature",
            "ours_branch": default_branch,
            "files": [
                {
                    "path": "note.txt",
                    "is_spec": False,
                    "llm_resolution": {"resolved_content": "merged\n"},
                },
            ],
            "orphan_guardrails_violations": [
                {
                    "file_path": "tianluo/specs/base/spec.md",
                    "violation_type": "DELETE",
                    "message": "requirement removed",
                },
            ],
        }), encoding="utf-8")
        Path(str(call_file) + ".response").write_text(
            json.dumps({"choice": "accept"}), encoding="utf-8",
        )

        assert process_merge_response(call_file, project_root=tmp_path) == 0
        assert (tmp_path / "note.txt").read_text() == "merged\n"

    def test_legacy_guardrail_failure_reason_still_renders(self) -> None:
        """Archived reports naming a removed reason must not crash the renderer."""
        from tianluo.commands.merge.failure_reason import (
            FailureReason,
            from_legacy_string,
        )
        from tianluo.commands.merge_cmd import _failure_title_and_summary

        reason, detail = from_legacy_string("guardrail_repair_exhausted")
        assert reason is FailureReason.UNEXPECTED
        assert detail == "guardrail_repair_exhausted"

        title, summary = _failure_title_and_summary("guardrail_repair_exhausted")
        assert title
        assert "guardrail_repair_exhausted" in summary
