"""Spec Indexer - Build capability to spec mapping from spec files.

Scans specs/ (with openspec/specs/ fallback) and builds an index for
programmatic access.
Supports:
- Capability -> spec mapping
- Keyword -> spec mapping
- Change size assessment based on affected capabilities
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field, asdict


@dataclass
class SpecInfo:
    """Information about a spec."""
    name: str
    path: Path
    capabilities: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    complexity: str = "medium"  # simple, medium, complex
    requires_plan: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "capabilities": self.capabilities,
            "keywords": self.keywords,
            "complexity": self.complexity,
            "requires_plan": self.requires_plan,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SpecInfo":
        return cls(
            name=data["name"],
            path=Path(data["path"]),
            capabilities=data.get("capabilities", []),
            keywords=data.get("keywords", []),
            complexity=data.get("complexity", "medium"),
            # Support both new and legacy serialized keys
            requires_plan=data.get("requires_plan", data.get("requires_design", True)),
        )


class SpecIndex:
    """Index of OpenSpec specs for programmatic access."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.specs_dir = self._resolve_specs_dir(project_root)
        self.specs: Dict[str, SpecInfo] = {}
        self.capability_map: Dict[str, List[str]] = {}
        self.keyword_map: Dict[str, List[str]] = {}
        self._index_file = project_root / "se3" / "cache" / "spec_index.json"

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

    def build_index(self) -> "SpecIndex":
        """Scan specs directory and build the index."""
        if not self.specs_dir.exists():
            return self

        for spec_dir in self.specs_dir.iterdir():
            if not spec_dir.is_dir():
                continue

            spec_file = spec_dir / "spec.md"
            if not spec_file.exists():
                continue

            try:
                spec_info = self._parse_spec(spec_dir.name, spec_file)
                self.specs[spec_info.name] = spec_info

                # Build capability map
                for cap in spec_info.capabilities:
                    if cap not in self.capability_map:
                        self.capability_map[cap] = []
                    self.capability_map[cap].append(spec_info.name)

                # Build keyword map
                for kw in spec_info.keywords:
                    if kw not in self.keyword_map:
                        self.keyword_map[kw] = []
                    self.keyword_map[kw].append(spec_info.name)

            except Exception as e:
                print(f"Warning: Failed to parse spec {spec_dir.name}: {e}")

        return self

    def _parse_spec(self, name: str, path: Path) -> SpecInfo:
        """Parse a spec file and extract metadata."""
        content = path.read_text(encoding="utf-8")

        # Extract capabilities from requirements
        capabilities = []
        kw_keywords = []

        # Look for capability mentions in requirements
        cap_pattern = r"###\s+Requirement.*?capability[\s\w]*:\s*([\w\-]+)"
        for match in re.finditer(cap_pattern, content, re.IGNORECASE):
            capabilities.append(match.group(1).lower())

        # Also extract from "Purpose" section
        purpose_match = re.search(r"##\s+Purpose\s*\n(.*?)(?=##|$)", content, re.DOTALL)
        if purpose_match:
            purpose = purpose_match.group(1).lower()
            # Extract key phrases as capabilities
            phrases = re.findall(r"([a-z]+[\s\-][a-z]+(?:[\s\-][a-z]+)*)", purpose)
            for phrase in phrases:
                if len(phrase) > 5:
                    kw_keywords.append(phrase)

        # Determine complexity based on scenario count
        scenario_count = len(re.findall(r"####\s+Scenario:", content))
        if scenario_count <= 3:
            complexity = "simple"
        elif scenario_count <= 8:
            complexity = "medium"
        else:
            complexity = "complex"

        # Determine if full planning is needed based on complexity
        requires_plan = complexity in ("medium", "complex")

        # Extract keywords from content
        keywords = set()
        # Key technical terms
        tech_terms = [
            "api", "authentication", "authorization", "cache", "database",
            "deployment", "error handling", "logging", "metrics", "migration",
            "notification", "performance", "queue", "routing", "security",
            "state machine", "storage", "testing", "validation", "workflow",
            "parallel", "async", "sync", "concurrent", "thread", "process",
            "engine", "step", "task", "flow", "pipeline", "orchestration",
        ]
        content_lower = content.lower()
        for term in tech_terms:
            if term in content_lower:
                keywords.add(term)

        return SpecInfo(
            name=name,
            path=path,
            capabilities=list(set(capabilities)),
            keywords=list(keywords) + kw_keywords[:5],
            complexity=complexity,
            requires_plan=requires_plan,
        )

    def save(self) -> None:
        """Save index to disk."""
        self._index_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "specs": {name: spec.to_dict() for name, spec in self.specs.items()},
            "capability_map": self.capability_map,
            "keyword_map": self.keyword_map,
        }
        # Atomic write
        tmp_file = self._index_file.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(data, f, indent=2)
        tmp_file.rename(self._index_file)

    def load(self) -> bool:
        """Load index from disk. Returns True if successful."""
        if not self._index_file.exists():
            return False

        try:
            with open(self._index_file) as f:
                data = json.load(f)

            self.specs = {
                name: SpecInfo.from_dict(spec_data)
                for name, spec_data in data.get("specs", {}).items()
            }
            self.capability_map = data.get("capability_map", {})
            self.keyword_map = data.get("keyword_map", {})
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    def get_spec(self, name: str) -> Optional[SpecInfo]:
        """Get a spec by name."""
        return self.specs.get(name)

    def find_by_capability(self, capability: str) -> List[SpecInfo]:
        """Find specs by capability."""
        spec_names = self.capability_map.get(capability.lower(), [])
        return [self.specs[name] for name in spec_names if name in self.specs]

    def find_by_keyword(self, keyword: str) -> List[SpecInfo]:
        """Find specs by keyword."""
        keyword = keyword.lower()
        results = []
        for kw, spec_names in self.keyword_map.items():
            if keyword in kw or kw in keyword:
                for name in spec_names:
                    if name in self.specs and self.specs[name] not in results:
                        results.append(self.specs[name])
        return results

    def search(self, query: str) -> List[SpecInfo]:
        """Search specs by query string."""
        query = query.lower()
        results = []
        scores = {}

        for name, spec in self.specs.items():
            score = 0

            # Name match
            if query in name.lower():
                score += 10

            # Capability match
            for cap in spec.capabilities:
                if query in cap.lower():
                    score += 5

            # Keyword match
            for kw in spec.keywords:
                if query in kw.lower():
                    score += 3

            if score > 0:
                scores[name] = score
                results.append(spec)

        # Sort by score
        results.sort(key=lambda s: scores.get(s.name, 0), reverse=True)
        return results

    def list_all(self) -> List[SpecInfo]:
        """List all specs."""
        return list(self.specs.values())


