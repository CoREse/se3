"""i18n routing for user-visible ``se3 run`` console text (src/tianluo/commands/run.py).

These assert that run.py's migrated prompts/status text render through the
``tianluo.i18n`` catalog — the en-US reference and the zh-CN translation — rather
than a hardcoded English literal. The suite-wide autouse fixture pins the
language to en-US; each language case flips it explicitly via ``set_language``
(which overrides the resolution chain) and the fixture resets afterwards.
"""

from __future__ import annotations

import builtins

import pytest

from tianluo import i18n
from tianluo.commands import run


class TestPromptUserChoiceNonInteractive:
    """The EOF (non-interactive) branch of ``prompt_user_choice`` is localized."""

    def _run(self, monkeypatch, capsys, lang):
        i18n.set_language(lang)

        def _raise_eof(*_a, **_k):
            raise EOFError

        # ``input()`` is only reached on a TTY — off one the menu answer comes
        # from the shared stdin funnel — so pin the TTY branch here.
        monkeypatch.setattr(run.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(builtins, "input", _raise_eof)
        idx = run.prompt_user_choice("Pick one", ["First", "Abort"])
        # Non-interactive default is always the last option.
        assert idx == 1
        return capsys.readouterr().out

    def test_en_us(self, monkeypatch, capsys):
        out = self._run(monkeypatch, capsys, "en-US")
        assert "Non-interactive mode detected, selecting option 2 (Abort)" in out

    def test_zh_cn(self, monkeypatch, capsys):
        out = self._run(monkeypatch, capsys, "zh-CN")
        assert "检测到非交互模式，自动选择第 2 项（Abort）" in out
        assert "Non-interactive mode detected" not in out


class TestResumePickerEmpty:
    """``handle_resume_interactive`` localizes the "no flows" notice."""

    def _capture(self, tmp_path, lang):
        # run.py's get_console() is re-exported from tianluo.engine.display, so its
        # console is the display module-level one — swap that to capture output.
        from tianluo.engine import display

        import io

        from rich.console import Console

        buf = io.StringIO()
        saved = display._console
        display.set_console(Console(file=buf, width=100, force_terminal=False))
        try:
            i18n.set_language(lang)
            result = run.handle_resume_interactive(tmp_path)
        finally:
            display._console = saved
        assert result is None
        return buf.getvalue()

    def test_en_us(self, tmp_path):
        out = self._capture(tmp_path, "en-US")
        assert "No existing flows found. Starting new flow." in out

    def test_zh_cn(self, tmp_path):
        out = self._capture(tmp_path, "zh-CN")
        assert "未找到已有流程，开始新流程。" in out


def test_unknown_run_key_falls_back_to_en_us():
    """A run key absent from zh-CN renders the en-US reference, never the raw key."""
    from tianluo.i18n.loader import load_catalog

    i18n.set_language("zh-CN")
    # Every run key ships in both catalogs today; assert the fallback machinery
    # still routes an en-US-only key (resume_hint) rather than surfacing the key.
    rendered = i18n.t("cli.run.resume_hint")
    assert rendered == load_catalog("en-US")["cli.run.resume_hint"]
    assert rendered != "cli.run.resume_hint"

# ---------------------------------------------------------------------------
# G10: run-command strategy / usage-cost labels localize
# ---------------------------------------------------------------------------
class TestRunStrategyAndUsageLabels:
    """The PLAN grouping option errors and the session usage block labels come
    from the catalog — the mode tokens (capability/granular, auto/single/
    conservative) stay raw config values, the surrounding prose localizes."""

    def test_invalid_plan_decomposition_error_localizes(self, monkeypatch):
        from tianluo.i18n.loader import load_catalog

        i18n.set_language("zh-CN")
        text = i18n.t(
            "cli.run.invalid_plan_decomposition",
            decomposition="bogus",
            valid_decompositions="capability, granular",
        )
        assert "bogus" in text
        assert text != "cli.run.invalid_plan_decomposition"
        # The zh-CN prose must not be the en-US sentence.
        assert text != load_catalog("en-US")[
            "cli.run.invalid_plan_decomposition"].format(
            decomposition="bogus", valid_decompositions="capability, granular")

    def test_invalid_plan_granularity_error_localizes(self, monkeypatch):
        from tianluo.i18n.loader import load_catalog

        i18n.set_language("zh-CN")
        text = i18n.t(
            "cli.run.invalid_plan_granularity",
            granularity="bogus",
            valid_granularities="auto, single, conservative",
        )
        assert "bogus" in text
        assert text != "cli.run.invalid_plan_granularity"
        assert text != load_catalog("en-US")[
            "cli.run.invalid_plan_granularity"].format(
            granularity="bogus", valid_granularities="auto, single, conservative")

    def test_plan_mode_help_and_deprecation_localize(self, monkeypatch):
        from tianluo.i18n.loader import load_catalog

        en = load_catalog("en-US")
        zh = load_catalog("zh-CN")
        for key in (
            "cli.help.run.plan_decomposition",
            "cli.help.run.plan_granularity",
            "cli.run.deprecated_implementation_strategy",
        ):
            assert zh[key] != en[key], f"zh-CN {key} must be translated"

    def test_invalid_legacy_strategy_error_still_localizes(self, monkeypatch):
        from tianluo.i18n.loader import load_catalog

        i18n.set_language("zh-CN")
        text = i18n.t(
            "cli.run.invalid_implementation_strategy",
            strategy="bogus",
            valid_strategies="auto, direct, planned",
        )
        assert "bogus" in text
        assert text != "cli.run.invalid_implementation_strategy"
        assert text != load_catalog("en-US")[
            "cli.run.invalid_implementation_strategy"].format(
            strategy="bogus", valid_strategies="auto, direct, planned")

    def test_usage_cost_labels_exist_and_localize(self):
        from tianluo.i18n.loader import load_catalog

        en = load_catalog("en-US")
        zh = load_catalog("zh-CN")
        for key in ("cli.display.usage.actual_cost",
                    "cli.display.usage.estimated_cost",
                    "cli.display.usage.unknown_cost"):
            assert zh[key] != en[key], f"zh-CN {key} must be translated"
        i18n.set_language("zh-CN")
        actual = i18n.t("cli.display.usage.actual_cost")
        assert actual == zh["cli.display.usage.actual_cost"]
        assert actual != "cli.display.usage.actual_cost"

    def test_usage_status_labels_distinguish_statuses(self):
        from tianluo.i18n.loader import load_catalog

        en = load_catalog("en-US")
        labels = {
            s: en[f"usage.status.{s}"]
            for s in ("available", "partial", "unavailable", "legacy_ambiguous")
        }
        assert len(set(labels.values())) == 4, (
            "each usage status needs a distinct label")

