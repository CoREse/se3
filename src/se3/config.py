"""SE3 configuration management."""

import logging
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field
from enum import Enum
import yaml

logger = logging.getLogger(__name__)


class BumpType(Enum):
    """Version bump types following Semantic Versioning."""
    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    NONE = "none"


@dataclass
class VersionConfig:
    """Version management configuration.
    
    Loads version-related settings from se3.yaml with sensible defaults.
    """
    # Whether automatic version bumping is enabled
    enabled: bool = True
    
    # Version file location (relative to project root)
    version_file: Optional[str] = None  # Auto-detect if None
    
    # Bump rules: map task type to bump type (following SemVer 2.0.0)
    # Note: These are used as fallback when version_analyze step is not available
    # or when smart_version_analysis is disabled
    bump_rules: dict[str, str] = field(default_factory=lambda: {
        "feature": "minor",   # New functionality
        "feat": "minor",
        "bugfix": "patch",    # Bug fixes
        "fix": "patch",
        "small": "patch",     # Small changes are also fixes (typo, etc.)
        "refactor": "patch",  # Refactoring is internal change
        "docs": "patch",      # Doc fixes are still fixes
        "test": "patch",      # Test additions/fixes
        "chore": "patch",     # Maintenance tasks
        "review": "none",     # No code changes
        "directive": "minor", # Following instructions may add features
    })
    
    # Smart version analysis configuration
    # Uses LLM to analyze actual changes and determine bump type
    smart_version_analysis: bool = True  # Enable version_analyze step
    
    # Auto-confirmation settings
    # If True, version bump is applied automatically without human confirmation
    auto_bump: bool = True
    
    # Confidence threshold for requiring human confirmation
    # None = no threshold (even "low" confidence is auto-confirmed)
    # "medium" = require confirmation for "low" confidence
    # "high" = require confirmation for "medium" or "low" confidence
    confidence_threshold: Optional[str] = None
    
    # Pre-release configuration
    prerelease_prefix: str = ""
    prerelease_number: int = 0
    
    # Template definitions
    templates: dict[str, str] = field(default_factory=lambda: {
        "readme_badge": "![Version](https://img.shields.io/badge/version-{version}-blue)",
        "versions_entry": "## {version} - {date}\n\n{changes}\n",
    })
    
    # README update settings
    readme_enabled: bool = True
    readme_marker: str = "<!-- SE3-VERSION -->"
    
    # VERSIONS.md settings  
    versions_enabled: bool = True
    versions_file: str = "VERSIONS.md"
    versions_header: str = "# Version History\n\n"
    
    # Whether to include version in commit message
    include_in_commit_message: bool = True

    # Version script interface
    script_path: Optional[str] = None  # Path to version script (None = default se3/scripts/version.py)
    auto_generate_script: bool = True  # Auto-generate script via LLM if not found
    
    @property
    def file_path(self) -> Optional[str]:
        """Alias for version_file (compatibility with version_bumper.VersionConfig)."""
        return self.version_file
    
    @classmethod
    def from_dict(cls, data: dict) -> "VersionConfig":
        """Create VersionConfig from dictionary (typically loaded from se3.yaml)."""
        if not data:
            return cls()
        
        # Extract version section if nested
        version_data = data.get("version", data)
        
        # Build bump rules from config or use defaults
        bump_rules = version_data.get("bump_rules", {})
        if not bump_rules:
            bump_rules = cls().bump_rules
        
        # Build templates from config or use defaults
        templates = cls().templates.copy()
        templates.update(version_data.get("templates", {}))
        
        return cls(
            enabled=version_data.get("enabled", True),
            version_file=version_data.get("version_file"),
            bump_rules=bump_rules,
            smart_version_analysis=version_data.get("smart_version_analysis", True),
            auto_bump=version_data.get("auto_bump", True),
            confidence_threshold=version_data.get("confidence_threshold", None),
            prerelease_prefix=version_data.get("prerelease_prefix", ""),
            prerelease_number=version_data.get("prerelease_number", 0),
            templates=templates,
            readme_enabled=version_data.get("readme_enabled", True),
            readme_marker=version_data.get("readme_marker", "<!-- SE3-VERSION -->"),
            versions_enabled=version_data.get("versions_enabled", True),
            versions_file=version_data.get("versions_file", "VERSIONS.md"),
            versions_header=version_data.get("versions_header", "# Version History\n\n"),
            include_in_commit_message=version_data.get("include_in_commit_message", True),
            script_path=version_data.get("script_path"),
            auto_generate_script=version_data.get("auto_generate_script", True),
        )
    
    @classmethod
    def load(cls, project_root: Path) -> "VersionConfig":
        """Load version configuration from se3.yaml in project root."""
        config_path = project_root / "se3.yaml"
        if not config_path.exists():
            return cls()
        
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls.from_dict(data)
        except Exception:
            return cls()
    
    def get_bump_type(self, task_type: str) -> BumpType:
        """Get the bump type for a given task type.
        
        Args:
            task_type: The type of task (feature, bugfix, small, etc.)
            
        Returns:
            The corresponding BumpType enum value
        """
        bump_rule = self.bump_rules.get(task_type, "none")
        try:
            return BumpType(bump_rule)
        except ValueError:
            return BumpType.NONE
    
    def get_template(self, name: str) -> str:
        """Get a template by name.
        
        Args:
            name: Template name (e.g., "readme_badge", "versions_entry")
            
        Returns:
            The template string, or empty string if not found
        """
        return self.templates.get(name, "")
    
    def should_update_readme(self) -> bool:
        """Check if README.md should be updated."""
        return self.readme_enabled
    
    def should_update_versions(self) -> bool:
        """Check if VERSIONS.md should be updated."""
        return self.versions_enabled


