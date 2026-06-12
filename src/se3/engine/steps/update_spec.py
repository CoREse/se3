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
from ..prompt_markers import inject_boundary
from ..spec_governance import BASE_ADMISSION_STANDARD
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


UPDATE_SPEC_PROMPT = """You are an expert technical writer. Update the project specifications to reflect the changes made.
{redo_guidance}
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

## Spec Access Protocol (index-first — do NOT read whole specs or the index cache)

Obtain spec context through the read-only `se3 spec` index commands. You MUST
NOT, for the purpose of gathering context, read an entire large spec file with
the Read tool, and you MUST NOT read the index cache file
`se3/cache/spec-index.json` directly — it is an internal, program-maintained
format, not an LLM-facing surface.

- `se3 spec index` — root view: every spec's name, a one-sentence locator, and item count. Start here.
- `se3 spec index <spec> [<group>...]` — drill into one spec's Requirement index (id / title / summary / tags); trailing group-path components open a folded domain group or a `pN` page.
- `se3 spec show <spec>::<requirement>` — the authoritative body of ONE Requirement plus its physical location (file path + 1-based inclusive line range).

**Directed edit of an existing Requirement (no whole-file reads):** to modify a
Requirement that already exists, FIRST run `se3 spec show <spec>::<requirement>`
to obtain its physical location (file path + line range), THEN Read ONLY that
line range and Edit it in place. Never read the whole spec file for a localized
change.

{base_admission_standard}

## Instructions
1. Use the index-first protocol above to locate the spec(s) and Requirement(s) that need updating — `se3 spec index` to navigate, `se3 spec show` to read a specific Requirement's body and its physical location. Do NOT read whole spec files or the index cache.
2. If Spec Change Guidance is provided above, use it as your primary checklist for updates — execute each declared change intent (add, modify, deprecate) in the corresponding spec files. This is your guided mode.
3. If no Spec Change Guidance is available, determine which specs need updating by analyzing the changes made and verification results. This is the inference mode.
4. Use the Edit tool to directly modify the spec files — for an existing Requirement, do a directed Read+Edit on the physical line range returned by `se3 spec show`, never an integral read of the whole file. Follow existing formatting conventions.
5. Only update specs that genuinely need changes — do not rewrite specs unnecessarily.
6. Follow spec guardrails: do NOT delete existing requirements, only add or modify.
7. If Design Context is provided, use it to understand the architectural rationale behind changes — this helps produce more accurate and well-motivated spec updates.
8. Respect the base Spec Admission Standard above: when adding content would push the `base` spec over its size limit, route that content into the corresponding module spec rather than appending it to `base`.

## New Spec Decision (Mandatory)

Consult the root index view appended below (the same view as `se3 spec index`),
which lists every existing spec with a one-sentence locator, to check for naming
collisions and to decide whether the new content belongs in an existing spec.

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
- Add `<!-- spec-format: v1 -->` as the first line.
- Immediately after the format marker, add a `<!-- domain: <layered/path> -->` header marker declaring the new spec's domain — a slash-separated classification path (e.g. `engine/steps`, `server/auth`) that places the spec above the spec level so the root index can group it. Choose the domain from where the subsystem lives relative to the existing specs in the root view.
- Open `## Purpose` with a one-sentence locator stating, in a single line, what the spec is about (the root view shows each spec's name plus this locator).
- Include `## Requirements` with at least one `### Requirement: <name>`, and `#### Scenario:` blocks.

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

# Two-segment marker only: USER_CONTENT region is empty.
# update_spec consumes upstream artifacts (changes_made / verification_result /
# spec_changes / design_doc / spec_content); no user-literal field is
# appended here. The web console renders the whole post-BEGIN tail inside
# the collapsed system-prompt chip.
UPDATE_SPEC_PROMPT = inject_boundary(UPDATE_SPEC_PROMPT, "## Task Description\n")


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

    # Mechanism A — SPEC_GATE redo inputs. When the SPEC_GATE artifact check
    # rejects this flow's spec edit (a deleted requirement or a structurally
    # invalid spec), the state machine routes back here with the gate's
    # diagnosis. Without surfacing it into the prompt the redo would re-issue
    # an identical LLM call and re-read the already-broken spec from disk,
    # never repairing the rejected artifact (the loop then just burns the
    # shared fix-iteration budget until exhaustion). Inject the diagnosis so
    # the redo knows WHICH requirement was dropped / WHICH rule failed.
    is_spec_redo = bool(step.inputs.get("is_spec_redo", False))
    redo_fix_instructions = step.inputs.get("fix_instructions", "")
    redo_fix_context = step.inputs.get("fix_context", {})

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
    redo_guidance_text = _format_redo_guidance(
        is_spec_redo, redo_fix_instructions, redo_fix_context
    )

    # Build prompt
    prompt = UPDATE_SPEC_PROMPT.format(
        task_description=task_description,
        changes_made=changes_text,
        verification_result=verification_text,
        spec_changes=spec_changes_text,
        design_doc=design_doc_text,
        specs_dir=specs_dir,
        redo_guidance=redo_guidance_text,
        base_admission_standard=BASE_ADMISSION_STANDARD,
    )

    # Append language instruction if configured
    from ..context_builder import (
        get_step_language_instruction,
        get_issue_discovery_injection,
        get_runtime_environment_injection,
    )
    lang_instruction = get_step_language_instruction("update_spec", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("update_spec", project_root)
    if injection:
        prompt += injection

    # Append the root index view (name + one-sentence locator + item count) for
    # the New Spec Decision step. This replaces the former plain spec-names list
    # (get_spec_names_injection): the root view is produced by the SAME renderer
    # as `se3 spec index`, so the LLM sees a consistent navigation surface and
    # can decide placement / detect naming collisions without reading whole spec
    # files or the internal index cache.
    root_view = _build_root_view_injection(project_root)
    if root_view:
        prompt += root_view

    # Append runtime environment injection if applicable
    runtime_env = get_runtime_environment_injection("update_spec", project_root)
    if runtime_env:
        prompt += runtime_env

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


def _build_root_view_injection(project_root: Path) -> str:
    """Render the spec-index root view for the New Spec Decision step.

    Returns a self-describing section — the root view (every spec's name, a
    one-sentence locator, and item count) produced by the SAME renderer as
    ``se3 spec index`` — plus a one-line reminder of the drill / read commands.

    The whole computation is read-only and never invokes the LLM. Any failure
    (no specs yet, index build error, import problem) degrades to an empty
    string so the step is never broken merely because the navigation aid could
    not be assembled.
    """
    try:
        from ..spec_index import load_or_build
        from ..spec_index_render import render_index
        from ...config import load_spec_governance_config

        index = load_or_build(project_root)
        threshold = load_spec_governance_config(project_root).index_render_threshold
        root_view = render_index(index, threshold=threshold)
        if not root_view or not root_view.strip():
            return ""
        return (
            "\n\n## Existing Specs — Root Index View\n\n"
            + root_view.rstrip("\n")
            + "\n\nDrill in with `se3 spec index <spec> [<group>...]` and read a "
            "single Requirement's body + physical location with "
            "`se3 spec show <spec>::<requirement>`. Do NOT read whole spec files "
            "or `se3/cache/spec-index.json`."
        )
    except Exception:
        logger.debug("Failed to build root-view injection for update_spec", exc_info=True)
        return ""


def _format_redo_guidance(
    is_spec_redo: bool,
    fix_instructions: str,
    fix_context: dict[str, Any],
) -> str:
    """Format the SPEC_GATE redo guidance section for the prompt.

    Returns an empty string for a normal (first-pass) update_spec run. On a
    redo (``is_spec_redo`` True) it returns a prominent block carrying the
    gate's ``fix_instructions`` plus the concrete spec errors / edited / new
    spec names from ``fix_context`` so the LLM repairs the SPECIFIC rejected
    artifact instead of re-issuing an identical update.

    The block is deliberately emphatic and placed at the top of the prompt
    (framework-prefix region) because the on-disk spec the LLM is about to
    re-read is the already-broken starting point: a no-change redo would leave
    the deletion / structural violation in place and the gate would reject it
    again on every iteration.
    """
    if not is_spec_redo:
        return ""

    if not isinstance(fix_context, dict):
        fix_context = {}

    lines: list[str] = [
        "",
        "## ⚠️ SPEC REDO — PREVIOUS update_spec WAS REJECTED",
        "",
        "Your previous spec edit did NOT pass the SPEC_GATE artifact check and "
        "must be redone. The spec file(s) currently on disk are the REJECTED, "
        "already-broken result of that edit — re-reading them is your starting "
        "point, but you MUST actively repair the problems below. Simply re-saving "
        "the current (broken) content will be rejected again.",
        "",
    ]

    instructions = (fix_instructions or "").strip()
    if instructions:
        lines.append("### Why it was rejected")
        lines.append(instructions)
        lines.append("")

    spec_errors = fix_context.get("spec_errors") or []
    if isinstance(spec_errors, list) and spec_errors:
        lines.append("### Specific problems to fix")
        for err in spec_errors:
            lines.append(f"- {err}")
        lines.append("")

    edited_specs = fix_context.get("edited_specs") or []
    new_specs = fix_context.get("new_specs") or []
    if isinstance(edited_specs, list) and edited_specs:
        lines.append(f"- Edited specs flagged: {', '.join(str(s) for s in edited_specs)}")
    if isinstance(new_specs, list) and new_specs:
        lines.append(f"- New specs flagged: {', '.join(str(s) for s in new_specs)}")
    if (isinstance(edited_specs, list) and edited_specs) or (
        isinstance(new_specs, list) and new_specs
    ):
        lines.append("")

    lines.append(
        "### Required actions\n"
        "1. Open each flagged spec and RESTORE any '### Requirement:' heading "
        "that was deleted relative to the version before this flow began — the "
        "se3 spec guardrails forbid deleting or weakening a requirement.\n"
        "2. Fix every structural violation named above so each spec conforms to "
        "spec-format v1 (v1 marker first line, '# <name> Specification' title, "
        "'## Purpose' section with content, at least one '### Requirement:', no "
        "narrative first line).\n"
        "3. Re-apply the intended spec update WITHOUT dropping or weakening any "
        "pre-existing requirement. Use the Edit tool to make these repairs; do "
        "NOT leave the spec in its current rejected state."
    )
    lines.append("")
    return "\n".join(lines)


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
