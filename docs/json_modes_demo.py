#!/usr/bin/env python3
"""Demo script showing three JSON extraction modes.

This script demonstrates how to use the three JSON modes in SE3.
Run with: python3 docs/json_modes_demo.py
"""

import sys
sys.path.insert(0, "src")

from se3.engine.llm_caller import LLMCaller


def demo_mode_resolution():
    """Demonstrate how JSON modes are resolved."""
    print("=" * 60)
    print("JSON Mode Resolution Demo")
    print("=" * 60)
    print()
    
    caller = LLMCaller()
    
    test_cases = [
        # (json_mode, require_json, two_phase_json, expected)
        (None, False, False, "off"),
        (None, True, False, "strict"),
        (None, False, True, "two_phase"),
        (None, True, True, "two_phase"),  # two_phase takes precedence
        ("strict", False, False, "strict"),
        ("extract", False, False, "extract"),
        ("two_phase", False, False, "two_phase"),
        ("off", True, True, "off"),  # json_mode takes precedence
    ]
    
    print("Mode resolution (json_mode, require_json, two_phase_json) -> result:")
    print()
    
    for json_mode, require_json, two_phase_json, expected in test_cases:
        result = caller._resolve_json_mode(json_mode, require_json, two_phase_json)
        status = "✓" if result == expected else "✗"
        print(f"  {status} ({json_mode!r}, {require_json}, {two_phase_json}) -> {result!r}")
        if result != expected:
            print(f"      Expected: {expected!r}")
    
    print()


def demo_prompt_wrapping():
    """Demonstrate how prompts are wrapped in different modes."""
    print("=" * 60)
    print("Prompt Wrapping Demo")
    print("=" * 60)
    print()
    
    original_prompt = "Analyze this task and return the task type."
    
    # STRICT mode - prompt gets wrapped
    strict_prompt = (
        "CRITICAL: You MUST respond with ONLY valid JSON. "
        "Do NOT include any text, explanation, or markdown before or after the JSON.\n\n"
        f"{original_prompt}\n\n"
        "REMINDER: Respond with ONLY the JSON object. No other text."
    )
    
    print("STRICT mode:")
    print(f"  Original: {original_prompt[:50]}...")
    print(f"  Wrapped length: {len(strict_prompt)} chars")
    print(f"  Contains 'CRITICAL': {'CRITICAL' in strict_prompt}")
    print()
    
    # EXTRACT mode - same wrapping as STRICT
    print("EXTRACT mode:")
    print(f"  Same wrapping as STRICT")
    print(f"  Difference: No retry on JSON error, uses LLM extraction")
    print()
    
    # TWO_PHASE mode - no wrapping
    print("TWO_PHASE mode:")
    print(f"  Original: {original_prompt[:50]}...")
    print(f"  Wrapped length: {len(original_prompt)} chars (no wrapping)")
    print(f"  Contains 'CRITICAL': {'CRITICAL' in original_prompt}")
    print()


def demo_usage_examples():
    """Show usage examples for each mode."""
    print("=" * 60)
    print("Usage Examples")
    print("=" * 60)
    print()
    
    print("# Mode 1: STRICT - Force JSON with retry")
    print("response = caller.call(")
    print("    prompt=prompt,")
    print("    json_mode='strict'")
    print(")")
    print()
    
    print("# Mode 2: EXTRACT - Request JSON, extract on failure")
    print("response = caller.call(")
    print("    prompt=prompt,")
    print("    json_mode='extract',")
    print("    json_schema_hint='{\"task_type\": \"...\"}'")
    print(")")
    print()
    
    print("# Mode 3: TWO_PHASE - Natural generation + extraction")
    print("response = caller.call(")
    print("    prompt=prompt,")
    print("    json_mode='two_phase',")
    print("    json_schema_hint='{\"files_changed\": [...]}'")
    print(")")
    print()
    
    print("# Legacy compatibility")
    print("response = caller.call(prompt=prompt, require_json=True)    # STRICT")
    print("response = caller.call(prompt=prompt, two_phase_json=True)  # TWO_PHASE")
    print()


def demo_cost_comparison():
    """Show cost comparison between modes."""
    print("=" * 60)
    print("Cost Comparison (relative units)")
    print("=" * 60)
    print()
    
    print("Scenario: Complex task with 20% failure rate")
    print()
    print("| Mode      | Success | Failure | Average |")
    print("|-----------|---------|---------|---------|")
    print("| STRICT    | 1.0     | 2.5     | 1.3     |")
    print("| EXTRACT   | 1.0     | 1.2     | 1.04    |")
    print("| TWO_PHASE | 2.0     | 2.0     | 2.0     |")
    print()
    print("Notes:")
    print("  - STRICT: May retry 2-3 times on failure")
    print("  - EXTRACT: Main call + light extraction call on failure")
    print("  - TWO_PHASE: Always 2 calls (generation + extraction)")
    print()
    print("Recommendation:")
    print("  - Simple outputs (<1K tokens): STRICT")
    print("  - Medium complexity: EXTRACT")
    print("  - Large outputs (>5K tokens): TWO_PHASE")
    print()


if __name__ == "__main__":
    demo_mode_resolution()
    demo_prompt_wrapping()
    demo_usage_examples()
    demo_cost_comparison()
    
    print("=" * 60)
    print("Demo complete!")
    print("=" * 60)
