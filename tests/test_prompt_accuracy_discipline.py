"""Guards for the two lightweight convention texts shipped with this change.

1. The discovery prompts' ACCURACY DISCIPLINE section ("coarse is fine, wrong
   is not"). The two discovery prompt suffixes are large near-duplicates, so
   the real hazard is a *one-sided* edit: someone tweaks the rule in the
   initial prompt and the continue prompt silently keeps the old wording. The
   tests below assert the section is present in both **and byte-identical**,
   so drift fails the suite rather than quietly degrading half the sessions.

2. The parallel-safety test convention scaffolded into every freshly generated
   charter, on both generation paths (`luo init` and `luo migrate`, including
   the migrate LLM-failure fallback).
"""

from __future__ import annotations

from pathlib import Path

from tianluo.engine import charter as charter_mod
from tianluo.engine.steps.discovery import (
    CONTINUE_DISCOVERY_PROMPT,
    INITIAL_DISCOVERY_PROMPT,
)

#: The section heading a reader (and this test) uses to find the rules.
_HEADING = "ACCURACY DISCIPLINE"

_PROMPTS = {
    "initial": INITIAL_DISCOVERY_PROMPT,
    "continue": CONTINUE_DISCOVERY_PROMPT,
}


def _extract_section(prompt: str) -> str:
    """Return the ACCURACY DISCIPLINE section: heading line + its bullet list.

    The section ends at the first blank line after the heading, which is how
    every other titled section in these templates is delimited.
    """
    assert prompt.count(_HEADING) == 1, (
        f"expected exactly one {_HEADING!r} section, found {prompt.count(_HEADING)}"
    )
    start = prompt.index(_HEADING)
    end = prompt.index("\n\n", start)
    return prompt[start:end]


def test_both_discovery_prompts_carry_the_accuracy_section():
    for name, prompt in _PROMPTS.items():
        assert _HEADING in prompt, f"{name} discovery prompt lost the {_HEADING} section"


def test_accuracy_section_is_identical_in_both_prompts():
    initial = _extract_section(INITIAL_DISCOVERY_PROMPT)
    continued = _extract_section(CONTINUE_DISCOVERY_PROMPT)
    assert initial == continued, (
        "the ACCURACY DISCIPLINE section drifted between the initial and "
        "continue discovery prompts; both must be spliced from the single "
        "shared constant"
    )


def test_accuracy_section_states_all_four_rules():
    """Semantic spot-check: each of the four rules must be recognisably present.

    Wording is the implementation's call, so this asserts on the load-bearing
    tokens of each rule rather than on whole sentences.
    """
    section = _extract_section(INITIAL_DISCOVERY_PROMPT)
    lowered = section.lower()

    # (1) every factual assertion must be verified against source this session
    assert "refined_description" in section
    assert "verified directly against the source" in lowered
    assert "this discovery session" in lowered
    # ... and not restated from history / a summary without re-verification
    assert "conversation history" in lowered
    assert "re-verified" in lowered

    # (2) unverifiable material becomes a requirement on the implementation
    assert "behavioural requirement on the implementation" in lowered
    assert "must ensure" in lowered and "must verify" in lowered

    # (3) stable references preferred over line numbers / file enumerations
    assert "function names, configuration keys, glob patterns" in lowered
    assert "line numbers" in lowered

    # (4) correctness beats detail
    assert "cut the detail and keep it correct" in lowered


def test_accuracy_section_sits_next_to_the_hard_invariant():
    """The rules only bind if they are read alongside the other hard rules on
    `refined_description`, so they must stay adjacent to the HARD INVARIANT
    block rather than drifting to the tail of the template."""
    for name, prompt in _PROMPTS.items():
        hard = prompt.index("HARD INVARIANT")
        accuracy = prompt.index(_HEADING)
        assert hard < accuracy, f"{name}: ACCURACY DISCIPLINE must follow HARD INVARIANT"
        between = prompt[hard:accuracy]
        assert between.count("\n\n") <= 1, (
            f"{name}: ACCURACY DISCIPLINE drifted away from the HARD INVARIANT "
            "section (another section slipped in between)"
        )


# ---------------------------------------------------------------------------
# charter default convention: parallel-safe tests
# ---------------------------------------------------------------------------


def test_parallel_safety_convention_covers_the_three_required_points():
    text = charter_mod.DEFAULT_PARALLEL_SAFE_TESTS_CONVENTION
    assert "并行安全" in text
    assert "执行顺序" in text
    assert "全局状态" in text
    assert "唯一路径" in text


def test_init_scaffolded_charter_carries_the_convention():
    from tianluo.commands.init_cmd import _get_charter_template

    rendered = _get_charter_template("demo-project")
    assert charter_mod.DEFAULT_PARALLEL_SAFE_TESTS_CONVENTION in rendered


def test_migrate_fallback_charter_carries_the_convention():
    """Even the degraded (LLM-failed) migrate path must keep the convention —
    otherwise a flaky call is enough to lose it."""
    from tianluo.commands.migrate_cmd import SalvageInput, _fallback_charter

    inp = SalvageInput(
        project_root=Path("/nonexistent"),
        base_spec_text="# old base spec\n",
        non_base_specs={},
        admission_standard=charter_mod.CHARTER_ADMISSION_STANDARD,
        charter_template=charter_mod.load_charter_template(),
        project_name="demo-project",
    )
    assert charter_mod.DEFAULT_PARALLEL_SAFE_TESTS_CONVENTION in _fallback_charter(inp)
