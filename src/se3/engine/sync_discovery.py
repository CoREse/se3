"""SE3 Spec Discovery — Scan codebase for uncovered subsystems and generate new specs.

Identifies functional subsystems not covered by any existing spec and generates
complete spec files for them, matching the project's spec format.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

_EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pycache",
    "se3",
    ".venv",
    "venv",
    ".env",
    "env",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "egg-info",
    ".eggs",
    ".idea",
    ".vscode",
}

_DISCOVERY_PROMPT_TEMPLATE = """\
You are an expert software architect analyzing a codebase to discover functional subsystems \
that are not covered by any existing specification.

## Project Directory Structure

{directory_tree}

## Existing Specifications Summary

{specs_summary}

## Task

Identify functional subsystems in this codebase that are NOT covered by any existing spec. \
A "functional subsystem" is a cohesive group of related files that implement a distinct feature \
or capability (typically 300-1500 lines of related code), matching the granularity of existing specs.

Guidelines:
- Each subsystem should be a coherent feature, not a single file
- Only report subsystems with NO spec coverage (partially covered subsystems are fine)
- Test files, config files, and build scripts are NOT subsystems
- Consider the existing specs' coverage carefully before reporting gaps

You MUST actively read source code files using Read, Grep, and Glob tools to understand \
what each part of the codebase does before making your assessment.

Return a JSON array of discovered subsystems:
{json_schema}
"""

_DISCOVERY_JSON_SCHEMA = """\
[
  {
    "name": "kebab-case-name for the new spec (e.g. 'user-auth', 'data-pipeline')",
    "description": "One paragraph describing what this subsystem does",
    "relevant_files": ["src/path/to/file1.py", "src/path/to/file2.py"]
  }
]

If no uncovered subsystems are found, return an empty array: []"""

_SPEC_GENERATION_PROMPT_TEMPLATE = """\
You are an expert software engineer creating a specification document for an existing code subsystem.

## Subsystem

**Name:** {name}
**Description:** {description}
**Relevant files:** {relevant_files}

## Task

Analyze the actual source code in the relevant files listed above and generate a complete \
specification document that accurately describes the current implementation.

You MUST actively read the source code files using Read, Grep, and Glob tools to understand \
the implementation before writing the spec.

## Output Format

**CRITICAL output rules:**
- Output ONLY the complete spec markdown document itself — no preamble, no \
explanation, no narration, no closing remarks. Your entire response must BE the spec.
- Do NOT create or modify any files. Do NOT use the Write, Edit, or NotebookEdit \
tools. The se3 framework — not you — writes the spec to disk; if you write a file \
yourself it lands in the wrong place. Use Read/Grep/Glob only, to inspect the code.
- The document MUST start with the literal first line `<!-- spec-format: v1 -->`.

Return a complete markdown spec document following this structure:

```
<!-- spec-format: v1 -->
# {name} Specification

## Purpose

<One paragraph describing what this subsystem does>

## Requirements

### Requirement: <Requirement Name>

<Detailed description>

#### Scenario: <Scenario Name>
- **WHEN** <trigger condition>
- **THEN** <expected behavior>
- **AND** <additional assertions>
```

