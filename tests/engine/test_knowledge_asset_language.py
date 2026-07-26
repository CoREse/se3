"""Knowledge-asset language injection routing (spec_language).

G4 repositions ``language.spec_language`` to mean the *knowledge-asset language*:
the language ``charter.md`` and the code-index are written in. Two live writers
now inject it into their LLM prompts — the ``charter_freshness`` propose prompt
and the code-index per-group summary prompt — where previously neither had any
language control.

These tests pin the injection ROUTING (not the LLM itself, which is stubbed):

* spec_language set  -> both prompts carry the knowledge-asset language
  instruction (including the "preserve technical symbols verbatim" clause, and
  free of the spec-file/SHALL-MUST wording, which would bias plain charter and
  code-index prose toward requirement statements);
* spec_language unset -> zero injection, so the prompt is byte-for-byte what it
  was before this change (a regression guard against accidental always-on
  injection);
* ``language.language`` set but ``spec_language`` unset -> still zero injection
  at both writers: the unified human language must NOT bleed into the knowledge
  asset, whose language is spec_language's alone.

Neither writer runs a shell command, so the SE3_TEST_RUNNING recursion guard in
``_run_command`` is not in play here; the tests stub the LLM caller directly and
never enter the test-step path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.engine import code_index
from tianluo.engine.code_index import SummaryTarget, _make_llm_summarizer
from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from tianluo.engine.steps import charter_freshness


# ---------------------------------------------------------------------------
# isolation + helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_global_home(tmp_path_factory, monkeypatch):
    """Point ``~/.se3/config.yaml`` at an empty isolated home.

    ``LanguageConfig.load`` merges the global ``~/.se3/config.yaml`` in, so a
    developer's real global ``language:`` block would otherwise leak in and flip
    the zero-injection assertions. Every case here runs against a clean home.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr("tianluo.config.Path.home", lambda: home)
    return home


def _write_project_language(project_root: Path, *, language=None, spec_language=None) -> None:
    """Write an ``se3.yaml`` with a ``language:`` section under ``project_root``."""
    lines = ["language:"]
    lines.append(f"  language: {language}" if language else "  language: null")
    lines.append(
        f"  spec_language: {spec_language}" if spec_language else "  spec_language: null"
    )
    (project_root / "se3.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- charter_freshness -----------------------------------------------------

def _install_fake_caller(monkeypatch, response: str) -> dict:
    """Stub ``charter_freshness.LLMCaller`` and capture the propose prompt."""
    state = {"prompts": []}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, prompt, **kwargs):
            state["prompts"].append(prompt)
            return response

    monkeypatch.setattr(charter_freshness, "LLMCaller", FakeCaller)
    return state


def _make_flow(project_root: Path) -> FlowInstance:
    flow = FlowInstance(
        task_description="Tweak the widget loop",
        task_type="feature",
        status=FlowStatus.INIT,
    )
    # change_path.parent is the project_root the handler loads config from.
    flow.change_path = project_root / "change"
    return flow


def _make_step() -> Step:
    # A non-empty diff is what triggers the propose LLM call (an empty diff takes
    # the cheap no-LLM pass).
    return Step(
        step_type=StepType.CHARTER_FRESHNESS,
        inputs={"changes_made": {"files_changed": ["src/foo.py"]}},
    )


_PROPOSE_RESPONSE = json.dumps(
    {
        "charter_update_needed": False,
        "touched_classes": [],
        "reason": "Implementation detail only.",
        "suggested_update": "",
        "patch": [],
    }
)


def _charter_prompt(tmp_path, monkeypatch, *, language=None, spec_language=None) -> str:
    _write_project_language(tmp_path, language=language, spec_language=spec_language)
    state = _install_fake_caller(monkeypatch, _PROPOSE_RESPONSE)
    result = charter_freshness.charter_freshness_handler(_make_step(), _make_flow(tmp_path))
    assert result is StepStatus.COMPLETED
    assert state["prompts"], "expected a propose LLM call"
    return state["prompts"][0]


