"""Tests for the hard primary layer of spec-write protection (G3).

Covers:
  * ``spec_write_hook.main()`` — PreToolUse deny/allow decisions
  * ``spec_write_hook.ensure_guard_plugin`` — controlled guard-plugin generator
  * ``spec_write_hook.snapshot_spec_files`` / ``diff_spec_files`` helpers
  * ``ClaudeCodeRunner.build_call_args`` ``--plugin-dir`` wiring
  * ``LLMCaller._resolve_spec_guard_settings`` enable decision via the shared
    ``SPEC_WRITE_ALLOWED_STEPS`` exemption set (esp. sync_respond not enabled)
  * ``SpecWriteProtectionConfig`` defaults / explicit-off / invalid-value
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from tianluo.engine import spec_write_hook
from tianluo.engine.context_builder import SPEC_WRITE_ALLOWED_STEPS
from tianluo.config import (
    ConfigError,
    SpecWriteProtectionConfig,
    load_spec_write_protection_config,
)
from tianluo.claude_runner import ClaudeCodeRunner
from tianluo.engine.llm_caller import LLMCaller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_main(raw, monkeypatch, capsys):
    """Run ``spec_write_hook.main()`` feeding *raw* (str) as stdin.

    Returns ``(exit_code, stdout, stderr)``.
    """
    if not isinstance(raw, str):
        raw = json.dumps(raw)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    with pytest.raises(SystemExit) as exc:
        spec_write_hook.main()
    captured = capsys.readouterr()
    return exc.value.code, captured.out, captured.err


def _claude_runner(tmp_path):
    return ClaudeCodeRunner(
        project_root=tmp_path,
        command={"cmd": "claude", "priority": 0},
        setting_sources=["user"],
    )


# ---------------------------------------------------------------------------
# main() — deny path
# ---------------------------------------------------------------------------

class TestHookDeny:
    @pytest.mark.parametrize("tool_name", ["Write", "Edit"])
    def test_deny_spec_write_via_file_path(self, tmp_path, monkeypatch, capsys, tool_name):
        spec_file = tmp_path / "se3" / "specs" / "base" / "spec.md"
        payload = {
            "tool_name": tool_name,
            "tool_input": {"file_path": str(spec_file)},
            "cwd": str(tmp_path),
        }
        code, out, err = _run_main(payload, monkeypatch, capsys)
        assert code == 2
        decision = json.loads(out)
        assert (
            decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        )
        assert decision["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert decision["hookSpecificOutput"]["permissionDecisionReason"]
        # Reason also surfaced on stderr (exit-2 blocking protocol).
        assert "se3/specs" in err

    def test_deny_notebookedit_via_notebook_path(self, tmp_path, monkeypatch, capsys):
        spec_file = tmp_path / "se3" / "specs" / "flow-engine" / "spec.md"
        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": str(spec_file)},
            "cwd": str(tmp_path),
        }
        code, out, _err = _run_main(payload, monkeypatch, capsys)
        assert code == 2
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_deny_relative_file_path(self, tmp_path, monkeypatch, capsys):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "se3/specs/base/spec.md"},
            "cwd": str(tmp_path),
        }
        code, _out, _err = _run_main(payload, monkeypatch, capsys)
        assert code == 2

    def test_deny_nested_spec_subdir(self, tmp_path, monkeypatch, capsys):
        spec_file = tmp_path / "se3" / "specs" / "a" / "b" / "spec.md"
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(spec_file)},
            "cwd": str(tmp_path),
        }
        code, _out, _err = _run_main(payload, monkeypatch, capsys)
        assert code == 2


# ---------------------------------------------------------------------------
# main() — allow path
# ---------------------------------------------------------------------------

class TestHookAllow:
    def test_allow_src_write(self, tmp_path, monkeypatch, capsys):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "src" / "se3" / "foo.py")},
            "cwd": str(tmp_path),
        }
        code, out, err = _run_main(payload, monkeypatch, capsys)
        assert code == 0
        assert out == ""
        assert err == ""

    def test_allow_se3_state_write(self, tmp_path, monkeypatch, capsys):
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "se3" / "state" / "engine.json")},
            "cwd": str(tmp_path),
        }
        code, _out, _err = _run_main(payload, monkeypatch, capsys)
        assert code == 0

    def test_allow_specs_lookalike_outside_se3(self, tmp_path, monkeypatch, capsys):
        # A top-level ``specs/`` (legacy fallback path) is NOT the protected
        # ``se3/specs/`` directory, so a write there is allowed by the hook.
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "specs" / "base" / "spec.md")},
            "cwd": str(tmp_path),
        }
        code, _out, _err = _run_main(payload, monkeypatch, capsys)
        assert code == 0


# ---------------------------------------------------------------------------
# main() — defensive / malformed inputs always allow (never crash)
# ---------------------------------------------------------------------------

class TestHookDefensive:
    def test_empty_stdin_allows(self, tmp_path, monkeypatch, capsys):
        code, _out, _err = _run_main("", monkeypatch, capsys)
        assert code == 0

    def test_malformed_json_allows(self, tmp_path, monkeypatch, capsys):
        code, _out, _err = _run_main("not json {", monkeypatch, capsys)
        assert code == 0

    def test_missing_tool_input_allows(self, tmp_path, monkeypatch, capsys):
        code, _out, _err = _run_main({"tool_name": "Write"}, monkeypatch, capsys)
        assert code == 0

    def test_missing_file_path_allows(self, tmp_path, monkeypatch, capsys):
        code, _out, _err = _run_main(
            {"tool_name": "Write", "tool_input": {}}, monkeypatch, capsys
        )
        assert code == 0

    def test_non_dict_payload_allows(self, tmp_path, monkeypatch, capsys):
        code, _out, _err = _run_main("[1, 2, 3]", monkeypatch, capsys)
        assert code == 0


# ---------------------------------------------------------------------------
# ensure_guard_plugin
# ---------------------------------------------------------------------------

class TestEnsureGuardPlugin:
    def test_structure(self, tmp_path):
        plugin_dir = spec_write_hook.ensure_guard_plugin(tmp_path)
        assert plugin_dir == tmp_path / "se3" / "tmp" / "spec_write_guard_plugin"
        assert plugin_dir.is_dir()

        manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
        hooks_path = plugin_dir / "hooks" / "hooks.json"
        assert manifest_path.is_file()
        assert hooks_path.is_file()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest.get("name")
        assert manifest.get("version")
        assert manifest.get("description")

        data = json.loads(hooks_path.read_text(encoding="utf-8"))
        pre = data["hooks"]["PreToolUse"]
        assert isinstance(pre, list) and len(pre) == 1
        assert pre[0]["matcher"] == "Write|Edit|NotebookEdit"
        inner = pre[0]["hooks"][0]
        assert inner["type"] == "command"
        assert inner["command"].endswith("-m tianluo.engine.spec_write_hook")
        assert sys.executable in inner["command"]
        # Carries ONLY the hook — no permissions.deny re-introduced.
        assert "permissions" not in data

    def test_idempotent_returns_same_dir(self, tmp_path):
        p1 = spec_write_hook.ensure_guard_plugin(tmp_path)
        p2 = spec_write_hook.ensure_guard_plugin(tmp_path)
        assert p1 == p2
        hooks = p1 / "hooks" / "hooks.json"
        assert hooks.read_text() == (p2 / "hooks" / "hooks.json").read_text()

    def test_idempotent_no_rewrite_when_identical(self, tmp_path):
        plugin_dir = spec_write_hook.ensure_guard_plugin(tmp_path)
        hooks_path = plugin_dir / "hooks" / "hooks.json"
        manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
        os.utime(hooks_path, (1_000_000, 1_000_000))
        os.utime(manifest_path, (1_000_000, 1_000_000))
        before_hooks = hooks_path.stat().st_mtime
        before_manifest = manifest_path.stat().st_mtime
        spec_write_hook.ensure_guard_plugin(tmp_path)
        # Identical content => no rewrite => mtime unchanged.
        assert hooks_path.stat().st_mtime == before_hooks
        assert manifest_path.stat().st_mtime == before_manifest

    def test_interpreter_path_with_space_is_shell_quoted(
        self, tmp_path, monkeypatch
    ):
        # Claude CLI runs the hook command through a shell. An interpreter path
        # containing a space (a venv under "/home/user/my env/bin/python") must
        # be shell-quoted or the shell splits it and the hook never launches,
        # silently degrading the primary hard guard.
        import shlex

        spaced = "/home/user/my env/bin/python"
        monkeypatch.setattr(spec_write_hook.sys, "executable", spaced)
        plugin_dir = spec_write_hook.ensure_guard_plugin(tmp_path)
        data = json.loads(
            (plugin_dir / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        command = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        # The path is quoted, and the shell would parse the first token back to
        # the original interpreter path.
        assert shlex.quote(spaced) in command
        assert shlex.split(command)[0] == spaced
        assert command.endswith("-m tianluo.engine.spec_write_hook")


# ---------------------------------------------------------------------------
# snapshot_spec_files / diff_spec_files
# ---------------------------------------------------------------------------

class TestSnapshotDiff:
    def test_snapshot_and_diff_lifecycle(self, tmp_path):
        specs = tmp_path / "se3" / "specs" / "base"
        specs.mkdir(parents=True)
        f = specs / "spec.md"
        f.write_text("hello")

        before = spec_write_hook.snapshot_spec_files(tmp_path)
        assert "se3/specs/base/spec.md" in before

        # No change => empty diff.
        same = spec_write_hook.snapshot_spec_files(tmp_path)
        assert spec_write_hook.diff_spec_files(before, same) == []

        # Modify => detected.
        f.write_text("changed")
        after = spec_write_hook.snapshot_spec_files(tmp_path)
        assert spec_write_hook.diff_spec_files(before, after) == [
            "se3/specs/base/spec.md"
        ]

    def test_diff_detects_add_and_remove(self, tmp_path):
        specs = tmp_path / "se3" / "specs" / "base"
        specs.mkdir(parents=True)
        (specs / "spec.md").write_text("x")
        before = spec_write_hook.snapshot_spec_files(tmp_path)

        # Add a file.
        (specs / "extra.md").write_text("y")
        after_add = spec_write_hook.snapshot_spec_files(tmp_path)
        assert "se3/specs/base/extra.md" in spec_write_hook.diff_spec_files(
            before, after_add
        )

        # Remove the original.
        (specs / "spec.md").unlink()
        after_rm = spec_write_hook.snapshot_spec_files(tmp_path)
        assert "se3/specs/base/spec.md" in spec_write_hook.diff_spec_files(
            before, after_rm
        )

    def test_snapshot_missing_specs_dir(self, tmp_path):
        assert spec_write_hook.snapshot_spec_files(tmp_path) == {}


# ---------------------------------------------------------------------------
# capture_spec_contents / restore_spec_files
# ---------------------------------------------------------------------------

class TestCaptureRestore:
    def _seed(self, tmp_path):
        spec = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("original\n", encoding="utf-8")
        return spec

    def test_capture_returns_bytes(self, tmp_path):
        spec = self._seed(tmp_path)
        captured = spec_write_hook.capture_spec_contents(tmp_path)
        assert captured == {"se3/specs/base/spec.md": b"original\n"}
        # diff_spec_files works directly on byte maps too.
        spec.write_text("changed\n", encoding="utf-8")
        after = spec_write_hook.capture_spec_contents(tmp_path)
        assert spec_write_hook.diff_spec_files(captured, after) == [
            "se3/specs/base/spec.md"
        ]

    def test_capture_missing_specs_dir(self, tmp_path):
        assert spec_write_hook.capture_spec_contents(tmp_path) == {}

    def test_restore_reverts_modified_file(self, tmp_path):
        spec = self._seed(tmp_path)
        before = spec_write_hook.capture_spec_contents(tmp_path)
        spec.write_text("tampered\n", encoding="utf-8")
        failed = spec_write_hook.restore_spec_files(
            tmp_path, before, ["se3/specs/base/spec.md"]
        )
        assert failed == []
        assert spec.read_text(encoding="utf-8") == "original\n"

    def test_restore_deletes_newly_created_file(self, tmp_path):
        self._seed(tmp_path)
        before = spec_write_hook.capture_spec_contents(tmp_path)
        new_spec = tmp_path / "se3" / "specs" / "new" / "spec.md"
        new_spec.parent.mkdir(parents=True, exist_ok=True)
        new_spec.write_text("injected\n", encoding="utf-8")
        failed = spec_write_hook.restore_spec_files(
            tmp_path, before, ["se3/specs/new/spec.md"]
        )
        assert failed == []
        assert not new_spec.exists()

    def test_restore_recreates_deleted_file(self, tmp_path):
        spec = self._seed(tmp_path)
        before = spec_write_hook.capture_spec_contents(tmp_path)
        spec.unlink()
        failed = spec_write_hook.restore_spec_files(
            tmp_path, before, ["se3/specs/base/spec.md"]
        )
        assert failed == []
        assert spec.read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# ClaudeCodeRunner.build_call_args — --plugin-dir wiring
# ---------------------------------------------------------------------------

class TestBuildCallArgs:
    def test_plugin_dir_appended_when_provided(self, tmp_path):
        runner = _claude_runner(tmp_path)
        plugin_dir = tmp_path / "se3" / "tmp" / "spec_write_guard_plugin"
        args = runner.build_call_args(
            "the prompt", read_only=False, spec_guard_plugin=plugin_dir
        )
        assert "--plugin-dir" in args
        idx = args.index("--plugin-dir")
        assert args[idx + 1] == str(plugin_dir)
        # The guard must NOT be injected via a second --settings flag: a
        # duplicated --settings clobbers the agent's own --settings (and model).
        assert "--settings" not in args

    def test_argv_byte_identical_when_none(self, tmp_path):
        runner = _claude_runner(tmp_path)
        base = runner.build_call_args("the prompt", read_only=False)
        explicit_none = runner.build_call_args(
            "the prompt", read_only=False, spec_guard_plugin=None
        )
        assert base == explicit_none
        assert "--plugin-dir" not in base
        assert "--settings" not in base

    def test_plugin_dir_composes_with_read_only(self, tmp_path):
        runner = _claude_runner(tmp_path)
        plugin_dir = tmp_path / "guard_plugin"
        args = runner.build_call_args(
            "p", read_only=True, spec_guard_plugin=plugin_dir
        )
        assert "--disallowedTools" in args
        assert "--plugin-dir" in args
        assert "--settings" not in args


# ---------------------------------------------------------------------------
# LLMCaller._resolve_spec_guard_settings — enable decision
# ---------------------------------------------------------------------------

def _caller(tmp_path, step_type):
    return LLMCaller(
        project_root=tmp_path,
        step_type=step_type,
        agents=[{"cmd": "claude", "name": "claude", "priority": 0}],
    )


class TestResolveSpecGuardSettings:
    def test_protected_step_enables_hook(self, tmp_path):
        caller = _caller(tmp_path, "implement")
        path = caller._resolve_spec_guard_settings()
        assert path is not None
        assert path.is_dir()
        assert path.name == "spec_write_guard_plugin"
        assert (path / "hooks" / "hooks.json").is_file()

    @pytest.mark.parametrize(
        "step",
        ["update_spec", "sync_scan", "sync_analyze", "sync_resolve", "sync_respond"],
    )
    def test_exempt_steps_do_not_enable_hook(self, tmp_path, step):
        # All steps in the shared exemption set return None — most importantly
        # sync_respond, whose omission would have its Way-A Edit denied.
        assert step in SPEC_WRITE_ALLOWED_STEPS
        caller = _caller(tmp_path, step)
        assert caller._resolve_spec_guard_settings() is None

    def test_decision_uses_shared_exemption_set_only(self, tmp_path):
        # Every exempt step must resolve to None; no literal list is re-derived.
        for step in SPEC_WRITE_ALLOWED_STEPS:
            caller = _caller(tmp_path, step)
            assert caller._resolve_spec_guard_settings() is None

    def test_hook_disabled_via_config(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "spec_write_protection:\n  hook_enabled: false\n"
        )
        caller = _caller(tmp_path, "implement")
        assert caller._resolve_spec_guard_settings() is None

    def test_result_is_cached(self, tmp_path):
        caller = _caller(tmp_path, "implement")
        first = caller._resolve_spec_guard_settings()
        second = caller._resolve_spec_guard_settings()
        assert first == second
        assert caller._spec_guard_settings_computed is True


# ---------------------------------------------------------------------------
# SpecWriteProtectionConfig
# ---------------------------------------------------------------------------

class TestSpecWriteProtectionConfig:
    def test_defaults_all_on(self, tmp_path):
        cfg = load_spec_write_protection_config(tmp_path)
        assert cfg.hook_enabled is True
        assert cfg.diff_fallback_enabled is True

    def test_none_project_root_defaults(self):
        cfg = load_spec_write_protection_config(None)
        assert cfg.hook_enabled is True
        assert cfg.diff_fallback_enabled is True

    def test_absent_section_defaults(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("workflow:\n  max_fix_iterations: 5\n")
        cfg = load_spec_write_protection_config(tmp_path)
        assert cfg.hook_enabled is True
        assert cfg.diff_fallback_enabled is True

    def test_explicit_off(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "spec_write_protection:\n"
            "  hook_enabled: false\n"
            "  diff_fallback_enabled: false\n"
        )
        cfg = load_spec_write_protection_config(tmp_path)
        assert cfg.hook_enabled is False
        assert cfg.diff_fallback_enabled is False

    def test_invalid_value_raises(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(
            "spec_write_protection:\n  hook_enabled: maybe\n"
        )
        with pytest.raises(ConfigError) as exc:
            load_spec_write_protection_config(tmp_path)
        assert "hook_enabled" in str(exc.value)

    def test_non_mapping_section_raises(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("spec_write_protection: [1, 2]\n")
        with pytest.raises(ConfigError):
            load_spec_write_protection_config(tmp_path)

    def test_from_dict_partial(self):
        cfg = SpecWriteProtectionConfig.from_dict({"hook_enabled": False})
        assert cfg.hook_enabled is False
        # Unspecified key keeps its default.
        assert cfg.diff_fallback_enabled is True