@dataclass
class Config:
    """Main SE3 configuration."""
    
    project_root: Path
    confirmation_enabled: bool = False
    confirmation_steps: list[str] = field(default_factory=list)
    
    def __post_init__(self):
        if isinstance(self.project_root, str):
            self.project_root = Path(self.project_root)
    
    @classmethod
    def load(cls, project_root: Path) -> "Config":
        """Load configuration from se3.yaml."""
        config_path = project_root / "se3.yaml"
        
        if not config_path.exists():
            return cls(project_root=project_root)
        
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        
        confirmation = data.get("confirmation", {})
        
        return cls(
            project_root=project_root,
            confirmation_enabled=confirmation.get("enabled", False),
            confirmation_steps=confirmation.get("steps", []),
        )


def load_version_config(project_root: Optional[Path] = None) -> VersionConfig:
    """Load version configuration from project.
    
    Args:
        project_root: Project root directory. If None, uses current working directory.
        
    Returns:
        VersionConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    
    return VersionConfig.load(project_root)


def load_config(project_root: Optional[Path] = None) -> Config:
    """Load main SE3 configuration.
    
    Args:
        project_root: Project root directory. If None, uses current working directory.
        
    Returns:
        Config instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    
    return Config.load(project_root)


def load_confirmation_config(project_root: Optional[Path] = None) -> dict:
    """Load confirmation configuration from project.
    
    Args:
        project_root: Project root directory. If None, uses current working directory.
        
    Returns:
        Dictionary with confirmation configuration settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    
    config_path = project_root / "se3.yaml"
    
    if not config_path.exists():
        return {"enabled": True, "steps": ["plan"]}
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        
        confirmation = data.get("confirmation", {})
        return {
            "enabled": confirmation.get("enabled", True),
            "steps": confirmation.get("steps", ["plan"]),
            "reviewer": confirmation.get("reviewer", "human"),
            "llm_reviewer": confirmation.get("llm_reviewer", {}),
        }
    except Exception:
        return {"enabled": True, "steps": ["plan"]}


def insert_confirmation_steps(
    steps: list,
    project_root: Optional[Path] = None,
) -> list:
    """Insert CONFIRM steps after configured step types.
    
    This is a standalone function that can be used by both the state machine
    and the analyze step handler to consistently insert confirmation steps.
    
    Args:
        steps: Original step sequence (list of StepType or StepType-like objects)
        project_root: Project root directory for loading config
        
    Returns:
        Modified step sequence with CONFIRM steps inserted
    """
    config = load_confirmation_config(project_root)
    
    if not config.get("enabled", True):
        return steps
    
    steps_requiring_confirm = config.get("steps", ["plan"])
    
    # Handle both StepType enum and string step types
    # Get step type values for comparison
    step_type_names = set()
    for s in steps:
        if hasattr(s, 'value'):
            step_type_names.add(s.value)
        else:
            step_type_names.add(str(s))
    
    # Only insert confirm for steps that are actually in the sequence
    steps_to_confirm = [s for s in steps_requiring_confirm if s in step_type_names]
    
    if not steps_to_confirm:
        return steps
    
    # Import StepType here to avoid circular imports
    from .engine.models import StepType
    
    result = []
    for step in steps:
        result.append(step)
        # Get step value for comparison
        step_value = step.value if hasattr(step, 'value') else str(step)
        if step_value in steps_to_confirm:
            # Insert CONFIRM step after this step
            result.append(StepType.CONFIRM)
    
    return result


_GLOBAL_CONFIG_PATH_SUFFIX = (".se3", "config.yaml")

_BUILTIN_DEFAULT_AGENT_NAME = "claude"


@dataclass
class AgentDef:
    """Typed representation of an agent entry in the top-level registry.

    ``agents`` in the new schema is a dict ``{name: AgentDef}``; the key
    is the name, and the value carries ``type`` / ``cmd`` / ``priority``.
    """

    name: str
    type: str = "claude-code"
    cmd: str = ""
    priority: int = 0

    def to_agent_dict(self) -> dict:
        """Return the legacy ``list[dict]`` shape consumed by LLMCaller."""
        return {
            "name": self.name,
            "type": self.type,
            "cmd": self.cmd,
            "priority": self.priority,
        }


def _read_yaml(path: Path) -> Optional[dict]:
    """Read and parse a YAML file; log on parse error and return None.

    Returns the parsed dict, an empty dict if the file is empty/yaml-null,
    or None if the file does not exist or failed to parse.
    """
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            logger.warning(
                "expected a YAML mapping at top of %s; got %s — ignoring",
                path, type(data).__name__,
            )
            return None
        return data
    except Exception as exc:
        logger.warning("failed to parse %s: %s", path, exc)
        return None


def _load_agent_configs(
    project_root: Optional[Path],
) -> tuple[dict, dict]:
    """Read global and project YAML configs in one pass.

    Returns ``(global_data, project_data)``; missing/invalid files are
    returned as empty dicts so callers can uniformly use ``.get(...)``.
    """
    global_data = _read_yaml(Path.home() / _GLOBAL_CONFIG_PATH_SUFFIX[0] / _GLOBAL_CONFIG_PATH_SUFFIX[1]) or {}
    project_data: dict = {}
    if project_root is not None:
        project_data = _read_yaml(Path(project_root) / "se3.yaml") or {}
    return global_data, project_data


_warned_list_agents_for: set[str] = set()
_warned_claude_commands_ignored_for: set[str] = set()
_warned_claude_commands_deprecated_for: set[str] = set()


def _slugify_cmd(cmd: str) -> str:
    """Slugify a cmd string to form an agent name.

    Letters, digits, hyphens, and underscores are preserved. Everything
    else becomes ``_``. Empty result defaults to ``agent``.
    """
    import re

    if not cmd:
        return "agent"
    slug = re.sub(r"[^A-Za-z0-9_\-]", "_", cmd)
    return slug or "agent"


def _unique_name(base: str, existing: set[str]) -> str:
    """Return ``base`` if unused, else ``base_2`` / ``base_3`` …"""
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _migrate_claude_commands(
    source_label: str,
    commands: list,
) -> tuple[dict[str, AgentDef], list[str]]:
    """Convert legacy ``claude_commands`` list to a registry + defaults.

    Returns ``(registry_entries, default_names)`` — ``registry_entries``
    is a mapping of ``name -> AgentDef`` synthesized from each legacy
    entry, and ``default_names`` is the name list in the original order
    to be used as an implicit ``llm_caller.defaults`` fallback.

    Name generation: each cmd is slugified; collisions append
    ``_2`` / ``_3`` / …. Emits a single DeprecationWarning per source
    showing the equivalent new-schema YAML snippet.
    """
    normalized = _normalize_commands(commands)
    if not normalized:
        return {}, []

    registry: dict[str, AgentDef] = {}
    defaults: list[str] = []
    for entry in normalized:
        cmd = entry["cmd"]
        priority = entry.get("priority", 0)
        base = _slugify_cmd(cmd)
        name = _unique_name(base, set(registry))
        registry[name] = AgentDef(
            name=name, type="claude-code", cmd=cmd, priority=priority,
        )
        defaults.append(name)

    if source_label not in _warned_claude_commands_deprecated_for:
        _warned_claude_commands_deprecated_for.add(source_label)
        lines = ["agents:"]
        for name, agent in registry.items():
            parts = [f"type: {agent.type}", f"cmd: {agent.cmd}"]
            if agent.priority:
                parts.append(f"priority: {agent.priority}")
            lines.append(f"  {name}: {{{', '.join(parts)}}}")
        lines.append("llm_caller:")
        lines.append(f"  defaults: [{', '.join(defaults)}]")
        snippet = "\n".join(lines)
        logger.warning(
            "%s: 'claude_commands' is deprecated; converting to top-level "
            "'agents' registry + 'llm_caller.defaults'. Equivalent new "
            "config:\n%s",
            source_label, snippet,
        )

    return registry, defaults


def _agents_dict_from_source(
    source_label: str, raw: Any,
) -> Optional[dict[str, AgentDef]]:
    """Parse a single source's top-level ``agents`` field.

    Returns the parsed registry, or ``None`` when the field is absent.
    Invalid shapes (list, scalar, non-dict) log a warning and return
    ``None`` so the caller can decide how to fall back.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        # Legacy list form is removed. Warn and ignore.
        if source_label not in _warned_list_agents_for:
            _warned_list_agents_for.add(source_label)
            logger.warning(
                "%s: top-level 'agents' is a list — this legacy form is "
                "no longer supported. Use a dict keyed by agent name "
                "instead, e.g. `agents:\\n  primary: {cmd: claude}`. "
                "Ignoring the entire agents field for this source.",
                source_label,
            )
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "%s: top-level 'agents' is not a mapping (got %s); ignoring",
            source_label, type(raw).__name__,
        )
        return None

    registry: dict[str, AgentDef] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not name.strip():
            logger.warning(
                "%s: agents registry key %r is not a non-empty string; "
                "skipping",
                source_label, name,
            )
            continue
        if isinstance(entry, str):
            registry[name] = AgentDef(
                name=name, type="claude-code", cmd=entry, priority=0,
            )
            continue
        if not isinstance(entry, dict):
            logger.warning(
                "%s: agents.%s is not a mapping (got %s); skipping",
                source_label, name, type(entry).__name__,
            )
            continue
        cmd = entry.get("cmd", "")
        if not (isinstance(cmd, str) and cmd.strip()):
            logger.warning(
                "%s: agents.%s has no usable 'cmd'; skipping",
                source_label, name,
            )
            continue
        registry[name] = AgentDef(
            name=name,
            type=entry.get("type", "claude-code"),
            cmd=cmd,
            priority=entry.get("priority", 0),
        )
    return registry


