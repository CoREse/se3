"""Context builder for automatic context collection.

Automatically gathers relevant context from specs, previous outputs,
project state, and code for LLM calls.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Builds context for flow engine steps.

    Automatically collects:
    - Relevant specifications
    - Outputs from previous steps
    - Current project state (git, files)
    - Code context based on task
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

    def build_step_context(
        self,
        step_type: str,
        task_description: str,
        previous_outputs: Optional[Dict[str, Any]] = None,
        relevant_specs: Optional[List[str]] = None,
    ) -> str:
        """Build full context string for a step.

        Args:
            step_type: Current step type (e.g., "analyze", "implement")
            task_description: Original task description
            previous_outputs: Outputs from completed steps
            relevant_specs: List of spec names to include

        Returns:
            Formatted context string for LLM prompt
        """
        sections = [
            self._build_header(step_type, task_description),
            self._build_specs_section(relevant_specs),
            self._build_previous_outputs_section(previous_outputs),
            self._build_project_state_section(),
        ]

        return "\n\n".join(filter(None, sections))

    def _build_header(self, step_type: str, task_description: str) -> str:
        """Build context header."""
        return f"""# Context for {step_type.upper()} Step

## Task Description
{task_description}
"""

    def _build_specs_section(self, spec_names: Optional[List[str]]) -> Optional[str]:
        """Build specifications section."""
        if not spec_names:
            return None

        lines = ["## Relevant Specifications", ""]

        for spec_name in spec_names:
            content = self._load_spec_content(spec_name)
            if content:
                lines.append(f"### {spec_name}")
                lines.append("")
                # Truncate very long specs
                if len(content) > 10000:
                    content = content[:10000] + "\n... [truncated]"
                lines.append(content)
                lines.append("")

        return "\n".join(lines) if len(lines) > 2 else None

    def _build_previous_outputs_section(
        self, outputs: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """Build previous outputs section."""
        if not outputs:
            return None

        lines = ["## Previous Step Outputs", ""]

        for key, value in outputs.items():
            if value is None:
                continue
            lines.append(f"**{key}:**")
            lines.append("")
            if isinstance(value, (dict, list)):
                import json
                lines.append(f"```json\n{json.dumps(value, indent=2, default=str)}\n```")
            else:
                lines.append(str(value))
            lines.append("")

        return "\n".join(lines) if len(lines) > 2 else None

    def _build_project_state_section(self) -> Optional[str]:
        """Build project state section."""
        state_parts = []

        # Git status
        git_info = self._get_git_info()
        if git_info:
            state_parts.append("### Git Status\n")
            state_parts.append(f"- Branch: {git_info.get('branch', 'unknown')}")
            state_parts.append(f"- Status: {git_info.get('status', 'unknown')}")
            if git_info.get('has_changes'):
                state_parts.append(f"- Uncommitted changes: yes")
            state_parts.append("")

        # Recent files (if relevant)
        recent_files = self._get_recently_modified_files(5)
        if recent_files:
            state_parts.append("### Recently Modified Files\n")
            for f in recent_files:
                state_parts.append(f"- {f}")
            state_parts.append("")

        if not state_parts:
            return None

        return "## Project State\n\n" + "\n".join(state_parts)

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

    def _get_git_info(self) -> Optional[Dict[str, Any]]:
        """Get git repository information."""
        try:
            # Current branch
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                cwd=self.project_root,
            )
            branch = result.stdout.strip()

            # Status
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=self.project_root,
            )
            status_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

            return {
                "branch": branch,
                "status": f"{len(status_lines)} changed files" if status_lines else "clean",
                "has_changes": len(status_lines) > 0,
            }

        except Exception as e:
            logger.debug(f"Failed to get git info: {e}")
            return None

    def _get_recently_modified_files(self, count: int = 5) -> List[str]:
        """Get list of recently modified files."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~5", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self.project_root,
            )
            files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return files[:count]
        except Exception:
            return []

    def find_relevant_specs(self, task_description: str) -> List[str]:
        """Find specs relevant to task description.

        Uses keyword matching with expanded synonym mapping and spec metadata.

        Args:
            task_description: Task description

        Returns:
            List of relevant spec names
        """
        task_lower = task_description.lower()
        relevant: Set[str] = set()

        # Expanded keyword to spec mapping with synonyms
        keyword_specs = {
            # Flow engine
            "flow": ["flow-engine"],
            "engine": ["flow-engine"],
            "state machine": ["flow-engine"],
            "state": ["flow-engine"],
            "workflow": ["flow-engine"],
            "step": ["flow-engine"],
            "run": ["flow-engine"],
            # Parallel execution / Collaboration
            "parallel": ["git-worktree-collab"],
            "concurrent": ["git-worktree-collab"],
            "worker": ["git-worktree-collab", "agent-team"],
            "collab": ["git-worktree-collab"],
            "collaboration": ["git-worktree-collab"],
            "worktree": ["git-worktree-collab"],
            "multi-agent": ["agent-team", "git-worktree-collab"],
            "sub-agent": ["agent-team"],
            "spawn": ["agent-team"],
            # Agent team
            "agent": ["agent-team"],
            "team": ["agent-team"],
            # Human interface
            "human": ["human-as-mcp"],
            "mcp": ["human-as-mcp"],
            "input": ["human-as-mcp", "requirement-intake"],
            "call": ["human-as-mcp"],
            # Requirements
            "requirement": ["requirement-intake"],
            "intake": ["requirement-intake"],
            "demand": ["requirement-intake"],
            "bootstrap": ["requirement-intake"],
            # SE3 commands
            "command": ["se3-commands", "se3-workflows"],
            "cli": ["se3-commands"],
            "se3": ["se3-commands", "se3-config", "se3-module-system", "se3-scaffold", "se3-workflows"],
            # SE3 config
            "config": ["se3-config"],
            "configuration": ["se3-config"],
            "yaml": ["se3-config"],
            # SE3 module
            "module": ["se3-module-system"],
            "skill": ["se3-module-system"],
            "plugin": ["se3-module-system"],
            # SE3 scaffold
            "scaffold": ["se3-scaffold"],
            "template": ["se3-scaffold"],
            "init": ["se3-scaffold"],
            # SE3 workflows
            "workflow": ["se3-workflows"],
            "start": ["se3-workflows", "session-protocol"],
            "work": ["se3-workflows"],
            "done": ["se3-workflows"],
            # Session protocol
            "session": ["session-protocol"],
            "protocol": ["session-protocol"],
            "progress": ["session-protocol"],
            "handoff": ["session-protocol"],
            # Spec guardrails
            "guardrail": ["spec-guardrails"],
            "constraint": ["spec-guardrails"],
            "rule": ["spec-guardrails"],
            "enforce": ["spec-guardrails"],
            # Spec lint
            "lint": ["spec-lint"],
            "validate": ["spec-lint", "change-verifier"],
            "check": ["spec-lint", "change-verifier"],
            "verify": ["spec-lint", "change-verifier"],
            # Change verifier
            "change": ["change-verifier"],
            "archive": ["change-verifier"],
            "sync": ["change-verifier", "output-sync"],
            # Output sync
            "output": ["output-sync"],
            # Status diagnostics
            "status": ["status-diagnostics"],
            "diagnostic": ["status-diagnostics"],
            "health": ["status-diagnostics"],
            "dashboard": ["status-diagnostics"],
        }

        for keyword, specs in keyword_specs.items():
            # Use word boundary matching to avoid partial matches (e.g., "about" matching "agent")
            pattern = r'\b' + re.escape(keyword) + r'\b'
            if re.search(pattern, task_lower):
                relevant.update(specs)

        # Check for spec directories that match task words (direct name matching)
        if self.specs_dir.exists():
            task_words = set(task_lower.replace("-", " ").split())
            for spec_dir in self.specs_dir.iterdir():
                if spec_dir.is_dir():
                    # Skip internal dirs like _changelog
                    if spec_dir.name.startswith("_"):
                        continue
                    spec_words = set(spec_dir.name.replace("-", " ").split())
                    if spec_words & task_words:  # Intersection
                        relevant.add(spec_dir.name)

        # Also check spec file content for keywords (if no matches yet)
        if not relevant and self.specs_dir.exists():
            relevant = self._find_specs_by_content(task_lower)

        return list(relevant)

    def _find_specs_by_content(self, task_lower: str) -> Set[str]:
        """Find specs by scanning their content for task keywords.
        
        This is a fallback when keyword matching finds nothing.
        Scans the Purpose section of each spec for matches.
        
        Args:
            task_lower: Lowercase task description
            
        Returns:
            Set of matching spec names
        """
        relevant: Set[str] = set()
        task_words = set(task_lower.replace("-", " ").split())
        
        if not self.specs_dir.exists():
            return relevant
            
        for spec_dir in self.specs_dir.iterdir():
            if not spec_dir.is_dir() or spec_dir.name.startswith("_"):
                continue
                
            # Try to read spec.md
            spec_file = spec_dir / "spec.md"
            if not spec_file.exists():
                continue
                
            try:
                content = spec_file.read_text(encoding="utf-8").lower()
                # Extract words from first 1000 chars (typically Purpose section)
                content_words = set(content[:1000].replace("-", " ").split())
                
                # Check for word intersection
                common_words = task_words & content_words
                # Require at least 2 matching words to avoid false positives
                if len(common_words) >= 2:
                    relevant.add(spec_dir.name)
                    logger.debug(f"Content match for {spec_dir.name}: {common_words}")
            except Exception:
                continue
                
        return relevant

    def get_step_prompt_template(self, step_type: str) -> str:
        """Get default prompt template for a step.

        Args:
            step_type: Type of step

        Returns:
            Prompt template string
        """
        templates = {
            "analyze": """You are the analyze step of the SE3 flow engine.

Your task is to analyze the input and determine:
1. What type of task this is (feature, bugfix, review, directive, small)
2. What the scope of the task is
3. Which steps from the step pool are needed

Step pool available:
- analyze: Analyze input and determine task type
- read_spec: Read relevant specs
- propose: Generate change proposal
- design: Design solution and architecture
- plan_tasks: Break down into concrete tasks
- implement: Write code
- test: Run tests
- verify_spec: Check implementation vs spec
- update_spec: Update spec records
- commit: Commit changes
- summarize: Generate summary

Task description: {task_description}

Respond in this format:
```json
{{
    "task_type": "feature|bugfix|review|directive|small",
    "scope": "brief description of scope",
    "required_steps": ["step1", "step2", ...],
    "reasoning": "brief explanation"
}}
```
""",
            "read_spec": """You are the read_spec step.

Relevant specifications have been loaded in the context above.
Extract the key requirements and constraints relevant to this task.

Respond with a summary of the key points from each spec.
""",
            "propose": """You are the propose step.

Generate a change proposal based on:
1. The task description
2. The relevant specifications

The proposal should include:
- Summary of the change
- Files that will be modified
- Any new files to be created
- Potential risks or considerations

Context is provided above.
""",
            "design": """You are the design step.

Create a design document for the implementation including:
1. Architecture decisions
2. Component design
3. Interface definitions
4. Implementation approach

Use the proposal and specs from previous steps (in context).
""",
            "plan_tasks": """You are the plan_tasks step.

Break down the implementation into concrete, verifiable tasks.

Each task should:
- Have a clear objective
- Be independently verifiable
- Have estimated complexity (small/medium/large)

Respond in this format:
```json
{{
    "tasks": [
        {{"id": 1, "description": "...", "complexity": "small"}},
        ...
    ]
}}
```
""",
            "implement": """You are the implement step.

Write the code to implement the design. Focus on:
- Correct functionality
- Clean, readable code
- Following project conventions
- Proper error handling

Use the context from previous steps for requirements.
""",
            "verify_spec": """You are the verify_spec step.

Check if the implementation matches the specifications.

Review:
1. Requirements from specs are met
2. Design decisions are followed
3. No unintended changes

Report any discrepancies.
""",
            "update_spec": """You are the update_spec step.

Update the relevant specifications to reflect the changes made.

Consider:
- New capabilities added
- Changes to existing behavior
- Documentation updates needed
""",
            "summarize": """You are the summarize step.

Generate a summary of what was accomplished including:
1. Changes made
2. Files modified
3. Tests status
4. Any remaining tasks or follow-ups
5. Handoff context for future sessions
""",
        }

        return templates.get(step_type, f"Execute the {step_type} step.")
