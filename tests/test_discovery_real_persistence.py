"""Real-engine verification that discovery persists a marker-wrapped user body.

The template-level / ``record_prompt`` round-trip is already covered by
``test_discovery_prompt_markers.py``. Those tests prove the prompt *strings*
are assembled and recorded with the three-segment markers, but they bypass the
actual step handler — they either mock ``LLMCaller`` wholesale or call
``record_prompt`` directly with a hand-rendered prompt.

This module closes that last gap for G4's "用真实 discovery 流程核验" criterion:
it drives the **real** :func:`tianluo.engine.steps.discovery.discovery_handler`
end-to-end (real ``LLMCaller.call`` → ``_call_two_phase`` → ``_call_with_retry``
→ ``_record_prompt`` → ``record_prompt``), stubbing only the subprocess
boundary (``LLMCaller._get_current_runner``) so no real ``claude`` process is
launched. It then reads back the per-step jsonl the daemon history reader and
the frontend ``splitUserPromptByMarker`` actually consume, and asserts the
persisted ``user`` record carries the full three-segment marker sequence with
the user's literal ``initial_description`` bounded strictly inside — i.e. the
real engine (not a test fixture) is the thing that persisted the marker-wrapped
body.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.prompt_markers import (
    TEMPLATE_PREFIX_END,
    USER_CONTENT_BEGIN,
    USER_CONTENT_END,
)
from tianluo.engine.steps.discovery import discovery_handler


# A user-typed initial description with non-ASCII prose and an embedded session
# reference, so we know unusual characters survive the marker boundary intact.
_INITIAL_DESCRIPTION = (
    "请帮我把 web running-flow console 的聊天渲染修好，"
    "参考 tianluo/history/20260520-142159_30166ecb 这个 session。"
)


class _FakeResult:
    """A stand-in for the agent runner's ``run_with_monitor`` return value.

    Only the attributes the success path of ``LLMCaller._call_with_retry``
    reads are populated.
    """

    def __init__(self, output: str) -> None:
        self.success = True
        self.output = output
        self.interrupted = False
        self.returncode = 0
        self.cmd_used = "fake-claude"


class _FakeRunner:
    """A fake :class:`AgentRunner` that never launches a subprocess."""

    def __init__(self, output: str) -> None:
        self._output = output

    def run_with_monitor(self, **kwargs):  # noqa: D401 - signature mirrors ABC
        return _FakeResult(self._output)

    def detect_infra_error(self, *args, **kwargs):
        from tianluo.agent_runner import InfraErrorType

        return InfraErrorType.NONE

    def build_call_args(self, prompt, read_only, context_files=None):
        return ["--output-format", "stream-json", "--verbose", "-p", prompt]


def _discovery_ndjson(result_obj: dict) -> str:
    """Build a Claude stream-json NDJSON output carrying *result_obj* as the
    assistant's text block (the shape ``_extract_text_from_ndjson`` parses)."""
    payload = json.dumps(result_obj, ensure_ascii=False)
    lines = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": payload}]},
        },
        {"type": "result", "subtype": "success", "result": payload},
    ]
    return "\n".join(json.dumps(line, ensure_ascii=False) for line in lines)


def _run_initial_discovery(tmp_path: Path, flow_id: str, step_id: str):
    """Drive the real discovery handler for an initial (round 0) turn.

    Returns the project root the handler wrote history under.
    """
    # project_root resolves to ``flow.change_path.parent`` inside the handler.
    project_root = tmp_path
    flow = FlowInstance(
        flow_id=flow_id,
        task_description=_INITIAL_DESCRIPTION,
        task_type="discovery",
        change_path=project_root / "tianluo",
    )
    step = Step(
        step_type=StepType.DISCOVERY,
        step_id=step_id,
        inputs={"task_description": _INITIAL_DESCRIPTION},
    )

    ndjson = _discovery_ndjson(
        {
            "mode": "question",
            "content": "我先确认几个问题。",
            "questions": ["要先跑哪个端到端测试？"],
            "thinking": "need scope",
        }
    )
    fake_runner = _FakeRunner(ndjson)

    # Stub ONLY the subprocess boundary; everything above it is the real engine.
    with patch(
        "tianluo.engine.llm_caller.LLMCaller._get_current_runner",
        return_value=fake_runner,
    ):
        result = discovery_handler(step, flow)

    # The handler should have paused awaiting the next user turn.
    assert result == StepStatus.PAUSED
    return project_root


