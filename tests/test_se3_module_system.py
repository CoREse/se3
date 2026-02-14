#!/usr/bin/env python3
"""Test script for SE3 Module System - Initialization only."""

import os
import shutil
import tempfile
import subprocess
from pathlib import Path

# Get the absolute path to the project root
PROJECT_ROOT = Path(__file__).parent.resolve()

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
        cmd = "/home/cre/.pixi/envs/pip/bin/se3 init"
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

        # Check SE3.md has metadata
        se3_content = se3_md.read_text()
        if "Checksum:" not in se3_content:
            print(f"ERROR: SE3.md missing checksum metadata")
            return False

        # Check CLAUDE.md has project-specific content
        claude_content = claude_md.read_text()
        if "{{PROJECT_NAME}}" in claude_content:
            print(f"ERROR: CLAUDE.md placeholders not replaced")
            return False

        print("✓ Initialization test passed")
        return True

def main():
    print("Testing SE3 Module System Initialization...")
    print("=" * 50)

    tests_passed = 0
    tests_failed = 0

    # Test initialization
    if test_initialization():
        tests_passed += 1
    else:
        tests_failed += 1

    print()
    print("=" * 50)
    print(f"Results: {tests_passed} passed, {tests_failed} failed")

    if tests_failed == 0:
        print("All initialization tests passed!")
    else:
        print("Some initialization tests failed!")

    return tests_failed == 0

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