def test_charter_prompt_injects_spec_language_when_set(tmp_path, monkeypatch):
    prompt = _charter_prompt(tmp_path, monkeypatch, spec_language="zh-CN")
    assert "zh-CN" in prompt
    assert "MUST respond in zh-CN" in prompt
    # The shared "preserve technical symbols verbatim" clause must ride along so
    # code identifiers survive translation.
    assert "Preserve all technical symbols verbatim" in prompt
    # Knowledge-asset framing: the asset's language is authoritative, and the
    # spec-file / SHALL-MUST wording must NOT leak in (it would bias charter.md
    # toward requirement statements).
    assert "knowledge asset" in prompt
    assert "authoritative" in prompt
    assert "spec file" not in prompt
    assert "SHALL/MUST" not in prompt


def test_charter_prompt_zero_injection_when_spec_language_unset(tmp_path, monkeypatch):
    prompt = _charter_prompt(tmp_path, monkeypatch)
    assert "MUST respond in" not in prompt
    assert "Preserve all technical symbols verbatim" not in prompt


def test_charter_prompt_ignores_human_language_when_spec_language_unset(tmp_path, monkeypatch):
    # language.language is the unified human language; it must NOT drive the
    # charter (a knowledge asset). spec_language alone governs here.
    prompt = _charter_prompt(tmp_path, monkeypatch, language="zh-CN")
    assert "MUST respond in" not in prompt
    assert "zh-CN" not in prompt


# --- code-index summaries --------------------------------------------------

def _install_fake_summary_caller(monkeypatch) -> dict:
    """Stub the LLMCaller that ``_summarize_group`` imports locally and capture
    each group's summary prompt.

    ``_make_llm_summarizer`` does ``from .llm_caller import LLMCaller`` inside the
    worker, so the stub must live on ``tianluo.engine.llm_caller`` (patching the name
    on ``code_index`` would miss the fresh local import).
    """
    from tianluo.engine import llm_caller

    state = {"prompts": []}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, prompt, **kwargs):
            state["prompts"].append(prompt)
            return json.dumps({"foo.py::foo": "does foo"})

    monkeypatch.setattr(llm_caller, "LLMCaller", FakeCaller)
    return state


def _make_summary_target() -> SummaryTarget:
    return SummaryTarget(
        id="foo.py::foo",
        path="foo.py",
        kind="function",
        name="foo",
        content="def foo():\n    return 1",
        level="symbol",
    )


def _code_index_prompt(tmp_path, monkeypatch, *, language=None, spec_language=None) -> str:
    _write_project_language(tmp_path, language=language, spec_language=spec_language)
    state = _install_fake_summary_caller(monkeypatch)
    summ = _make_llm_summarizer(tmp_path)
    out = summ([_make_summary_target()])
    assert out == {"foo.py::foo": "does foo"}
    assert state["prompts"], "expected a summary LLM call"
    return state["prompts"][0]


def test_code_index_prompt_injects_spec_language_when_set(tmp_path, monkeypatch):
    prompt = _code_index_prompt(tmp_path, monkeypatch, spec_language="zh-CN")
    assert "MUST respond in zh-CN" in prompt
    assert "Preserve all technical symbols verbatim" in prompt
    assert "knowledge asset" in prompt
    assert "authoritative" in prompt
    assert "spec file" not in prompt
    assert "SHALL/MUST" not in prompt


def test_code_index_prompt_zero_injection_when_spec_language_unset(tmp_path, monkeypatch):
    prompt = _code_index_prompt(tmp_path, monkeypatch)
    assert "MUST respond in" not in prompt
    assert "Preserve all technical symbols verbatim" not in prompt


def test_code_index_prompt_ignores_human_language_when_spec_language_unset(tmp_path, monkeypatch):
    prompt = _code_index_prompt(tmp_path, monkeypatch, language="zh-CN")
    assert "MUST respond in" not in prompt
    assert "zh-CN" not in prompt


def test_code_index_summary_stays_single_line_with_injection(tmp_path, monkeypatch):
    # The injection appends to the PROMPT, never the returned summary, so the
    # _flatten_summary single-line md contract is untouched: the stub returns a
    # summary and the summarizer surfaces it verbatim (single line) regardless of
    # spec_language being set.
    _write_project_language(tmp_path, spec_language="zh-CN")
    state = _install_fake_summary_caller(monkeypatch)
    summ = _make_llm_summarizer(tmp_path)
    out = summ([_make_summary_target()])
    assert list(out.values()) == ["does foo"]
    assert "\n" not in out["foo.py::foo"]
    assert state["prompts"]
