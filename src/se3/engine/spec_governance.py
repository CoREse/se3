"""Single authoritative source for se3's spec **volume / content governance**.

This module is the prompt-and-tooling source of truth for the rules that keep
spec files from bloating the LLM context: what the ``base`` spec is allowed to
carry, the per-Requirement / per-spec writing discipline, the criteria for
splitting an over-sized spec, and the markers used to classify specs above the
spec level (the ``<!-- domain: ... -->`` header metadata).

It carries **only normative text and small constants** — no thresholds (those
live in :class:`se3.config.SpecGovernanceConfig`, which is the configurable
numeric counterpart) and no behaviour. ``update_spec`` / ``se3 sync`` prompts
inject these constants so every LLM sub-process shares one description of the
governance rules, mirroring the way :mod:`se3.engine.spec_role` fixes the
code-first role wording.

This module has **no import-time side effects** and depends only on the
standard library, so prompt modules and tests can import it freely.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# domain header metadata
# ---------------------------------------------------------------------------
# Each spec MAY declare a layered-path classification in a header HTML comment,
# placed alongside the existing ``<!-- spec-format: v1 -->`` marker, e.g.::
#
#     <!-- domain: engine/steps -->
#
# The domain is a hierarchical path (slash-separated). It is the source of the
# "spec above the spec" grouping the index renderer uses: when the root view
# exceeds its size threshold the renderer groups specs by their domain path,
# descending one path level at a time. A spec with no domain marker renders
# under the :data:`UNCLASSIFIED_GROUP` bucket.
DOMAIN_MARKER_PREFIX = "<!-- domain:"
DOMAIN_MARKER_SUFFIX = "-->"

# Group name used when a spec declares no ``<!-- domain: ... -->`` marker.
UNCLASSIFIED_GROUP = "(未分类)"


# ---------------------------------------------------------------------------
# base admission standard
# ---------------------------------------------------------------------------
# What the ``base`` spec is allowed to contain. base is the only spec that is
# injected, in full, into every LLM step unconditionally, so its size is a
# fixed per-call cost. It must stay a small top-level index, not a dumping
# ground for module detail.
BASE_ADMISSION_STANDARD = """\
## base Spec Admission Standard

The `base` spec is the ONLY spec injected — in full — into every step of every
session. Its size is therefore a fixed cost paid on every single LLM call, and
neither on-demand loading nor index drill-down can reduce it. Keep it small and
top-level.

`base` MAY carry ONLY content that every session genuinely needs loaded in full:

- Project identity / positioning (what this project is, its primary language /
  framework).
- The global architecture picture (the top-level directory map, how the major
  pieces fit together).
- Cross-cutting conventions that apply project-wide (coding conventions, key
  constraints, workflow conventions).
- A one-line locator index of each module / spec (the name plus a single
  sentence pointing at where its detail lives).

`base` MUST NOT carry module-specific detail. Anything that belongs to one
subsystem — its submodule list, its internal mechanics, its per-step behaviour —
belongs in that module's own spec, reachable on demand via `se3 spec index` /
`se3 spec show`, NOT in `base`. When a write would push `base` over its
configured size limit, the new content MUST be routed into the corresponding
module spec rather than appended to `base`."""


# ---------------------------------------------------------------------------
# writing discipline
# ---------------------------------------------------------------------------
# The per-Requirement / per-spec authoring rules (a)-(d) that make the
# program-derived index views (summary, locator, domain grouping) carry
# navigational quality for free, because every layer's semantics already exist
# in the authoritative store at write time.
WRITING_DISCIPLINE = """\
## Spec Writing Discipline

Each layer of the spec index is derived mechanically (no LLM) from text that
already exists in the spec at write time. Follow these rules so that derived
text is navigable:

(a) Each Requirement's body SHALL open its first paragraph with a one-sentence,
    summary-level overview. The index truncates that opening paragraph (first
    ~200 chars) into the item's entry summary, so a leading throwaway sentence
    yields a useless summary.
(b) Each spec's `## Purpose` section SHALL open with a one-sentence locator that
    states, in a single line, what the spec is about. The root index view shows
    each spec's name plus this one-sentence locator.
(c) Each spec SHALL declare a `<!-- domain: <layered/path> -->` header marker
    alongside the `<!-- spec-format: v1 -->` marker. The domain path is how the
    root view groups specs when it grows past its size threshold.
(d) Section organisation SHALL keep the number of items under any single `###`
    section moderate; avoid hanging an excessive number of Requirements off one
    section heading. (The renderer paginates over-large groups as a fallback, so
    this is guidance, not a hard checked rule.)"""


# ---------------------------------------------------------------------------
# split criteria
# ---------------------------------------------------------------------------
# How to decide whether an over-sized spec should be split into parallel specs
# vs left alone — "cohesion first, size second" — plus the responsibility
# split between update_spec (advisory only) and se3 sync (the only actor that
# may actually split).
SPLIT_CRITERIA = """\
## Spec Split Criteria (cohesion first, size second)

A spec growing past its size warning threshold is a *signal to evaluate*, not an
order to split. Apply cohesion before size:

- If the over-sized spec is multi-topic (its items cluster into groups with
  sparse cross-cluster references), it SHOULD be split into parallel specs.
- If the spec is internally cohesive and merely long, do NOT force a split. Its
  byte size does not pressure the LLM context under the size-bounded index, and
  it should be slimmed gradually via the per-Requirement discipline instead.

Responsibility split:

- Splitting into parallel specs is a semantic-level refactor (it produces a new
  spec name and must update logical addresses, cross-spec refs, the index, and
  domain metadata). It is performed ONLY by `se3 sync`, with the split plan
  confirmed by the user through sync's respond channel.
- `update_spec` MUST NOT create a parallel spec on its own. When it judges that
  a spec should be split, it only records the recommendation in its output and
  leaves the split to `se3 sync`."""