def _agent_registry_from_data(
    global_data: dict, project_data: dict,
) -> tuple[dict[str, AgentDef], list[str]]:
    """Build the agent registry from global + project YAML data.

    Returns ``(registry, legacy_defaults)`` — the registry is the
    entry-level merged agent directory (project overrides global by
    name); ``legacy_defaults`` is an implicit default chain (list of
    names) derived from ``claude_commands`` when either source omits
    ``agents`` (project legacy defaults win over global).
    """
    sources = [
        ("~/.se3/config.yaml", global_data),
        ("se3.yaml", project_data),
    ]

    merged: dict[str, AgentDef] = {}
    legacy_defaults: list[str] = []

    for source_label, data in sources:
        raw_agents = data.get("agents")
        parsed = _agents_dict_from_source(source_label, raw_agents)

        has_explicit_agents = parsed is not None and raw_agents is not None and isinstance(raw_agents, dict)

        if parsed:
            # Merge entry-level — later source (project) overrides by
            # name; non-conflicting entries from either side coexist.
            for name, agent in parsed.items():
                merged[name] = agent

        claude_commands = data.get("claude_commands")
        if claude_commands:
            if has_explicit_agents:
                # Both set: ignore claude_commands with warning.
                if source_label not in _warned_claude_commands_ignored_for:
                    _warned_claude_commands_ignored_for.add(source_label)
                    logger.warning(
                        "%s: both 'agents' and 'claude_commands' are "
                        "set; ignoring legacy 'claude_commands'",
                        source_label,
                    )
            else:
                migrated, defaults = _migrate_claude_commands(
                    source_label, claude_commands,
                )
                # Merge migrated entries, not overriding an already-merged
                # name (so project claude_commands can still add to a
                # registry built from global agents).
                for name, agent in migrated.items():
                    if name not in merged:
                        merged[name] = agent
                # Project's legacy defaults take precedence over global's.
                legacy_defaults = defaults

    return merged, legacy_defaults


