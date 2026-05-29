"""Anti-regression guardrail: prompt and documentation source must stay free of
the rejected *spec-driven* framing.

se3 is code-first (see ``se3.engine.spec_role``): a spec is the documented
snapshot of the code, never a driver the code must obey. Groups G2/G3 scrubbed
the spec-driven residuals out of the README mirrors, the step prompts, and the
templates. This test is the *precision guardrail* that keeps them out: it scans
the prompt and documentation source files for the curated
``SPEC_DRIVEN_FRAMING_PHRASES`` set and fails — pinpointing ``file:line`` — if
any reappears, so a regression is blocked at commit/CI time.

Design notes (read before extending):

- The phrase set is owned by ``se3.engine.spec_role`` and imported here, so the
  guardrail and the prompts share a *single source* for what counts as a
  residual. We never re-list phrases locally.
- The set deliberately excludes generic / border-line tokens (``contract``,
  ``source of truth``, ``two-way governance``) that have legitimate compliant
  uses across the repo (wire-protocol contracts, ``pyproject.toml`` as the
  single source of truth, the asymmetric within-flow governance model). This
  test pins that exclusion down with an explicit false-positive guard so the
  guardrail cannot be quietly weakened into uselessness.
- Mirrors the source-scanning style of ``tests/test_discovery_prompt_markers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.spec_role import (
    SPEC_DRIVEN_FRAMING_PHRASES,
    SPEC_ROLE_DEFINITION,
    find_spec_driven_framing,
)

# Repo root = parent of the tests/ directory holding this file.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _scanned_source_files() -> list[Path]:
    """Enumerate the prompt and documentation source files in scan scope.

    Scope (per G5 task): step prompts, the README mirrors, ``docs/`` markdown,
    the project-init markdown templates, and the runtime-environment injection
    markdown. The authoritative-definition module ``spec_role.py`` is
    intentionally NOT scanned — it is the single place that legitimately stores
    the curated phrases (the rejected framing it scans *for*), so scanning it
    would be a guaranteed self-match.
    """
    files: list[Path] = []
    files += sorted((_REPO_ROOT / "src/se3/engine/steps").glob("*.py"))
    files += [_REPO_ROOT / "README.md", _REPO_ROOT / "README.zh.md"]
    files += sorted((_REPO_ROOT / "docs").glob("*.md"))
    files += sorted((_REPO_ROOT / "src/se3/templates").glob("*.md"))
    files += [_REPO_ROOT / "src/se3/engine/runtime_environment.md"]
    # Keep only files that actually exist; glob already returns existing paths,
    # the explicit additions are stable repo fixtures but we stay defensive.
    return [f for f in files if f.is_file()]


def test_scan_scope_is_non_empty():
    """Guard against a silently empty scan (e.g. a moved directory) that would
    make the residual check vacuously pass."""
    files = _scanned_source_files()
    assert files, "no source files were found to scan — scope resolution broke"
    # Sanity: the README mirrors and the step package must always be present.
    names = {f.name for f in files}
    assert "README.md" in names
    assert "README.zh.md" in names
    assert "discovery.py" in names
    assert "runtime_environment.md" in names


def test_no_spec_driven_framing_in_prompt_and_doc_source():
    """No curated spec-driven framing phrase may appear in any scanned source.

    On failure the assertion message reports every offending ``file:line`` with
    the matched phrase(s), so a regression is immediately localized.
    """
    violations: list[str] = []
    for path in _scanned_source_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            matched = find_spec_driven_framing(line)
            if matched:
                rel = path.relative_to(_REPO_ROOT)
                violations.append(
                    f"{rel}:{lineno}: {matched} :: {line.strip()[:160]}"
                )

    assert not violations, (
        "spec-driven framing residual(s) detected in prompt/doc source — "
        "rewrite to the code-first / spec-assistant framing (see "
        "se3.engine.spec_role.SPEC_ROLE_DEFINITION):\n"
        + "\n".join(violations)
    )


def test_spec_role_definition_is_present_and_normative():
    """The authoritative role definition must exist and carry the code-first /
    spec-assistant normative wording the prompts mirror."""
    assert isinstance(SPEC_ROLE_DEFINITION, str)
    assert SPEC_ROLE_DEFINITION.strip(), "SPEC_ROLE_DEFINITION must be non-empty"
    lowered = SPEC_ROLE_DEFINITION.lower()
    # The defining vocabulary of the code-first stance.
    assert "code-first" in lowered
    assert "spec-assistant" in lowered
    # The asymmetric governance model must be stated, not just claimed.
    assert "code → spec" in SPEC_ROLE_DEFINITION
    assert "spec → code" in SPEC_ROLE_DEFINITION
    assert "within-flow" in lowered
    # And the authoritative definition itself must obey the guardrail.
    assert find_spec_driven_framing(SPEC_ROLE_DEFINITION) == []


@pytest.mark.parametrize(
    "compliant_text",
    [
        # README:46 — the canonical compliant within-flow governance line. It
        # uses "two-way governance" and "implementation contract" legitimately
        # and MUST NOT be flagged.
        (
            "Spec ↔ code two-way governance (asymmetric): code → spec is "
            "primary; spec → code is only a bounded, within-flow drift guard; "
            "the spec is the implementation contract for the duration of a flow."
        ),
        # Generic "source of truth" uses elsewhere in the repo.
        "pyproject.toml — Single source of truth for project version",
        "The daemon↔server wire protocol has a single source of truth.",
        # A bare wire-protocol / parser "contract" mention.
        "the version-script output contract must not change without callers.",
    ],
)
def test_compliant_borderline_text_is_not_flagged(compliant_text):
    """The curated set must not false-positive on the compliant border-line
    uses of ``contract`` / ``source of truth`` / ``two-way governance``."""
    assert find_spec_driven_framing(compliant_text) == []


def test_curated_set_excludes_generic_borderline_tokens():
    """The guardrail's precision depends on the curated set never absorbing the
    generic tokens that have compliant uses. Pin that exclusion so the guardrail
    cannot be weakened into a blunt grep-replacer."""
    forbidden_tokens = ("contract", "source of truth", "two-way governance")
    for phrase in SPEC_DRIVEN_FRAMING_PHRASES:
        for token in forbidden_tokens:
            assert token not in phrase, (
                f"curated phrase {phrase!r} contains generic token {token!r}; "
                "this would produce false positives on compliant text — see "
                "se3.engine.spec_role curation policy"
            )
