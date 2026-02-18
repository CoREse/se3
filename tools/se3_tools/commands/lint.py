"""Spec lint command for SE 3.0."""

# Verify: se3-scaffold/Spec validation

import re
from pathlib import Path
from typing import Dict, List, Any

import typer

from se3_tools.utils import discover_specs, parse_spec, get_exit_code

app = typer.Typer(invoke_without_command=True)


def validate_spec(filepath: str) -> List[Dict[str, Any]]:
    """
    Validate a single spec file.

    Args:
        filepath: Path to the spec.md file

    Returns:
        List of validation results with keys: file, line, level, message
    """
    results = []
    path = Path(filepath)

    try:
        parsed = parse_spec(filepath)
    except Exception as e:
        results.append({
            "file": filepath,
            "line": 0,
            "level": "runtime",
            "message": f"Failed to parse spec: {e}",
        })
        return results

    # Check for title
    if not parsed["title"]:
        results.append({
            "file": filepath,
            "line": 1,
            "level": "error",
            "message": "Missing title header (expected '# <name> Specification')",
        })

    # Check for Purpose section
    if not parsed["purpose"]:
        results.append({
            "file": filepath,
            "line": 1,
            "level": "error",
            "message": "Missing '## Purpose' section",
        })

    # Check for Requirements section
    if not parsed["requirements"]:
        results.append({
            "file": filepath,
            "line": 1,
            "level": "error",
            "message": "Missing '## Requirements' section or no requirements found",
        })

    # Check each requirement
    for req in parsed["requirements"]:
        req_title = req.get("title", "")
        req_line = req.get("line", 1)
        req_content = " ".join(req.get("content", []))

        # Check if requirement uses SHALL
        uses_shall = "**SHALL**" in req_content or "SHALL" in req_content

        # Check for scenarios
        scenarios = req.get("scenarios", [])

        if uses_shall and not scenarios:
            results.append({
                "file": filepath,
                "line": req_line,
                "level": "error",
                "message": f"Requirement '{req_title}' uses SHALL but has no WHEN/THEN scenarios",
            })

        # Validate each scenario
        for scenario in scenarios:
            scenario_title = scenario.get("title", "")
            scenario_line = scenario.get("line", 1)

            if not scenario.get("when"):
                results.append({
                    "file": filepath,
                    "line": scenario_line,
                    "level": "error",
                    "message": f"Scenario '{scenario_title}' is missing WHEN clause",
                })

            if not scenario.get("then"):
                results.append({
                    "file": filepath,
                    "line": scenario_line,
                    "level": "error",
                    "message": f"Scenario '{scenario_title}' is missing THEN clause",
                })

    return results


def run_lint(path: str) -> int:
    """
    Run lint on all specs in the given path.

    Args:
        path: Base path to search for specs

    Returns:
        Exit code (0=success, 1=errors, 2=runtime error)
    """
    try:
        spec_files = discover_specs(path)
    except Exception as e:
        print(f"Error discovering specs: {e}")
        return 2

    if not spec_files:
        print(f"No spec files found in {path}")
        return 0

    all_results = []

    for spec_file in spec_files:
        results = validate_spec(spec_file)
        all_results.extend(results)

    # Print results
    if all_results:
        print(f"Found {len(all_results)} issue(s):\n")
        for result in all_results:
            level = result["level"].upper()
            file_path = result["file"]
            line = result["line"]
            message = result["message"]
            print(f"{level}: {file_path}:{line}: {message}")
    else:
        print(f"All {len(spec_files)} spec(s) passed validation.")

    return get_exit_code(all_results)


@app.callback()
def lint(
    path: str = typer.Argument(".", help="Path to lint"),
    fix: bool = typer.Option(False, "--fix", help="Attempt to fix auto-correctable issues"),
):
    """Lint OpenSpec files in the given path.

    Validates spec files for:
    - Required sections (Purpose, Requirements)
    - Correct scenario format (WHEN/THEN)
    - SHALL requirements having scenarios
    """
    if fix:
        print("Note: --fix is not yet implemented. Running lint without fixes.")
    exit_code = run_lint(path)
    raise typer.Exit(code=exit_code)
