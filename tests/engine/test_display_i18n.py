"""i18n routing for the UI-framework text in src/tianluo/engine/display.py.

The section labels / usage-column headers / default block titles rendered by
display.py are UI-framework text and must go through the ``tianluo.i18n`` catalog;
the LLM content threaded into those blocks (summary bodies, decision text) is
data and must pass through verbatim. These tests assert both: the framework
labels flip with the language, and the content stays untranslated.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from tianluo import i18n
from tianluo.engine import display
from tianluo.engine.display import render_proposal, render_usage_block, set_console
from tianluo.engine.token_usage import UsageTotals


@pytest.fixture(autouse=True)
def _isolate_console():
    saved = display._console
    yield
    display._console = saved


def _plain_console() -> tuple[Console, StringIO]:
    buf = StringIO()
    return Console(file=buf, width=100, force_terminal=True, highlight=False), buf


def _usage() -> UsageTotals:
    return UsageTotals(
        input_tokens=12345,
        output_tokens=6789,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=0,
        total_cost_usd=0.0123,
    )


class TestUsageBlockLabels:
    def test_default_title_and_labels_en_us(self):
        i18n.set_language("en-US")
        console, buf = _plain_console()
        set_console(console)
        render_usage_block(self._usage_or_skip())
        out = buf.getvalue()
        assert " ## Token Usage " in out
        assert "Input tokens" in out
        assert "Cache read" in out
        assert "Cost" in out

    def test_default_title_and_labels_zh_cn(self):
        i18n.set_language("zh-CN")
        console, buf = _plain_console()
        set_console(console)
        render_usage_block(self._usage_or_skip())
        out = buf.getvalue()
        assert " ## Token 用量 " in out
        assert "输入 tokens" in out
        assert "缓存读取" in out
        assert "费用" in out
        # No English label bled through.
        assert "Input tokens" not in out

    def test_explicit_title_preserved(self):
        # An explicit title passed by a caller is used verbatim, not overridden.
        i18n.set_language("zh-CN")
        console, buf = _plain_console()
        set_console(console)
        render_usage_block(self._usage_or_skip(), title="Session Token Usage")
        assert " ## Session Token Usage " in buf.getvalue()

    def _usage_or_skip(self) -> UsageTotals:
        return _usage()


class TestProposalLabelsVsContent:
    def test_labels_localized_content_verbatim(self):
        i18n.set_language("zh-CN")
        console, buf = _plain_console()
        set_console(console)
        # The summary body is LLM content and must survive untranslated.
        render_proposal({"summary": "Add a widget", "rationale": "because"})
        out = buf.getvalue()
        assert " ## 方案 " in out
        assert "摘要：" in out
        assert "理由：" in out
        # Content passed through verbatim (not translated / not dropped).
        assert "Add a widget" in out
        assert "because" in out
        # English framework labels are gone.
        assert "Summary:" not in out
        assert "Rationale:" not in out
