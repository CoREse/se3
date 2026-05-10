"""Update Spec step handler.

Updates specifications to reflect the changes made.
Uses LLM to generate spec updates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


UPDATE_SPEC_PROMPT = """You are an expert technical writer. Update the project specifications to reflect the changes made.

## Task Description
{task_description}

## Changes Made
{changes_made}

## Verification Results
{verification_result}

## Spec Change Guidance
{spec_changes}

## Design Context
{design_doc}

## Specs Directory
{specs_dir}

## Instructions
1. Read the relevant spec files in the specs directory using the Read tool.
2. If Spec Change Guidance is provided above, use it as your primary checklist for updates — execute each declared change intent (add, modify, deprecate) in the corresponding spec files. This is your guided mode.
3. If no Spec Change Guidance is available, determine which specs need updating by analyzing the changes made and verification results. This is the inference mode.
4. Use the Edit tool to directly modify the spec files. Follow existing formatting conventions.
5. Only update specs that genuinely need changes — do not rewrite specs unnecessarily.
6. Follow spec guardrails: do NOT delete existing requirements, only add or modify.
7. If Design Context is provided, use it to understand the architectural rationale behind changes — this helps produce more accurate and well-motivated spec updates.

## New Spec Decision (Mandatory)

Before appending ANY new Requirement to an existing spec, you MUST evaluate the following four criteria. ALL four must pass to append; if ANY fails, create a new spec.

1. **Conceptual Independence** — The new content shares the same conceptual domain as the existing spec. It is about the same subsystem, mechanism, or abstraction level. If the content introduces a fundamentally different concept (e.g., "how to format JSON" into a spec about "error handling patterns"), it fails this test.

2. **Dependency Direction** — The new content does NOT cause existing Requirements in the spec to depend on it. If adding the Requirement would force older Requirements to reference or assume the new behavior (e.g., an existing "Retry Logic" Requirement now needs to know about a new "Circuit Breaker" Requirement), the dependency direction is wrong and a new spec is needed.

3. **Naming Test** — The new Requirement can be naturally named under the existing spec's title. A reader encountering the Requirement name should not be surprised to find it in this spec. If the name feels like it belongs in a different category, it fails this test.

4. **Cross-Scenario Reusability** — The new content is NOT expected to be referenced by multiple unrelated capabilities. If the content is a cross-cutting concern (e.g., "Authentication", "Configuration Format", "Versioning Rules") that multiple specs will need to cite, it should be its own spec to avoid circular references and provide a single source of truth.

**Decision rule:**
- If ALL four criteria pass → **append** the new Requirement to the existing spec.
- If ANY criterion fails → **create a new spec** at `se3/specs/<new_name>/spec.md` with standard structure (Purpose, Requirements, Scenarios).

When creating a new spec:
- Choose a concise, kebab-case directory name (e.g., `issue-discovery`, `test-runner`).
- Include `## Purpose`, `## Requirements` with at least one `### Requirement: <name>`, and `#### Scenario:` blocks.
- Add `<!-- spec-format: v1 -->` as the first line.

When you are done, output a JSON summary:
```json
{{
    "specs_updated": [
        {{
            "spec_name": "name-of-spec",
            "change_description": "What was changed and why"
        }}
    ],
    "new_capabilities": ["capability1", "capability2"],
    "spec_decisions": [
        {{
            "requirement_name": "Name of the new requirement",
            "decision": "append|new_spec",
            "target_spec": "spec-name where requirement was placed",
            "reasoning": "Brief justification referencing which criteria passed/failed"
        }}
    ],
    "notes": "Any additional notes"
}}
```

