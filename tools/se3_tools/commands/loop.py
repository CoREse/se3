"""SE3 Loop command — repeatedly run full-cycle workflow.

This command runs the se3 workflow in a loop for a specified number
of iterations, creating a new change for each iteration.

Usage:
    se3 loop "prompt" [--iterations 10]
    se3 loop "prompt" --exec              # Exclusive mode: auto-execute with bash while loop
    se3 loop "prompt" --exec --iterations 5 --quick

Modes:
    1. Default mode: Creates change, reports to user, exits
    2. Exclusive mode (--exec): Generates bash script, loops calling claude until complete
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime


def sanitize_change_name(description: str) -> str:
    """Convert a description into a valid change name."""
    name = description.lower().strip()
    name = "".join(c for c in name if c.isalnum() or c in " -_/")
    name = name.replace(" ", "-").replace("_", "-")
    while "--" in name:
        name = name.replace("--", "-")
    if len(name) > 40:
        name = name[:40].rsplit("-", 1)[0]
    return name.strip("-")


def load_loop_state(project_root: Path) -> Dict[str, Any]:
    """Load the loop state file."""
    state_file = project_root / ".se3-loop-state.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except:
            pass
    return {
        "current_iteration": 0,
        "total_iterations": 10,
        "base_prompt": "",
        "changes": [],
        "status": "idle",  # idle, working, complete
    }


def save_loop_state(project_root: Path, state: Dict[str, Any]) -> None:
    """Save the loop state file."""
    state_file = project_root / ".se3-loop-state.json"
    state_file.write_text(json.dumps(state, indent=2, default=str))


def check_incomplete_changes(project_root: Path) -> Optional[Dict[str, Any]]:
    """Check if there are incomplete changes from previous iteration."""
    changes_dir = project_root / "openspec" / "changes"
    if not changes_dir.exists():
        return None

    for change_path in changes_dir.iterdir():
        if not change_path.is_dir() or change_path.name == "archive":
            continue

        state_file = change_path / ".se3-state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                if not state.get("complete", False):
                    return {
                        "name": change_path.name,
                        "path": str(change_path),
                        "current_step": state.get("current_step"),
                        "workflow": state.get("workflow"),
                    }
            except:
                continue

    return None


def run_loop_iteration(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
) -> Dict[str, Any]:
    """Run one iteration of the loop.

    This function:
    1. Loads or initializes loop state
    2. Checks for incomplete changes from previous iteration
    3. If no incomplete changes and iterations remain: creates new change
    4. Returns action for agent to execute
    """
    from .work import WORKFLOWS, StepStatus

    root = Path(project_root).resolve()
    result = {
        "prompt": prompt,
        "iterations": iterations,
        "project_root": str(root),
        "iteration": 0,
        "total_iterations": iterations,
        "change": None,
        "actions": [],
        "status": "idle",
        "complete": False,
    }

    # Load loop state
    loop_state = load_loop_state(root)

    # If this is a new loop (idle status), initialize it
    if loop_state["status"] == "idle":
        loop_state["base_prompt"] = prompt
        loop_state["total_iterations"] = iterations
        loop_state["current_iteration"] = 0
        loop_state["changes"] = []
        loop_state["status"] = "working"

    # Check for incomplete changes
    incomplete = check_incomplete_changes(root)
    if incomplete:
        result["iteration"] = loop_state["current_iteration"]
        result["total_iterations"] = loop_state["total_iterations"]
        result["change"] = incomplete
        result["status"] = "continue"
        result["actions"] = [{
            "type": "continue_work",
            "change": incomplete["name"],
            "reason": f"Continue working on incomplete change (iteration {loop_state['current_iteration']}/{loop_state['total_iterations']})",
        }]
        return result

    # Check if we've completed all iterations
    if loop_state["current_iteration"] >= loop_state["total_iterations"]:
        loop_state["status"] = "complete"
        save_loop_state(root, loop_state)
        result["iteration"] = loop_state["current_iteration"]
        result["total_iterations"] = loop_state["total_iterations"]
        result["status"] = "complete"
        result["complete"] = True
        result["actions"] = [{
            "type": "complete",
            "reason": f"All {loop_state['total_iterations']} iterations complete",
        }]
        return result

    # Start a new iteration
    loop_state["current_iteration"] += 1
    current_iter = loop_state["current_iteration"]

    # Generate change name
    base_name = sanitize_change_name(prompt)
    change_name = f"{base_name}-{current_iter:02d}"

    # Ensure unique name
    changes_dir = root / "openspec" / "changes"
    change_path = changes_dir / change_name
    counter = 1
    while change_path.exists():
        change_name = f"{base_name}-{current_iter:02d}-{counter}"
        change_path = changes_dir / change_name
        counter += 1

    # Create the change
    workflow_type = "small" if quick else "feature"
    steps = WORKFLOWS.get(workflow_type, WORKFLOWS["feature"])

    change_path.mkdir(parents=True, exist_ok=True)

    state = {
        "workflow": workflow_type,
        "current_step": steps[0],
        "steps": {step: StepStatus.PENDING.value for step in steps},
        "step_history": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "description": f"{prompt} (Iteration {current_iter}/{iterations})",
        "loop_iteration": current_iter,
        "loop_total": iterations,
    }

    state_file = change_path / ".se3-state.json"
    state_file.write_text(json.dumps(state, indent=2, default=str))

    # Create minimal tasks.md
    tasks_file = change_path / "tasks.md"
    tasks_file.write_text(f"# {prompt} (Iteration {current_iter}/{iterations})\n\n## Tasks\n\n- [ ] {prompt}\n")

    # Update loop state
    loop_state["changes"].append({
        "iteration": current_iter,
        "name": change_name,
        "created_at": datetime.now().isoformat(),
    })
    save_loop_state(root, loop_state)

    # Build result
    result["iteration"] = current_iter
    result["total_iterations"] = iterations
    result["change"] = {
        "name": change_name,
        "path": str(change_path),
        "workflow": workflow_type,
    }
    result["status"] = "new_iteration"
    result["actions"] = [
        {
            "type": "implement",
            "description": prompt,
            "change": change_name,
            "reason": f"Loop iteration {current_iter}/{iterations}: Implement the change",
        },
        {
            "type": "run_tests",
            "reason": "Verify implementation before next iteration",
        },
        {
            "type": "commit",
            "reason": f"Commit iteration {current_iter} and continue to next",
        },
        {
            "type": "loop_continue",
            "reason": f"Run 'se3 loop' again for iteration {current_iter + 1}",
        },
    ]

    return result


def generate_loop_script(
    prompt: str,
    project_root: str,
    iterations: int,
    quick: bool,
) -> str:
    """Generate the bash loop script for exclusive execution mode."""
    quick_flag = "--quick" if quick else ""

    script = f'''#!/bin/bash
#
# SE3 Loop - Exclusive Execution Mode
# Auto-generated by: se3 loop --exec
#
# This script runs Claude Code in a loop, executing the SE3 workflow
# for each iteration until all iterations are complete.
#

set -e

PROMPT="{prompt.replace('"', '\\"')}"
ITERATIONS={iterations}
PROJECT_ROOT="{project_root}"
QUICK_FLAG="{quick_flag}"

echo "============================================================"
echo "SE3 Loop - Exclusive Execution Mode"
echo "============================================================"
echo ""
echo "Prompt: $PROMPT"
echo "Iterations: $ITERATIONS"
echo "Project: $PROJECT_ROOT"
echo ""
echo "This will start Claude Code and execute the loop."
echo "Press Ctrl+C to stop at any time."
echo "============================================================"
echo ""

# Check for claude command
if ! command -v claude &> /dev/null; then
    echo "❌ Error: 'claude' command not found in PATH"
    echo "Please install Claude Code CLI first."
    exit 1
fi

cd "$PROJECT_ROOT"

# Reset loop state to start fresh
rm -f .se3-loop-state.json

ITERATION=0
while [ $ITERATION -lt $ITERATIONS ]; do
    ITERATION=$((ITERATION + 1))

    echo ""
    echo "╔════════════════════════════════════════════════════════════╗"
    echo "║  Iteration $ITERATION / $ITERATIONS"
    echo "╚════════════════════════════════════════════════════════════╝"
    echo ""

    # Get the next change from se3 loop
    echo "[SE3 Loop] Preparing iteration $ITERATION..."

    # Run se3 loop to create/continue change
    LOOP_OUTPUT=$(se3 loop "$PROMPT" --iterations $ITERATIONS $QUICK_FLAG --format json 2>/dev/null || echo '{{"status": "error"}}')

    # Check status
    STATUS=$(echo "$LOOP_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "error")

    if [ "$STATUS" = "complete" ]; then
        echo "[SE3 Loop] All iterations complete!"
        break
    fi

    if [ "$STATUS" = "error" ]; then
        echo "[SE3 Loop] Error preparing iteration, retrying..."
        sleep 2
        continue
    fi

    # Get change name
    CHANGE_NAME=$(echo "$LOOP_OUTPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('change',{{}}).get('name',''))" 2>/dev/null || echo "")

    if [ -z "$CHANGE_NAME" ]; then
        echo "[SE3 Loop] Warning: No change name returned, skipping iteration"
        sleep 2
        continue
    fi

    echo "[SE3 Loop] Change: $CHANGE_NAME"
    echo "[SE3 Loop] Starting Claude Code..."
    echo ""

    # Create the prompt file for Claude
    PROMPT_FILE=$(mktemp /tmp/se3-loop-prompt-XXXXXX.md)
    cat > "$PROMPT_FILE" << 'CLAUDE_EOF'
/se3:work ''' + '"$CHANGE_NAME"' + f'''

$PROMPT

(这是 SE3 Loop 的迭代 $ITERATION / $ITERATIONS。请按照 SE3 流程完成这个 change：
1. 读取相关 spec
2. 实现需求
3. 运行测试
4. 提交更改
5. 运行 /se3:done 结束会话)
CLAUDE_EOF

    # Execute Claude Code with the prompt
    # This runs Claude in the foreground, showing all output
    echo "------------------------------------------------------------"
    if claude "$PROMPT_FILE" 2>&1; then
        echo "------------------------------------------------------------"
        echo "[Claude] Session completed normally"
    else
        echo "------------------------------------------------------------"
        echo "[Claude] Session exited with error (may be normal)"
    fi

    # Clean up prompt file
    rm -f "$PROMPT_FILE"

    echo ""
    echo "[SE3 Loop] Iteration $ITERATION processing complete"

    # Brief pause between iterations
    if [ $ITERATION -lt $ITERATIONS ]; then
        echo "[SE3 Loop] Continuing to next iteration..."
        sleep 2
    fi
done

echo ""
echo "============================================================"
echo "SE3 Loop Complete"
echo "============================================================"
echo ""
echo "Completed $ITERATION iterations"
echo ""

# Clean up loop state
rm -f .se3-loop-state.json
'''
    return script


def run_exclusive_loop(
    prompt: str,
    project_root: str = ".",
    iterations: int = 10,
    quick: bool = False,
) -> None:
    """Run the loop in exclusive mode - generates and executes bash script."""
    root = Path(project_root).resolve()

    print(f"\n{'=' * 60}")
    print("SE3 Loop - Exclusive Execution Mode")
    print(f"{'=' * 60}")
    print("")
    print(f"Prompt: {prompt}")
    print(f"Iterations: {iterations}")
    print(f"Project: {root}")
    print("")
    print("Generating loop script...")

    # Generate the script
    script_content = generate_loop_script(prompt, str(root), iterations, quick)

    # Write to temporary file
    script_path = root / ".se3-loop-exec.sh"
    script_path.write_text(script_content)
    script_path.chmod(0o755)

    print(f"Script created: {script_path}")
    print("")
    print("Starting exclusive loop execution...")
    print("This will takeover the terminal and run Claude Code repeatedly.")
    print("Press Ctrl+C to stop at any time.")
    print("")
    print(f"{'=' * 60}")
    print("")

    # Execute the script, replacing current process
    # This gives the loop full control of the terminal
    try:
        subprocess.run([str(script_path)], check=False)
    except KeyboardInterrupt:
        print("")
        print("\n[SE3 Loop] Interrupted by user")
    finally:
        # Clean up script
        if script_path.exists():
            script_path.unlink()


def print_text_report(result: Dict[str, Any]) -> None:
    """Print a human-readable loop report."""
    print(f"\n{'=' * 60}")
    print("SE 3.0 Loop")
    print(f"{'=' * 60}")

    print(f"\nPrompt: {result.get('prompt', 'N/A')}")
    print(f"Iteration: {result.get('iteration', 0)}/{result.get('total_iterations', 0)}")

    status = result.get('status', 'unknown')
    print(f"\n{'-' * 60}")
    print(f"Status: {status.upper()}")
    print(f"{'-' * 60}")

    if status == "continue":
        change = result.get('change', {})
        print(f"\nContinue working on: {change.get('name', 'N/A')}")
        print(f"Workflow: {change.get('workflow', 'N/A')}")
        print(f"\nComplete this change before running 'se3 loop' again.")

    elif status == "new_iteration":
        change = result.get('change', {})
        print(f"\nNew change created: {change.get('name', 'N/A')}")
        print(f"Workflow: {change.get('workflow', 'N/A')}")

        print(f"\n{'-' * 60}")
        print("Action Sequence:")
        print(f"{'-' * 60}")
        for i, action in enumerate(result.get('actions', []), 1):
            print(f"\n{i}. [{action['type']}] {action.get('reason', '')}")
            if 'description' in action:
                print(f"   Description: {action['description']}")
            if 'change' in action:
                print(f"   Change: {action['change']}")

    elif status == "complete":
        print(f"\nAll iterations complete!")
        print(f"Total iterations: {result.get('total_iterations', 0)}")

    print(f"\n{'=' * 60}\n")


def print_json_report(result: Dict[str, Any]) -> None:
    """Print JSON loop report."""
    print(json.dumps(result, indent=2, default=str))
