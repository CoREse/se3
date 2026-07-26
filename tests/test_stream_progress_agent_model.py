"""Tests for agent/model attribution on the stream_progress channel (G1).

Covers three production-side behaviors:

1. ``chat_history.record_stream_progress`` writes the optional
   ``agent_name`` / ``model_name`` keys only when non-None, and stays
   byte-identical to the legacy schema when both are absent.
2. The shared model-name extraction helpers
   (``extract_model_name_from_obj`` / ``extract_model_name_from_ndjson``)
   parse init/system metadata and tolerate junk.
3. ``StreamJSONTracker`` labels every progress line with its agent from the
   first fragment, and upgrades subsequent lines to carry the actual model
   once an init/system line streams.
4. On agent rotation each attempt's stream_progress records carry that
   attempt's own real agent (no stale-name carryover).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.engine import chat_history
from tianluo.engine.chat_history import (
    extract_model_name_from_ndjson,
    extract_model_name_from_obj,
)
from tianluo.engine.llm_caller import LLMCaller, StreamJSONTracker


# ---------------------------------------------------------------------------
# Shared model-name extraction helpers
# ---------------------------------------------------------------------------


def test_extract_model_from_obj_top_level():
    assert (
        extract_model_name_from_obj({"type": "init", "model": "claude-opus-4-8"})
        == "claude-opus-4-8"
    )


def test_extract_model_from_obj_system_and_nested_session():
    assert (
        extract_model_name_from_obj({"type": "system", "model": "claude-haiku-4-5"})
        == "claude-haiku-4-5"
    )
    assert (
        extract_model_name_from_obj(
            {"type": "init", "session": {"model": "claude-sonnet-4-6"}}
        )
        == "claude-sonnet-4-6"
    )


def test_extract_model_from_obj_non_metadata_returns_none():
    # Wrong type, missing model, and non-dict all yield None without raising.
    assert extract_model_name_from_obj({"type": "assistant", "message": {}}) is None
    assert extract_model_name_from_obj({"type": "init"}) is None
    assert extract_model_name_from_obj("not a dict") is None
    assert extract_model_name_from_obj(None) is None


def test_extract_model_from_ndjson_multiline_and_single_line():
    stream = "\n".join(
        [
            '{"type": "init", "model": "claude-opus-4-8"}',
            '{"type": "assistant", "message": {"content": []}}',
        ]
    )
    assert extract_model_name_from_ndjson(stream) == "claude-opus-4-8"
    # Single line also works.
    assert (
        extract_model_name_from_ndjson('{"type": "system", "model": "m-1"}') == "m-1"
    )
    # Already-parsed list form.
    assert (
        extract_model_name_from_ndjson([{"type": "init", "model": "m-2"}]) == "m-2"
    )


def test_extract_model_from_ndjson_tolerates_garbage():
    assert extract_model_name_from_ndjson("") is None
    assert extract_model_name_from_ndjson("not json at all") is None
    assert extract_model_name_from_ndjson("=== Command: foo ===") is None


# ---------------------------------------------------------------------------
# record_stream_progress field semantics
# ---------------------------------------------------------------------------


_LEGACY_KEYS = {
    "type",
    "role",
    "step_type",
    "content",
    "raw_json",
    "timestamp",
    "attempt",
    "partial",
}


def _read_record(tmp_path, flow_id, step_id):
    path = tmp_path / "tianluo" / "history" / flow_id / f"{step_id}.jsonl"
    return json.loads(path.read_text(encoding="utf-8").strip())


def test_record_stream_progress_omits_agent_model_when_none(tmp_path):
    """All-None new fields → byte-identical legacy schema (no extra keys)."""
    chat_history.record_stream_progress(
        tmp_path,
        "flow-none",
        "01_discovery_abc12345",
        "discovery",
        "narrative chunk",
        None,
        attempt=0,
    )
    rec = _read_record(tmp_path, "flow-none", "01_discovery_abc12345")
    assert set(rec.keys()) == _LEGACY_KEYS


def test_record_stream_progress_writes_agent_only(tmp_path):
    """agent_name present but model_name still None → only agent_name added."""
    chat_history.record_stream_progress(
        tmp_path,
        "flow-agent",
        "01_implement_abc12345",
        "implement",
        "hello",
        None,
        attempt=0,
        agent_name="dclaude",
    )
    rec = _read_record(tmp_path, "flow-agent", "01_implement_abc12345")
    assert rec["agent_name"] == "dclaude"
    assert "model_name" not in rec


def test_record_stream_progress_writes_agent_and_model(tmp_path):
    chat_history.record_stream_progress(
        tmp_path,
        "flow-both",
        "01_implement_abc12345",
        "implement",
        "hello",
        None,
        attempt=0,
        agent_name="dclaude",
        model_name="claude-opus-4-8",
    )
    rec = _read_record(tmp_path, "flow-both", "01_implement_abc12345")
    assert rec["agent_name"] == "dclaude"
    assert rec["model_name"] == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# StreamJSONTracker — agent from first fragment, model upgrade mid-stream
# ---------------------------------------------------------------------------


def _make_capturing_tracker(monkeypatch, tmp_path, agent_name="dclaude"):
    captured = []

    def fake_record_stream_progress(
        project_root,
        flow_id,
        step_id,
        step_type,
        content,
        raw_obj,
        attempt,
        timestamp=None,
        **kwargs,
    ):
        captured.append({"content": content, **kwargs})

    monkeypatch.setattr(
        chat_history, "record_stream_progress", fake_record_stream_progress
    )
    tracker = StreamJSONTracker(
        project_root=tmp_path,
        flow_id="flow-t",
        step_id="step-t",
        step_type="implement",
        attempt=0,
        agent_name=agent_name,
    )
    return tracker, captured


def test_tracker_stores_agent_name():
    t = StreamJSONTracker(agent_name="kclaude")
    assert t._agent_name == "kclaude"
    assert t._model_name is None


def test_emit_agent_identity_seeds_agent_before_any_output(monkeypatch, tmp_path):
    """At attempt start the tracker emits an identity-only record carrying the
    agent (empty content) so the web console shows the real agent before any
    text/tool fragment — or even when the call returns only a final result."""
    tracker, captured = _make_capturing_tracker(monkeypatch, tmp_path)

    tracker.emit_agent_identity()

    assert len(captured) == 1, "exactly one identity record expected"
    rec = captured[0]
    assert rec["content"] == "", "identity record carries no visible content"
    assert rec["agent_name"] == "dclaude"
    assert "model_name" not in rec  # model not parsed yet


def test_emit_agent_identity_includes_model_when_known(monkeypatch, tmp_path):
    """If the model is already parsed, the identity seed carries agent · model."""
    tracker, captured = _make_capturing_tracker(monkeypatch, tmp_path)
    tracker._model_name = "claude-opus-4-8"

    tracker.emit_agent_identity()

    assert captured[-1]["agent_name"] == "dclaude"
    assert captured[-1]["model_name"] == "claude-opus-4-8"


def test_emit_agent_identity_noop_without_agent(monkeypatch, tmp_path):
    """No agent name → no identity record (legacy/ad-hoc callers unaffected)."""
    tracker, captured = _make_capturing_tracker(
        monkeypatch, tmp_path, agent_name=None
    )

    tracker.emit_agent_identity()

    assert captured == []


def test_emit_agent_identity_noop_without_flow_context():
    """No flow_id/step_id → progress disabled → no write attempted."""
    t = StreamJSONTracker(agent_name="dclaude")  # no flow context
    # Should simply not raise and not attempt any record write.
    t.emit_agent_identity()


def test_call_emits_identity_seed_before_result(monkeypatch, tmp_path):
    """The regular call() path emits an identity seed at attempt start, so even
    a call that streams no intermediate fragments still surfaces its agent."""
    captured = []

    def fake_record_stream_progress(
        project_root,
        flow_id,
        step_id,
        step_type,
        content,
        raw_obj,
        attempt,
        timestamp=None,
        **kwargs,
    ):
        captured.append({"content": content, **kwargs})

    monkeypatch.setattr(
        chat_history, "record_stream_progress", fake_record_stream_progress
    )

    caller = LLMCaller(
        project_root=tmp_path,
        max_retries=1,
        retry_delay=0,
        flow_id="flow-seed",
        step_id="01_implement_abc12345",
        step_type="implement",
        agents=[{"name": "agentA", "type": "claude-code", "cmd": "echo"}],
    )

    class _ResultOnlyRunner:
        """Streams nothing intermediate — returns only the final result line."""

        def build_call_args(self, prompt, read_only, context_files=None, spec_guard_plugin=None):
            return ["-p", prompt]

        def detect_infra_error(self, returncode, output, stderr_tail):
            from tianluo.agent_runner import InfraErrorType

            return InfraErrorType.NONE

        def run_with_monitor(self, args, on_output=None, **kwargs):
            # No on_output calls — the tracker sees no fragments at all.
            return _FakeResult(
                success=True,
                output='{"type": "result", "result": "done"}',
            )

    with patch.object(caller, "_get_current_runner", return_value=_ResultOnlyRunner()), \
         patch.object(LLMCaller, "_record_prompt"), \
         patch.object(LLMCaller, "_record_response"):
        caller.call("do the thing", json_mode="off")

    # The identity seed (empty content, agentA) must be present even though the
    # runner streamed no intermediate fragments.
    seeds = [c for c in captured if c.get("content") == "" and c.get("agent_name") == "agentA"]
    assert seeds, f"expected an identity seed record, got {captured}"


def test_tracker_progress_carries_agent_before_model(monkeypatch, tmp_path):
    """A tool_use that streams before any init/system line yields a progress
    record carrying agent_name but no model_name yet."""
    tracker, captured = _make_capturing_tracker(monkeypatch, tmp_path)

    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-1",
                            "name": "Read",
                            "input": {"file_path": "a.py"},
                        }
                    ]
                },
            }
        )
    )
    assert captured, "expected at least one progress record"
    first = captured[0]
    assert first["agent_name"] == "dclaude"
    assert "model_name" not in first  # not parsed yet


def test_tracker_upgrades_to_model_after_init_line(monkeypatch, tmp_path):
    """Once an init/system line streams, subsequent progress records carry the
    parsed model_name in addition to the agent_name."""
    tracker, captured = _make_capturing_tracker(monkeypatch, tmp_path)

    # First fragment: tool_use before the model is known.
    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-1",
                            "name": "Read",
                            "input": {"file_path": "a.py"},
                        }
                    ]
                },
            }
        )
    )
    # The model metadata arrives (init line).
    tracker.process_line(json.dumps({"type": "init", "model": "claude-opus-4-8"}))
    assert tracker._model_name == "claude-opus-4-8"

    # A later fragment now carries both agent and model.
    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-2",
                            "name": "Read",
                            "input": {"file_path": "b.py"},
                        }
                    ]
                },
            }
        )
    )

    # Earliest record had no model; the post-init record has it.
    assert "model_name" not in captured[0]
    assert captured[-1]["agent_name"] == "dclaude"
    assert captured[-1]["model_name"] == "claude-opus-4-8"


def test_tracker_init_line_first_all_records_carry_model(monkeypatch, tmp_path):
    """When the init line streams first (the real Claude Code ordering), every
    subsequent progress record carries agent · model from the start."""
    tracker, captured = _make_capturing_tracker(monkeypatch, tmp_path)

    tracker.process_line(json.dumps({"type": "init", "model": "claude-opus-4-8"}))
    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-1",
                            "name": "Read",
                            "input": {"file_path": "a.py"},
                        }
                    ]
                },
            }
        )
    )
    assert captured
    for rec in captured:
        assert rec["agent_name"] == "dclaude"
        assert rec["model_name"] == "claude-opus-4-8"


def test_init_line_emits_model_upgrade_without_further_fragments(monkeypatch, tmp_path):
    """The moment an init/system line reveals the model, the tracker MUST emit an
    identity-only progress record so the reply bubble's badge upgrades from
    "agent" to "agent · model" immediately — without waiting for the next
    text/tool fragment (which may pause indefinitely or never arrive for a
    result-only call)."""
    tracker, captured = _make_capturing_tracker(monkeypatch, tmp_path)

    # Only the init line streams; no subsequent text/tool fragment follows.
    tracker.process_line(json.dumps({"type": "init", "model": "claude-opus-4-8"}))

    # An upgrade record (empty content, agent · model) must already be present.
    upgrades = [
        c
        for c in captured
        if c.get("content") == ""
        and c.get("agent_name") == "dclaude"
        and c.get("model_name") == "claude-opus-4-8"
    ]
    assert upgrades, f"expected an immediate model-upgrade record, got {captured}"


# ---------------------------------------------------------------------------
# Agent rotation — each attempt's stream_progress carries its own agent
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, *, success, output="", returncode=0, cmd_used="cmd"):
        self.success = success
        self.output = output
        self.interrupted = False
        self.returncode = returncode
        self.cmd_used = cmd_used
        self.stderr_tail = ""


class _RotationRunner:
    """Fake runner that streams one init line + one tool_use then returns a
    pre-programmed success/failure, so the tracker writes one progress record
    per attempt."""

    def __init__(self, succeed):
        self._succeed = succeed

    def build_call_args(self, prompt, read_only, context_files=None, spec_guard_plugin=None):
        return ["-p", prompt]

    def detect_infra_error(self, returncode, output, stderr_tail):
        from tianluo.agent_runner import InfraErrorType

        return InfraErrorType.NONE

    def run_with_monitor(self, args, on_output=None, **kwargs):
        if on_output:
            on_output(json.dumps({"type": "init", "model": "claude-opus-4-8"}))
            on_output(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tu-x",
                                    "name": "Read",
                                    "input": {"file_path": "a.py"},
                                }
                            ]
                        },
                    }
                )
            )
        return _FakeResult(
            success=self._succeed,
            output='{"type": "init", "model": "claude-opus-4-8"}',
            returncode=0 if self._succeed else 1,
        )


def test_rotation_progress_records_carry_per_attempt_agent(monkeypatch, tmp_path):
    """First attempt on agent A fails → rotation to B. The progress record from
    each attempt must carry that attempt's own agent (A then B), not a stale
    name."""
    captured = []

    def fake_record_stream_progress(
        project_root,
        flow_id,
        step_id,
        step_type,
        content,
        raw_obj,
        attempt,
        timestamp=None,
        **kwargs,
    ):
        captured.append({"content": content, **kwargs})

    monkeypatch.setattr(
        chat_history, "record_stream_progress", fake_record_stream_progress
    )

    caller = LLMCaller(
        project_root=tmp_path,
        max_retries=2,
        retry_delay=0,
        flow_id="flow-rot",
        step_id="01_implement_abc12345",
        step_type="implement",
        agents=[
            {"name": "agentA", "type": "claude-code", "cmd": "echo"},
            {"name": "agentB", "type": "claude-code", "cmd": "echo"},
        ],
    )

    # Runner depends on the current agent index: A fails, B succeeds.
    runners = {0: _RotationRunner(succeed=False), 1: _RotationRunner(succeed=True)}

    def fake_get_current_runner():
        return runners[caller._current_agent_index]

    with patch.object(caller, "_get_current_runner", side_effect=fake_get_current_runner), \
         patch.object(LLMCaller, "_record_prompt"), \
         patch.object(LLMCaller, "_record_response"):
        caller.call("do the thing", json_mode="off")

    agent_names = [c.get("agent_name") for c in captured if c.get("agent_name")]
    assert "agentA" in agent_names, f"expected agentA in {agent_names}"
    assert "agentB" in agent_names, f"expected agentB in {agent_names}"
    # The first attempt's records belong to A, the post-rotation ones to B,
    # and no record carries the wrong agent for its attempt.
    assert agent_names[0] == "agentA"
    assert agent_names[-1] == "agentB"
