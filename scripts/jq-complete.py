#!/usr/bin/env python3
"""Complete jq replacement using Python."""

import json
import sys
import re
import argparse

def evaluate_filter(data, filter_str):
    """Evaluate a jq-like filter on data."""
    # Handle identity
    if filter_str == '.':
        return data

    # Handle field access like .field or .field.subfield
    if filter_str.startswith('.'):
        # Remove leading dot
        expr = filter_str[1:]

        # Handle array iteration .field[]
        if '[]' in expr:
            base = expr.replace('[]', '')
            if base:
                parts = base.split('.')
                for p in parts:
                    if isinstance(data, dict):
                        data = data.get(p)
                    else:
                        return None
            if isinstance(data, list):
                return data
            return None

        # Handle simple field access
        parts = expr.split('.')
        for p in parts:
            if not p:
                continue
            if isinstance(data, dict):
                data = data.get(p)
            elif isinstance(data, list) and p.isdigit():
                idx = int(p)
                data = data[idx] if idx < len(data) else None
            else:
                data = None
            if data is None:
                break
        return data

    return data

def apply_assignment(data, assignment):
    """Apply an assignment like .status = \"value\" or .count += 1."""
    # Match pattern like .key = value, .key += value, or .key.subkey = value
    m = re.match(r'^\.(\w+(?:\.\w+)*)\s*(\+=|=)\s*(.+)$', assignment)
    if not m:
        return data

    keys = m.group(1).split('.')
    op = m.group(2)  # '=' or '+='
    value_str = m.group(3).strip()

    # Parse value
    try:
        value = json.loads(value_str)
    except:
        # Try as string literal
        if value_str.startswith('"') and value_str.endswith('"'):
            value = value_str[1:-1]
        else:
            value = value_str

    # Navigate and set
    d = data
    for k in keys[:-1]:
        if k not in d:
            d[k] = {}
        d = d[k]

    # Apply operation
    if op == '+=' and isinstance(value, (int, float)):
        current = d.get(keys[-1], 0)
        d[keys[-1]] = current + value
    else:
        d[keys[-1]] = value
    return data

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-r', '--raw-output', action='store_true')
    parser.add_argument('-e', '--exit-status', action='store_true')
    parser.add_argument('-c', '--compact-output', action='store_true')
    parser.add_argument('-R', '--raw-input', action='store_true')
    parser.add_argument('-s', '--slurp', action='store_true')
    parser.add_argument('expr', nargs='?')
    parser.add_argument('file', nargs='?')
    args = parser.parse_args()

    # Read input
    try:
        if args.file and args.file != '-':
            with open(args.file) as f:
                if args.raw_input:
                    data = f.read()
                else:
                    data = json.load(f)
        else:
            if args.raw_input:
                data = sys.stdin.read()
            else:
                data = json.load(sys.stdin)
    except Exception:
        if args.exit_status:
            sys.exit(1)
        print('null')
        return

    if not args.expr or args.expr == '.':
        print(json.dumps(data))
        return

    expr = args.expr

    # Handle default value syntax: .field // "default"
    if '//' in expr:
        m = re.match(r'^(\.\w+(?:\.\w+)*)\s*//\s*(.+)$', expr)
        if m:
            base_expr = m.group(1)
            default_str = m.group(2).strip()
            try:
                default_val = json.loads(default_str)
            except:
                default_val = default_str.strip('"')

            result = evaluate_filter(data, base_expr)
            if result is None:
                result = default_val
            data = result
        else:
            data = evaluate_filter(data, expr)
    # Handle assignment: .field = value (supports pipe-chained assignments)
    elif '=' in expr and not expr.startswith('.'):
        # Not an assignment, just a filter with equals sign
        data = evaluate_filter(data, expr)
    elif '|' in expr:
        # Handle pipe-separated assignments like: .status = "done" | .review.status = "approved"
        assignments = [a.strip() for a in expr.split('|')]
        for assignment in assignments:
            if re.match(r'^(\.\w+(?:\.\w+)*)\s*(\+=|=)', assignment):
                data = apply_assignment(data, assignment)
            elif assignment.startswith('.'):
                # It's a filter, not an assignment
                data = evaluate_filter(data, assignment)
    elif re.match(r'^\.(\w+(?:\.\w+)*)\s*(\+=|=)', expr):
        data = apply_assignment(data, expr)
    else:
        data = evaluate_filter(data, expr)

    # Output result
    if data is None:
        if args.exit_status:
            sys.exit(1)
        print('null')
    elif isinstance(data, str):
        if args.raw_output:
            print(data)
        else:
            print(json.dumps(data))
    elif isinstance(data, (dict, list)):
        print(json.dumps(data))
    elif isinstance(data, bool):
        print('true' if data else 'false')
    else:
        # numbers
        print(json.dumps(data))

if __name__ == '__main__':
    main()