def _registry_to_sorted_list(
    registry: dict[str, AgentDef], names: list[str],
) -> list[dict]:
    """Resolve a name list against the registry and sort by priority.

    Caller is responsible for validating that all names are registered.
    """
    resolved = [registry[n].to_agent_dict() for n in names]
    resolved.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return resolved


def load_agent_registry(
    project_root: Optional[Path] = None,
) -> dict[str, AgentDef]:
    """Load the top-level agent registry from global + project configs.

    Returns a merged ``{name: AgentDef}`` mapping. Legacy
    ``claude_commands`` in either source is auto-migrated when that
    source omits ``agents``. Invalid top-level forms (list, scalar)
    produce a warning and are ignored.
    """
    global_data, project_data = _load_agent_configs(project_root)
    registry, _ = _agent_registry_from_data(global_data, project_data)
    return registry


def _resolve_name_list(
    reference_location: str,
    names: list[str],
    registry: dict[str, AgentDef],
) -> list[dict]:
    """Resolve a list of names against the registry.

    Raises ``ValueError`` on any unknown name, with a message containing
    the reference location and the sorted list of registered names.
    """
    missing = [n for n in names if n not in registry]
    if missing:
        available = sorted(registry.keys())
        raise ValueError(
            f"{reference_location}: unknown agent name(s) {missing!r}; "
            f"registered agents: {available}"
        )
    return _registry_to_sorted_list(registry, names)


def _read_llm_caller_section(
    data: dict, source_label: str,
) -> Optional[dict]:
    """Read and validate the ``llm_caller`` section of a source."""
    llm_caller = data.get("llm_caller")
    if llm_caller is None:
        return None
    if not isinstance(llm_caller, dict):
        if source_label not in _warned_non_dict_llm_caller_for:
            _warned_non_dict_llm_caller_for.add(source_label)
            logger.warning(
                "%s: top-level 'llm_caller' is not a mapping (got %s); "
                "ignoring",
                source_label, type(llm_caller).__name__,
            )
        return None
    return llm_caller


def _explicit_defaults(
    data: dict, source_label: str,
) -> Optional[list[str]]:
    """Read ``llm_caller.defaults`` from a source as a list of names.

    Returns ``None`` when absent; warns + returns ``None`` for malformed
    values (non-list, or list with non-string entries after filtering).
    """
    llm_caller = _read_llm_caller_section(data, source_label)
    if llm_caller is None:
        return None
    raw = llm_caller.get("defaults")
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning(
            "%s: llm_caller.defaults is not a list (got %s); ignoring",
            source_label, type(raw).__name__,
        )
        return None
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            names.append(entry)
        else:
            logger.warning(
                "%s: llm_caller.defaults entry %r is not a non-empty "
                "string; skipping",
                source_label, entry,
            )
    return names if names else None


