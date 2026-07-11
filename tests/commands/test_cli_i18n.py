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
        # cli.merge.branch_required (BadParameter path — no filesystem needed).
        (["merge"], "At least one branch name is required", "至少需要提供一个分支名称"),
        # cli.version.
        (["--version"], "se3 version", "se3 版本"),
    ],
)
def test_command_output_switches_language(args, en_substr, zh_substr):
    runner = CliRunner()
    i18n.set_language("en-US")
    en_out = runner.invoke(app, args).output
    i18n.set_language("zh-CN")
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
