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


def load_session_config(project_root: Optional[Path] = None) -> dict:
    """Load session configuration from project.
    
    Args:
        project_root: Project root directory. If None, uses current working directory.
        
    Returns:
        Dictionary with session configuration settings.
    """
    if project_root is None:
        project_root = Path.cwd()
    
    config_path = project_root / "se3.yaml"
    
    if not config_path.exists():
        return {"max_tasks_per_change": 5}
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        
        # Get session settings from config
        session = data.get("session", {})
        return {
            "max_tasks_per_change": session.get("max_tasks_per_change", 5),
        }
    except Exception:
        return {"max_tasks_per_change": 5}


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
        return {"enabled": True, "steps": ["propose", "design"]}
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        
        confirmation = data.get("confirmation", {})
        return {
            "enabled": confirmation.get("enabled", True),
            "steps": confirmation.get("steps", ["propose", "design"]),
            "reviewer": confirmation.get("reviewer", "human"),
            "llm_reviewer": confirmation.get("llm_reviewer", {}),
        }
    except Exception:
        return {"enabled": True, "steps": ["propose", "design"]}


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
    
    steps_requiring_confirm = config.get("steps", ["propose", "design"])
    
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


def load_claude_commands(project_root: Optional[Path] = None) -> list[dict]:
    """Load Claude CLI commands from project and global configuration.
    
    Loads commands from se3.yaml (project-level) and ~/.se3/config.yaml (global-level).
    Commands are sorted by priority (higher first). String entries are normalized to dicts.
    
    Args:
        project_root: Project root directory. If None, uses global config only.
        
    Returns:
        List of command dictionaries with 'cmd' and 'priority' keys, sorted by priority.
    """
    commands = []
    
    # Load global config
    global_config_path = Path.home() / ".se3" / "config.yaml"
    if global_config_path.exists():
        try:
            with open(global_config_path, "r", encoding="utf-8") as f:
                global_data = yaml.safe_load(f) or {}
            global_commands = global_data.get("claude_commands", [])
            commands.extend(_normalize_commands(global_commands))
        except Exception:
            pass
    
    # Load project config (overrides global)
    if project_root is not None:
        project_root = Path(project_root)
        project_config_path = project_root / "se3.yaml"
        if project_config_path.exists():
            try:
                with open(project_config_path, "r", encoding="utf-8") as f:
                    project_data = yaml.safe_load(f) or {}
                project_commands = project_data.get("claude_commands", [])
                if project_commands:
                    # Project commands override global
                    commands = _normalize_commands(project_commands)
            except Exception:
                pass
    
    # If no commands found, use default
    if not commands:
        commands = [{"cmd": "claude", "priority": 0}]
    
    # Sort by priority (higher first)
    commands.sort(key=lambda x: x.get("priority", 0), reverse=True)
    
    return commands


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


def is_chinese_language(language: str) -> bool:
    """Check if the language is Chinese.

    Args:
        language: Language code (e.g., 'en', 'zh', 'zh-CN')

    Returns:
        True if the language is Chinese
    """
    return language.lower().startswith("zh")


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
