"""SE3 configuration management."""

from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum
import yaml


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
    agents: list[dict] = []

    # --- Load global config ---
    global_config_path = Path.home() / ".se3" / "config.yaml"
    global_data: dict = {}
    if global_config_path.exists():
        try:
            with open(global_config_path, "r", encoding="utf-8") as f:
                global_data = yaml.safe_load(f) or {}
        except Exception:
            pass

    global_agents_raw = global_data.get("agents")
    if global_agents_raw:
        agents = _normalize_agents(global_agents_raw)
    else:
        global_commands = global_data.get("claude_commands", [])
        if global_commands:
            agents = _commands_to_agents(_normalize_commands(global_commands))

    # --- Load project config (overrides global) ---
    if project_root is not None:
        project_root = Path(project_root)
        project_config_path = project_root / "se3.yaml"
        if project_config_path.exists():
            try:
                with open(project_config_path, "r", encoding="utf-8") as f:
                    project_data = yaml.safe_load(f) or {}
            except Exception:
                project_data = {}

            project_agents_raw = project_data.get("agents")
            if project_agents_raw:
                agents = _normalize_agents(project_agents_raw)
            else:
                project_commands = project_data.get("claude_commands", [])
                if project_commands:
                    agents = _commands_to_agents(_normalize_commands(project_commands))

    # Default: single claude agent
    if not agents:
        agents = [{"name": "claude", "type": "claude-code", "cmd": "claude", "priority": 0}]

    # Sort by priority (higher first)
    agents.sort(key=lambda x: x.get("priority", 0), reverse=True)

    return agents


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


def _normalize_agents(agents_raw: list) -> list[dict]:
    """Normalize agent entries from config to standard dicts.

    Each entry can be a dict with keys ``name``, ``type``, ``cmd``, ``priority``.
    Strings are treated as cmd values with defaults for everything else.
    """
    normalized = []
    for i, entry in enumerate(agents_raw):
        if isinstance(entry, str):
            normalized.append({
                "name": entry,
                "type": "claude-code",
                "cmd": entry,
                "priority": 0,
            })
        elif isinstance(entry, dict):
            cmd = entry.get("cmd", "claude")
            normalized.append({
                "name": entry.get("name", cmd),
                "type": entry.get("type", "claude-code"),
                "cmd": cmd,
                "priority": entry.get("priority", 0),
            })
    return normalized


def _commands_to_agents(commands: list[dict]) -> list[dict]:
    """Convert legacy command dicts to agent dicts."""
    return [
        {
            "name": cmd.get("cmd", "claude"),
            "type": "claude-code",
            "cmd": cmd.get("cmd", "claude"),
            "priority": cmd.get("priority", 0),
        }
        for cmd in commands
    ]


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


@dataclass
class TestConfig:
    """Test step configuration loaded from se3.yaml test: section."""

    command: Optional[str] = None
    timeout: int = 1800
    phases: list[dict] = field(default_factory=list)
    fix_loop_max_iterations: int = 3

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
            return cls(
                command=test_data.get("command"),
                timeout=test_data.get("timeout", 1800),
                phases=test_data.get("phases", []),
                fix_loop_max_iterations=fix_loop.get("max_iterations", 3),
            )
        except Exception:
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
    """Get the maximum number of fix iterations for the test-verify-fix loop.

    Reads from se3.yaml workflow.max_fix_iterations, defaults to 3.

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
        return 3

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        workflow = data.get("workflow", {})
        return workflow.get("max_fix_iterations", 3)
    except Exception:
        return 3