def _default_chain_from_data(global_data: dict, project_data: dict) -> list[dict]:
    """Build the default agent chain from already-parsed YAML data.

    Priority (first non-empty wins):
      1. ``project.llm_caller.defaults`` (explicit, name list)
      2. ``global.llm_caller.defaults`` (explicit, name list)
      3. Implicit defaults from legacy ``claude_commands`` (project > global)
      4. Built-in ``[claude]``.

    Unknown names at the selected level raise ``ValueError``.
    """
    registry, legacy_defaults = _agent_registry_from_data(
        global_data, project_data,
    )

    # 1 & 2: explicit defaults.
    project_names = _explicit_defaults(project_data, "se3.yaml")
    if project_names is not None:
        return _resolve_name_list(
            "se3.yaml: llm_caller.defaults", project_names, registry,
        )
    global_names = _explicit_defaults(global_data, "~/.se3/config.yaml")
    if global_names is not None:
        return _resolve_name_list(
            "~/.se3/config.yaml: llm_caller.defaults", global_names, registry,
        )

    # 3: implicit from legacy claude_commands.
    if legacy_defaults:
        return _resolve_name_list(
            "legacy claude_commands migration", legacy_defaults, registry,
        )

    # 4: built-in.
    return [
        {
            "name": _BUILTIN_DEFAULT_AGENT_NAME,
            "type": "claude-code",
            "cmd": _BUILTIN_DEFAULT_AGENT_NAME,
            "priority": 0,
        }
    ]


def _valid_step_keys() -> set[str]:
    """Return the set of known StepType value strings (for typo detection)."""
    # Import locally to avoid circular import at module load.
    from .engine.models import StepType

    return {s.value for s in StepType}


_warned_unknown_step_keys_for: set[tuple[str, ...]] = set()
_warned_non_dict_llm_caller_for: set[str] = set()


def _warn_on_unknown_step_keys(
    source_label: str, steps_dict: dict,
) -> None:
    """Log a warning when ``llm_caller.steps`` contains keys that are not
    valid StepType values. Idempotent per (source, keyset) pair to avoid
    flooding logs when resolve_agents is called many times per flow.
    """
    if not steps_dict:
        return
    valid = _valid_step_keys()
    unknown = sorted(k for k in steps_dict.keys() if k not in valid)
    if not unknown:
        return
    dedup_key = (source_label, *unknown)
    if dedup_key in _warned_unknown_step_keys_for:
        return
    _warned_unknown_step_keys_for.add(dedup_key)
    logger.warning(
        "%s: llm_caller.steps has unknown step key(s) %s — likely a typo; "
        "these declarations will be ignored",
        source_label, unknown,
    )


def _step_override_from_data(
    global_data: dict, project_data: dict, step_type: str,
) -> Optional[list[dict]]:
    """Extract and validate a per-step override from already-parsed YAML.

    New schema: ``llm_caller.steps.<step>`` is a list of name strings
    that refer to entries in the top-level ``agents`` registry. Legacy
    inline-dict entries (``- cmd: claude-opus``) are rejected with a
    warning and treated as "no override"; an unknown agent name raises
    ``ValueError`` at startup so typos fail loudly.

    Returns normalized+sorted agent dicts, or ``None`` if no valid
    override is declared for ``step_type``.
    """
    def _section(data: dict, source_label: str) -> dict:
        llm_caller = _read_llm_caller_section(data, source_label)
        if llm_caller is None:
            return {}
        section = llm_caller.get("steps", {})
        return section if isinstance(section, dict) else {}

    global_steps = _section(global_data, "~/.se3/config.yaml")
    project_steps = _section(project_data, "se3.yaml")

    _warn_on_unknown_step_keys("~/.se3/config.yaml", global_steps)
    _warn_on_unknown_step_keys("se3.yaml", project_steps)

    # Source label is tracked so ValueError messages point the user to
    # the exact YAML file where the typo lives.
    if step_type in project_steps:
        raw = project_steps[step_type]
        source_label = "se3.yaml"
    elif step_type in global_steps:
        raw = global_steps[step_type]
        source_label = "~/.se3/config.yaml"
    else:
        return None

    if not isinstance(raw, list):
        logger.warning(
            "%s: llm_caller.steps.%s is not a list (got %s); ignoring "
            "override",
            source_label, step_type, type(raw).__name__,
        )
        return None

    per_entry_warned = False
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            if entry.strip():
                names.append(entry)
            else:
                logger.warning(
                    "%s: llm_caller.steps.%s entry %r is a blank string; "
                    "skipping",
                    source_label, step_type, entry,
                )
                per_entry_warned = True
        elif isinstance(entry, dict):
            # New schema uses name references only; inline dict form is
            # a breaking change → warn and skip this entry.
            logger.warning(
                "%s: llm_caller.steps.%s contains an inline dict entry "
                "%r — the new schema requires agent name references "
                "(list of str). Define the agent under top-level "
                "'agents' and reference it by name. Skipping this entry.",
                source_label, step_type, entry,
            )
            per_entry_warned = True
        else:
            logger.warning(
                "%s: llm_caller.steps.%s contains non-str entry %r; "
                "skipping",
                source_label, step_type, entry,
            )
            per_entry_warned = True

    if not names:
        if not per_entry_warned:
            logger.warning(
                "%s: llm_caller.steps.%s is empty or has no valid "
                "entries; ignoring override",
                source_label, step_type,
            )
        return None

    registry, _ = _agent_registry_from_data(global_data, project_data)
    return _resolve_name_list(
        f"{source_label}: llm_caller.steps.{step_type}",
        names,
        registry,
    )


