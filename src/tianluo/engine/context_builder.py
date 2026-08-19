"""Context builder for automatic context collection.

Automatically gathers relevant context from previous outputs, project
state, and code for LLM calls.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir, runtime_dir_name

import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import LanguageConfig
    from .models import FlowInstance

logger = logging.getLogger(__name__)

# Steps whose output is human-facing (always use general language setting)
HUMAN_FACING_STEPS = {"summarize", "discovery"}

# Steps that write the knowledge asset governed by spec_language.
# spec_language is now the *knowledge-asset language* — the language in which
# charter.md and the code-index are written. This state-machine mapping still
# routes update_spec (the legacy spec-writing step) through spec_language for
# backward compatibility; the two live knowledge-asset writers (charter_freshness
# and code-index summaries) inject spec_language directly at their own prompt
# assembly, NOT through this mapping, because neither routes through
# get_step_language_instruction. The sync_* write paths keep their existing
# injection via get_spec_language_instruction unchanged.
SPEC_STEPS = {"update_spec"}


def get_step_language_instruction(step_type: str, project_root: Path) -> str:
    """Get the language instruction for a step based on config.

    Determines the appropriate language instruction by checking:
    1. If the step is in HUMAN_FACING_STEPS -> use config.language
    2. If the step is in SPEC_STEPS -> use config.spec_language
    3. If the step is configured for human confirmation -> use config.language
    4. Otherwise -> no language instruction

    Args:
        step_type: Current step type name (e.g., "summarize", "implement")
        project_root: Project root directory for loading config

    Returns:
        Language instruction string to append to prompt, or empty string.
    """
    from ..config import load_language_config, get_language_instruction, load_confirmation_config

    lang_config = load_language_config(project_root)

    # Check spec steps first. Spec-writing steps honor spec_language and use the
    # spec-flavored instruction (technical symbols preserved, spec_language
    # authoritative) so the written spec body is unambiguously in spec_language.
    if step_type in SPEC_STEPS:
        return get_language_instruction(
            lang_config.spec_language, step_type, for_spec=True,
        )

    # Check human-facing steps
    if step_type in HUMAN_FACING_STEPS:
        return get_language_instruction(lang_config.language, step_type)

    # Check if this step has confirmation configured (human or LLM reviewer)
    if lang_config.language:
        confirm_config = load_confirmation_config(project_root)
        if step_type in confirm_config.get("steps", {}):
            return get_language_instruction(lang_config.language, step_type)
        # LLM review prompt uses the general language setting
        if step_type == "confirm_llm_review":
            return get_language_instruction(lang_config.language, step_type)

    # Intentional non-injection for analyze / implement / verify_spec (and
    # plan/test/commit when unconfirmed): per the se3-config "Language
    # Configuration" requirement these steps let the LLM choose its own
    # language. Language is forced ONLY on (a) human-facing steps and (b)
    # spec-writing paths. analyze/verify_spec are read-only and implement
    # writes code, not spec, so none of them is a spec-writing path — there is
    # no gap to fill here. The genuine spec-writing gap lived in the sync_*
    # modules (see get_spec_language_instruction), not in this step mapping.
    return ""


def get_spec_language_instruction(project_root: Path) -> str:
    """Language instruction for knowledge-asset write paths outside the engine.

    spec_language governs the *knowledge-asset language*. The ``sync_*`` modules
    (``sync_engine`` / ``sync_discovery`` / ``sync_analyzer``) write or regenerate
    spec files but are NOT ``luo run`` state-machine steps, so they cannot route
    through :func:`get_step_language_instruction`. This helper gives them the same
    spec-flavored instruction ``update_spec`` receives, driven by
    ``language.spec_language``. (The two live knowledge-asset writers —
    charter_freshness and code-index summaries — inject spec_language at their own
    prompt assembly rather than through this helper; the sync_* injection points
    here are unchanged.)

    Returns the instruction string (technical symbols preserved, spec_language
    authoritative), or ``""`` when ``spec_language`` is unset — preserving the
    "no config → no injection" contract so existing sync behavior is unchanged.
    """
    from ..config import load_language_config, get_language_instruction

    lang_config = load_language_config(project_root)
    return get_language_instruction(lang_config.spec_language, for_spec=True)


# Steps explicitly forbidden from issue discovery injection
ISSUE_DISCOVERY_FORBIDDEN_STEPS = {"implement", "test"}

# Default steps that receive B-class issue discovery prompt injection.
#
# Empty by default: no step receives B-class issue-discovery injection unless a
# project explicitly opts a step in via ``issue_discovery.steps`` in tianluo.yaml.
# The whitelist mechanism itself is retained so steps that can surface
# ``discovered_issues`` from their own output (e.g. verify_spec via its JSON
# response) can still be enabled through config.
#
# ``summarize`` is intentionally NOT eligible for B-class discovery: its
# collection logic (``_extract_discovered_issues``) was removed so the step's
# sole job is to report what the session actually did. Whitelisting it via
# config would inject the prompt fragment with nothing to collect the result,
# so the summarize handler no longer calls the injection at all.
ISSUE_DISCOVERY_DEFAULT_STEPS: list[str] = []

# Steps explicitly forbidden from runtime environment injection.
# These are mechanical steps where awareness of read-only luo history/issue
# commands adds no value (and just inflates the prompt).
RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS = frozenset({"commit", "version_analyze"})

# Default steps that receive the runtime environment capability injection.
# Covers all "LLM free-decision" steps where the model might benefit from
# proactively consulting history or issues, while excluding purely mechanical
# steps via the FORBIDDEN set above.
RUNTIME_ENV_INJECTION_DEFAULT_STEPS = [
    "analyze",
    "plan",
    "plan_tasks",
    "implement",
    "investigate",
    "verify_spec",
    "update_spec",
    "self_check",
    "discovery",
    "summarize",
]

# Module-level cache slot for the runtime_environment.md contents. The file is
# part of the wheel and never changes at runtime, so a single read per process
# is plenty. Sentinel `object()` distinguishes "not loaded yet" from "loaded
# but empty/missing" (which is cached as "").
_RUNTIME_ENV_MARKDOWN_UNSET: Any = object()
_runtime_env_markdown_cache: Any = _RUNTIME_ENV_MARKDOWN_UNSET
_runtime_env_warning_logged: bool = False


def _load_runtime_environment_markdown() -> str:
    """Load runtime_environment.md once per process, caching the result.

    Returns the file contents (without leading newlines — callers prepend the
    \\n\\n separator themselves). On any read error, returns ``""`` and logs
    a single warning so misconfigured installs don't spam the logs.
    """
    global _runtime_env_markdown_cache, _runtime_env_warning_logged
    if _runtime_env_markdown_cache is not _RUNTIME_ENV_MARKDOWN_UNSET:
        return _runtime_env_markdown_cache

    md_path = Path(__file__).parent / "runtime_environment.md"
    try:
        _runtime_env_markdown_cache = md_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        if not _runtime_env_warning_logged:
            logger.warning(
                "runtime_environment.md missing or unreadable at %s: %s; "
                "runtime environment injection disabled",
                md_path,
                e,
            )
            _runtime_env_warning_logged = True
        _runtime_env_markdown_cache = ""
    return _runtime_env_markdown_cache


def _reset_runtime_environment_cache() -> None:
    """Test-only: reset the markdown cache so tests can exercise reload paths."""
    global _runtime_env_markdown_cache, _runtime_env_warning_logged
    _runtime_env_markdown_cache = _RUNTIME_ENV_MARKDOWN_UNSET
    _runtime_env_warning_logged = False


def get_runtime_environment_injection(step_type: str, project_root: Path) -> str:
    """Get the luo runtime environment prompt injection for a step.

    Loads ``src/tianluo/engine/runtime_environment.md`` and returns its content
    (prefixed with ``\\n\\n``) for whitelisted steps. The whitelist is the
    union of:
      * default list :data:`RUNTIME_ENV_INJECTION_DEFAULT_STEPS`, OR
      * ``runtime_environment_injection.steps`` from ``tianluo.yaml`` when present
        and a list,
    minus :data:`RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS` which always wins.

    Args:
        step_type: Current step type name (e.g., ``"plan"``).
        project_root: Project root directory for loading config.

    Returns:
        The injection string to append to the prompt, or ``""`` when the step
        is not whitelisted (or the markdown file cannot be read).
    """
    # FORBIDDEN always wins — short-circuit before any I/O so a misconfigured
    # yaml cannot re-enable injection on mechanical steps.
    if step_type in RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS:
        return ""

    whitelist: list[str] = RUNTIME_ENV_INJECTION_DEFAULT_STEPS
    from ..config import load_project_yaml

    config, _src = load_project_yaml(project_root)
    section = config.get("runtime_environment_injection") if isinstance(config, dict) else None
    section = section or {}
    if isinstance(section, dict):
        configured_steps = section.get("steps")
        # Accept only list overrides; ignore malformed values (bare strings,
        # dicts) to avoid surprising substring/key-lookup semantics.
        if isinstance(configured_steps, list):
            whitelist = configured_steps

    if step_type not in whitelist:
        return ""

    body = _load_runtime_environment_markdown()
    if not body:
        return ""
    # Legacy-layout projects still keep their runtime content under tianluo/ —
    # rewrite the canonical path references so the injected instructions
    # point at directories that actually exist (12.x transition only).
    if runtime_dir_name(project_root) == "se3":
        body = body.replace("tianluo/", "se3/")
    return "\n\n" + body


def get_issue_discovery_injection(step_type: str, project_root: Path) -> str:
    """Get the issue discovery prompt injection for a step.

    Checks if the step is in the whitelist (from tianluo.yaml config or defaults)
    and not in the forbidden list, then returns the injection prompt.

    Args:
        step_type: Current step type name (e.g., "summarize", "verify_spec")
        project_root: Project root directory for loading config

    Returns:
        Issue discovery prompt string to append to prompt, or empty string.
    """
    # Forbidden steps never get injection
    if step_type in ISSUE_DISCOVERY_FORBIDDEN_STEPS:
        return ""

    # Read whitelist from the active project YAML (tianluo.local.yaml when
    # present, otherwise tianluo.yaml). Routing through load_project_yaml
    # keeps malformed-local-shadow warnings firing here too instead of
    # relying on some other loader having run first.
    whitelist = ISSUE_DISCOVERY_DEFAULT_STEPS
    from ..config import load_project_yaml

    config, _src = load_project_yaml(project_root)
    issue_section = config.get("issue_discovery") if isinstance(config, dict) else None
    if isinstance(issue_section, dict):
        configured_steps = issue_section.get("steps")
        if configured_steps is not None:
            whitelist = configured_steps

    if step_type not in whitelist:
        return ""

    # Double-check forbidden list even if config includes them
    if step_type in ISSUE_DISCOVERY_FORBIDDEN_STEPS:
        return ""

    # Return the prompt directly — don't delegate to IssueDiscovery.get_injection_prompt()
    # which has its own hardcoded whitelist. The configurable whitelist here is authoritative.
    from .issue_discovery import ISSUE_DISCOVERY_PROMPT
    return ISSUE_DISCOVERY_PROMPT


def get_charter_injection(project_root: Path) -> str:
    """Get the full-charter prompt injection.

    The charter (``tianluo/charter.md``) is the shrunk rename of the retired base
    spec and plays exactly one runtime role: it is injected **in full, into
    every step, unconditionally**, doubling as the conventions channel for the
    sandboxed LLM sub-process (which cannot read CLAUDE.md and obtains
    project-level conventions only through what luo injects). It is the sole
    surviving project-convention injection surface — the spec-name listing it
    replaced was removed along with the spec mirror.

    Returns the charter wrapped in a labelled section (prefixed with ``\\n\\n``
    so it concatenates cleanly onto a prompt suffix), or ``""`` when the charter
    file is absent / empty — a missing charter degrades to "no project-level
    conventions injected" rather than breaking the flow.
    """
    from .charter import load_charter

    charter_text = load_charter(project_root)
    if not charter_text.strip():
        return ""

    return (
        "\n\n## Project Charter\n"
        "The charter below is the project's authoritative, high-altitude "
        "convention channel — project identity, top-level architecture, and "
        "project-wide cross-cutting constraints. It is injected in full on every "
        "step (it is also the only way a sandboxed sub-process learns project "
        "conventions, since it cannot read CLAUDE.md). Treat it as authoritative "
        "project-level context.\n\n"
        + charter_text.rstrip()
        + "\n"
    )


def get_self_check_constraint_sources(
    project_root: Path,
    changes_made: Any,
    baseline_commit: Optional[str] = None,
) -> Dict[str, str]:
    """Return recorded project constraints relevant to a SELF_CHECK review.

    The effective task description remains the functional-requirement authority;
    this helper supplies the separate project-constraint channel: the full
    charter and colocated WHY:/INVARIANT: comments from touched code.  The
    comment harvest includes the baseline revision when available, so a change
    cannot erase the very constraint against which it should be reviewed.

    The returned strings are suitable both for prompt rendering and for
    verbatim-quote validation.  Missing files or an unavailable baseline simply
    omit that source instead of weakening the rest of the review.
    """
    from .charter import load_charter

    sources: Dict[str, str] = {}
    charter = load_charter(project_root)
    if charter.strip():
        sources["charter"] = charter

    changed_paths: set[str] = set()
    if isinstance(changes_made, dict):
        entries = changes_made.get("files_changed") or []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, str) and entry:
                    changed_paths.add(entry)
                elif isinstance(entry, dict):
                    path = entry.get("path") or entry.get("file_path")
                    if isinstance(path, str) and path:
                        changed_paths.add(path)

    if changed_paths:
        # Import lazily: invariant_check reuses self_check's validators, so a
        # module-level import here would create a cycle during step registration.
        from .steps.invariant_check import (
            _harvest_why_comments,
            _is_marked_why_comment,
        )

        why_comments = _harvest_why_comments(
            project_root, changed_paths, baseline_commit,
        )
        marked_blocks = []
        for block in why_comments:
            marked = [
                line for line in block.splitlines()
                if _is_marked_why_comment(line)
            ]
            if marked:
                marked_blocks.append("\n".join(marked))
        if marked_blocks:
            sources["why_comments"] = "\n\n".join(marked_blocks)

    return sources


def get_code_index_injection(project_root: Path) -> str:
    """Get the code-index root-map prompt injection.

    The **code-index** is the project's structural orientation map. Only the
    **adaptive root view** — a zoomable directory tree expanded to a byte budget
    (``code_index.view_budget_bytes``) — is injected on every step, so the map is
    bounded no matter how large the project. The agent drills deeper **on
    demand**: ``luo code-index index <path>`` shows exactly one more literal
    level, and ``luo code-index show <path>`` shows a file's full
    function/method detail.

    The map is read **only** from the authoritative ``tianluo/code-index.md``, and
    this helper never triggers a (re)build (regeneration is the lazy/incremental
    ``load_or_build`` job the CLI / consuming steps own). When the md has not been
    built yet the helper still injects the drill-down protocol plus a one-line
    note so the agent knows the map exists and how to materialise it.

    Returns the injection string (prefixed with ``\\n\\n`` for clean suffix
    concatenation). It is non-empty regardless of build state, because the
    "consult code-index before reading source" convention is itself valuable.
    """
    from ..config import load_code_index_config
    from .code_index_render import load_for_display, render_adaptive

    header = (
        "\n\n## Code Index (project structure map)\n"
        "The map below is the project's structural orientation map — a zoomable "
        "directory tree. The top level is always shown; code-bearing directories "
        "are expanded a few levels deep within a byte budget, so it is bounded no "
        "matter how large the project. It is your project-wide structural "
        "awareness, injected on every step.\n\n"
        "**Before reading source code, FIRST consult the code-index to locate "
        "the relevant symbols.** Scan this map to find the directory / file(s) "
        "that matter. A collapsed directory shown as a single line can be opened "
        "one more level with `luo code-index index <path>`; a file's "
        "function/method-level detail is pulled with `luo code-index show <path>` "
        "(both run via Bash) — instead of reading whole source files blindly, the "
        "map points you at the few symbols worth reading.\n\n"
        "To find items by keyword or regex, use `luo code-index search <pattern>` "
        "**instead of** `grep 'pattern' tianluo/code-index.md`: each hit carries the "
        "item's full locating path (a symbol renders as `relpath::local_id`, so "
        "you see the file it lives in — which a raw grep line cannot give you). "
        "Its syntax matches grep — `pattern` is a regex by default; `-i` for "
        "case-insensitive, `-F` for literal substrings, `-m N` to cap the number "
        "of matches.\n\n"
    )

    index = load_for_display(Path(project_root))
    if index is None or not index.files:
        return header + (
            "_(The code-index has not been built yet. Run "
            "`luo code-index rebuild` to generate `tianluo/code-index.md`; the "
            "display commands `luo code-index index <path>` / "
            "`luo code-index show <path>` read that map and report it is not "
            "built until you do.)_\n"
        )

    cfg = load_code_index_config(Path(project_root))
    return (
        header
        + render_adaptive(index, cfg.primary_roots, cfg.view_budget_bytes).rstrip()
        + "\n"
    )


def ensure_code_index_fresh(
    project_root: Path,
    *,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    step_type: str = "commit",
) -> None:
    """Trigger the lazy-incremental code-index rebuild so a consuming flow step
    sees a fresh map, mirroring ``spec_index``'s ``load_or_build`` invalidation.

    When both ``flow_id`` and ``step_id`` are supplied (the commit path, which
    owns a flow + step context), a progress emitter is constructed and passed to
    ``load_or_build`` so the rebuild streams per-node progress to the running
    flow's web console via ``chat_history.record_index_progress`` — writing a
    plain jsonl line the daemon's existing ``history_data`` channel forwards, so
    the engine takes on **no** ``tianluo.server`` import. Without a flow context
    (e.g. the read-side refresh before implement) the emitter is ``None`` and the
    rebuild is silent, exactly as before.

    A flow step that injects the code-index (analyze / plan / implement / …)
    calls this immediately before :func:`get_code_index_injection`, so the
    authoritative ``tianluo/code-index.md`` is re-enumerated and only the symbols
    whose content fingerprint changed are re-summarised (unchanged symbols reuse
    their md summary, preserving human corrections). Without this, source files
    edited since the last build — e.g. by a prior flow's commits — would leave a
    stale map injected into every step until a human ran ``luo code-index
    rebuild`` manually.

    Gated on the md already existing: the **initial** build is owned by
    ``luo migrate`` / the explicit ``luo code-index rebuild`` command (the
    injection itself surfaces a build note when no map exists), so a project that
    has never built the index does not pay a full LLM build mid-flow. Once a map
    exists, every consuming step keeps it fresh incrementally.

    Best-effort: any failure is logged and swallowed so prompt construction (and
    therefore the flow step) never breaks on a flaky rebuild.
    """
    try:
        from . import code_index

        root = Path(project_root)
        if not code_index.md_path(root).exists():
            # No built map yet — leave the initial build to migrate / the
            # explicit rebuild command; there is nothing to keep fresh.
            return

        emitter = None
        if flow_id and step_id:
            from .chat_history import record_index_progress

            def emitter(path: str, kind: str, done: int, total: int, phase: str) -> None:
                # record_index_progress is itself OSError-guarded, so a write
                # hiccup never surfaces here and never breaks the rebuild.
                record_index_progress(
                    root,
                    flow_id,
                    step_id,
                    step_type,
                    path=path,
                    kind=kind,
                    done=done,
                    total=total,
                    phase=phase,
                )

        code_index.load_or_build(root, progress=emitter)
    except Exception as exc:  # noqa: BLE001 — never break prompt construction
        logger.warning("code_index: lazy freshness refresh failed: %s", exc)


# Internal LLMCaller step types that are NOT `luo run` state-machine steps (so
# they are absent from STEP_POOL) but whose sub-agents are *pure functions*:
# they only read code and RETURN structured text/JSON, while SE3's own code does
# every disk write. They MUST run read-only so the sub-agent cannot write stray
# files into the project (which would otherwise be picked up by the gitignore-
# respecting enumerator and pollute the index / working tree):
#   - ``code_index`` — the per-node summariser (returns {id: summary}; SE3 writes
#     tianluo/code-index.md itself via code_index._write_md).
#   - ``migrate``    — the spec-salvage LLM (returns charter text + colocations;
#     SE3 writes tianluo/charter.md and the why-comments itself).
_READ_ONLY_INTERNAL_STEPS = frozenset({"code_index", "migrate"})


def is_step_read_only(step_type: str) -> bool:
    """Return True if ``step_type`` runs under read-only constraints.

    Resolution order:
      1. ``luo run`` state-machine steps — looked up in STEP_POOL by name,
         honoring each step's ``read_only`` attribute.
      2. Internal pure-data sub-agent steps (``code_index`` / ``migrate``) whose
         agent only reads and returns text — SE3 does every write itself.

    Every writable step (implement / commit / …) returns False, as do unknown
    step types.

    This is the single source of truth for both the prompt-level read-only
    injection (:func:`get_read_only_injection`) and the tool-level
    ``--disallowedTools`` enforcement in ``llm_caller``.
    """
    from .models import STEP_POOL

    for _st, info in STEP_POOL.items():
        if info.get("name") == step_type:
            return bool(info.get("read_only", False))

    return step_type in _READ_ONLY_INTERNAL_STEPS


def get_read_only_injection(step_type: str, force: bool = False) -> str:
    """Get read-only constraint prompt injection for a step.

    Delegates the read-only decision to :func:`is_step_read_only`, which
    covers both STEP_POOL steps and the internal pure-data sub-agent steps.
    If the step is read-only, returns a prompt constraint forbidding file
    modifications; otherwise returns an empty string.

    Args:
        step_type: Current step type name (e.g., "analyze", "implement",
            "code_index")
        force: Emit the constraint even when ``step_type`` is not registry
            read-only. Used by a call-level read-only override (LLMCaller's
            ``force_read_only``) so a step whose handler writes files can still
            hold its LLM sub-call read-only without mutating ``is_step_read_only``.

    Returns:
        Read-only constraint prompt string, or empty string if step is not read-only.
    """
    if not force and not is_step_read_only(step_type):
        return ""

    return (
        "\n\n## READ-ONLY STEP CONSTRAINT\n"
        "**CRITICAL: This is a read-only analysis step. You MUST NOT modify any files.**\n\n"
        "Forbidden actions:\n"
        "- Do NOT use the Write tool to create or overwrite any files\n"
        "- Do NOT use the Edit tool to modify any files\n"
        "- Do NOT use the NotebookEdit tool\n"
        "- Do NOT create new files of any kind\n"
        "- Do NOT run shell commands that modify files (e.g., sed, awk, tee, redirects with >)\n\n"
        "Allowed actions:\n"
        "- Use Read to read file contents\n"
        "- Use Grep to search file contents\n"
        "- Use Glob to find files by pattern\n"
        "- Use Bash for read-only commands (e.g., git log, git diff, ls, cat)\n\n"
        "Your sole purpose in this step is analysis and reasoning. "
        "Output your findings as structured data only."
    )


def get_runtime_context_injection(project_root: Path, main_repo_root: Path | None = None) -> str:
    """Get runtime directory structure context for LLM.

    When running in a worktree, gitignored runtime directories (state/, history/,
    issues/open/, etc.) are absent. This function detects worktree isolation and
    injects context from the main repository so the LLM knows what's available.

    Args:
        project_root: Current working directory (may be a worktree)
        main_repo_root: Main repository root. If None, tries to detect from
            git worktree metadata.

    Returns:
        Context string describing the main repo's runtime state, or empty string
        if not in a worktree or main repo has no additional context.
    """
    # Determine main repo root (if not explicitly provided, detect it)
    if main_repo_root is None:
        main_repo_root = _detect_main_repo_root(project_root)

    # Not in a worktree, or same as project_root — no injection needed
    if main_repo_root is None or main_repo_root.resolve() == project_root.resolve():
        return ""

    main_se3 = runtime_dir(main_repo_root)
    if not main_se3.exists():
        return ""

    # Collect context from gitignored runtime directories that are
    # absent in the worktree but present in the main repository
    context_parts: list[str] = []

    # Open issues (useful for understanding known problems)
    issues_dir = main_se3 / "issues" / "open"
    if issues_dir.exists():
        issue_files = sorted(f.name for f in issues_dir.iterdir() if f.is_file() and f.suffix in (".yaml", ".yml"))
        if issue_files:
            context_parts.append(f"### Open Issues ({len(issue_files)})")
            for name in issue_files[:10]:
                context_parts.append(f"- `tianluo/issues/open/{name}`")

    # Active flow state
    state_dir = main_se3 / "state"
    if state_dir.exists():
        state_files = sorted(f.name for f in state_dir.iterdir() if f.is_file())
        if state_files:
            context_parts.append(f"\n### Flow State")
            for name in state_files[:5]:
                context_parts.append(f"- `tianluo/state/{name}`")

    # History summary
    history_dir = main_se3 / "history"
    if history_dir.exists():
        flow_dirs = sorted(d.name for d in history_dir.iterdir() if d.is_dir())
        if flow_dirs:
            context_parts.append(f"\n### History: {len(flow_dirs)} flow(s)")

    # If no runtime context was collected, skip injection
    if not context_parts:
        return ""

    # Build full injection
    parts = ["\n\n## Runtime Context (from main repository)"]
    parts.append("You are running in an isolated worktree. The following runtime "
                 "state exists in the main repository:\n")
    parts.extend(context_parts)
    parts.append("")

    return "\n".join(parts)


def _detect_main_repo_root(worktree_path: Path) -> Path | None:
    """Detect the main repository root from a worktree path.

    Uses git to find the common directory (main repo .git dir).

    Args:
        worktree_path: Path that may be a worktree

    Returns:
        Main repo root path, or None if detection fails
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            common_dir = Path(result.stdout.strip())
            if not common_dir.is_absolute():
                common_dir = (worktree_path / common_dir).resolve()
            # common_dir is the .git directory of the main repo
            main_root = common_dir.parent
            if main_root != worktree_path and (runtime_dir(main_root)).exists():
                return main_root
    except Exception:
        pass
    return None