def _read_user_record(project_root: Path, flow_id: str, step_id: str) -> dict:
    """Return the single persisted ``user`` record from the step jsonl."""
    path = project_root / "tianluo" / "history" / flow_id / f"{step_id}.jsonl"
    assert path.exists(), f"history jsonl not written at {path}"
    user_records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("role") == "user":
            user_records.append(rec)
    assert len(user_records) == 1, (
        f"expected exactly one persisted user prompt, got {len(user_records)}"
    )
    return user_records[0]


def _user_segment(body: str) -> str:
    """Return the substring strictly between USER_CONTENT_BEGIN/END markers."""
    assert body.count(TEMPLATE_PREFIX_END) == 1
    assert body.count(USER_CONTENT_BEGIN) == 1
    assert body.count(USER_CONTENT_END) == 1
    i = body.index(TEMPLATE_PREFIX_END)
    j = body.index(USER_CONTENT_BEGIN)
    k = body.index(USER_CONTENT_END)
    assert i < j < k, f"marker order wrong: {i}, {j}, {k}"
    return body[j + len(USER_CONTENT_BEGIN):k]


def test_real_discovery_persists_three_segment_markers(tmp_path):
    """The real discovery handler persists a user record with all three markers."""
    flow_id = "20260525-000001_realdisc"
    step_id = "00_discovery_real"
    project_root = _run_initial_discovery(tmp_path, flow_id, step_id)

    rec = _read_user_record(project_root, flow_id, step_id)
    body = rec["content"]

    # All three markers present, in canonical order — the contract
    # splitUserPromptByMarker depends on to produce a user bubble.
    assert TEMPLATE_PREFIX_END in body
    assert USER_CONTENT_BEGIN in body
    assert USER_CONTENT_END in body


def test_real_discovery_user_segment_is_literal_input_only(tmp_path):
    """The persisted user-content region equals the literal initial_description,
    with no framework boilerplate leaking in (so the web user bubble is clean)."""
    flow_id = "20260525-000002_realdisc"
    step_id = "00_discovery_real"
    project_root = _run_initial_discovery(tmp_path, flow_id, step_id)

    rec = _read_user_record(project_root, flow_id, step_id)
    seg = _user_segment(rec["content"])

    assert seg.strip() == _INITIAL_DESCRIPTION.strip()
    # Framework-injected prose must live in prefix/suffix, never the user region.
    for forbidden in (
        "## Project Context",
        "## Available Specifications",
        "## Discovery Context",
        "Respond in JSON format",
        "Guidelines:",
        "You are an expert software engineering assistant",
        "READ-ONLY",
    ):
        assert forbidden not in seg, (
            f"framework substring {forbidden!r} leaked into the persisted "
            f"user-content region"
        )


def test_real_discovery_record_is_role_user(tmp_path):
    """The persisted prompt turn is role=user — the role the frontend keys its
    marker-aware split off of."""
    flow_id = "20260525-000003_realdisc"
    step_id = "00_discovery_real"
    project_root = _run_initial_discovery(tmp_path, flow_id, step_id)

    rec = _read_user_record(project_root, flow_id, step_id)
    assert rec["role"] == "user"
    # The framework boilerplate stays in the body (collapsed into the chip on
    # the frontend), proving the whole prompt — not just the user input — was
    # persisted verbatim.
    assert "You are an expert software engineering assistant" in rec["content"]
