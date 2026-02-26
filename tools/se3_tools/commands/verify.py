"""Change verification command for SE 3.0 tools.

Implements the change-verifier spec:
- Extracts scenarios from change specs
- Searches for verification markers in codebase
- Generates coverage reports
- Supports skip annotations
"""

# Verify: change-verifier/Extract scenarios from change
# Verify: change-verifier/Find test marker
# Verify: change-verifier/Incomplete implementation
# Verify: change-verifier/Complete implementation
# Verify: change-verifier/Skip with reason

import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

from ..utils import discover_specs_in_change, parse_spec, find_verification_markers

import typer

app = typer.Typer(invoke_without_command=True)


def extract_scenarios_with_skips(spec_path: str) -> List[Dict[str, Any]]:
    """Extract scenarios from a spec file, including skip annotations.

    Args:
        spec_path: Path to the spec file

    Returns:
        List of scenario dicts with skip information
    """
    parsed = parse_spec(spec_path)
    spec_name = Path(spec_path).parent.name

    scenarios = []
    for scenario in parsed.get('scenarios', []):
        scenario_id = f"{spec_name}/{scenario['title']}"
        scenarios.append({
            'id': scenario_id,
            'spec': spec_name,
            'name': scenario['title'],
            'when': scenario.get('when', ''),
            'then': scenario.get('then', ''),
            'line': scenario.get('line', 0),
            'file': spec_path,
            'skipped': False,
            'skip_reason': None
        })

    # Check for skip annotations in raw content
    content = Path(spec_path).read_text(encoding='utf-8')
    lines = content.split('\n')

    for i, line in enumerate(lines):
        skip_match = __import__('re').search(r'<!--\s*verify-skip:\s*(.+?)\s*-->', line)
        if skip_match:
            reason = skip_match.group(1)
            # Find the next scenario after this skip annotation
            for j in range(i + 1, len(lines)):
                scen_match = __import__('re').search(r'####\s+Scenario:\s*(.+)', lines[j])
                if scen_match:
                    scenario_name = scen_match.group(1).strip()
                    for scenario in scenarios:
                        if scenario['name'] == scenario_name:
                            scenario['skipped'] = True
                            scenario['skip_reason'] = reason
                    break

    return scenarios


def verify_change(change_name: str, project_root: str = ".") -> Dict[str, Any]:
    """Verify a change by checking scenario coverage.

    Args:
        change_name: Name of the change to verify
        project_root: Root directory of the project

    Returns:
        Dict with verification results
    """
    root = Path(project_root).resolve()

    # Find all specs in the change
    spec_files = discover_specs_in_change(change_name, str(root / "openspec" / "changes"))

    if not spec_files:
        return {
            'success': False,
            'error': f"No specs found for change '{change_name}'",
            'scenarios': [],
            'covered': [],
            'uncovered': [],
            'skipped': []
        }

    # Extract all scenarios
    all_scenarios = []
    for spec_file in spec_files:
        scenarios = extract_scenarios_with_skips(spec_file)
        all_scenarios.extend(scenarios)

    # Search for verification markers
    search_paths = [str(root / "tools"), str(root)]

    covered = []
    uncovered = []
    skipped = []

    for scenario in all_scenarios:
        if scenario['skipped']:
            skipped.append(scenario)
            continue

        markers = find_verification_markers(scenario['id'], search_paths)

        if markers:
            scenario['markers'] = markers
            covered.append(scenario)
        else:
            uncovered.append(scenario)

    return {
        'success': len(uncovered) == 0,
        'change': change_name,
        'total': len(all_scenarios),
        'covered': covered,
        'uncovered': uncovered,
        'skipped': skipped,
        'coverage_pct': (len(covered) / (len(all_scenarios) - len(skipped)) * 100) if (len(all_scenarios) - len(skipped)) > 0 else 100
    }


def print_text_report(results: Dict[str, Any]) -> None:
    """Print a human-readable coverage report.

    Args:
        results: Verification results dict
    """
    print(f"\n{'=' * 60}")
    print(f"Change Verification Report: {results.get('change', 'unknown')}")
    print(f"{'=' * 60}")

    if 'error' in results and results['error']:
        print(f"\nERROR: {results['error']}")
        return

    total = results['total']
    covered = len(results['covered'])
    uncovered = len(results['uncovered'])
    skipped = len(results['skipped'])

    print(f"\nSummary:")
    print(f"  Total scenarios: {total}")
    print(f"  Covered: {covered}")
    print(f"  Uncovered: {uncovered}")
    print(f"  Skipped: {skipped}")
    print(f"  Coverage: {results['coverage_pct']:.1f}%")

    if results['skipped']:
        print(f"\n{'-' * 60}")
        print("Skipped Scenarios:")
        print(f"{'-' * 60}")
        for scenario in results['skipped']:
            print(f"  - {scenario['id']}")
            print(f"    Reason: {scenario['skip_reason']}")

    if results['covered']:
        print(f"\n{'-' * 60}")
        print("Covered Scenarios:")
        print(f"{'-' * 60}")
        for scenario in results['covered']:
            print(f"  ✓ {scenario['id']}")
            if 'markers' in scenario:
                for marker in scenario['markers'][:1]:  # Show first marker
                    rel_path = Path(marker['file']).relative_to(Path.cwd())
                    print(f"    ({marker['type']}) {rel_path}:{marker['line']}")

    if results['uncovered']:
        print(f"\n{'-' * 60}")
        print("UNCOVERED Scenarios:")
        print(f"{'-' * 60}")
        for scenario in results['uncovered']:
            print(f"  ✗ {scenario['id']}")
            rel_path = Path(scenario['file']).relative_to(Path.cwd())
            print(f"    File: {rel_path}:{scenario['line']}")
            print(f"    WHEN: {scenario['when'][:60]}..." if len(scenario['when']) > 60 else f"    WHEN: {scenario['when']}")

    print(f"\n{'=' * 60}")
    if results['success']:
        print("Result: ALL SCENARIOS COVERED ✓")
    else:
        print("Result: GAPS FOUND ✗")
    print(f"{'=' * 60}\n")