def load_agents(project_root: Optional[Path] = None) -> list[dict]:
    """Load agent configurations from project and global configuration.

    Supports two configuration formats:
    1. New ``agents`` field (recommended): list of dicts with name, type, cmd, priority.
    2. Legacy ``claude_commands`` field: auto-converted with type='claude-code'.

    When both exist, ``agents`` takes priority.  Project config overrides global.

    Args:
        project_root: Project root directory. If None, uses global config only.

    Returns:
        List of agent config dicts ``{name, type, cmd, priority}`` sorted by
        priority descending.
    """
    global_data, project_data = _load_agent_configs(project_root)
    return _default_chain_from_data(global_data, project_data)


def load_step_agents(
    project_root: Optional[Path],
    step_type: Optional[str],
) -> Optional[list[dict]]:
    """Load per-step agent override from ``llm_caller.steps.<step_type>``.

    Reads ``llm_caller.steps.<step_type>`` from project-level se3.yaml with
    fallback to the global ``~/.se3/config.yaml`` entry of the same shape.
    Project-level declaration of a given step fully replaces the global
    declaration for that step (no deep merge), mirroring ``load_agents``.

    When the step has no declaration, returns None so callers can fall back
    to the default chain from :func:`load_agents`. Empty lists and
    structurally invalid values (not a list, entries not str/dict, dict
    entries lacking a usable ``cmd``) are treated as "no declaration" — a
    warning is logged but no exception is raised, matching the
    clamp-and-warn policy used elsewhere (e.g. :class:`TestConfig`).

    Args:
        project_root: Project root directory, or None to use global config
            only.
        step_type: StepType value string (e.g. ``"implement"``, ``"plan"``).
            None or empty string short-circuits to None.

    Returns:
        Normalized agent dicts sorted by ``priority`` descending, or None
        when no override is declared for this step.
    """
    if not step_type:
        return None
    global_data, project_data = _load_agent_configs(project_root)
    return _step_override_from_data(global_data, project_data, step_type)


def resolve_agents(
    project_root: Optional[Path],
    step_type: Optional[str],
) -> tuple[list[dict], bool]:
    """Resolve the effective agent chain for a step in a single YAML read.

    Returns ``(agents, is_step_override)``. When ``step_type`` declares a
    valid ``llm_caller.steps.<step_type>`` override, that list is returned
    verbatim (no fallback to the default chain) and the flag is True.
    Otherwise the default chain from the top-level ``agents`` /
    ``claude_commands`` (or built-in default) is returned and the flag is
    False.

    Used by :class:`LLMCaller` to avoid the cost of reading the same YAML
    files twice (once via ``load_step_agents``, once via ``load_agents``).
    """
    global_data, project_data = _load_agent_configs(project_root)
    if step_type:
        override = _step_override_from_data(global_data, project_data, step_type)
        if override:
            return override, True
    return _default_chain_from_data(global_data, project_data), False


def load_claude_commands(project_root: Optional[Path] = None) -> list[dict]:
    """Load Claude CLI commands from project and global configuration.

    .. deprecated::
        Use :func:`load_agents` instead.  This function now delegates to
        ``load_agents()`` and converts the result back to the legacy
        ``{cmd, priority}`` format for backward compatibility.

    Args:
        project_root: Project root directory. If None, uses global config only.

    Returns:
        List of command dictionaries with 'cmd' and 'priority' keys, sorted by priority.
    """
    agents = load_agents(project_root)
    return _agents_to_commands(agents)


def _agents_to_commands(agents: list[dict]) -> list[dict]:
    """Convert agent dicts back to legacy command dicts."""
    return [
        {"cmd": a.get("cmd", "claude"), "priority": a.get("priority", 0)}
        for a in agents
    ]


def _normalize_commands(commands: list) -> list[dict]:
    """Normalize command entries to dictionaries.

    Args:
        commands: List of command entries (dicts or strings)

    Returns:
        List of normalized command dictionaries
    """
    normalized = []
    for cmd in commands:
        if isinstance(cmd, str):
            normalized.append({"cmd": cmd, "priority": 0})
        elif isinstance(cmd, dict):
            if "cmd" in cmd:
                normalized.append({
                    "cmd": cmd["cmd"],
                    "priority": cmd.get("priority", 0)
                })
    return normalized


def get_language_labels(language: str) -> dict[str, str]:
    """Get translated labels for a given language.
    
    Args:
        language: Language code (e.g., 'en', 'zh', 'zh-CN')
        
    Returns:
        Dictionary of translated labels
    """
    labels = {
        "en": {
            "human_input": "Human Input",
            "review": "Review",
            "approve": "Approve",
            "reject": "Reject",
            "comment": "Comment",
            "submit": "Submit",
        },
        "zh": {
            "human_input": "人工输入",
            "review": "审查",
            "approve": "批准",
            "reject": "拒绝",
            "comment": "评论",
            "submit": "提交",
        },
    }
    
    # Map language codes
    lang = language.lower()
    if lang.startswith("zh"):
        return labels["zh"]
    return labels["en"]


