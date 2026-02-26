"""Unified configuration loading for SE3.

Supports two-level config:
- Global: ~/.se3/config.yaml (shared across all projects)
- Project: se3.yaml (project-specific, overrides global)
"""

# Verify: se3-config/Using default configuration
# Verify: se3-config/Custom configuration
# Verify: se3-config/Global configuration
# Verify: se3-config/Project overrides global

from pathlib import Path
from typing import Any, Dict, List, Optional


def load_global_config() -> Dict[str, Any]:
    """Load global config from ~/.se3/config.yaml."""
    config_file = Path.home() / ".se3" / "config.yaml"
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def load_project_config(project_root: Path | str) -> Dict[str, Any]:
    """Load project config from se3.yaml (fallback: se3.config.yaml)."""
    if isinstance(project_root, str):
        project_root = Path(project_root)
    config_file = project_root / "se3.yaml"
    if not config_file.exists():
        config_file = project_root / "se3.config.yaml"  # legacy fallback
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            import sys
            print(f"[se3-config] Warning: Failed to parse {config_file}: {e}", file=sys.stderr)
            pass
    return {}


def merge_configs(global_cfg: Dict[str, Any], project_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Merge global and project configs. Project-level overrides global.

    Top-level keys from project_cfg replace global_cfg entirely (no deep merge).
    """
    merged = dict(global_cfg)
    merged.update(project_cfg)
    return merged


def load_claude_commands(project_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load and sort claude commands from config.

    Resolution order:
    1. Project se3.yaml claude_commands (if present, replaces global)
    2. Global ~/.se3/config.yaml claude_commands
    3. Default: [{cmd: "claude", priority: 0}]

    Returns list sorted by priority descending, then list order.
    """
    global_cfg = load_global_config()
    project_cfg = load_project_config(project_root) if project_root else {}

    # Project-level claude_commands fully overrides global
    commands = project_cfg.get("claude_commands") or global_cfg.get("claude_commands")

    if not commands:
        return [{"cmd": "claude", "priority": 0}]

    # Normalize: ensure each entry has cmd and priority
    normalized = []
    for entry in commands:
        if isinstance(entry, str):
            normalized.append({"cmd": entry, "priority": 0})
        elif isinstance(entry, dict):
            normalized.append({
                "cmd": entry.get("cmd", "claude"),
                "priority": entry.get("priority", 0),
            })

    if not normalized:
        return [{"cmd": "claude", "priority": 0}]

    # Sort by priority descending (higher first), stable sort preserves list order for ties
    normalized.sort(key=lambda x: -x["priority"])

    return normalized


def load_human_call_config(project_root: Optional[Path] = None) -> Dict[str, Any]:
    """Load human call configuration from config.

    Resolution order:
    1. Project se3.yaml human_call (if present, overrides global)
    2. Global ~/.se3/config.yaml human_call
    3. Default values

    Returns dict with:
        - language: Language code (default: "en")
        - timeout_days: Async call timeout in days (default: 7)
        - directory: Human calls directory (default: "human-calls")
    """
    global_cfg = load_global_config()
    project_cfg = load_project_config(project_root) if project_root else {}

    # Merge human_call configs: project overrides global
    global_hc = global_cfg.get("human_call", {})
    project_hc = project_cfg.get("human_call", {})

    merged = dict(global_hc)
    merged.update(project_hc)

    return {
        "language": merged.get("language", "en"),
        "timeout_days": merged.get("timeout_days", 7),
        "directory": merged.get("directory", "human-calls"),
    }


def load_session_config(project_root: Optional[Path] = None) -> Dict[str, Any]:
    """Load session configuration from config.

    Resolution order:
    1. Project se3.yaml session (if present, overrides global)
    2. Global ~/.se3/config.yaml session
    3. Default values

    Returns dict with:
        - max_tasks_per_change: Max tasks per group (default: 5)
        - max_progress_entries: Max progress entries before archiving (default: 20)
    """
    global_cfg = load_global_config()
    project_cfg = load_project_config(project_root) if project_root else {}

    # Merge session configs: project overrides global
    global_session = global_cfg.get("session", {})
    project_session = project_cfg.get("session", {})

    merged = dict(global_session)
    merged.update(project_session)

    return {
        "max_tasks_per_change": merged.get("max_tasks_per_change", 5),
        "max_progress_entries": merged.get("max_progress_entries", 20),
    }


def get_language_labels(language: str) -> Dict[str, str]:
    """Get human call template labels for a given language.

    Args:
        language: Language code (e.g., "en", "zh-CN", "zh-TW")

    Returns dict with localized labels for human call templates.
    """
    # Normalize language code
    lang_lower = language.lower()

    # Chinese variants (zh-CN, zh-TW, zh-HK, zh, etc.)
    if lang_lower.startswith("zh"):
        return {
            "type": "类型",
            "urgency": "紧急程度",
            "context": "上下文",
            "tasks": "当前任务状态",
            "response": "回复",
            "prompt": "<!-- 人类：请在下方输入您的回复 -->",
            "request_prefix": "请求",
            "source": "来源",
        }

    # Default to English
    return {
        "type": "Type",
        "urgency": "Urgency",
        "context": "Context",
        "tasks": "Current Task States",
        "response": "Response",
        "prompt": "<!-- Human: write your response below -->",
        "request_prefix": "Request",
        "source": "Source",
    }


def is_chinese_language(language: str) -> bool:
    """Check if the language code is a Chinese variant.

    Args:
        language: Language code (e.g., "zh-CN", "zh-TW", "en-US")

    Returns True if the language is a Chinese variant.
    """
    return language.lower().startswith("zh")
