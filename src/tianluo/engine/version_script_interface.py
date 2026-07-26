"""Version script interface for SE3.

Provides a unified script-based interface for version management operations.
When a version script exists (tianluo/scripts/version.py or .sh), it is called
via subprocess for get/bump/set operations. When no script exists and
auto_generate_script is enabled, LLM generates one based on the project structure.

Falls back to built-in handlers when no script is available.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from .version_bumper import Version

logger = logging.getLogger(__name__)

# Default script search paths (relative to project root). Canonical
# ``tianluo/`` first, legacy ``se3/`` fallback so pre-rename projects keep
# resolving their existing script.
DEFAULT_SCRIPT_PATHS = [
    "tianluo/scripts/version.py",
    "tianluo/scripts/version.sh",
    "se3/scripts/version.py",
    "se3/scripts/version.sh",
]

# Timeout for script execution (seconds)
SCRIPT_TIMEOUT = 30
VALIDATE_TIMEOUT = 10


class VersionScriptRunner:
    """Runs version script commands via subprocess.

    Provides get_version(), bump_version(), and set_version() methods
    that delegate to a version script's get/bump/set subcommands.

    Args:
        script_path: Path to the version script
        project_root: Project root directory (used as cwd for subprocess)
    """

    def __init__(self, script_path: Path, project_root: Optional[Path] = None):
        self.script_path = Path(script_path)
        self.project_root = project_root or self.script_path.parent.parent.parent

        if not self.script_path.exists():
            raise FileNotFoundError(f"Version script not found: {self.script_path}")

    def _run_script(self, args: list[str], timeout: int = SCRIPT_TIMEOUT) -> str:
        """Run the version script with given arguments.

        Args:
            args: Arguments to pass to the script
            timeout: Timeout in seconds

        Returns:
            Stripped stdout output

        Raises:
            RuntimeError: If script exits with non-zero code or times out
        """
        # Determine how to invoke the script
        script_str = str(self.script_path)
        if script_str.endswith(".py"):
            cmd = [sys.executable, script_str] + args
        elif script_str.endswith(".sh"):
            cmd = ["bash", script_str] + args
        else:
            # Try to run directly (must be executable)
            cmd = [script_str] + args

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.project_root),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Version script timed out after {timeout}s: {' '.join(cmd)}"
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"Failed to run version script: {e}")

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(
                f"Version script failed (exit code {result.returncode}): {stderr}"
            )

        return result.stdout.strip()

    def get_version(self) -> Version:
        """Get current version by calling script's 'get' command.

        Returns:
            Parsed Version object

        Raises:
            RuntimeError: If script fails
            ValueError: If output is not valid semver
        """
        output = self._run_script(["get"])
        return Version.parse(output)

    def bump_version(self, bump_type: str) -> Version:
        """Bump version by calling script's 'bump' command.

        Args:
            bump_type: One of "major", "minor", "patch"

        Returns:
            New Version object after bump

        Raises:
            RuntimeError: If script fails
            ValueError: If output is not valid semver
        """
        output = self._run_script(["bump", "--type", bump_type])
        return Version.parse(output)

    def set_version(self, version_str: str) -> Version:
        """Set version explicitly by calling script's 'set' command.

        Args:
            version_str: Version string to set

        Returns:
            Version object of the set version

        Raises:
            RuntimeError: If script fails
            ValueError: If output is not valid semver
        """
        output = self._run_script(["set", "--version", version_str])
        return Version.parse(output)


def find_version_script(
    project_root: Path,
    config_script_path: Optional[str] = None,
) -> Optional[Path]:
    """Find the version script in the project.

    Searches in order:
    1. Configured script_path (if set)
    2. Default paths: tianluo/scripts/version.py, tianluo/scripts/version.sh

    Args:
        project_root: Project root directory
        config_script_path: Configured script path from VersionConfig (optional)

    Returns:
        Path to the version script, or None if not found
    """
    # Check configured path first
    if config_script_path:
        path = Path(config_script_path)
        if not path.is_absolute():
            path = project_root / path
        if path.exists():
            return path
        return None

    # Check default paths
    for rel_path in DEFAULT_SCRIPT_PATHS:
        path = project_root / rel_path
        if path.exists():
            return path

    return None


def validate_script(
    script_path: Path,
    project_root: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate a version script by running its 'get' command.

    Args:
        script_path: Path to the version script
        project_root: Project root directory

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        runner = VersionScriptRunner(script_path, project_root)
        version = runner.get_version()
        # Verify it's valid semver (Version.parse already validates)
        logger.info(f"Version script validated: get returned {version}")
        return True, None
    except FileNotFoundError as e:
        return False, f"Script not found: {e}"
    except RuntimeError as e:
        return False, f"Script execution failed: {e}"
    except ValueError as e:
        return False, f"Script output is not valid semver: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def generate_version_script(
    project_root: Path,
    script_path: Optional[Path] = None,
) -> Path:
    """Generate a version script using LLM based on project structure.

    Instructs the LLM to write the script directly to disk via its Write tool,
    then validates the result.

    Args:
        project_root: Project root directory
        script_path: Target path for the script (default: tianluo/scripts/version.py)

    Returns:
        Path to the generated script

    Raises:
        RuntimeError: If generation or validation fails
    """
    from .llm_caller import LLMCaller
    from .version_bumper import VersionDetector

    if script_path is None:
        script_path = runtime_dir(project_root) / "scripts" / "version.py"

    # Ensure parent directory exists so the LLM's Write tool can succeed
    script_path.parent.mkdir(parents=True, exist_ok=True)

    # Scan project structure
    detector = VersionDetector()
    project_info = _scan_project_for_generation(project_root, detector)

    # Load template
    template_content = _load_template()

    # Build LLM prompt (includes target file path for direct write)
    prompt = _build_generation_prompt(project_info, template_content, str(script_path))

    # Call LLM to generate and write the script directly
    logger.info("Generating version script via LLM...")
    caller = LLMCaller(project_root, step_id="version_script_gen")
    caller.call(prompt=prompt, require_json=False)

    # Post-write validation: verify the LLM actually wrote the file correctly
    _validate_generated_script(script_path)

    # Make executable
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    logger.info(f"Generated version script: {script_path}")

    # Validate by running the script
    is_valid, error = validate_script(script_path, project_root)
    if not is_valid:
        script_content = script_path.read_text(encoding="utf-8")
        raise RuntimeError(
            f"Generated version script validation failed: {error}\n"
            f"Script path: {script_path}\n"
            f"Script content:\n{script_content[:500]}..."
        )

    return script_path


def _validate_generated_script(script_path: Path) -> None:
    """Validate that the LLM-generated script file exists and is valid Python.

    Args:
        script_path: Path where the script should have been written

    Raises:
        RuntimeError: If file doesn't exist, has invalid syntax, or missing required functions
    """
    # Check file exists
    if not script_path.exists():
        raise RuntimeError(
            f"LLM did not write the version script to {script_path}. "
            f"The file does not exist after LLM call."
        )

    # Read and check content
    content = script_path.read_text(encoding="utf-8")
    if not content.strip():
        raise RuntimeError(
            f"LLM wrote an empty file to {script_path}."
        )

    # Verify valid Python syntax
    try:
        compile(content, str(script_path), "exec")
    except SyntaxError as e:
        raise RuntimeError(
            f"Generated version script has invalid Python syntax: {e}\n"
            f"Script path: {script_path}\n"
            f"Script content:\n{content[:500]}..."
        )

    # Verify required functions exist
    if "def read_version" not in content:
        raise RuntimeError(
            f"Generated version script is missing required function 'read_version()'. "
            f"Script path: {script_path}\n"
            f"Script content:\n{content[:500]}..."
        )


def _scan_project_for_generation(project_root: Path, detector) -> dict:
    """Scan project structure to inform script generation.

    Returns:
        Dict with project info: type, version_files found, etc.
    """
    info = {
        "project_type": "unknown",
        "version_files": [],
        "current_version": None,
    }

    # Detect project type
    project_type = detector.detect_project_type(project_root)
    info["project_type"] = project_type.value

    # Check common version file locations
    version_file_candidates = [
        ("pyproject.toml", "project.version or tool.poetry.version"),
        ("package.json", "version field"),
        ("Cargo.toml", "package.version"),
        ("setup.py", "version argument in setup()"),
        ("version.py", "__version__ variable"),
        ("setup.cfg", "version in metadata section"),
    ]

    for filename, description in version_file_candidates:
        path = project_root / filename
        if path.exists():
            info["version_files"].append({
                "path": filename,
                "description": description,
            })

    # Also check src/ directory for version.py or __init__.py
    src_dir = project_root / "src"
    if src_dir.exists():
        for item in src_dir.iterdir():
            if item.is_dir():
                for vf in ["__init__.py", "version.py"]:
                    vf_path = item / vf
                    if vf_path.exists():
                        rel = vf_path.relative_to(project_root)
                        info["version_files"].append({
                            "path": str(rel),
                            "description": f"__version__ variable in {rel}",
                        })

    # Try to read current version
    try:
        version_file = detector.get_version_file_path(project_root)
        if version_file:
            info["current_version"] = detector.read_version(version_file)
    except Exception:
        pass

    return info


def _load_template() -> str:
    """Load the version script template."""
    template_path = Path(__file__).parent.parent / "templates" / "version_script.py.tmpl"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return "(template not available)"


def _build_generation_prompt(project_info: dict, template: str, target_path: str) -> str:
    """Build the LLM prompt for script generation.

    Args:
        project_info: Dict with project type, version files, current version
        template: Template script content
        target_path: Absolute path where the script should be written
    """
    version_files_desc = ""
    if project_info["version_files"]:
        for vf in project_info["version_files"]:
            version_files_desc += f"  - {vf['path']}: {vf['description']}\n"
    else:
        version_files_desc = "  (no standard version files found)\n"

    current_ver = project_info.get("current_version", "unknown")

    return f"""You are generating a Python version script for an SE3 project.

## Project Info
- Project type: {project_info['project_type']}
- Version files detected:
{version_files_desc}
- Current version: {current_ver}

## Task
Generate a complete Python script that implements the version management interface.
The script must implement `read_version()` and `write_version(version)` functions
that read from and write to the project's SINGLE authoritative version source.

**IMPORTANT: You MUST use your Write tool to write the complete script to this exact file path:**
{target_path}

Do NOT output the script as text. Use the Write tool to write it directly to the file.

## Template Structure
Follow this exact structure (argparse CLI, get/bump/set commands):

```python
{template}
```

## Requirements
1. Replace the TODO `read_version()` and `write_version()` with real implementations
2. The version source must be ONE file (single source of truth)
3. `read_version()` must return a clean semver string (e.g., "1.2.3")
4. `write_version()` must write the version back to the same file
5. Keep the argparse CLI and `bump_version()` logic from the template unchanged
6. Handle the `v` prefix if present (strip on read, preserve on write if original had it)
7. Do NOT add any external dependencies beyond Python stdlib
8. Start with #!/usr/bin/env python3 and include the complete implementation
"""
