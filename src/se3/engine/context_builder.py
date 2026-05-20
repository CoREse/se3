"""Context builder for automatic context collection.

Automatically gathers relevant context from specs, previous outputs,
project state, and code for LLM calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import LanguageConfig

logger = logging.getLogger(__name__)

# Steps whose output is human-facing (always use general language setting)
HUMAN_FACING_STEPS = {"summarize", "discovery"}

# Steps that write specs (use spec_language setting)
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

    # Check spec steps first
    if step_type in SPEC_STEPS:
        return get_language_instruction(lang_config.spec_language, step_type)

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

    return ""


# Steps explicitly forbidden from issue discovery injection
ISSUE_DISCOVERY_FORBIDDEN_STEPS = {"implement", "test"}

# Default steps that receive issue discovery prompt injection
ISSUE_DISCOVERY_DEFAULT_STEPS = ["summarize"]

# Steps explicitly forbidden from spec names injection
SPEC_NAMES_INJECTION_FORBIDDEN_STEPS = frozenset({"summarize", "commit"})

# Default steps that receive the available-specs names injection.
# Note: deprecated step types (propose/design) are NOT listed here — their
# stub handlers forward to the unified plan_handler, which keys its injection
# on "plan". There is therefore no code path that would lookup "design" or
# "propose" against this whitelist.
SPEC_NAMES_INJECTION_DEFAULT_STEPS = [
    "plan",
    "plan_tasks",
    "implement",
    "verify_spec",
    "update_spec",
    "self_check",
]

# Steps explicitly forbidden from runtime environment injection.
# These are mechanical steps where awareness of read-only se3 history/issue
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
    """Get the se3 runtime environment prompt injection for a step.

    Loads ``src/se3/engine/runtime_environment.md`` and returns its content
    (prefixed with ``\\n\\n``) for whitelisted steps. The whitelist is the
    union of:
      * default list :data:`RUNTIME_ENV_INJECTION_DEFAULT_STEPS`, OR
      * ``runtime_environment_injection.steps`` from ``se3.yaml`` when present
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
    return "\n\n" + body


