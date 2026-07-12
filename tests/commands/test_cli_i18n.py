"""Tests for the G2 CLI command-layer i18n migration (tasks 5 & 6).

These guard the two contracts the migration must uphold:

1. Coverage — every ``t("<key>")`` literal used by a migrated command module has
   a matching entry in the ``en-US`` base catalog, so a missing key can never
   silently degrade to the raw dotted key in user output. This is derived
   structurally from the source (AST), so a newly added ``t()`` call with a
   forgotten catalog entry fails here rather than in production.
2. Fallback safety — ``zh-CN`` holds no key absent from the ``en-US`` base (the
   base is the full key set), so per-key fallback to ``en-US`` is always
   possible for an untranslated key.

Plus behavioural switching: representative commands render in ``en-US`` vs
``zh-CN`` and honour the ``SE3_LANG`` precedence entry. The autouse
``_pin_ui_language_en`` fixture in ``tests/conftest.py`` pins the language to
en-US; the switching tests override it explicitly and reset afterward.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from se3 import i18n
from se3.cli import app
from se3.i18n import loader

# Repo ``src/`` root (this file is tests/commands/, so up two + /src).
_SRC = Path(__file__).resolve().parents[2] / "src"

# Every module migrated in group G2 (tasks 5 & 6).
MIGRATED_MODULES = [
    "se3/cli.py",
    "se3/commands/end_session_cmd.py",
    "se3/commands/salvage_cmd.py",
    "se3/commands/migrate_cmd.py",
    "se3/commands/merge_respond.py",
    "se3/commands/merge_cmd.py",
    "se3/commands/history_cmd.py",
    "se3/commands/issue_cmd.py",
    "se3/commands/init_cmd.py",
    "se3/commands/code_index_cmd.py",
    "se3/commands/worktree_cmd.py",
]


def _t_literal_keys(relpath: str) -> set:
    """Return the set of first-arg string literals passed to ``t(...)`` in a file.

    Structural (AST) extraction, not a text grep: only ``t("literal", ...)``
    call sites with a constant first argument are collected, which is exactly
    the set that must resolve against the catalog.
    """
    tree = ast.parse((_SRC / relpath).read_text(encoding="utf-8"))
    keys = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "t"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
    return keys


@pytest.mark.parametrize("relpath", MIGRATED_MODULES)
def test_every_used_key_present_in_en_us(relpath):
    """en-US holds every t() key any migrated module renders."""
    en = loader.load_catalog("en-US")
    missing = sorted(k for k in _t_literal_keys(relpath) if k not in en)
    assert not missing, f"{relpath}: keys missing from en-US.json: {missing}"


def test_all_modules_actually_migrated():
    """Sanity: each migrated module actually routes text through t()."""
    for relpath in MIGRATED_MODULES:
        assert _t_literal_keys(relpath), f"{relpath}: no t() call sites found"


def test_zh_keys_are_subset_of_en_base():
    """The base (en-US) is the full key set; zh-CN adds no orphan key."""
    en = set(loader.load_catalog("en-US"))
    zh = set(loader.load_catalog("zh-CN"))
    orphan = sorted(zh - en)
    assert not orphan, f"zh-CN has keys absent from the en-US base: {orphan}"


def test_missing_zh_key_falls_back_to_en_per_key():
    """A key present in en-US but absent from zh-CN renders the en-US string."""
    i18n.set_language("zh-CN")
    # Seeded intentionally absent from zh-CN in G1 to exercise the fallback.
    assert "cli.run.resume_hint" not in loader.load_catalog("zh-CN")
    assert (
        i18n.t("cli.run.resume_hint")
        == loader.load_catalog("en-US")["cli.run.resume_hint"]
    )


@pytest.mark.parametrize(
    "args, en_substr, zh_substr",
    [
        # cli.merge.branch_required (BadParameter path, raised after the merge
        # command binds the project root).
        (["merge"], "At least one branch name is required", "至少需要提供一个分支名称"),
        # cli.version.
        (["--version"], "se3 version", "se3 版本"),
    ],
)
def test_command_output_switches_language(args, en_substr, zh_substr, monkeypatch):
    """Language is driven through SE3_LANG, not a bare ``set_language()``.

    Commands that resolve a project root re-bind the language from the full
    resolution chain (so a project's ``language.language`` reaches text emitted
    after the bind). That re-bind discards a singleton pinned by ``set_language``,
    so the env tier — which the bind honors — is what a command-level language
    assertion must use.
    """
    runner = CliRunner()

    monkeypatch.setenv("SE3_LANG", "en-US")
    i18n.reset_language()
    en_out = runner.invoke(app, args).output

    monkeypatch.setenv("SE3_LANG", "zh-CN")
    i18n.reset_language()
    zh_out = runner.invoke(app, args).output

    assert en_substr in en_out
    assert zh_substr in zh_out
    # The two renderings must actually differ (guards an un-migrated string).
    assert en_out != zh_out


def test_se3_lang_env_drives_cli_output(monkeypatch):
    """SE3_LANG (top of the precedence chain) selects the CLI output language."""
    monkeypatch.setenv("SE3_LANG", "zh-CN")
    # Re-resolve from the env (the conftest fixture pinned en-US via set_language).
    i18n.reset_language()
    out = CliRunner().invoke(app, ["merge"]).output
    assert "至少需要提供一个分支名称" in out
    i18n.reset_language()


# ---------------------------------------------------------------------------
# Late-bound interactive prompt chrome
# ---------------------------------------------------------------------------


def test_multiline_prompt_chrome_is_resolved_at_call_time(monkeypatch):
    """``_read_multiline_input`` must translate its title/message when called,
    not when the module is imported.

    Signature defaults evaluate once at import — before a command resolves the
    project root and re-binds the language — which pinned the task-input prompt
    to the import-time language while the rest of the output followed the
    project. The defaults are therefore ``None`` and resolved in the body.
    """
    import inspect
    import io

    from se3 import cli

    sig = inspect.signature(cli._read_multiline_input)
    assert sig.parameters["prompt_title"].default is None
    assert sig.parameters["prompt_message"].default is None

    captured: dict = {}
    monkeypatch.setattr(
        cli, "render_full",
        lambda content, title=None: captured.update(title=title),
    )
    # Non-interactive branch: reads stdin whole and renders it under the title.
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("a task\n"))

    monkeypatch.setenv("SE3_LANG", "zh-CN")
    i18n.reset_language()
    cli._read_multiline_input()
    zh_title = captured["title"]

    monkeypatch.setenv("SE3_LANG", "en-US")
    i18n.reset_language()
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("a task\n"))
    cli._read_multiline_input()
    en_title = captured["title"]

    assert zh_title == loader.load_catalog("zh-CN")["cli.input.title"]
    assert en_title == loader.load_catalog("en-US")["cli.input.title"]
    assert zh_title != en_title


# ---------------------------------------------------------------------------
# Status *values* (not just their column labels) render through i18n
# ---------------------------------------------------------------------------


def _cell_texts(render) -> str:
    """Render a Rich table/console callable into plain text for assertions."""
    from rich.console import Console

    console = Console(width=200, record=True)
    render(console)
    return console.export_text()


def test_issue_list_and_show_localize_status_values(tmp_path, monkeypatch):
    """A localized ``状态`` column over a raw ``open`` value is a half-migrated
    table: the status token is user-facing text and must follow the UI language."""
    from unittest.mock import patch

    from se3.commands import issue_cmd
    from se3.engine.issue_manager import IssueManager

    (tmp_path / ".git").mkdir()
    mgr = IssueManager(tmp_path)
    mgr._ensure_dirs()
    issue = mgr.create(title="a task", description="d", source="cli")

    monkeypatch.setenv("SE3_LANG", "zh-CN")
    i18n.reset_language()
    runner = CliRunner()
    with patch.object(issue_cmd, "get_project_root", return_value=tmp_path):
        list_out = runner.invoke(issue_cmd.app, ["list"]).output
        show_out = runner.invoke(issue_cmd.app, ["show", issue.id]).output

    assert "待处理" in list_out
    assert "open" not in list_out
    assert "待处理" in show_out
    assert "open" not in show_out


def test_history_tables_localize_status_values(monkeypatch):
    """Flow-list and step-detail tables render engine status tokens; under zh-CN
    they must show translated text, not the raw enum value."""
    from se3.commands import history_cmd

    monkeypatch.setenv("SE3_LANG", "zh-CN")
    i18n.reset_language()

    flows = [
        {
            "flow_id": "f1",
            "status": "completed",
            "task_description": "t",
            "progress": "3/3",
            "updated_at": "",
            "source": "state",
        }
    ]
    out = _cell_texts(
        lambda console: monkeypatch.setattr(history_cmd, "console", console)
        or history_cmd._render_flows_table(flows, "T")
    )
    assert "已完成" in out
    assert "completed" not in out
