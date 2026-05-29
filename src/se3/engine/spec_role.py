"""Single authoritative definition of the spec's role in se3.

se3 is **code-first**: the project source code is primary, and a spec is the
*documented snapshot / reference* of that code (a spec-assistant view), never a
driver the code must be made to obey. This module is the one place that fixes
the normative wording so prompts and docs reference it instead of each
improvising their own framing.

Two constants are exported:

- :data:`SPEC_ROLE_DEFINITION` — the human/LLM-readable normative statement of
  the code-first / spec-assistant role, including the asymmetric code↔spec
  governance model. Step prompts inject this so every LLM sub-process shares
  one description of what a spec is for.
- :data:`SPEC_DRIVEN_FRAMING_PHRASES` — a *curated* set of phrases that express
  the rejected "spec drives / overrides the code" framing. The anti-regression
  guardrail test scans prompt and documentation source for these phrases.

This module has **no import-time side effects** and depends only on the stdlib,
so both prompt modules and tests can import it freely.

Curation policy for :data:`SPEC_DRIVEN_FRAMING_PHRASES` (read before editing):
the set records ONLY phrases that unambiguously frame the spec as the thing
that drives or overrides the code. It deliberately excludes generic /
border-line terms that have legitimate, compliant uses elsewhere in the repo,
so the guardrail does not produce false positives that would force-rewrite
correct text (or be quietly suppressed, defeating the guardrail). In
particular it MUST NOT contain:

- ``contract`` — used legitimately for wire-protocol contracts, parser
  contracts, the version-script output contract, and the *within-flow*
  temporary contract (see below).
- ``source of truth`` — used legitimately for "single source of truth:
  pyproject.toml", "the markdown body is the single source of truth", etc.
- ``two-way governance`` / ``Spec ↔ code two-way governance`` — describes the
  compliant asymmetric governance model, not a spec-driven framing.

These exclusions are load-bearing: any phrase added here must be specific
enough that every occurrence in the repo is a genuine spec-driven residual to
fix, not compliant text.
"""

from __future__ import annotations

# The normative role statement injected into LLM sub-process prompts and
# mirrored (for human readers) by README. Keep this the single wording source;
# do not re-paraphrase the spec's role inline in individual step prompts.
SPEC_ROLE_DEFINITION = """\
## The Role of Specs in se3 (code-first)

se3 is code-first. The project's source code is primary and authoritative;
a spec under `se3/specs/**/spec.md` is the *documented snapshot* of what the
code currently does — a reference for humans and agents, not a design the code
must be bent to satisfy. Specs are a spec-assistant, not a spec-driver.

Code↔spec governance is intentionally asymmetric:

- **code → spec is the primary direction.** `se3 sync` regenerates the spec
  from the current code so the spec keeps reflecting reality. When the spec and
  the code disagree, the code wins and the spec is updated — never the reverse.
- **spec → code is only a bounded, within-flow drift guard.** For the duration
  of a single flow, `se3 guardrails` treats the already-recorded SHALL/MUST
  requirements as the implementation contract *for that flow*, so an
  in-progress implementation cannot silently weaken or delete requirements
  mid-flow. This is drift prevention scoped to one flow; it does NOT make the
  spec authoritative over the code in general.

Consequences for how you work:

- Treat any "Available Specifications" you are given as a read-only reference to
  the code's current state, not as architectural contracts you must align the
  code to.
- Future intent and desired changes enter through issues (`se3 issue`), not by
  rewriting specs to describe a not-yet-built future.
- Do not proactively propose creating or rewriting spec files. Recording code
  into specs is the job of the `update_spec` step and `se3 sync`, not of
  analysis/discovery work."""


# Curated phrases expressing the rejected spec-driven / spec-overrides-code
# framing. Stored lower-cased; consumers MUST match case-insensitively (lower
# the haystack) so capitalization variants are caught. See the module docstring
# for the curation policy and the terms that are deliberately excluded.
SPEC_DRIVEN_FRAMING_PHRASES: tuple[str, ...] = (
    # Direct "spec-driven" labels (hyphen and space variants).
    "spec-driven",
    "spec driven",
    "specs-driven",
    "specs driven",
    "规范驱动",
    # "specs drive / drive the code / drive the agent" verb framing.
    "specs drive",
    "spec drives",
    "spec 驱动",
    "驱动 ai agent",
    "drive an ai coding agent",
    "that drive an ai coding agent",
    "driving an ai coding agent",
    # "driven by the spec(s)" passive framing.
    "driven by the spec",
    "driven by specs",
    "spec-driven development",
    # Spec presented as the authority the implementation must conform to.
    # (Phrased without the generic "contract" / "source of truth" tokens that
    # have compliant uses — see module docstring.)
    "spec as the source-of-truth",
    "spec is the source-of-truth",
    "must align with the spec",
    "must align with spec",
    "must conform to the spec",
    "must obey the spec",
    "spec overrides the code",
    "spec takes precedence over the code",
)


def find_spec_driven_framing(text: str) -> list[str]:
    """Return the curated framing phrases that occur in *text*.

    Matching is case-insensitive. This is the single matching helper the
    anti-regression guardrail test and any prompt-linting code should use, so
    the scan semantics do not drift from the curated set above.

    Returns the matched phrases in their canonical (lower-cased) form, in the
    order they appear in :data:`SPEC_DRIVEN_FRAMING_PHRASES`, with no
    duplicates.
    """
    haystack = text.lower()
    return [phrase for phrase in SPEC_DRIVEN_FRAMING_PHRASES if phrase in haystack]
