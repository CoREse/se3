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

from tianluo import i18n
from tianluo.cli import app
from tianluo.i18n import loader

# Repo ``src/`` root (this file is tests/commands/, so up two + /src).
_SRC = Path(__file__).resolve().parents[2] / "src"

# Every module migrated in group G2 (tasks 5 & 6).
MIGRATED_MODULES = [
    "tianluo/cli.py",
    "tianluo/commands/end_session_cmd.py",
    "tianluo/commands/salvage_cmd.py",
    "tianluo/commands/migrate_cmd.py",
    "tianluo/commands/merge_respond.py",
    "tianluo/commands/merge_cmd.py",
    "tianluo/commands/history_cmd.py",
    "tianluo/commands/issue_cmd.py",
    "tianluo/commands/init_cmd.py",
    "tianluo/commands/code_index_cmd.py",
    "tianluo/commands/worktree_cmd.py",
    # Added with the command itself: every user-visible string of
    # `luo review-scope diff` goes through the catalog from day one.
    "tianluo/commands/review_scope_cmd.py",
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
        (["--version"], "luo version", "luo 版本"),
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

    from tianluo import cli

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

    from tianluo.commands import issue_cmd
    from tianluo.engine.issue_manager import IssueManager

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
    from tianluo.commands import history_cmd

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


def test_history_show_scope_round_localized(monkeypatch, tmp_path):
    """The SELF_CHECK scope-audit row is a keyed template: under zh-CN it must
    render translated text, never the hardcoded English '(fix N)'."""
    from unittest.mock import patch

    from tianluo.commands import history_cmd

    detail = {
        "flow_id": "f1",
        "status": "completed",
        "task_description": "t",
        "task_type": "feature",
        "progress": {"completed": 3, "total": 3},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "chat_sessions": 1,
        "steps": [],
        "review_scope": {
            "active_round": {
                "round_id": "scr-x",
                "scope_mode": "incremental",
                "pass_index": 2,
                "fix_iteration": 1,
            },
            "completed_full_rounds": 1,
        },
    }
    monkeypatch.setenv("SE3_LANG", "zh-CN")
    i18n.reset_language()
    runner = CliRunner()
    with patch.object(history_cmd, "get_project_root", return_value=tmp_path), \
         patch.object(history_cmd, "get_flow_detail", return_value=detail):
        out = runner.invoke(history_cmd.app, ["show", "f1"]).output

    # The scope_mode value itself is localized too (WebUI scope.mode.* parity).
    assert "增量#2（修复 1）" in out
    assert "(fix 1)" not in out
    assert "incremental" not in out

    # The en-US rendering keeps the historical format.
    monkeypatch.setenv("SE3_LANG", "en-US")
    i18n.reset_language()
    with patch.object(history_cmd, "get_project_root", return_value=tmp_path), \
         patch.object(history_cmd, "get_flow_detail", return_value=detail):
        en_out = runner.invoke(history_cmd.app, ["show", "f1"]).output
    assert "incremental#2 (fix 1)" in en_out

def test_history_show_plan_mode_value_and_legacy_reason_localized(
    monkeypatch, tmp_path,
):
    """Plan-mode enum values and the projection-authored legacy-inference
    sentence are UI chrome: under zh-CN both must render translated, matching
    the WebUI's ``plan.*`` / ``plan.reason.*`` treatment."""
    from unittest.mock import patch

    from tianluo.commands import history_cmd
    from tianluo.strategy_view import LEGACY_INFER_REASON

    detail = {
        "flow_id": "f1",
        "status": "completed",
        "task_description": "t",
        "task_type": "small",
        "progress": {"completed": 3, "total": 3},
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "chat_sessions": 1,
        "steps": [],
        "plan_mode": {
            "decomposition": None,
            "granularity": None,
            "group_count": None,
            "reason": LEGACY_INFER_REASON,
            "reason_key": "legacy_inference",
            "legacy_strategy": "not_applicable",
            "inferred": True,
        },
    }
    monkeypatch.setenv("SE3_LANG", "zh-CN")
    i18n.reset_language()
    runner = CliRunner()
    with patch.object(history_cmd, "get_project_root", return_value=tmp_path), \
         patch.object(history_cmd, "get_flow_detail", return_value=detail):
        out = runner.invoke(history_cmd.app, ["show", "f1"]).output

    assert "不适用" in out
    assert "not_applicable" not in out
    assert "Inferred from persisted legacy" not in out

    # A persisted (engine-authored) reason stays verbatim: it is flow data.
    detail["plan_mode"] = {
        "decomposition": "capability",
        "granularity": "single",
        "group_count": 1,
        "reason": "PLAN sized this as one autonomous call.",
        "reason_key": "",
        "legacy_strategy": None,
        "inferred": False,
    }
    with patch.object(history_cmd, "get_project_root", return_value=tmp_path), \
         patch.object(history_cmd, "get_flow_detail", return_value=detail):
        out = runner.invoke(history_cmd.app, ["show", "f1"]).output
    assert "PLAN sized this as one autonomous call." in out


# ---------------------------------------------------------------------------
# G10: plan-mode / scope / usage-cost labels across both catalogs
# ---------------------------------------------------------------------------
G10_CLI_KEYS = [
    "history.field.plan_mode",
    "history.field.plan_mode_value",
    "history.field.plan_mode_groups",
    "history.field.plan_mode_legacy",
    "history.field.plan_mode_inferred",
    "history.field.plan_mode_reason",
    "history.field.scope",
    "history.field.scope_round",
    "history.field.scope_full_rounds",
    "history.field.plan_mode_reason_legacy_inference",
    "history.field.plan_mode_reason_legacy_strategy",
    "history.field.plan_mode_reason_no_plan_surface",
    "history.plan.decomposition.capability",
    "history.plan.decomposition.granular",
    "history.plan.granularity.auto",
    "history.plan.granularity.single",
    "history.plan.granularity.conservative",
    "history.plan.legacy.direct",
    "history.plan.legacy.planned",
    "history.plan.legacy.not_applicable",
    "history.plan.unknown",
    "history.scope.mode.full",
    "history.scope.mode.incremental",
    "history.usage.header",
    "history.usage.no_usage",
    "history.usage.calls_header",
    "history.usage.steps_header",
    "history.usage.flow_header",
    "history.usage.legacy_note",
    "history.usage.col.actual",
    "history.usage.col.estimate",
    "history.usage.col.completeness",
    "history.plan_artifacts.header",
    "history.plan_artifacts.task_groups",
    "history.plan_artifacts.adjudicated_plan",
    "history.plan_artifacts.plan_task_finding",
    "usage.status.available",
    "usage.status.partial",
    "usage.status.unavailable",
    "usage.status.legacy_ambiguous",
    "usage.completeness_complete",
    "usage.completeness_partial",
    "cli.display.usage.actual_cost",
    "cli.display.usage.estimated_cost",
    "cli.display.usage.unknown_cost",
    "cli.display.usage.unknown_calls",
    "cli.display.usage.unknown_model",
    "cli.display.usage.model_unknown",
    "cli.display.usage.unknown_price",
    "cli.display.usage.unknown_cache_ttl",
    "cli.display.usage.completeness",
    "cli.display.usage.completeness_complete",
    "cli.display.usage.completeness_partial",
    "cli.run.invalid_implementation_strategy",
    "cli.help.run.implementation_strategy",
    "cli.run.invalid_plan_decomposition",
    "cli.run.invalid_plan_granularity",
    "cli.run.deprecated_implementation_strategy",
    "cli.help.run.plan_decomposition",
    "cli.help.run.plan_granularity",
]


def test_g10_cli_keys_exist_in_both_catalogs():
    """The G10 plan-mode/scope/usage labels ship in both catalogs; a missing
    zh-CN entry must fall back to en-US, not render the raw dotted key."""
    from tianluo.i18n.loader import load_catalog

    en = load_catalog("en-US")
    zh = load_catalog("zh-CN")
    missing_en = [k for k in G10_CLI_KEYS if k not in en]
    missing_zh = [k for k in G10_CLI_KEYS if k not in zh]
    assert not missing_en, f"en-US is missing G10 keys: {missing_en}"
    assert not missing_zh, f"zh-CN is missing G10 keys: {missing_zh}"


def test_g10_cli_prose_keys_are_translated():
    """Prose G10 labels must be genuinely translated in zh-CN (token values such
    as usage statuses may differ in kind but never be the raw key)."""
    from tianluo.i18n.loader import load_catalog

    en = load_catalog("en-US")
    zh = load_catalog("zh-CN")
    for key in ("history.field.plan_mode", "history.field.scope",
                "history.usage.header", "cli.display.usage.actual_cost",
                "cli.display.usage.estimated_cost"):
        assert zh[key] != key, f"zh-CN {key} must be translated"
        assert zh[key] != en[key], f"zh-CN {key} must not copy the en-US value"