def get_or_build_index(project_root: Path) -> SpecIndex:
    """Get existing index or build a new one."""
    index = SpecIndex(project_root)

    # Try to load existing
    if index.load():
        return index

    # Build new index
    index.build_index()
    index.save()
    return index


def match_specs_for_task(task_description: str, project_root: Path) -> List[SpecInfo]:
    """Find relevant specs for a task description.

    Args:
        task_description: Description of the task
        project_root: Project root directory

    Returns:
        List of relevant specs, sorted by relevance
    """
    index = get_or_build_index(project_root)

    # Search using the full description
    results = index.search(task_description)

    # Also try individual keywords
    words = re.findall(r"\b[a-zA-Z]{4,}\b", task_description.lower())
    for word in set(words):
        keyword_results = index.find_by_keyword(word)
        for spec in keyword_results:
            if spec not in results:
                results.append(spec)

    return results


def assess_change_size(task_description: str, relevant_specs: List[SpecInfo]) -> Dict[str, Any]:
    """Assess the size of a change based on task and relevant specs.

    Args:
        task_description: Description of the task
        relevant_specs: List of relevant specs

    Returns:
        Dict with size assessment and required artifacts
    """
    # Count specs by complexity
    simple_count = sum(1 for s in relevant_specs if s.complexity == "simple")
    medium_count = sum(1 for s in relevant_specs if s.complexity == "medium")
    complex_count = sum(1 for s in relevant_specs if s.complexity == "complex")

    # Assess size
    if complex_count > 0 or medium_count >= 3:
        size = "large"
    elif medium_count > 0 or simple_count >= 3:
        size = "medium"
    else:
        size = "small"

    # Determine if planning step is needed
    requires_plan = any(s.requires_plan for s in relevant_specs) or size in ("medium", "large")

    # Estimate step count
    estimated_steps = 3  # analyze, implement, commit
    if requires_plan:
        estimated_steps += 1  # plan step

    return {
        "size": size,
        "simple_specs": simple_count,
        "medium_specs": medium_count,
        "complex_specs": complex_count,
        "requires_plan": requires_plan,
        "estimated_steps": estimated_steps,
    }
