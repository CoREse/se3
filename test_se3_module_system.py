#!/usr/bin/env python3
"""Test script for SE3 Module System."""

import os
import shutil
import tempfile
import subprocess
from pathlib import Path

# Get the absolute path to the project root
PROJECT_ROOT = Path(__file__).parent.resolve()
TOOLS_PATH = str(PROJECT_ROOT / "tools")

def run_command(cmd: str, cwd: str = None) -> tuple:
    """Run a shell command and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=cwd
    )
    return result.stdout, result.stderr, result.returncode

def test_initialization():
    """Test se3 init command creates the correct files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Testing in temporary directory: {temp_dir}")

        # Run se3 init
        cmd = f"PYTHONPATH={TOOLS_PATH} /home/cre/.pixi/envs/pip/bin/se3 init"
        stdout, stderr, returncode = run_command(cmd, cwd=temp_dir)

        if returncode != 0:
            print(f"ERROR: se3 init failed\nstdout: {stdout}\nstderr: {stderr}")
            return False

        # Check if .claude directory exists
        claude_dir = Path(temp_dir) / ".claude"
        if not claude_dir.exists():
            print(f"ERROR: .claude directory not created")
            return False

        # Check if CLAUDE.md exists
        claude_md = claude_dir / "CLAUDE.md"
        if not claude_md.exists():
            print(f"ERROR: CLAUDE.md not created")
            return False

        # Check if SE3.md exists
        se3_md = claude_dir / "SE3.md"
        if not se3_md.exists():
            print(f"ERROR: SE3.md not created")
            return False

        print("✓ Initialization test passed")
        return True

def test_checksum_validation():
    """Test se3 doctor checksum validation."""
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"Testing in temporary directory: {temp_dir}")

        # Run se3 init
        cmd = f"PYTHONPATH={TOOLS_PATH} /home/cre/.pixi/envs/pip/bin/se3 init"
        run_command(cmd, cwd=temp_dir)

        # Check initial state
        cmd = f"PYTHONPATH={TOOLS_PATH} python -m se3_tools.commands.lint {temp_dir}"
        stdout, stderr, returncode = run_command(cmd)

        if returncode != 0:
            print(f"ERROR: Initial check failed\nstdout: {stdout}\nstderr: {stderr}")
            return False

        # Tamper with SE3.md
        se3_md = Path(temp_dir) / ".claude" / "SE3.md"
        if not se3_md.exists():
            print(f"ERROR: SE3.md not found")
            return False

        with open(se3_md, "a") as f:
            f.write("\ntampered content")

        # Check again - should fail
        cmd = f"PYTHONPATH={TOOLS_PATH} python -m se3_tools.commands.lint {temp_dir}"
        stdout, stderr, returncode = run_command(cmd)

        if returncode == 0:
            print(f"ERROR: Tampering not detected\nstdout: {stdout}")
            return False

        print("✓ Checksum validation test passed")
        return True

def main():
    print("Testing SE3 Module System...")
    print("=" * 50)

    tests_passed = 0
    tests_failed = 0

    # Test initialization
    if test_initialization():
        tests_passed += 1
    else:
        tests_failed += 1

    print()

    # Test checksum validation
    if test_checksum_validation():
        tests_passed += 1
    else:
        tests_failed += 1

    print()
    print("=" * 50)
    print(f"Results: {tests_passed} passed, {tests_failed} failed")

    if tests_failed == 0:
        print("All tests passed!")
    else:
        print("Some tests failed!")

    return tests_failed == 0

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
