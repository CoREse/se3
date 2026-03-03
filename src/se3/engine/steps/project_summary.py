"""Project Summary step handler.

Generates a concise project context summary using LLM.
The summary is passed to all subsequent LLM steps to provide
global project awareness.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..project_context import ProjectContextCollector
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


PROJECT_SUMMARY_PROMPT = """You are a project context summarizer for the SE3 flow engine.

Given the raw project data below, produce a concise project context summary (max 300 words) that a downstream LLM step can use to understand the project state at a glance.

Include:
1. **Current phase** — what stage is the project in?
2. **Recent progress** — what was done recently (last few commits)?
3. **Active work** — any in-progress flows or uncommitted changes?
4. **Key constraints** — anything important to keep in mind?
5. **Backlog highlights** — what's planned next?

## Raw Project Data

### Git Status
Branch: {branch}
Uncommitted changes: {uncommitted_count}
Recent commits:
{recent_commits}

### Flow Engine
{flow_engine_summary}

### Backlog Items
{backlog_summary}

### Available Specs
{specs_list}

Respond in JSON format:
{{
    "summary": "The concise project context summary text"
}}
"""


def generate_project_summary(
    project_root: Path,
    flow_id: str | None = None,
    step_id: str | None = None,
    step_type: str | None = None,
) -> str:
    """Generate a project summary using ProjectContextCollector + LLM.

    This is the shared core logic used by both the PROJECT_SUMMARY step
    and the `se3 summary` CLI command.

    Args:
        project_root: Project root directory
        flow_id: Optional flow ID for chat history tracking
        step_id: Optional step ID for chat history tracking
        step_type: Optional step type for chat history tracking

    Returns:
        Project summary string

    Raises:
        Exception: If LLM call fails after retries
    """
    collector = ProjectContextCollector(project_root)
    raw = collector.collect()

    # Format raw data for prompt
    git = raw.get("git", {})
    branch = git.get("branch", "unknown")
    uncommitted_count = git.get("uncommitted_count", 0)
    recent_commits = "\n".join(f"  - {c}" for c in git.get("last_commits", [])) or "  (none)"

    flow_engine = raw.get("flow_engine")
    if flow_engine:
        active = flow_engine.get("active_flows", [])
        completed = flow_engine.get("completed_flows", [])
        failed = flow_engine.get("failed_flows", [])
        flow_engine_summary = (
            f"Total flows: {flow_engine.get('total_flows', 0)}\n"
            f"Active: {len(active)}, Completed: {len(completed)}, Failed: {len(failed)}"
        )
        for f in active:
            flow_engine_summary += f"\n  Active: {f.get('description', 'unknown')}"
    else:
        flow_engine_summary = "Not initialized"

    backlog = raw.get("backlog", [])
    if backlog:
        backlog_summary = "\n".join(
            f"  - [{item.get('status', '?')}] {item.get('title', item.get('slug', '?'))}"
            + (f" (Phase: {item['phase']})" if item.get("phase") else "")
            for item in backlog
        )
    else:
        backlog_summary = "  (none)"

    specs = raw.get("specs", [])
    specs_list = ", ".join(specs) if specs else "(none)"

    prompt = PROJECT_SUMMARY_PROMPT.format(
        branch=branch,
        uncommitted_count=uncommitted_count,
        recent_commits=recent_commits,
        flow_engine_summary=flow_engine_summary,
        backlog_summary=backlog_summary,
        specs_list=specs_list,
    )

    caller = LLMCaller(project_root, flow_id=flow_id, step_id=step_id, step_type=step_type)
    response = caller.call(
        prompt=prompt,
        json_mode="extract",
        json_schema_hint='{"summary": "..."}',
    )

    result = parse_json_response(response, required_keys=["summary"])
    if result:
        return result["summary"]

    # Fallback: return raw response text if JSON parsing fails
    return response


def project_summary_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the project_summary step.

    Collects project context and generates an LLM summary.
    Outputs `project_summary` for downstream steps.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    logger.info("Generating project summary...")

    try:
        summary = generate_project_summary(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
        )

        step.outputs["project_summary"] = summary

        # Also store in flow context for easy access by later steps
        flow.state.context["project_summary"] = summary

        logger.info(f"Project summary generated ({len(summary)} chars)")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Project summary step failed")
        step.error_message = f"Project summary generation failed: {str(e)}"
        return StepStatus.FAILED
