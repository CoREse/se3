"""Pytest bridge for the implement card's fix-round "This Round" block (G3).

``state_machine._transition_to_fix`` re-uses the SAME implement ``Step`` object
for every fix iteration, so the step's jsonl accumulates one ``step_completed``
record per round — and every number those cards showed was cumulative by
construction (``files_changed`` is re-derived from a flow-baseline git diff by
``implement._resolve_files_changed`` after each round, ``token_usage`` is
published as the carried total). A reader of round 3's card therefore had no way
to see what round 3 actually did.

The behavioural assertions live in
``tests/frontend/implement_fix_round.test.mjs``, which the Node assertion
harness ``tests/frontend/test_app_pure.mjs`` loads and runs. This module pulls
that suite into the pytest run, asserts the checks actually executed, and adds
static guardrails on the shipped assets (the record path carrying ``inputs``,
the renderer's context parameter, and i18n key parity across both bundles).

The Node suite is skipped when ``node`` is not on PATH; it is still runnable by
hand via ``node tests/frontend/test_app_pure.mjs``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "src" / "tianluo" / "server" / "static"
APP_JS = STATIC_DIR / "app.js"
EN_JSON = STATIC_DIR / "i18n" / "en-US.json"
ZH_JSON = STATIC_DIR / "i18n" / "zh-CN.json"
HARNESS = REPO_ROOT / "tests" / "frontend" / "test_app_pure.mjs"
FIX_ROUND_TEST = REPO_ROOT / "tests" / "frontend" / "implement_fix_round.test.mjs"

# Every user-visible string the block renders goes through i18n, so a key added
# to one bundle and forgotten in the other must fail here rather than silently
# painting English into a zh-CN console.
NEW_I18N_KEYS = [
    "stepReport.implement.thisRound",
    "stepReport.implement.cumulative",
    "stepReport.implement.roundFiles",
    "stepReport.implement.roundNoNewFiles",
    "stepReport.implement.roundFilesUnknown",
    "stepReport.usageCumulative",
]


def test_fix_round_module_present_and_registered_in_the_harness():
    assert FIX_ROUND_TEST.is_file(), f"missing {FIX_ROUND_TEST}"
    harness = HARNESS.read_text(encoding="utf-8")
    assert "implement_fix_round.test.mjs" in harness, (
        "implement_fix_round.test.mjs is not registered in test_app_pure.mjs"
    )
    assert "registerImplementFixRoundTests" in harness


def test_step_event_records_carry_the_step_inputs():
    """Without ``inputs`` on the normalized record there is no fix-round signal.

    The implement Step object is re-used across rounds, so ``fix_iteration`` /
    ``is_fix_iteration`` on the snapshot is the ONLY thing distinguishing a fix
    round's card from round one's.
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert "inputs: (innerStep && innerStep.inputs)" in src, (
        "normalizeRecord's step-event branch must carry the snapshot inputs"
    )
    assert "inputs: norm.stepReport.inputs || {}," in src, (
        "renderStepEventRecord must pass inputs into renderStepReport"
    )
    assert "{ priorFilesChanged: norm.priorFilesChanged }" in src, (
        "renderStepEventRecord must pass the predecessor's files as context"
    )


def test_renderers_receive_the_record_context():
    src = APP_JS.read_text(encoding="utf-8")
    assert "function renderStepReport(step, context)" in src
    assert "renderer(step, step.outputs || {}, context || null)" in src
    assert "function renderImplementReport(step, outputs, context)" in src
    assert "const priorFilesChanged = accumulatePriorFilesChangedByStep(records);" in src, (
        "the render loop must compute predecessors over the FULL ordered array"
    )


def test_new_i18n_keys_exist_in_both_web_locale_bundles():
    en = json.loads(EN_JSON.read_text(encoding="utf-8"))
    zh = json.loads(ZH_JSON.read_text(encoding="utf-8"))
    for key in NEW_I18N_KEYS:
        assert key in en, f"{key} missing from en-US.json"
        assert key in zh, f"{key} missing from zh-CN.json"
    # en-US stays the baseline superset (the per-key fallback chain depends on it).
    assert [k for k in zh if k not in en] == []


def test_frontend_implement_fix_round_node_suite_passes():
    """Run the Node assertion suite and confirm the G3 checks actually ran."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    assert HARNESS.is_file(), f"missing {HARNESS}"
    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"frontend test runner exited {result.returncode}:\n{combined}"
    )
    for needle in (
        "fix iteration: a non-implement step is never a fix round",
        "round files: the engine's own fix_round_files_changed wins over the diff",
        "round files: without the new key, the predecessor's list is differenced out",
        "round files: an EMPTY difference is a real answer, not a missing one",
        "predecessor: each round sees the PREVIOUS round's cumulative list",
        "predecessor: a re-delivered record resolves to the SAME predecessor, not itself",
        "round one renders NO 'This Round' block and NO cumulative marker",
        "a fix round renders the block ABOVE the cumulative body, with this round's summary",
        "a fix round's status bar is labelled cumulative, with the iteration count",
        "old history without the new key falls back to the set difference",
        "an empty difference says so instead of showing an empty list",
        "usage footnote: a fix round's card labels the carried total as cumulative",
        "usage footnote: round one's footnote is left exactly as it was",
        "record path: a fix-round record renders the block with its diffed files",
    ):
        assert needle in combined, (
            f"expected check {needle!r} in node output:\n{combined}"
        )
    assert "checks passed" in combined, combined