def print_json_report(results: Dict[str, Any]) -> None:
    """Print JSON coverage report.

    Args:
        results: Verification results dict
    """
    import json
    print(json.dumps(results, indent=2, default=str))


def verify_all_specs(project_root: str = ".") -> Dict[str, Any]:
    """Verify all specs in the project.

    Args:
        project_root: Root directory of the project

    Returns:
        Dict with verification results
    """
    root = Path(project_root).resolve()

    # Find all specs in specs/ (or openspec/specs/ fallback)
    spec_files = []
    specs_dir = root / "specs"
    if not specs_dir.exists():
        specs_dir = root / "openspec" / "specs"
    if specs_dir.exists():
        for spec_dir in specs_dir.iterdir():
            if spec_dir.is_dir() and not spec_dir.name.startswith("_"):
                spec_file = spec_dir / "spec.md"
                if spec_file.exists():
                    spec_files.append(str(spec_file))

    if not spec_files:
        return {
            'success': False,
            'error': "No specs found in specs/ or openspec/specs/",
            'scenarios': [],
            'covered': [],
            'uncovered': [],
            'skipped': []
        }

    # Extract all scenarios
    all_scenarios = []
    for spec_file in spec_files:
        scenarios = extract_scenarios_with_skips(spec_file)
        all_scenarios.extend(scenarios)

    # Search for verification markers
    search_paths = [str(root / "tools"), str(root)]

    covered = []
    uncovered = []
    skipped = []

    for scenario in all_scenarios:
        if scenario['skipped']:
            skipped.append(scenario)
            continue

        markers = find_verification_markers(scenario['id'], search_paths)

        if markers:
            scenario['markers'] = markers
            covered.append(scenario)
        else:
            uncovered.append(scenario)

    return {
        'success': len(uncovered) == 0,
        'change': "all-specs",
        'total': len(all_scenarios),
        'covered': covered,
        'uncovered': uncovered,
        'skipped': skipped,
        'coverage_pct': (len(covered) / (len(all_scenarios) - len(skipped)) * 100) if (len(all_scenarios) - len(skipped)) > 0 else 100
    }


def main(change: Optional[str], format: str = "text", project_root: str = ".") -> int:
    """Main entry point for verify command.

    Args:
        change: Name of the change to verify (None to verify all specs)
        format: Output format (text or json)
        project_root: Root directory of the project

    Returns:
        Exit code (0 = success, 1 = gaps found)
    """
    if change:
        results = verify_change(change, project_root)
    else:
        results = verify_all_specs(project_root)

    if format == "json":
        print_json_report(results)
    else:
        print_text_report(results)

    return 0 if results.get('success', False) else 1


def _show_deprecation_warning():
    """Show deprecation warning for verify command."""
    import warnings
    warnings.warn(
        "'verify' is deprecated and will be removed in SE3 3.0. "
        "Use 'se3 run' which includes automatic verify-spec step.",
        DeprecationWarning,
        stacklevel=3
    )
    print(
        "⚠️  WARNING: 'verify' is deprecated and will be removed in SE3 3.0.",
        file=sys.stderr
    )
    print("   Use 'se3 run' which includes automatic verify-spec step.\n", file=sys.stderr)


@app.callback()
def verify(
    change: str = typer.Argument(None, help="Name of the change to verify (default: verify all specs)"),
    format: str = typer.Option("text", "--format", "-f", help="Output format (text or json)"),
    project_root: str = typer.Option(".", "--project-root", "-p", help="Root directory of the project"),
):
    """Verify spec coverage for a change or all specs.

    [DEPRECATED] Use 'se3 run' which includes automatic verify-spec step.
    This command will be removed in SE3 3.0.

    If change is specified, verifies only that change's specs.
    If no change is specified, verifies all spec scenarios in the project.
    """
    _show_deprecation_warning()
    exit_code = main(change, format, project_root)
    raise typer.Exit(code=exit_code)
