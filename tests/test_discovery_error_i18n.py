"""The discovery step's LLM-failure console panel is rendered through i18n.

The ``except LLMCallError`` branch of ``discovery_handler`` is user-visible
``se3 run`` console output (a Rich panel) and is also persisted into
``step.error_message`` — so its body and title must follow the selected UI
language like every other discovery render path.
"""

from types import SimpleNamespace

import pytest

from se3.engine.llm_caller import LLMCallError
from se3.engine.models import StepStatus
from se3.engine.steps import discovery as discovery_mod
from se3.i18n import set_language, t


@pytest.fixture
def captured_panels(monkeypatch):
    panels = []
    monkeypatch.setattr(
        "se3.engine.output.render_full",
        lambda content, title=None, **kw: panels.append((content, title)),
    )
    return panels


def _run_failing_discovery(monkeypatch, tmp_path, error):
    def _boom(**kwargs):
        raise error

    monkeypatch.setattr(discovery_mod, "_run_discovery_round", _boom)
    step = SimpleNamespace(
        inputs={"task_description": "add a widget"},
        outputs={},
        error_message=None,
    )
    flow = SimpleNamespace(change_path=tmp_path / "change.md", flow_id="f1")
    status = discovery_mod.discovery_handler(step, flow)
    return status, step


@pytest.mark.parametrize("lang", ["en-US", "zh-CN"])
def test_json_extraction_failure_panel_is_translated(
    monkeypatch, tmp_path, captured_panels, lang
):
    set_language(lang)
    status, step = _run_failing_discovery(
        monkeypatch, tmp_path, LLMCallError("JSON extraction failed: narrative text")
    )

    assert status == StepStatus.FAILED
    body, title = captured_panels[-1]
    assert body == t("engine.discovery.error_json_extraction")
    assert title == t("engine.discovery.error_title")
    # The same translated copy is what gets persisted for later display.
    assert step.error_message == body
    assert "engine.discovery." not in body  # keys resolved, not echoed back


def test_generic_llm_failure_panel_is_translated(monkeypatch, tmp_path, captured_panels):
    set_language("en-US")
    status, step = _run_failing_discovery(
        monkeypatch, tmp_path, LLMCallError("agent exited 1")
    )

    assert status == StepStatus.FAILED
    body, title = captured_panels[-1]
    assert body == t("engine.discovery.error_llm_call", error="agent exited 1")
    assert "agent exited 1" in body
    assert title == t("engine.discovery.error_title")
    assert step.error_message == body


def test_error_copy_translations_differ_by_language():
    set_language("en-US")
    en_title = t("engine.discovery.error_title")
    en_body = t("engine.discovery.error_json_extraction")
    set_language("zh-CN")
    assert t("engine.discovery.error_title") != en_title
    assert t("engine.discovery.error_json_extraction") != en_body
