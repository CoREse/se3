"""Unified configuration loading for SE3.

Supports two-level config:
- Global: ~/.se3/config.yaml (shared across all projects)
- Project: se3.config.yaml (project-specific, overrides global)
"""

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


def load_project_config(project_root: Path) -> Dict[str, Any]:
    """Load project config from se3.config.yaml."""
    config_file = project_root / "se3.config.yaml"
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
    1. Project se3.config.yaml claude_commands (if present, replaces global)
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