def build_llm_review_prompt(
    step_to_review_type: str,
    step_output: Dict[str, Any],
    task_description: str,
    revision_feedback: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> str:
    """Build a structured prompt for LLM-based review of a step's output.

    Args:
        step_to_review_type: Type of the step being reviewed (e.g., "plan")
        step_output: The outputs from the reviewed step
        task_description: Original task description from the flow
        revision_feedback: Previous revision feedback if this is a re-review
        project_root: Project root directory for language config

    Returns:
        Formatted prompt string for LLM reviewer
    """
    import json as _json

    # Format step output for display
    output_parts = []
    for key, value in step_output.items():
        if key.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            output_parts.append(f"**{key}:**\n```json\n{_json.dumps(value, indent=2, default=str)}\n```")
        elif value is not None:
            output_parts.append(f"**{key}:**\n{value}")
    step_output_text = "\n\n".join(output_parts) if output_parts else "(no output)"

    revision_section = ""
    if revision_feedback:
        revision_section = f"""
## Previous Revision Feedback
The following feedback was given in a previous review. Check whether it has been addressed:
{revision_feedback}
"""

    # Language instruction
    lang_instruction = ""
    if project_root:
        lang_instruction = get_step_language_instruction("confirm_llm_review", project_root)

    prompt = f"""You are reviewing the output of the **{step_to_review_type}** step in an SE3 development workflow.

## Original Task
{task_description}

## Output of the {step_to_review_type.upper()} Step
{step_output_text}
{revision_section}
## Evaluation Criteria
Evaluate the output against these criteria:
1. **Completeness**: Does the output fully address the task requirements?
2. **Correctness**: Is the content accurate and free of errors?
3. **Clarity**: Is the output well-structured and clear?
4. **Feasibility**: Are the proposed approaches realistic and implementable?

## Response Format
You MUST respond with a JSON object in this exact format:
```json
{{
    "approved": true or false,
    "feedback": "Your detailed feedback here. If not approved, explain what needs to be changed."
}}
```

If the output is acceptable, set "approved" to true. If changes are needed, set "approved" to false and provide specific, actionable feedback.
{lang_instruction}"""

    return prompt


# WHY the count dimension is granularity-dependent while dimensions 2 and 3 are
# not: the group *count* is the one thing `plan_granularity` overrides. Under
# `auto` the count is PLAN's judgement and the gate may overturn it; under
# `single` it is a configured guarantee PLAN is forbidden to deviate from, and
# under `conservative` the configuration deliberately lowers the split threshold
# below the capability edge. Reviewing all three against the `auto` standard
# would make the gate demand a regrouping the configuration forbids PLAN to
# produce, burning the whole revision budget until max_iterations auto-approves.
# Forbidden artifact/layer splits and `depends_on` truthfulness are doctrine
# invariants no granularity relaxes, so they stay identical across the three.
_GROUPING_REVIEW_COUNT_DIMENSION_AUTO = """1. **Group count vs. independent tasks**: does the number of groups equal the
   number of mutually unrelated, independent tasks in the requirement? Flag both
   directions — unrelated tasks fused into one group (which needlessly
   serializes work that could have run in parallel in isolated worktrees), and
   one coherent task carved into several groups. For every group beyond
   one-per-task, check that the plan actually gives a capability-edge reason and
   that it holds: one autonomous implement call could not complete the task, or
   forcing it into one call would substantially degrade execution quality. Flag
   any group that exists only because a task was pre-cut along implementation
   phases, implementation paths, or artifact types, and flag any group so large
   that one autonomous implement call could not safely carry it."""

_GROUPING_REVIEW_COUNT_DIMENSION_SINGLE = """1. **Group count (fixed by configuration — never a defect here)**: this flow is
   pinned to `plan_granularity: single`, so PLAN was ordered to emit exactly one
   group covering the entire task and forbidden to split it under any
   circumstances. The count is therefore a configured guarantee, not a judgement
   PLAN made, and the engine runs the whole task through one autonomous
   implement call whatever count the plan carries. Do NOT fail the review on the
   group count: a single group covering several mutually unrelated tasks is the
   correct output here and must not be reported as a grouping defect, and no
   feedback may ask for the work to be spread across more groups."""

_GROUPING_REVIEW_COUNT_DIMENSION_CONSERVATIVE = """1. **Group count vs. independent tasks**: mutually unrelated, independent tasks
   fused into one group is a defect — flag it, since it needlessly serializes
   work that could have run in parallel in isolated worktrees. In the other
   direction this flow is configured `plan_granularity: conservative`, which
   deliberately lowers the splitting threshold below the capability edge: PLAN
   was told to split whenever there is *any* doubt that one call can carry a
   task, so a coherent task carved into several groups is expected here and must
   NOT be flagged as over-splitting. Still flag any group that exists only
   because a task was pre-cut along implementation phases, implementation paths,
   or artifact types — a lowered threshold licenses smaller groups, never splits
   along those axes — and flag any group so large that one autonomous implement
   call could not safely carry it."""

_GROUPING_REVIEW_COUNT_DIMENSION = {
    "auto": _GROUPING_REVIEW_COUNT_DIMENSION_AUTO,
    "single": _GROUPING_REVIEW_COUNT_DIMENSION_SINGLE,
    "conservative": _GROUPING_REVIEW_COUNT_DIMENSION_CONSERVATIVE,
}

_GROUPING_REVIEW_GRANULARITY_NOTE = {
    "single": """
This flow additionally runs under `plan_granularity: single`, a configured pin
that overrides the count rule above: PLAN was ordered to emit exactly one group
for the whole requirement, unrelated tasks included, and the engine executes it
as one autonomous call. The group count is out of your hands and out of scope.
""",
    "conservative": """
This flow additionally runs under `plan_granularity: conservative`, a configured
pin that lowers the split threshold below the capability edge: PLAN was told to
split whenever there is any doubt that one call can carry a task, so more groups
than the default sizing would produce is the configured intent, not a defect.
""",
}


def _build_capability_grouping_review_prompt(
    *,
    task_description: str,
    step_output_text: str,
    revision_section: str,
    lang_instruction: str,
    granularity: Any = None,
) -> str:
    """Render the capability-doctrine *grouping* review prompt.

    Three review dimensions, matching the three ways a capability grouping can
    be wrong: a group count that does not match the number of mutually
    independent tasks (unrelated tasks fused, or one task pre-cut without a
    capability-edge reason), a split made along artifact type or code layer
    instead of along a task unit, and a ``depends_on`` graph that does not
    describe real dependencies.

    ``granularity`` is the flow's persisted :class:`PlanGranularity` (or its
    string value); it selects the count dimension's wording so the gate never
    demands a group count the configuration forbids PLAN to emit. An unknown or
    missing value reads as ``auto``.
    """
    key = str(getattr(granularity, "value", granularity) or "auto").strip().lower()
    count_dimension = _GROUPING_REVIEW_COUNT_DIMENSION.get(
        key, _GROUPING_REVIEW_COUNT_DIMENSION_AUTO
    )
    granularity_note = _GROUPING_REVIEW_GRANULARITY_NOTE.get(key, "")
    return f"""You are reviewing the output of the **plan** step in an SE3 development workflow.

Your one and only job here is to review the **grouping**: whether the plan cut the
work into the right task groups. Do NOT grade the plan on general quality, style,
or feasibility, and do NOT ask for the requirements to be decomposed into a
per-requirement task list — that is out of scope for this review. Under this
doctrine a group is deliberately coarse: its description names a whole task, and
the detailed breakdown happens inside the implementation call, not here.

## Original Task (the tasks to be grouped live in here)
{task_description}

## Output of the PLAN Step (task_groups)
{step_output_text}
{revision_section}
## The Grouping Doctrine Being Reviewed
The unit of grouping is a **task** — one coherent piece of work the user would
regard as a single thing — and the one and only reason to split a task any
further is **whether a single autonomous implement call can safely carry it**:
- one task that one call can finish → one group;
- one task too large for one call → split into two or more groups;
- mutually unrelated, independent tasks → one group each, so that they can be
  executed in parallel in isolated worktrees;
- default to aggregation — one task, one group. A task may be split only at the
  capability edge, i.e. PLAN positively judged that a single autonomous
  implement call cannot complete it, or that forcing it into one call would
  substantially degrade the quality of the execution; that reason belongs in the
  group's description. Pre-cutting one task along implementation phases,
  implementation paths, or artifact types is never a valid split — how a task is
  broken down internally is decided inside the implement call, not by PLAN.
{granularity_note}
## Review Dimensions (check all three)
{count_dimension}
2. **Forbidden splits by artifact type or layer**: grouping must follow task
   units, never files, modules, or code layers. A standalone test group, docs
   group, or config group is forbidden — tests and verification are part of
   what each group itself delivers. Flag any group whose identity is an
   artifact type or an architectural layer rather than a task.
3. **Dependency declarations**: does each `depends_on` describe a real ordering
   constraint (the depended-on group produces something this group needs)? Flag
   dangling group ids, cycles, dependencies that do not actually exist (which
   needlessly serialize work that could run in parallel), and missing
   dependencies between groups that genuinely must be ordered.

Approve if the grouping is sound on all three dimensions. If any dimension fails,
do NOT approve — name the offending groups and say how they should be regrouped.

## Response Format
You MUST respond with a JSON object in this exact format:
```json
{{
    "approved": true or false,
    "feedback": "Your detailed feedback here. If not approved, state which of the three dimensions failed, name the offending group ids, and describe the regrouping you expect."
}}
```

If the grouping is sound, set "approved" to true. Otherwise set "approved" to
false and give specific, actionable regrouping feedback.
{lang_instruction}"""


def build_plan_confirm_prompt(
    step_output: Dict[str, Any],
    task_description: str,
    revision_feedback: Optional[str] = None,
    project_root: Optional[Path] = None,
    decomposition: Optional[Any] = None,
    granularity: Optional[Any] = None,
) -> str:
    """Build the plan-specific review prompt for the flow's decomposition doctrine.

    Unlike the generic ``build_llm_review_prompt``, this prompt is narrowly
    specialized for confirming a ``plan`` step: it does not score the plan on
    general quality axes (completeness/clarity/feasibility). What it *does* ask
    depends on which doctrine PLAN followed:

    - ``capability`` (default): review the **grouping** — whether the group
      count matches the number of mutually independent tasks (and whether every
      further split names a capability-edge reason that holds), whether any
      group is split by artifact type or code layer, and whether the
      ``depends_on`` declarations hold. The count half of that is stated
      relative to the flow's ``granularity``: ``single`` and ``conservative``
      are configured pins on the count, so the reviewer is told not to demand a
      regrouping PLAN was forbidden (or told not) to produce.
    - ``granular`` (legacy): the historical requirement -> task coverage review,
      kept verbatim because ``granular`` is a behaviour-preserving legacy value
      and its per-task listing still makes coverage checkable.

    The capability branch deliberately drops the requirement-coverage review;
    see the colocated ``WHY:`` note in the body for the reasoning.

    The output schema is the same ``{approved, feedback}`` contract the generic
    builder uses in both branches, so ``_llm_review``'s parsing, revision loop,
    and cross-revision max_iterations counting are reused unchanged.

    Args:
        step_output: The outputs from the plan step (task_groups, complexity, …)
        task_description: Original task description from the flow
        revision_feedback: Previous revision feedback if this is a re-review
        project_root: Project root directory for language config
        decomposition: The flow's persisted ``PlanDecomposition`` (or its string
            value). ``None`` falls back to the current default doctrine.
        granularity: The flow's persisted ``PlanGranularity`` (or its string
            value), used only by the capability branch to phrase the group-count
            dimension. ``None``/unknown reads as ``auto``.

    Returns:
        Formatted prompt string for the plan reviewer
    """
    import json as _json

    from .plan_decomposition import PlanDecomposition, PlanModeResolver

    if decomposition is None:
        resolved_decomposition = PlanModeResolver.DEFAULT_DECOMPOSITION
    else:
        raw = getattr(decomposition, "value", decomposition)
        try:
            resolved_decomposition = PlanDecomposition(str(raw).strip().lower())
        except ValueError:
            # An unrecognized value must not silently pick the legacy review;
            # the current doctrine is the safer reading of an unknown flow.
            resolved_decomposition = PlanModeResolver.DEFAULT_DECOMPOSITION

    # Format plan output for display (task_groups, total_complexity, etc.).
    output_parts = []
    for key, value in step_output.items():
        if key.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            output_parts.append(f"**{key}:**\n```json\n{_json.dumps(value, indent=2, default=str)}\n```")
        elif value is not None:
            output_parts.append(f"**{key}:**\n{value}")
    step_output_text = "\n\n".join(output_parts) if output_parts else "(no output)"

    revision_section = ""
    if revision_feedback:
        revision_section = f"""
## Previous Revision Feedback
The following feedback was given in a previous review. Check whether it has been addressed:
{revision_feedback}
"""

    # Language instruction
    lang_instruction = ""
    if project_root:
        lang_instruction = get_step_language_instruction("confirm_llm_review", project_root)

    # WHY: the capability branch reviews the grouping instead of requirement
    # coverage, in three steps: (1) self_check is task-description-authoritative
    # — the effective task description chain is what it accepts against; (2) the
    # requirement -> code coverage check is therefore already performed by
    # self_check, and against the real implementation rather than against a
    # plan, which is strictly the stronger check; (3) a capability group carries
    # no per-task listing — its description simply *is* the capability — so a
    # requirement -> task coverage review has lost its discriminating power,
    # since every requirement trivially "maps" to some group. Reviewing the
    # grouping is the one judgement left here that no later gate can make. The
    # granular branch keeps the coverage review because that legacy doctrine
    # still emits the per-task listing the review needs to discriminate on, and
    # its behaviour is contractually unchanged.
    if resolved_decomposition is PlanDecomposition.CAPABILITY:
        return _build_capability_grouping_review_prompt(
            task_description=task_description,
            step_output_text=step_output_text,
            revision_section=revision_section,
            lang_instruction=lang_instruction,
            granularity=granularity,
        )

    prompt = f"""You are reviewing the output of the **plan** step in an SE3 development workflow.

Your one and only job here is to verify **requirement coverage**: that the plan's
tasks together cover every requirement embedded in the original task. Do NOT
grade the plan on general quality, style, or feasibility — that is out of scope
for this review.

## Original Task (the requirements live in here)
{task_description}

## Output of the PLAN Step (task_groups)
{step_output_text}
{revision_section}
## Review Procedure (follow in order)
1. **Decompose discrete requirements from the task_description**: read the
   Original Task above and break it into a numbered list of discrete, atomic
   requirements. A single sentence may contain several requirements; split them.
2. **Check requirement-by-requirement coverage**: for each requirement, check
   whether the plan's task_groups contain at least one task that covers it.
   In other words, verify that **every requirement has a corresponding task**.
3. **List uncovered requirements**: explicitly call out any requirement that has
   no corresponding task, or that is only partially covered. These coverage gaps
   are the reason to request a revision.

Approve only if every discrete requirement maps to at least one covering task.
If any requirement is uncovered or under-covered, do NOT approve — the plan must
be regenerated to add the missing tasks.

## Response Format
You MUST respond with a JSON object in this exact format:
```json
{{
    "approved": true or false,
    "feedback": "Your detailed feedback here. If not approved, list the numbered requirements and, for each, which task covers it; name every uncovered requirement explicitly."
}}
```

If every requirement is covered, set "approved" to true. If any requirement
lacks a corresponding task, set "approved" to false and list the uncovered
requirements with specific, actionable feedback.
{lang_instruction}"""

    return prompt


def build_confirm_prompt(step_to_review_type: str) -> str:
    """Human-readable one-line prompt for a CONFIRM approval gate.

    The web console renders Approve/Reject buttons for a ``confirm``-kind call,
    but a legacy free-text responder (and the daemon call list) still shows the
    ``prompt`` string, so it must stand on its own as the question being asked.
    """
    label = (step_to_review_type or "step").replace("_", " ")
    if step_to_review_type == "adjudicate":
        # The surfaces the boundary clause claims to govern are named explicitly:
        # the human gate is the only place a wrongly-swept sibling surface can be
        # caught before the clause is written into the contract.
        return (
            "Review the adjudication ruling below (rationale + description diff, "
            "plus the surfaces the boundary clause claims to cover) and approve "
            "it, or request changes."
        )
    return f"Review the {label} output and approve it, or request changes."


def _display_covered_surfaces(raw: Any) -> List[Dict[str, str]]:
    """Project a ruling's ``covered_surfaces`` into a display-safe list.

    The adjudicate step already refuses to land an incomplete entry (its
    ``_normalized_covered_surfaces`` gate), so in practice this only ever sees a
    clean list. It re-sanitizes anyway — and *drops* bad entries instead of
    raising — because the display layer is on the human-approval path: a
    hand-edited or legacy state file must degrade to "nothing to show" rather
    than crash the gate that a human is waiting at.
    """
    if not isinstance(raw, list):
        return []
    clean: List[Dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        surface = entry.get("surface")
        justification = entry.get("justification")
        if not isinstance(surface, str) or not isinstance(justification, str):
            continue
        surface, justification = surface.strip(), justification.strip()
        if not surface or not justification:
            continue
        clean.append({"surface": surface, "justification": justification})
    return clean


def build_adjudicate_review_context(
    flow: "FlowInstance", step_to_review_id: Optional[str]
) -> Dict[str, Any]:
    """Assemble the adjudicate-approval display payload for a confirm call.

    When a CONFIRM gate reviews an ADJUDICATE ruling the web console needs to
    show *what* is being approved: the ruling's ``adjudication_rationale``, the
    post-ruling ``adjudicated_description``, and the pre-ruling ``baseline`` it
    replaces — so the operator reads a before/after diff instead of guessing.

    ``baseline`` is the description effective *before* this ruling: a prior
    adjudication's description if one exists, else the discovery-refined /
    original base. It is resolved via ``_effective_task_description_base`` with
    the reviewed step excluded, so the ruling's own not-yet-approved rewrite is
    never mistaken for its own baseline.

    ``covered_surfaces`` carries the homomorphic-surface sweep: every surface the
    ruling's boundary clause claims to govern, each with its by-construction
    justification. It is a read-only projection of the reviewed step's outputs —
    the audit record is written there unconditionally, this payload only renders
    it — and it is defensively re-sanitized here so the display layer always
    receives a predictable list of ``{surface, justification}`` (a malformed or
    incomplete entry is dropped rather than crashing the gate).

    Returns a dict with ``adjudication_rationale`` / ``adjudicated_description`` /
    ``baseline`` (each a string, possibly empty) and ``covered_surfaces`` (a list,
    possibly empty); an empty dict when the reviewed step is missing or is not an
    ADJUDICATE ruling (so a non-adjudicate confirm call carries none of these
    fields).
    """
    from .models import StepType

    if not (flow.state and step_to_review_id):
        return {}
    reviewed = flow.state.steps.get(step_to_review_id)
    if reviewed is None or reviewed.step_type != StepType.ADJUDICATE:
        return {}

    rationale = reviewed.outputs.get("adjudication_rationale") or ""
    adjudicated_description = reviewed.outputs.get("adjudicated_description") or ""
    covered_surfaces = _display_covered_surfaces(reviewed.outputs.get("covered_surfaces"))

    # Resolve the pre-ruling baseline through the shared effective-text layer so
    # the diff anchor matches exactly what the flow considered effective before
    # this ruling landed (prior adjudication > discovery-refined > original).
    from .state_machine import _effective_task_description_base

    baseline = (
        _effective_task_description_base(flow, exclude_step_id=step_to_review_id) or ""
    )

    return {
        "adjudication_rationale": rationale,
        "adjudicated_description": adjudicated_description,
        "baseline": baseline,
        "covered_surfaces": covered_surfaces,
    }