@dataclass
class LanguageConfig:
    """Language configuration for controlling output language.

    Two-tier language settings:
    - language: for human-facing steps (summarize, discovery, confirmed steps)
    - spec_language: for spec writing (update_spec step)
    Both default to None (no restriction).
    """

    language: Optional[str] = None
    spec_language: Optional[str] = None

    @classmethod
    def load(cls, project_root: Path) -> "LanguageConfig":
        """Load language configuration from se3.yaml."""
        config_path = Path(project_root) / "se3.yaml"
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            lang_section = data.get("language", {})
            if not lang_section or not isinstance(lang_section, dict):
                return cls()
            return cls(
                language=lang_section.get("language"),
                spec_language=lang_section.get("spec_language"),
            )
        except Exception:
            return cls()


def load_language_config(project_root: Optional[Path] = None) -> "LanguageConfig":
    """Load language configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        LanguageConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return LanguageConfig.load(project_root)


def get_language_instruction(language: Optional[str], context: str = "") -> str:
    """Get a language instruction string for LLM prompts.

    Args:
        language: Language code (e.g., 'zh-CN', 'en'). None means no restriction.
        context: Optional context description for the instruction.

    Returns:
        Prompt instruction string when language is set, empty string when None.
    """
    if not language:
        return ""
    ctx = f" in the {context} step" if context else ""
    return f"\n\nIMPORTANT: You MUST respond in {language}{ctx}."


def is_chinese_language(language: str) -> bool:
    """Check if the language is Chinese.

    Args:
        language: Language code (e.g., 'en', 'zh', 'zh-CN')

    Returns:
        True if the language is Chinese
    """
    return language.lower().startswith("zh")


@dataclass
class ConflictResolverConfig:
    """Conflict resolution configuration for loop branch merges.

    Supports two strategies:
    - 'human': Preserve conflict state, create call file, wait for human resolution.
    - 'llm': Attempt LLM-based per-file resolution, fallback to human on failure.
    """

    strategy: str = "human"

    @classmethod
    def from_dict(cls, data: dict) -> "ConflictResolverConfig":
        """Create ConflictResolverConfig from dictionary."""
        if not data:
            return cls()
        strategy = data.get("strategy", "human")
        if strategy not in ("human", "llm"):
            strategy = "human"
        return cls(strategy=strategy)

    @classmethod
    def load(cls, project_root: Path) -> "ConflictResolverConfig":
        """Load conflict resolver configuration from se3.yaml."""
        config_path = Path(project_root) / "se3.yaml"
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cr_data = data.get("conflict_resolver", {})
            if not cr_data or not isinstance(cr_data, dict):
                return cls()
            return cls.from_dict(cr_data)
        except Exception:
            return cls()


def load_conflict_resolver_config(project_root: Optional[Path] = None) -> ConflictResolverConfig:
    """Load conflict resolver configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        ConflictResolverConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return ConflictResolverConfig.load(project_root)

DEFAULT_MAX_FIX_ITERATIONS = 20
@dataclass
class TestConfig:
    """Test step configuration loaded from se3.yaml test: section."""

    command: Optional[str] = None
    timeout: int = 1800
    phases: list[dict] = field(default_factory=list)
    fix_loop_max_iterations: int = DEFAULT_MAX_FIX_ITERATIONS
    timeout_multiplier: float = 2.0
    min_dynamic_timeout: int = 30
    # Upper sanity cap on computed dynamic timeout. Without this, repeated
    # timeouts in the fix loop can compound the LLM's estimated duration
    # beyond any reasonable bound, masking a hung test as "just slow".
    max_dynamic_timeout: int = 14400  # 4 hours

    @classmethod
    def load(cls, project_root: Path) -> "TestConfig":
        """Load test configuration from se3.yaml."""
        config_path = Path(project_root) / "se3.yaml"
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            test_data = data.get("test", {})
            if not test_data:
                return cls()
            fix_loop = test_data.get("fix_loop", {})

            # Validate timeout_multiplier: clamp to >= 1.0 so a typo like
            # 0 / negative / 0.1 does not silently disable the feature.
            raw_multiplier = test_data.get("timeout_multiplier", 2.0)
            try:
                multiplier = float(raw_multiplier)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid timeout_multiplier %r in se3.yaml; using default 2.0",
                    raw_multiplier,
                )
                multiplier = 2.0
            if multiplier < 1.0:
                logger.warning(
                    "timeout_multiplier=%.3f is below the minimum of 1.0; clamping",
                    multiplier,
                )
                multiplier = 1.0

            timeout = int(test_data.get("timeout", 1800))

            # Parse min_dynamic_timeout with validation.
            raw_min = test_data.get("min_dynamic_timeout", 30)
            try:
                min_dyn = int(raw_min)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid min_dynamic_timeout %r in se3.yaml; using default 30",
                    raw_min,
                )
                min_dyn = 30
            if min_dyn < 1:
                logger.warning(
                    "min_dynamic_timeout=%d is below the minimum of 1; clamping",
                    min_dyn,
                )
                min_dyn = 1

            # Default max_dynamic_timeout respects the user's fallback timeout:
            # a project that deliberately sets test.timeout above 14400s (e.g.
            # a legitimately slow suite) should not have dynamic timeouts
            # silently capped below that explicit intent.
            default_max = max(14400, timeout)
            raw_max = test_data.get("max_dynamic_timeout", default_max)
            try:
                max_dyn = int(raw_max)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid max_dynamic_timeout %r in se3.yaml; using default %d",
                    raw_max, default_max,
                )
                max_dyn = default_max
            if max_dyn < min_dyn:
                logger.warning(
                    "max_dynamic_timeout=%d is below min_dynamic_timeout=%d; "
                    "raising max to match min",
                    max_dyn, min_dyn,
                )
                max_dyn = min_dyn

            return cls(
                command=test_data.get("command"),
                timeout=timeout,
                phases=test_data.get("phases", []),
                fix_loop_max_iterations=fix_loop.get("max_iterations", DEFAULT_MAX_FIX_ITERATIONS),
                timeout_multiplier=multiplier,
                min_dynamic_timeout=min_dyn,
                max_dynamic_timeout=max_dyn,
            )
        except Exception as e:
            logger.warning("Failed to load TestConfig from se3.yaml, using defaults: %s", e)
            return cls()

    def get_phases_for_run(self, is_fix_iteration: bool = False) -> list[dict]:
        """Get phases to run, filtering by fix loop if needed."""
        if not self.phases:
            return []
        if not is_fix_iteration:
            return self.phases
        return [p for p in self.phases if p.get("in_fix_loop", True)]