- `spec_decisions` is REQUIRED whenever you add a new Requirement. If you only modified existing Requirements, return an empty array.
- If no spec updates are needed at all, return empty arrays for both `specs_updated` and `spec_decisions`.
"""


def update_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the update_spec step.

    Updates spec files to reflect changes made during implementation.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    changes_made = step.inputs.get("changes_made", {})
    verification_result = step.inputs.get("verification_result", {})
    spec_changes = step.inputs.get("spec_changes", [])
    design_doc = step.inputs.get("design_doc", {})

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Resolve specs directory for LLM tool access
    from ..context_builder import ContextBuilder
    builder = ContextBuilder(project_root)
    specs_dir = str(builder.specs_dir.resolve())

    # Format inputs
    changes_text = _format_changes(changes_made)
    verification_text = _format_verification(verification_result)
    spec_changes_text = _format_spec_changes(spec_changes)
    design_doc_text = _format_design_doc(design_doc)

    # Build prompt
    prompt = UPDATE_SPEC_PROMPT.format(
        task_description=task_description,
        changes_made=changes_text,
        verification_result=verification_text,
        spec_changes=spec_changes_text,
        design_doc=design_doc_text,
        specs_dir=specs_dir,
    )

    # Append language instruction if configured
    from ..context_builder import (
        get_step_language_instruction,
        get_issue_discovery_injection,
        get_spec_names_injection,
    )
    lang_instruction = get_step_language_instruction("update_spec", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("update_spec", project_root)
    if injection:
        prompt += injection

    # Append available-specs names injection if applicable
    spec_names = get_spec_names_injection(
        "update_spec", project_root, step.inputs.get("relevant_specs"),
    )
    if spec_names:
        prompt += spec_names

    logger.info("Updating specs to reflect implementation...")

    try:
        # Call LLM with tool access (TWO_PHASE) so it can read and edit spec files
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"specs_updated": [{"spec_name": "...", "change_description": "..."}], "new_capabilities": [], "spec_decisions": [{"requirement_name": "...", "decision": "append|new_spec", "target_spec": "...", "reasoning": "..."}], "notes": "..."}',
        )

        # Parse JSON response
        update_result = parse_json_response(response, required_keys=[])

        if not update_result:
            logger.warning("Could not parse update_spec summary, using defaults")
            update_result = {"specs_updated": [], "new_capabilities": []}

        # Store outputs
        specs_updated = update_result.get("specs_updated", [])
        step.outputs["updated_specs"] = specs_updated
        step.outputs["new_capabilities"] = update_result.get("new_capabilities", [])
        step.outputs["spec_decisions"] = update_result.get("spec_decisions", [])

        if specs_updated:
            logger.info(f"Specs updated: {len(specs_updated)}")
            for spec in specs_updated:
                logger.info(f"  - {spec.get('spec_name', '?')}: {spec.get('change_description', '')}")
        else:
            logger.info("No spec updates needed")

        # Log spec decisions if present
        spec_decisions = step.outputs["spec_decisions"]
        if spec_decisions:
            logger.info(f"Spec decisions recorded: {len(spec_decisions)}")
            for dec in spec_decisions:
                logger.info(
                    f"  - {dec.get('requirement_name', '?')}: {dec.get('decision', '?')} → {dec.get('target_spec', '?')}"
                )

        # Rebuild spec index for touched specs so the next load picks up changes.
        # rebuild_for() re-parses the on-disk file and updates the in-memory
        # index, so save() persists the fresh data (not a stale snapshot).
        try:
            from ...engine.spec_index import SpecIndex
            from ...engine.spec_format import parse_spec

            try:
                import fcntl as _fcntl
            except ImportError:
                _fcntl = None

            # Acquire advisory lock BEFORE loading the index so the entire
            # load → rebuild → verify → save sequence is atomic. This prevents
            # a race where two processes load stale data, then the second
            # writer overwrites the first writer's fresher index.
            lock_file = (project_root / "se3" / "cache" / "spec-index.json.lock")
            lock_acquired = False
            # Pre-derive touched_specs from prior outputs so the OSError fallback
            # below has a defined value even if open(lock_file) or flock() fails
            # before line where it would otherwise be assigned inside the with block.
            touched_specs = {s.get("spec_name", "") for s in specs_updated}
            touched_specs.update(d.get("target_spec", "") for d in spec_decisions)
            try:
                with open(lock_file, "w") as lock_fd:
                    if _fcntl is not None:
                        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
                        lock_acquired = True

                    # Direct instantiation to avoid reentrant flock deadlock:
                    # load_or_build() would try to acquire the same lock again.
                    index = SpecIndex(project_root)
                    if not index.load():
                        index.build()
                    for spec_name in touched_specs:
                        if spec_name:
                            index.rebuild_for(spec_name)

                    # Verify new-spec creations actually materialised on disk.
                    # LLMs can hallucinate target_spec names; catch drift early.
                    builder = ContextBuilder(project_root)
                    for dec in spec_decisions:
                        if dec.get("decision") == "new_spec":
                            target = dec.get("target_spec", "")
                            if target:
                                spec_file = builder.specs_dir / target / "spec.md"
                                if not spec_file.exists():
                                    logger.error(
                                        "New spec '%s' was declared but file does not exist: %s",
                                        target, spec_file,
                                    )
                                    step.error_message = (
                                        f"Spec update failed: declared new spec '{target}' "
                                        f"but {spec_file} does not exist."
                                    )
                                    if lock_acquired and _fcntl is not None:
                                        try:
                                            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                                        except OSError:
                                            pass
                                    return StepStatus.FAILED

                                # Strengthened check: require at least one
                                # Requirement and a non-empty header.
                                try:
                                    parsed = parse_spec(spec_file.read_text(encoding="utf-8"))
                                except Exception as exc:
                                    logger.error(
                                        "New spec '%s' exists but cannot be parsed: %s",
                                        target, exc,
                                    )
                                    step.error_message = (
                                        f"Spec update failed: declared new spec '{target}' "
                                        f"exists but is unparsable."
                                    )
                                    if lock_acquired and _fcntl is not None:
                                        try:
                                            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                                        except OSError:
                                            pass
                                    return StepStatus.FAILED

                                if not parsed.requirements:
                                    logger.error(
                                        "New spec '%s' has no Requirements — "
                                        "it may be empty or structurally invalid.",
                                        target,
                                    )
                                    step.error_message = (
                                        f"Spec update failed: declared new spec '{target}' "
                                        f"has no Requirements."
                                    )
                                    if lock_acquired and _fcntl is not None:
                                        try:
                                            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                                        except OSError:
                                            pass
                                    return StepStatus.FAILED

                                if not parsed.header_text or len(parsed.header_text.strip()) < 10:
                                    logger.error(
                                        "New spec '%s' has an empty or very short header.",
                                        target,
                                    )
                                    step.error_message = (
                                        f"Spec update failed: declared new spec '{target}' "
                                        f"has an empty or very short header."
                                    )
                                    if lock_acquired and _fcntl is not None:
                                        try:
                                            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                                        except OSError:
                                            pass
                                    return StepStatus.FAILED

                    index.save()
                    if lock_acquired and _fcntl is not None:
                        try:
                            _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                        except OSError:
                            pass
            except OSError:
                # Fallback: load/rebuild/save without lock
                logger.warning(
                    "File lock not available; rebuilding spec index without coordination."
                )
                # Direct instantiation — no lock is held here, but stay consistent.
                index = SpecIndex(project_root)
                if not index.load():
                    index.build()
                for spec_name in touched_specs:
                    if spec_name:
                        index.rebuild_for(spec_name)
                index.save()
                if lock_acquired and _fcntl is not None:
                    try:
                        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                    except OSError:
                        pass
        except Exception:
            logger.warning("Failed to rebuild spec index after update_spec", exc_info=True)

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Update spec step failed")
        step.error_message = f"Spec update failed: {str(e)}"
        return StepStatus.FAILED


def _format_spec_changes(spec_changes: list[dict[str, Any]]) -> str:
    """Format spec_changes list into readable text for the prompt."""
    if not spec_changes:
        return "No specific spec changes planned."

    lines = []
    for change in spec_changes:
        spec_name = change.get("spec_name", "unknown")
        change_type = change.get("change_type", "unknown")
        target = change.get("target", "")
        description = change.get("description", "")
        rationale = change.get("rationale", "")

        lines.append(f"- [{change_type}] {spec_name}: {target}")
        if description:
            lines.append(f"  Description: {description}")
        if rationale:
            lines.append(f"  Rationale: {rationale}")

    return "\n".join(lines)


def _format_design_doc(design_doc: dict[str, Any]) -> str:
    """Format design_doc dict into readable text for the prompt."""
    if not design_doc:
        return "No design document available."

    lines = []

    overview = design_doc.get("overview", "")
    if overview:
        lines.append(f"### Overview\n{overview}")

    components = design_doc.get("components", [])
    if components:
        lines.append("\n### Components")
        for comp in components:
            if isinstance(comp, str):
                lines.append(f"- {comp}")
                continue
            name = comp.get("component", comp.get("name", "unknown"))
            resp = comp.get("responsibilities", comp.get("description", ""))
            lines.append(f"- **{name}**: {resp}")

    decisions = design_doc.get("architecture_decisions", [])
    if decisions:
        lines.append("\n### Architecture Decisions")
        for dec in decisions:
            if isinstance(dec, str):
                lines.append(f"- **{dec}**")
                continue
            decision = dec.get("decision", "")
            rationale = dec.get("rationale", "")
            lines.append(f"- **{decision}**")
            if rationale:
                lines.append(f"  Rationale: {rationale}")

    return "\n".join(lines) if lines else "No design document available."


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Format changes for inclusion in prompt."""
    if not changes_made:
        return "No changes recorded."

    lines = []
    files_changed = changes_made.get("files_changed", [])
    for file_change in files_changed:
        if isinstance(file_change, str):
            # implement step may output plain file paths
            lines.append(f"- modified: {file_change}")
        elif isinstance(file_change, dict):
            path = file_change.get("path", "?")
            action = file_change.get("action", "?")
            explanation = file_change.get("explanation", "")
            lines.append(f"- {action}: {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {file_change}")

    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _format_verification(verification_result: dict[str, Any]) -> str:
    """Format verification results for inclusion in prompt."""
    if not verification_result:
        return "No verification results available."

    verified = verification_result.get("verified", False)
    summary = verification_result.get("summary", "")

    lines = [f"Verification passed: {verified}"]
    if summary:
        lines.append(f"Summary: {summary}")

    return "\n".join(lines)