Include all significant requirements and scenarios discovered from the code. \
The spec should accurately reflect the current implementation, not aspirational features.
"""


def _do_delete_spec_dir(spec_dir: Path, spec_name: str) -> None:
    """Delete a spec directory and log the result."""
    try:
        shutil.rmtree(spec_dir)
        logger.info("Deleted obsolete spec directory '%s' at %s", spec_name, spec_dir)
    except OSError as exc:
        logger.error(
            "Failed to delete obsolete spec directory '%s' at %s: %s",
            spec_name, spec_dir, exc,
        )


class SpecDiscovery:
    """Discovers functional subsystems not covered by existing specs."""

    def __init__(
        self,
        project_root: Path,
        llm_caller: Any,
    ) -> None:
        self.project_root = Path(project_root)
        self.llm_caller = llm_caller

    def discover_missing_specs(
        self,
        existing_specs: Dict[str, Any],
        project_root: Optional[Path] = None,
    ) -> List[Dict[str, Any]]:
        """Scan the codebase and identify subsystems not covered by any spec.

        Args:
            existing_specs: Dict mapping spec name to dict with 'name', 'content'.
            project_root: Override project root (defaults to self.project_root).

        Returns:
            List of dicts with 'name', 'description', 'relevant_files' for each
            uncovered subsystem.
        """
        root = Path(project_root) if project_root else self.project_root

        directory_tree = self._get_directory_tree(root)
        specs_summary = self._build_specs_summary(existing_specs)

        prompt = _DISCOVERY_PROMPT_TEMPLATE.format(
            directory_tree=directory_tree,
            specs_summary=specs_summary,
            json_schema=_DISCOVERY_JSON_SCHEMA,
        )

        try:
            response = self.llm_caller.call(
                prompt=prompt,
                json_mode="extract",
            )
            subsystems = json.loads(response)

            if not isinstance(subsystems, list):
                logger.warning("Discovery LLM returned non-list response, ignoring")
                return []

            valid = []
            for item in subsystems:
                if not isinstance(item, dict):
                    continue
                if not item.get("name") or not item.get("description"):
                    continue
                valid.append({
                    "name": item["name"],
                    "description": item["description"],
                    "relevant_files": item.get("relevant_files", []),
                })

            logger.info("Discovered %d uncovered subsystems", len(valid))
            return valid

        except Exception as e:
            logger.error("Spec discovery failed: %s", e)
            return []

    def generate_spec_for_subsystem(
        self,
        subsystem: Dict[str, Any],
    ) -> Optional[Path]:
        """Generate a complete spec file for an uncovered subsystem.

        Args:
            subsystem: Dict with 'name', 'description', 'relevant_files'.

        Returns:
            Path to the created spec.md file, or None on failure.
        """
        name = subsystem["name"]
        description = subsystem["description"]
        relevant_files = subsystem.get("relevant_files", [])

        prompt = _SPEC_GENERATION_PROMPT_TEMPLATE.format(
            name=name,
            description=description,
            relevant_files=", ".join(relevant_files) if relevant_files else "(none listed)",
        )

        # New specs are written by this path — honor spec_language so the
        # generated spec body is in the configured language. No-op when unset.
        from .context_builder import get_spec_language_instruction
        prompt += get_spec_language_instruction(self.project_root)

        try:
            spec_content = self.llm_caller.call(
                prompt=prompt,
                json_mode="off",
            )
            spec_content = spec_content.strip()

            from .sync_engine import strip_markdown_fences
            spec_content = strip_markdown_fences(spec_content)

            from .spec_validator import (
                V1_MARKER,
                extract_spec_body,
                validate_spec_structure,
            )

            # Purify the agentic output: an off-mode call returns the full
            # sub-agent stream (narrative preamble + tool process + the spec
            # body at the tail). Slice out the spec document from its first
            # structural anchor so the validation/write path below sees a
            # clean spec body rather than leading prose.
            spec_content = extract_spec_body(spec_content, name)

            # Auto-prepend the v1 marker if the LLM forgot it — AFTER
            # extraction, so the marker attaches to the spec body and not to
            # discarded narrative. The validator below still enforces every
            # other structural rule, so an LLM that returned only a meta
            # summary (no anchor to slice on) is still rejected.
            if spec_content and not spec_content.lstrip().startswith(V1_MARKER):
                spec_content = f"{V1_MARKER}\n{spec_content}"

            validation = validate_spec_structure(spec_content, name)
            if not validation.passed:
                logger.warning(
                    "Generated spec '%s' failed structural validation; "
                    "discarding LLM output.",
                    name,
                )
                for err in validation.errors:
                    logger.warning("  spec '%s' validation error: %s", name, err)
                return None

            spec_dir = self.project_root / "se3" / "specs" / name
            spec_dir.mkdir(parents=True, exist_ok=True)
            spec_path = spec_dir / "spec.md"
            spec_path.write_text(spec_content, encoding="utf-8")

            logger.info("Created new spec '%s' at %s", name, spec_path)
            return spec_path

        except Exception as e:
            logger.error("Failed to generate spec for '%s': %s", name, e)
            return None

    @staticmethod
    def delete_obsolete_specs(
        project_root: Path,
        obsolete_specs: List[str],
        confirm: bool = False,
    ) -> Dict[str, Any]:
        """Delete obsolete spec directories after convergence.

        Args:
            project_root: Project root directory.
            obsolete_specs: List of spec names to delete.
            confirm: If True, prompt user interactively for each spec.
                If False, delete all directly without interaction.

        Returns:
            Dict with keys ``deleted`` (list of spec names deleted) and
            ``kept`` (list of spec names kept/not deleted).
        """
        deleted: List[str] = []
        kept: List[str] = []

        for spec_name in sorted(obsolete_specs):
            spec_dir = project_root / "se3" / "specs" / spec_name
            if not spec_dir.is_dir():
                logger.debug(
                    "Obsolete spec dir '%s' not found, skipping deletion", spec_name
                )
                kept.append(spec_name)
                continue

            if confirm:
                try:
                    answer = input(
                        f"Delete obsolete spec '{spec_name}'? "
                        f"[y/N]: "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    logger.info(
                        "User interrupted during obsolete spec confirmation; "
                        "keeping '%s'", spec_name
                    )
                    kept.append(spec_name)
                    continue

                if answer in ("y", "yes"):
                    _do_delete_spec_dir(spec_dir, spec_name)
                    deleted.append(spec_name)
                else:
                    logger.info("User chose to keep obsolete spec '%s'", spec_name)
                    kept.append(spec_name)
            else:
                _do_delete_spec_dir(spec_dir, spec_name)
                deleted.append(spec_name)

        return {"deleted": deleted, "kept": kept}

    def _get_directory_tree(self, root: Path) -> str:
        """Get a filtered directory tree of the project.

        Uses git ls-files when available for accurate file listing,
        falls back to filesystem walk with exclusion filters.
        """
        try:
            result = subprocess.run(
                ["git", "ls-files"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return self._tree_from_git_files(result.stdout.strip().splitlines())
        except Exception:
            pass

        return self._tree_from_walk(root)

    def _tree_from_git_files(self, files: List[str]) -> str:
        """Build a directory tree string from git ls-files output."""
        filtered = []
        for f in files:
            parts = Path(f).parts
            if any(p in _EXCLUDED_DIRS for p in parts):
                continue
            filtered.append(f)

        dirs: Dict[str, List[str]] = {}
        for f in filtered:
            p = Path(f)
            parent = str(p.parent) if str(p.parent) != "." else "."
            dirs.setdefault(parent, []).append(p.name)

        lines = []
        for d in sorted(dirs.keys()):
            lines.append(f"{d}/")
            for fname in sorted(dirs[d]):
                lines.append(f"  {fname}")

        return "\n".join(lines)

    def _tree_from_walk(self, root: Path) -> str:
        """Build a directory tree by walking the filesystem."""
        lines = []
        for dirpath in sorted(root.rglob("*")):
            if not dirpath.is_dir():
                continue
            rel = dirpath.relative_to(root)
            parts = rel.parts
            if any(p in _EXCLUDED_DIRS or p.endswith(".egg-info") for p in parts):
                continue
            children = sorted(
                f.name for f in dirpath.iterdir()
                if f.is_file() and not f.name.startswith(".")
            )
            if children:
                lines.append(f"{rel}/")
                for c in children:
                    lines.append(f"  {c}")

        return "\n".join(lines)

    def _build_specs_summary(self, existing_specs: Dict[str, Any]) -> str:
        """Build a compact summary of existing specs.

        For each spec, includes the Purpose paragraph and Requirements heading list.
        """
        if not existing_specs:
            return "(no existing specs)"

        parts = []
        for name, spec_info in sorted(existing_specs.items()):
            content = spec_info.get("content", "")
            summary = self._extract_spec_summary(name, content)
            parts.append(summary)

        return "\n\n".join(parts)

    def _extract_spec_summary(self, name: str, content: str) -> str:
        """Extract Purpose paragraph and Requirement titles from a spec."""
        lines = content.split("\n")

        purpose = ""
        requirements: List[str] = []

        in_purpose = False
        for line in lines:
            stripped = line.strip()

            if stripped == "## Purpose":
                in_purpose = True
                continue

            if in_purpose:
                if stripped.startswith("## "):
                    in_purpose = False
                elif stripped:
                    if not purpose:
                        purpose = stripped
                    else:
                        purpose += " " + stripped

            if stripped.startswith("### Requirement:"):
                req_title = stripped.replace("### Requirement:", "").strip()
                requirements.append(req_title)

        summary = f"### {name}\n"
        if purpose:
            summary += f"Purpose: {purpose}\n"
        if requirements:
            summary += "Requirements: " + ", ".join(requirements)
        else:
            summary += "Requirements: (none found)"

        return summary