@dataclass
class ImplementConfig:
    """Implement step configuration loaded from se3.yaml implement: section."""

    group_loc_threshold: int = 300

    @classmethod
    def from_dict(cls, data: dict) -> "ImplementConfig":
        """Create ImplementConfig from dictionary."""
        if not data:
            return cls()
        return cls(
            group_loc_threshold=int(data.get("group_loc_threshold", 300)),
        )

    @classmethod
    def load(cls, project_root: Path) -> "ImplementConfig":
        """Load implement configuration from se3.yaml."""
        config_path = Path(project_root) / "se3.yaml"
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            impl_data = data.get("implement", {})
            if not impl_data or not isinstance(impl_data, dict):
                return cls()
            return cls.from_dict(impl_data)
        except Exception:
            return cls()


@dataclass
class StepConfig:
    """Step sequence configuration loaded from se3.yaml steps: section.

    Allows appending optional steps (e.g. summarize) back into the default
    step sequence via configuration.

    Example se3.yaml:
        steps:
          append:
            - summarize
    """

    append_steps: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, project_root: Path) -> "StepConfig":
        """Load step configuration from se3.yaml."""
        config_path = Path(project_root) / "se3.yaml"
        if not config_path.exists():
            return cls()
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            steps_data = data.get("steps", {})
            if not steps_data or not isinstance(steps_data, dict):
                return cls()
            append_raw = steps_data.get("append", [])
            if not isinstance(append_raw, list):
                return cls()
            return cls(append_steps=[str(s) for s in append_raw])
        except Exception:
            return cls()


def load_step_config(project_root: Optional[Path] = None) -> StepConfig:
    """Load step configuration from project.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        StepConfig instance with loaded or default settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    return StepConfig.load(project_root)


def apply_step_config(steps: list, project_root: Optional[Path] = None) -> list:
    """Append configured steps to the step sequence.

    Reads ``steps.append`` from se3.yaml and appends valid StepType values
    to the end of the step sequence (if not already present).

    Args:
        steps: Original step sequence (list of StepType)
        project_root: Project root directory for loading config

    Returns:
        Modified step sequence with appended steps
    """
    config = load_step_config(project_root)
    if not config.append_steps:
        return steps

    from .engine.models import StepType

    # Build set of existing step values for dedup
    existing = {s.value if hasattr(s, "value") else str(s) for s in steps}

    result = list(steps)
    for step_name in config.append_steps:
        if step_name in existing:
            continue
        # Validate the step name against StepType enum
        try:
            step_type = StepType(step_name)
            result.append(step_type)
            existing.add(step_name)
        except ValueError:
            pass  # Ignore invalid step names

    return result


def get_max_fix_iterations(project_root: Optional[Path] = None) -> int:
    f"""Get the maximum number of fix iterations for the test-verify-fix loop.

    Reads from se3.yaml workflow.max_fix_iterations, defaults to {DEFAULT_MAX_FIX_ITERATIONS}.

    Args:
        project_root: Project root directory. If None, uses current working directory.

    Returns:
        Maximum number of fix iterations allowed.
    """
    if project_root is None:
        project_root = Path.cwd()
    else:
        project_root = Path(project_root)

    config_path = project_root / "se3.yaml"

    if not config_path.exists():
        return DEFAULT_MAX_FIX_ITERATIONS

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        workflow = data.get("workflow", {})
        return workflow.get("max_fix_iterations", DEFAULT_MAX_FIX_ITERATIONS)
    except Exception:
        return DEFAULT_MAX_FIX_ITERATIONS
