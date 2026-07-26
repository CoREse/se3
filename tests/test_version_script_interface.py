"""Tests for version_script_interface module.

Tests VersionScriptRunner, find_version_script, validate_script,
and integration with VersionBumper's script mode.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.version_script_interface import (
    VersionScriptRunner,
    find_version_script,
    validate_script,
    generate_version_script,
    _validate_generated_script,
)
from tianluo.engine.version_bumper import Version, VersionBumper, VersionConfig, BumpType


# ---------------------------------------------------------------------------
# Helpers: create real test scripts
# ---------------------------------------------------------------------------

def _write_test_script(path: Path, version: str = "1.2.3") -> Path:
    """Create a minimal working version script for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import argparse, re, sys

        VERSION_FILE = __file__ + ".version"

        def _ensure_version_file():
            import os
            if not os.path.exists(VERSION_FILE):
                with open(VERSION_FILE, "w") as f:
                    f.write("{version}")

        def read_version():
            _ensure_version_file()
            with open(VERSION_FILE) as f:
                return f.read().strip()

        def write_version(v):
            with open(VERSION_FILE, "w") as f:
                f.write(v)

        def bump_version(current, bump_type):
            m = re.match(r"(\\d+)\\.(\\d+)\\.(\\d+)", current)
            major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if bump_type == "major": major += 1; minor = 0; patch = 0
            elif bump_type == "minor": minor += 1; patch = 0
            elif bump_type == "patch": patch += 1
            return f"{{major}}.{{minor}}.{{patch}}"

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd", required=True)
        sub.add_parser("get")
        bp = sub.add_parser("bump")
        bp.add_argument("--type", required=True)
        sp = sub.add_parser("set")
        sp.add_argument("--version", required=True)

        args = parser.parse_args()
        if args.cmd == "get":
            print(read_version())
        elif args.cmd == "bump":
            cur = read_version()
            nv = bump_version(cur, args.type)
            write_version(nv)
            print(nv)
        elif args.cmd == "set":
            write_version(args.version)
            print(args.version)
    """), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _write_failing_script(path: Path) -> Path:
    """Create a script that always fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        print("Something went wrong", file=sys.stderr)
        sys.exit(1)
    """), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _write_invalid_output_script(path: Path) -> Path:
    """Create a script that outputs non-semver."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd", required=True)
        sub.add_parser("get")
        args = parser.parse_args()
        if args.cmd == "get":
            print("not-a-version")
    """), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# ---------------------------------------------------------------------------
# VersionScriptRunner tests
# ---------------------------------------------------------------------------

class TestVersionScriptRunner:
    """Tests for VersionScriptRunner."""

    def test_get_version_success(self, tmp_path):
        script = _write_test_script(tmp_path / "version.py", "1.2.3")
        runner = VersionScriptRunner(script, tmp_path)
        version = runner.get_version()
        assert version == Version(1, 2, 3)

    def test_get_version_with_prefix(self, tmp_path):
        """Script that outputs 'v1.2.3' should be parseable."""
        script = tmp_path / "version.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import argparse
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers(dest="cmd", required=True)
            sub.add_parser("get")
            args = parser.parse_args()
            if args.cmd == "get":
                print("v2.0.1")
        """), encoding="utf-8")
        runner = VersionScriptRunner(script, tmp_path)
        version = runner.get_version()
        assert version == Version(2, 0, 1)

    def test_bump_version_minor(self, tmp_path):
        script = _write_test_script(tmp_path / "version.py", "1.2.3")
        runner = VersionScriptRunner(script, tmp_path)
        new_version = runner.bump_version("minor")
        assert new_version == Version(1, 3, 0)

    def test_bump_version_major(self, tmp_path):
        script = _write_test_script(tmp_path / "version.py", "1.2.3")
        runner = VersionScriptRunner(script, tmp_path)
        new_version = runner.bump_version("major")
        assert new_version == Version(2, 0, 0)

    def test_bump_version_patch(self, tmp_path):
        script = _write_test_script(tmp_path / "version.py", "1.2.3")
        runner = VersionScriptRunner(script, tmp_path)
        new_version = runner.bump_version("patch")
        assert new_version == Version(1, 2, 4)

    def test_set_version(self, tmp_path):
        script = _write_test_script(tmp_path / "version.py", "1.0.0")
        runner = VersionScriptRunner(script, tmp_path)
        result = runner.set_version("3.0.0")
        assert result == Version(3, 0, 0)
        # Verify it persisted
        v = runner.get_version()
        assert v == Version(3, 0, 0)

    def test_script_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            VersionScriptRunner(tmp_path / "nonexistent.py", tmp_path)

    def test_script_execution_error(self, tmp_path):
        script = _write_failing_script(tmp_path / "version.py")
        runner = VersionScriptRunner(script, tmp_path)
        with pytest.raises(RuntimeError, match="failed.*exit code"):
            runner.get_version()

    def test_invalid_version_output(self, tmp_path):
        script = _write_invalid_output_script(tmp_path / "version.py")
        runner = VersionScriptRunner(script, tmp_path)
        with pytest.raises(ValueError, match="Invalid SemVer"):
            runner.get_version()

    def test_script_timeout(self, tmp_path):
        """Test that timeout is handled."""
        script = tmp_path / "version.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import time, argparse
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers(dest="cmd", required=True)
            sub.add_parser("get")
            args = parser.parse_args()
            time.sleep(10)
            print("1.0.0")
        """), encoding="utf-8")
        runner = VersionScriptRunner(script, tmp_path)
        with pytest.raises(RuntimeError, match="timed out"):
            runner._run_script(["get"], timeout=1)


# ---------------------------------------------------------------------------
# find_version_script tests
# ---------------------------------------------------------------------------

class TestFindVersionScript:
    """Tests for find_version_script."""

    def test_find_default_py_path(self, tmp_path):
        script = tmp_path / "se3" / "scripts" / "version.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# placeholder")
        result = find_version_script(tmp_path)
        assert result == script

    def test_find_default_sh_path(self, tmp_path):
        script = tmp_path / "se3" / "scripts" / "version.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# placeholder")
        result = find_version_script(tmp_path)
        assert result == script

    def test_py_preferred_over_sh(self, tmp_path):
        py_script = tmp_path / "se3" / "scripts" / "version.py"
        sh_script = tmp_path / "se3" / "scripts" / "version.sh"
        py_script.parent.mkdir(parents=True, exist_ok=True)
        py_script.write_text("# py")
        sh_script.write_text("# sh")
        result = find_version_script(tmp_path)
        assert result == py_script

    def test_find_configured_path(self, tmp_path):
        custom = tmp_path / "tools" / "my_version.py"
        custom.parent.mkdir(parents=True, exist_ok=True)
        custom.write_text("# custom")
        result = find_version_script(tmp_path, config_script_path="tools/my_version.py")
        assert result == custom

    def test_configured_path_not_found(self, tmp_path):
        result = find_version_script(tmp_path, config_script_path="nonexistent.py")
        assert result is None

    def test_not_found(self, tmp_path):
        result = find_version_script(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# validate_script tests
# ---------------------------------------------------------------------------

class TestValidateScript:
    """Tests for validate_script."""

    def test_valid_script(self, tmp_path):
        script = _write_test_script(tmp_path / "version.py", "2.0.0")
        is_valid, error = validate_script(script, tmp_path)
        assert is_valid is True
        assert error is None

    def test_failing_script(self, tmp_path):
        script = _write_failing_script(tmp_path / "version.py")
        is_valid, error = validate_script(script, tmp_path)
        assert is_valid is False
        assert "failed" in error.lower()

    def test_invalid_output(self, tmp_path):
        script = _write_invalid_output_script(tmp_path / "version.py")
        is_valid, error = validate_script(script, tmp_path)
        assert is_valid is False
        assert "semver" in error.lower() or "invalid" in error.lower()

    def test_nonexistent_script(self, tmp_path):
        is_valid, error = validate_script(tmp_path / "nope.py", tmp_path)
        assert is_valid is False
        assert "not found" in error.lower()


# ---------------------------------------------------------------------------
# _validate_generated_script tests
# ---------------------------------------------------------------------------

class TestValidateGeneratedScript:
    """Tests for _validate_generated_script (post-write validation)."""

    def test_valid_script(self, tmp_path):
        script = tmp_path / "version.py"
        script.write_text("#!/usr/bin/env python3\ndef read_version(): pass\n")
        _validate_generated_script(script)  # Should not raise

    def test_file_not_found(self, tmp_path):
        script = tmp_path / "nonexistent.py"
        with pytest.raises(RuntimeError, match="does not exist"):
            _validate_generated_script(script)

    def test_empty_file(self, tmp_path):
        script = tmp_path / "version.py"
        script.write_text("")
        with pytest.raises(RuntimeError, match="empty"):
            _validate_generated_script(script)

    def test_invalid_python_syntax(self, tmp_path):
        script = tmp_path / "version.py"
        script.write_text("def read_version(\n  this is not valid python")
        with pytest.raises(RuntimeError, match="invalid Python syntax"):
            _validate_generated_script(script)

    def test_missing_read_version_function(self, tmp_path):
        script = tmp_path / "version.py"
        script.write_text("#!/usr/bin/env python3\ndef write_version(v): pass\n")
        with pytest.raises(RuntimeError, match="read_version"):
            _validate_generated_script(script)


# ---------------------------------------------------------------------------
# VersionBumper script mode integration tests
# ---------------------------------------------------------------------------

class TestVersionBumperScriptMode:
    """Tests for VersionBumper's script mode integration."""

    def _make_bumper_config(self):
        """Create a VersionConfig for testing."""
        return VersionConfig(
            enabled=True,
            file_path=None,
            include_in_commit_message=True,
        )

    def test_script_mode_detection(self, tmp_path):
        """When version script exists, VersionBumper uses script mode."""
        _write_test_script(tmp_path / "se3" / "scripts" / "version.py", "1.0.0")
        config = self._make_bumper_config()
        bumper = VersionBumper(config)
        result = bumper.detect_version_file(tmp_path)
        assert result is not None
        assert bumper._use_script_mode is True
        assert bumper._script_runner is not None

    def test_fallback_when_no_script(self, tmp_path):
        """Without script, fallback to built-in handler detection."""
        # Create a pyproject.toml for detection
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "0.5.0"\n'
        )
        config = self._make_bumper_config()
        bumper = VersionBumper(config)
        result = bumper.detect_version_file(tmp_path)
        assert result is not None
        assert bumper._use_script_mode is False

    def test_script_mode_read_version(self, tmp_path):
        """Script mode read_version delegates to runner."""
        _write_test_script(tmp_path / "se3" / "scripts" / "version.py", "3.1.4")
        config = self._make_bumper_config()
        bumper = VersionBumper(config)
        bumper.detect_version_file(tmp_path)
        version = bumper.read_version()
        assert version == "3.1.4"

    def test_script_mode_bump_version(self, tmp_path):
        """Script mode bump_version delegates to runner."""
        _write_test_script(tmp_path / "se3" / "scripts" / "version.py", "1.0.0")
        config = self._make_bumper_config()
        bumper = VersionBumper(config)
        bumper.detect_version_file(tmp_path)
        new_version = bumper.bump_version(bump_type=BumpType.MINOR)
        assert new_version == "1.1.0"

    def test_script_mode_rollback(self, tmp_path):
        """Script mode rollback uses set command to restore version."""
        _write_test_script(tmp_path / "se3" / "scripts" / "version.py", "2.0.0")
        config = self._make_bumper_config()
        bumper = VersionBumper(config)
        bumper.detect_version_file(tmp_path)

        # Bump then rollback
        bumper.bump_version(bump_type=BumpType.MINOR)
        assert bumper.read_version() == "2.1.0"

        bumper.rollback()
        assert bumper.read_version() == "2.0.0"

    def test_script_mode_clear_backup(self, tmp_path):
        """Clear backup works in script mode."""
        _write_test_script(tmp_path / "se3" / "scripts" / "version.py", "1.0.0")
        config = self._make_bumper_config()
        bumper = VersionBumper(config)
        bumper.detect_version_file(tmp_path)

        bumper.bump_version(bump_type=BumpType.PATCH)
        bumper.clear_backup()
        assert bumper._backup_version is None
        assert bumper._backup_path is None


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestVersionConfigNewFields:
    """Tests for new VersionConfig fields."""

    def test_defaults(self):
        from tianluo.config import VersionConfig as AppVersionConfig
        config = AppVersionConfig()
        assert config.script_path is None
        assert config.auto_generate_script is True

    def test_from_dict(self):
        from tianluo.config import VersionConfig as AppVersionConfig
        data = {
            "version": {
                "script_path": "tools/ver.py",
                "auto_generate_script": False,
            }
        }
        config = AppVersionConfig.from_dict(data)
        assert config.script_path == "tools/ver.py"
        assert config.auto_generate_script is False

    def test_from_dict_defaults(self):
        from tianluo.config import VersionConfig as AppVersionConfig
        config = AppVersionConfig.from_dict({})
        assert config.script_path is None
        assert config.auto_generate_script is True

    def test_from_dict_ignores_deprecated_bump_rules(self, caplog):
        """Legacy ``version.bump_rules`` is silently dropped with a single
        deprecation warning; the rest of the section still loads."""
        import logging

        from tianluo.config import VersionConfig as AppVersionConfig

        data = {
            "version": {
                "enabled": True,
                "bump_rules": {"feature": "major"},
                "confidence_threshold": "high",
            }
        }
        with caplog.at_level(logging.WARNING, logger="tianluo.config"):
            config = AppVersionConfig.from_dict(data)

        assert not hasattr(config, "bump_rules")
        assert config.enabled is True
        assert config.confidence_threshold == "high"
        assert any("bump_rules" in rec.message for rec in caplog.records)

    def test_from_dict_ignores_deprecated_smart_version_analysis(self, caplog):
        """Legacy ``version.smart_version_analysis`` is silently dropped with
        a deprecation warning."""
        import logging

        from tianluo.config import VersionConfig as AppVersionConfig

        data = {
            "version": {
                "enabled": True,
                "smart_version_analysis": False,
            }
        }
        with caplog.at_level(logging.WARNING, logger="tianluo.config"):
            config = AppVersionConfig.from_dict(data)

        assert not hasattr(config, "smart_version_analysis")
        assert config.enabled is True
        assert any(
            "smart_version_analysis" in rec.message for rec in caplog.records
        )
