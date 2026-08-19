"""Charter subsystem: load, render, and altitude-gate the project charter.

The **charter** (`tianluo/charter.md`) is the shrunk rename of the retired base
spec. It plays exactly one runtime role: it is injected **in full, into every
`luo run` step, unconditionally**, and so doubles as the conventions channel
for sandboxed LLM sub-processes (which cannot read CLAUDE.md and obtain
project-level conventions only through what luo injects).

Three capabilities live here:

- :func:`load_charter` — read ``tianluo/charter.md`` for whole-text injection.
- :func:`render_charter_template` — render the packaged ``charter.md`` template
  with project-init placeholder substitution (used by init / migrate).
- :func:`check_admission` — the **altitude gate**. The normative admission
  standard (:data:`CHARTER_ADMISSION_STANDARD`) is the LLM-facing text that
  guards against low-level content leaking into the charter; the byte threshold
  is a **monitoring light**, not a hard wall — over-threshold flags a review of
  whether low-level content has leaked in, it never blocks.
- :func:`build_admission_gate_prompt` — the LLM prompt builder for the
  ``charter_freshness`` auto-update closed loop. On that path the admission
  verdict is **gating** (a candidate charter is written only if admitted), and
  the prompt adds a removal-weakening question the plain standard omits.

This module has **no import-time side effects** and depends only on the
standard library, so prompt modules, the migrate command, and tests can import
it freely without pulling in the heavier config / engine stack.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

# ---------------------------------------------------------------------------
# physical location
# ---------------------------------------------------------------------------
#: Path of the charter file relative to the project root. The charter lives at
#: the top of the runtime directory (sibling of ``tianluo/code-index.md``), pulled
#: out of ``tianluo/specs/`` because the spec corpus is retired by this refactor.
CHARTER_RELATIVE_PATH = Path("tianluo") / "charter.md"
LEGACY_CHARTER_RELATIVE_PATH = Path("se3") / "charter.md"

#: Filename of the packaged charter template (mirrors templates.CHARTER_TEMPLATE).
CHARTER_TEMPLATE_NAME = "charter.md"

# ---------------------------------------------------------------------------
# admission monitoring threshold
# ---------------------------------------------------------------------------
# Charter content is decoupled from project size — it grows only with
# architectural complexity, not with LOC — so a fixed byte ceiling is a
# reasonable monitoring light. The default mirrors the base spec's historical
# 32 KiB ceiling. It is NOT a hard wall: see check_admission.
DEFAULT_CHARTER_MAX_BYTES = 32768


# ---------------------------------------------------------------------------
# altitude gate — admission standard
# ---------------------------------------------------------------------------
# The normative text that defines what the charter MAY carry. It is injected
# into the LLM-driven charter-admission check (see flow check (b)), which is the
# component that actually judges whether low-level content leaked in. The
# admission altitude is inherited from the retired base spec — charter IS the
# renamed base — but the wording points at code-index as the home of per-module
# locators, since the spec surface it used to name no longer exists.
CHARTER_ADMISSION_STANDARD = """\
## Charter Admission Standard

The `charter` (`tianluo/charter.md`) is injected — in full — into every step of
every session, and is the conventions channel for sandboxed sub-processes. Its
size is therefore a fixed cost paid on every single LLM call. Keep it small and
high-altitude.

The charter MAY carry ONLY content that is code-inexpressible AND that every
session genuinely needs loaded in full:

- Project identity / positioning (what this project is, its primary language /
  framework).
- The top-level architecture picture — specifically the *semantic / subjective*
  layering that mechanical structure cannot express (why these modules form one
  subsystem, where the cross-subsystem boundaries are).
- Project-wide cross-cutting conventions and hard constraints (coding
  conventions, key constraints, workflow conventions) that apply everywhere.
- Version-management policy.

The charter MUST NOT carry low-level content:

- Per-module / per-file / per-symbol locators — *where a thing lives, what it
  does, what its key symbols are* — belong to **code-index**
  (`luo code-index` for the top map, `luo code-index show <path>` to drill in),
  NOT to the charter. Copying them in only yields a size-bloating mirror that
  is less accurate than the code itself.
- Implementation detail of any single module — its mechanics, its internal
  behaviour — belongs in that module's code and its colocated why-comments, not
  in the charter.

When charter content exceeds its configured byte threshold, treat it as a RED
LIGHT prompting a review of whether low-level content has leaked in — NOT as a
reason to build an index over the charter. The fix is to remove the leaked
low-level content (relocating locator detail to code-index), not to chunk the
charter."""


# ---------------------------------------------------------------------------
# admission result
# ---------------------------------------------------------------------------
@dataclass
class AdmissionResult:
    """Outcome of the charter altitude gate (a monitoring-light check).

    Attributes:
        size_bytes: UTF-8 byte size of the charter text.
        threshold_bytes: The monitoring-light threshold checked against.
        over_threshold: ``True`` iff ``size_bytes > threshold_bytes``. This is
            advisory — it NEVER means the charter is rejected.
        admission_standard: The normative altitude-gate text
            (:data:`CHARTER_ADMISSION_STANDARD`) to feed the LLM admission
            check that judges low-level-content leakage.
        warning: A human-readable monitoring-light message when over threshold,
            else ``None``.
    """

    size_bytes: int
    threshold_bytes: int
    over_threshold: bool
    admission_standard: str
    warning: Optional[str] = None


def charter_path(project_root: Union[str, Path]) -> Path:
    """Return the absolute charter path for *project_root*."""
    return runtime_dir(project_root) / "charter.md"


def load_charter(project_root: Union[str, Path]) -> str:
    """Read ``tianluo/charter.md`` for whole-text injection into every step.

    Returns the full charter text, or an empty string when the file is absent
    or unreadable (the loader never raises — a missing charter degrades to "no
    project-level conventions injected" rather than breaking the flow). This is
    the single entry point steps and the sandbox conventions channel call.
    """
    path = charter_path(project_root)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


#: Default project-level convention scaffolded into every freshly generated
#: charter (`luo init` / `luo migrate`). It lives here rather than being typed
#: out at each generation site so both entry points stay in step. It is a soft,
#: prompt-level convention deliberately: parallel-safety of generated tests is
#: a habit worth stating up front so a project can later turn on parallel test
#: execution without an order-sensitive suite, but it is NOT worth a hard gate
#: (no mechanical check can decide whether a test shares mutable global state).
DEFAULT_PARALLEL_SAFE_TESTS_CONVENTION = (
    "流程生成的测试应**并行安全**：不依赖测试之间的执行顺序、不共享可变全局状态、"
    "临时资源（文件/目录/端口/数据库名等）一律使用唯一路径。此为软约定，"
    "与其他 conventions 同等强度，不设硬性检查门。"
)


def load_charter_template() -> str:
    """Return the raw packaged charter template text (no substitution)."""
    template_path = Path(__file__).parent.parent / "templates" / CHARTER_TEMPLATE_NAME
    return template_path.read_text(encoding="utf-8")


def render_charter_template(**values: str) -> str:
    """Render the packaged charter template, substituting ``{key}`` placeholders.

    Uses literal ``str.replace`` (not ``str.format``) so incidental braces in
    the template body — e.g. inside a fenced code block — are left untouched and
    never raise ``KeyError``. Mirrors ``init_cmd._render_template`` so charter
    rendering stays consistent with the other init templates.
    """
    content = load_charter_template()
    for key, value in values.items():
        content = content.replace("{" + key + "}", value)
    return content


def _changed_paths_touch_charter(changed_files: Optional[list]) -> bool:
    """Return True iff the project charter appears in *changed_files*.

    The match is on the path tail so it works whether callers pass
    project-relative (``tianluo/charter.md`` / legacy ``se3/charter.md``) or
    absolute paths, and tolerates Windows-style separators. Non-string /
    malformed entries are skipped.
    """
    if not changed_files:
        return False
    targets = (
        CHARTER_RELATIVE_PATH.as_posix(),
        LEGACY_CHARTER_RELATIVE_PATH.as_posix(),
    )
    for entry in changed_files:
        if not isinstance(entry, str) or not entry:
            continue
        norm = entry.replace("\\", "/")
        if norm in targets or norm.endswith(tuple("/" + t for t in targets)):
            return True
    return False


def admission_check_for_changes(
    project_root: Union[str, Path],
    changed_files: Optional[list],
    threshold_bytes: int = DEFAULT_CHARTER_MAX_BYTES,
) -> Optional[AdmissionResult]:
    """Run the altitude gate **only when the charter was actually touched**.

    This is the trigger point wired into the flow: the admission gate
    (low-level-content-leakage monitoring) should fire *when the charter
    changes*, not on every flow. When ``tianluo/charter.md`` is not among
    *changed_files* the function returns ``None`` (nothing to check); otherwise
    it loads the current charter and returns the :class:`AdmissionResult` from
    :func:`check_admission`, whose ``warning`` is a monitoring light — it never
    blocks the flow.
    """
    if not _changed_paths_touch_charter(changed_files):
        return None
    charter_text = load_charter(project_root)
    return check_admission(charter_text, threshold_bytes=threshold_bytes)


# ---------------------------------------------------------------------------
# admission gate — LLM prompt for the charter_freshness auto-update closed loop
# ---------------------------------------------------------------------------
# The charter_freshness step runs a propose -> gate -> apply closed loop that may
# auto-write tianluo/charter.md. Its gate has two halves: the mechanical anchored-
# replace check (a program), and this LLM admission gate (b). On THIS path the
# admission verdict is **gating**, not the monitoring-light role check_admission
# plays elsewhere — the candidate text is only written to disk if this gate (and
# the mechanical check) pass. Beyond the standard altitude/content-class audit,
# the gate asks one extra question the plain admission standard does not cover:
# whether removing any replaced text weakened a pre-existing convention that is
# unrelated to the current change (the admission standard audits content
# class / altitude / size, but is silent on the *deletion* of existing clauses,
# so that defence must be carried here and by the mechanical anchored check).
CHARTER_ADMISSION_GATE_REMOVAL_QUESTION = (
    "Does the removal of any replaced text weaken a convention or constraint "
    "that is unrelated to the current change? A descriptive freshness update "
    "may reword or correct a stale statement, but it MUST NOT quietly drop or "
    "dilute an existing agreement that this change did not touch. For every "
    "replaced (removed) passage listed above, judge whether its removal erases "
    "a still-valid, out-of-scope commitment; list any such removals in "
    "`weakened_removals`."
)


def build_admission_gate_prompt(
    candidate_text: str,
    replaced_texts: Optional[list] = None,
    diff_summary: str = "",
) -> str:
    """Build the LLM prompt for the charter-auto-update admission **gate**.

    This is gate half (b) of the ``charter_freshness`` propose -> gate -> apply
    closed loop. Unlike :func:`check_admission` (whose byte check is a
    *monitoring light*), the verdict this prompt elicits is **gating**: the
    candidate charter text is written to ``tianluo/charter.md`` only if the LLM
    admits it (and the mechanical anchored-replace check also passes).

    The prompt injects, in order:

    - :data:`CHARTER_ADMISSION_STANDARD` — the normative altitude/content-class
      standard (what the charter MAY vs MUST NOT carry);
    - the full ``candidate_text`` — the proposed post-update charter, judged as
      a whole so leaked low-level content or over-legislation is visible;
    - each entry of ``replaced_texts`` — the old passages this patch removes /
      rewrites, listed verbatim so the gate can judge each removal;
    - an optional ``diff_summary`` describing the code change that triggered the
      freshness update (context for "is this update merely descriptive");
    - :data:`CHARTER_ADMISSION_GATE_REMOVAL_QUESTION` — the extra removal-weakening
      question that the plain admission standard does not cover.

    ``replaced_texts`` may be empty / ``None`` for a pure-insertion patch (no old
    text removed); the prompt then states that no text is being removed so the
    removal question is trivially satisfied.

    The LLM is instructed to reply with a JSON object of the shape::

        {
          "admitted": bool,           # true iff the candidate passes the gate
          "violations": [str, ...],   # altitude/content-class problems, if any
          "weakened_removals": [str, ...]  # out-of-scope commitments a removal erased
        }

    Returns the prompt string. Pure string assembly — no I/O, no LLM call.
    """
    replaced = [t for t in (replaced_texts or []) if isinstance(t, str) and t.strip()]

    if replaced:
        removed_block_lines = ["The following existing charter passages are being "
                               "REMOVED or REWRITTEN by this update:", ""]
        for i, text in enumerate(replaced, start=1):
            removed_block_lines.append(f"--- replaced passage #{i} ---")
            removed_block_lines.append(text)
            removed_block_lines.append("")
        removed_block = "\n".join(removed_block_lines).rstrip()
    else:
        removed_block = (
            "This update is a PURE INSERTION: no existing charter text is being "
            "removed or rewritten. The removal-weakening question below is "
            "therefore trivially satisfied (`weakened_removals` must be empty)."
        )

    diff_block = (
        diff_summary.strip()
        if diff_summary and diff_summary.strip()
        else "(no diff summary provided)"
    )

    return f"""\
You are the admission GATE for a proposed descriptive update to the project
charter (`tianluo/charter.md`). On this path your verdict is BINDING: the candidate
charter below is written to disk only if you admit it. Judge whether the
candidate stays within the charter admission standard AND whether any removed
text weakens an unrelated existing convention.

{CHARTER_ADMISSION_STANDARD}

## Change that triggered this update

{diff_block}

## Candidate charter (proposed full text after the update)

{candidate_text}

## Removed / rewritten passages

{removed_block}

## Removal-weakening question

{CHARTER_ADMISSION_GATE_REMOVAL_QUESTION}

## Your verdict

Reply with ONLY a JSON object of this exact shape:

{{
  "admitted": true or false,
  "violations": ["<altitude / content-class problem>", ...],
  "weakened_removals": ["<out-of-scope commitment a removal erased>", ...]
}}

Set `admitted` to false if the candidate carries low-level content that belongs
in code-index, over-legislates a one-off as a universal rule, or if any removal
weakens an unrelated existing convention. Otherwise set `admitted` to true with
empty lists."""


def check_admission(
    charter_text: str,
    threshold_bytes: int = DEFAULT_CHARTER_MAX_BYTES,
) -> AdmissionResult:
    """Run the charter altitude gate against *charter_text*.

    The byte-size check is a **monitoring light**, not a hard wall: exceeding
    *threshold_bytes* sets ``over_threshold`` / ``warning`` so a reviewer (or a
    downstream LLM gate) is prompted to check for low-level-content leakage, but
    it NEVER blocks or raises. The returned ``admission_standard`` carries the
    normative text the LLM-driven admission check uses to judge what counts as
    leaked low-level content.
    """
    size_bytes = len((charter_text or "").encode("utf-8"))
    over_threshold = size_bytes > threshold_bytes
    warning: Optional[str] = None
    if over_threshold:
        warning = (
            f"charter is {size_bytes} bytes, over the {threshold_bytes}-byte "
            "monitoring threshold. This is a red light prompting a review of "
            "whether low-level content (per-module locators, implementation "
            "detail) has leaked in — relocate such content to code-index. It is "
            "NOT a hard limit and does not block the flow."
        )
    return AdmissionResult(
        size_bytes=size_bytes,
        threshold_bytes=threshold_bytes,
        over_threshold=over_threshold,
        admission_standard=CHARTER_ADMISSION_STANDARD,
        warning=warning,
    )
