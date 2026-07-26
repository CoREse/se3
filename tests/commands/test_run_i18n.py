"""i18n routing for user-visible ``se3 run`` console text (src/se3/commands/run.py).

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
