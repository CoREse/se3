#!/usr/bin/env python3
"""Check for human responses in human-calls directory.

This script is called by collab-orchestrator.sh to detect when humans have
responded to escalation calls. It outputs JSON with response details.

Exit codes:
    0 - No responses detected
    1 - One or more responses detected (JSON output)
"""

import json
import os
import re
import sys
from pathlib import Path


def get_project_root() -> Path:
    """Find project root by looking for .claude/ directory."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".claude").is_dir():
            return current
        current = current.parent
    return Path.cwd()


def has_meaningful_response(content: str) -> tuple[bool, str]:
    """Check if there's a meaningful human response in the content.

    Returns (has_response, response_text)
    """
    # Find the Response/回复 section
    response_patterns = [
        r'### Response\s*\n(.*?)(?=###|\Z)',
        r'### 回复\s*\n(.*?)(?=###|\Z)',
        r'### Réponse\s*\n(.*?)(?=###|\Z)',
    ]

    response_text = ""
    for pattern in response_patterns:
        match = re.search(pattern, content, re.DOTALL)
        if match:
            response_text = match.group(1).strip()
            break

    if not response_text:
        return False, ""

    # Remove HTML comments
    cleaned = re.sub(r'<!--.*?-->', '', response_text, flags=re.DOTALL).strip()
    if not cleaned:
        return False, ""

    # Check for placeholder text
    placeholders = [
        "human: write your response below",
        "人类：请在下方输入您的回复",
        "humain: écrivez votre réponse ci-dessous",
        "agent will fill this in",
        "to be filled",
        "placeholder",
    ]
    if any(p in cleaned.lower() for p in placeholders):
        return False, ""

    # Check minimum meaningful content (at least 3 non-whitespace chars)
    meaningful = re.sub(r'\s', '', cleaned)
    if len(meaningful) < 3:
        return False, ""

    return True, cleaned


def check_responses() -> list[dict]:
    """Check all pending human call files for responses.

    Returns list of dicts with file info and responses.
    """
    project_root = get_project_root()
    human_calls_dir = project_root / "human-calls"

    if not human_calls_dir.exists():
        return []

    responses = []

    for call_file in human_calls_dir.glob("*.md"):
        # Skip archived files
        if ".archived" in call_file.name or ".responded" in call_file.name:
            continue

        try:
            content = call_file.read_text(encoding="utf-8")

            # Check if status is pending
            if not re.search(r'^status:\s*pending', content, re.MULTILINE):
                continue

            # Check for meaningful response
            has_response, response_text = has_meaningful_response(content)

            if has_response:
                # Extract ID from frontmatter
                id_match = re.search(r'^id:\s*(.+)$', content, re.MULTILINE)
                call_id = id_match.group(1).strip() if id_match else call_file.stem

                responses.append({
                    "file": call_file.name,
                    "id": call_id,
                    "response": response_text,
                    "path": str(call_file),
                })

        except Exception:
            continue

    return responses


def main():
    responses = check_responses()

    if responses:
        print(json.dumps(responses, ensure_ascii=False))
        return 1
    else:
        print("[]")
        return 0


if __name__ == "__main__":
    sys.exit(main())