def get_issue_discovery_injection(step_type: str, project_root: Path) -> str:
    """Get the issue discovery prompt injection for a step.

    Checks if the step is in the whitelist (from se3.yaml config or defaults)
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

    # Read whitelist from the active project YAML (se3.local.yaml when
    # present, otherwise se3.yaml). Routing through load_project_yaml
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


def get_spec_names_injection(
    step_type: str,
    project_root: Path,
    relevant_specs: list[str] | None = None,
) -> str:
    """Get the available-specs names prompt injection for a step.

    Lists all available specs under ``se3/specs/`` and declares which are already
    loaded into the prompt (from ``relevant_specs``), so the LLM can optionally
    read additional specs via the Read tool if the analyze step missed them.

    Args:
        step_type: Current step type name (e.g., "plan", "implement").
        project_root: Project root directory for loading config and specs.
        relevant_specs: Spec names already loaded into the prompt. May be ``None``
            or empty; treated as "no specs loaded" in that case.

    Returns:
        Spec-names injection string to append to prompt, or empty string when the
        step is not in the whitelist.
    """
    # Forbidden steps never get injection — short-circuit before any I/O.
    # This also makes a later re-check unnecessary: yaml cannot re-enable a
    # forbidden step because the early return fires first.
    if step_type in SPEC_NAMES_INJECTION_FORBIDDEN_STEPS:
        return ""

    # Read whitelist from the active project YAML (se3.local.yaml when
    # present, otherwise se3.yaml). Routing through load_project_yaml
    # ensures malformed-local-shadow warnings surface here too rather
    # than depending on some other loader having run first.
    whitelist = SPEC_NAMES_INJECTION_DEFAULT_STEPS
    from ..config import load_project_yaml

    config, _src = load_project_yaml(project_root)
    # Use `or {}` rather than the default arg so that an explicit
    # `spec_names_injection: null` (common when users "disable" a key)
    # falls through to defaults instead of raising AttributeError
    # on the subsequent .get("steps") call.
    section = config.get("spec_names_injection") if isinstance(config, dict) else None
    section = section or {}
    if isinstance(section, dict):
        configured_steps = section.get("steps")
        # Only accept list overrides; silently ignore malformed values
        # (e.g. a bare string / dict from a user typo) which would
        # otherwise turn the `in` check into surprising substring or
        # key-lookup semantics.
        if isinstance(configured_steps, list):
            whitelist = configured_steps

    if step_type not in whitelist:
        return ""

    # Scan the resolved specs dir (se3/specs preferred, specs/ fallback,
    # openspec/specs legacy) so projects using the fallback layout get the
    # correct listing and the prompt points at a real path.
    specs_dir = ContextBuilder._resolve_specs_dir(project_root)
    try:
        specs_rel = specs_dir.relative_to(project_root).as_posix()
    except ValueError:
        specs_rel = specs_dir.as_posix()
    all_spec_names: list[str] = []
    if specs_dir.exists():
        for entry in specs_dir.iterdir():
            if entry.is_dir() and (entry / "spec.md").exists():
                all_spec_names.append(entry.name)
    all_spec_names.sort()

    # Defensive filter: upstream inputs can occasionally contain non-string
    # entries (e.g. dicts from a malformed analyze output). `sorted()` on a
    # mixed-type list would raise TypeError — silently drop non-strings.
    # The `isinstance(list)` guard also prevents a bare string from being
    # iterated character-by-character (yielding bogus per-letter entries).
    if isinstance(relevant_specs, list):
        loaded_spec_names = sorted(s for s in relevant_specs if isinstance(s, str))
    else:
        loaded_spec_names = []
    loaded_display = ", ".join(loaded_spec_names) if loaded_spec_names else "none"
    all_display = ", ".join(all_spec_names) if all_spec_names else "(none found)"

    return (
        "\n\n## Available Specifications\n"
        f"All available specs in this project: {all_display}.\n\n"
        f"Specs already loaded above: {loaded_display}.\n\n"
        "If a spec above is not yet included but you believe it is relevant to "
        "the current task, you MAY read it using the Read tool at "
        f"`{specs_rel}/<name>/spec.md`. Only consult specs that directly help "
        "the task — avoid reading broadly."
    )


def get_read_only_injection(step_type: str) -> str:
    """Get read-only constraint prompt injection for a step.

    Queries STEP_POOL to check if the given step_type is marked as read_only.
    If so, returns a prompt constraint forbidding file modifications.

    Args:
        step_type: Current step type name (e.g., "analyze", "implement")

    Returns:
        Read-only constraint prompt string, or empty string if step is not read-only.
    """
    from .models import STEP_POOL

    # Find matching step in STEP_POOL by name
    is_read_only = False
    for _st, info in STEP_POOL.items():
        if info.get("name") == step_type:
            is_read_only = info.get("read_only", False)
            break

    if not is_read_only:
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

    main_se3 = main_repo_root / "se3"
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
                context_parts.append(f"- `se3/issues/open/{name}`")

    # Active flow state
    state_dir = main_se3 / "state"
    if state_dir.exists():
        state_files = sorted(f.name for f in state_dir.iterdir() if f.is_file())
        if state_files:
            context_parts.append(f"\n### Flow State")
            for name in state_files[:5]:
                context_parts.append(f"- `se3/state/{name}`")

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
            if main_root != worktree_path and (main_root / "se3").exists():
                return main_root
    except Exception:
        pass
    return None


class ContextBuilder:
    """Provides spec directory resolution and spec content loading.

    Used by the analyze and discovery handlers (via ContextBuilder.load_specs_for_step)
    to locate and load specification files from the project's specs directory.
    """

    def __init__(self, project_root: Path):
        """Initialize context builder.

        Args:
            project_root: Project root directory
        """
        self.project_root = Path(project_root)
        self.specs_dir = self._resolve_specs_dir(self.project_root)

    @staticmethod
    def _resolve_specs_dir(project_root: Path) -> Path:
        """Resolve specs directory: se3/specs/ preferred, specs/ fallback, openspec/specs/ legacy."""
        primary = project_root / "se3" / "specs"
        fallback = project_root / "specs"
        legacy = project_root / "openspec" / "specs"
        if primary.exists():
            return primary
        if fallback.exists():
            return fallback
        return legacy

    def _load_spec_content(self, spec_name: str) -> Optional[str]:
        """Load spec content by name.

        Args:
            spec_name: Name of spec (e.g., "flow-engine")

        Returns:
            Spec content or None
        """
        # Try different paths
        paths = [
            self.specs_dir / spec_name / "spec.md",
            self.specs_dir / f"{spec_name}.md",
            self.project_root / spec_name,
            self.project_root / f"{spec_name}.md",
        ]

        for path in paths:
            if path.exists():
                try:
                    return path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Failed to read spec {path}: {e}")

        return None

    def load_specs_for_step(
        self,
        step_type: str,
        selected_items: list[dict[str, str]] | None,
        mode: Literal["items", "full_spec"] = "items",
    ) -> str:
        """Assemble spec text for a step using the item-level loader.

        Delegates to :func:`spec_loader.load_for_step`.  When
        *selected_items* is empty/None, falls back to loading only the
        base spec in full-spec mode.

        Args:
            step_type: Current step type name.
            selected_items: Items selected by the analyze step selector.
            mode: ``"items"`` (default) or ``"full_spec"``.

        Returns:
            Assembled spec text string, ready for LLM prompt injection.
        """
        from .spec_loader import load_for_step, load_full

        if not selected_items:
            # Fallback: load base spec only (full text)
            return load_full(["base"], self.project_root)

        result = load_for_step(
            step_type=step_type,
            selected_items=selected_items,
            project_root=self.project_root,
            mode=mode,
        )
        return result.text


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
