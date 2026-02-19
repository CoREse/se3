"""Tests for se3 loop command."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import signal
import threading
import queue
import subprocess
import time

from se3_tools.commands.loop import (
    LoopState,
    sanitize_change_name,
    run_claude_with_renderer,
)


class TestSanitizeChangeName:
    """Test sanitize_change_name function."""

    def test_simple_description(self):
        """Test basic description sanitization."""
        assert sanitize_change_name("Fix bug") == "fix-bug"

    def test_description_with_special_chars(self):
        """Test description with special characters."""
        assert sanitize_change_name("Fix @#$ bug") == "fix-bug"

    def test_chinese_characters_fallback(self):
        """Test Chinese-only description falls back to timestamp."""
        result = sanitize_change_name("修复错误")
        assert result.startswith("loop-")

    def test_mixed_chinese_english(self):
        """Test mixed Chinese and English."""
        result = sanitize_change_name("修复 Fix bug")
        assert result == "fix-bug"


class TestLoopState:
    """Test LoopState class for Ctrl-C handling."""

    def test_initial_state(self):
        """Test initial state of LoopState."""
        state = LoopState()
        assert state.supplemental_prompts == []
        assert state.in_supplemental_mode is False
        assert state.should_exit is False

    def test_first_ctrl_c_enters_supplemental_mode(self):
        """Test first Ctrl-C enters supplemental mode."""
        state = LoopState()
        state.handle_sigint(signal.SIGINT, None)

        assert state.in_supplemental_mode is True
        assert state.should_exit is False

    def test_second_ctrl_c_exits(self):
        """Test second Ctrl-C in supplemental mode sets should_exit."""
        state = LoopState()
        # First Ctrl-C
        state.handle_sigint(signal.SIGINT, None)
        # Second Ctrl-C
        state.handle_sigint(signal.SIGINT, None)

        assert state.should_exit is True

    def test_add_supplemental_prompt(self):
        """Test adding supplemental prompts."""
        state = LoopState()
        state.supplemental_prompts.append("Additional instruction")

        assert len(state.supplemental_prompts) == 1
        assert state.supplemental_prompts[0] == "Additional instruction"


class TestPromptBuilding:
    """Test prompt building functionality."""

    def test_get_full_prompt_basic(self):
        """Test basic prompt building."""
        state = LoopState()
        prompt = state.get_full_prompt(
            base_prompt="Test prompt",
            iteration=1,
            iterations=5,
            change_name="test-change",
            quick=False
        )

        assert "/se3:work test-change" in prompt
        assert "Test prompt" in prompt
        assert "iteration 1 / 5" in prompt

    def test_get_full_prompt_with_supplemental(self):
        """Test prompt building with supplemental prompts."""
        state = LoopState()
        state.supplemental_prompts = ["Extra instruction 1", "Extra instruction 2"]

        prompt = state.get_full_prompt(
            base_prompt="Test prompt",
            iteration=2,
            iterations=5,
            change_name="test-change",
            quick=False
        )

        assert "Supplemental Instructions" in prompt
        assert "1. Extra instruction 1" in prompt
        assert "2. Extra instruction 2" in prompt

    def test_get_full_prompt_with_previous_summary(self):
        """Test prompt building with previous iteration summary."""
        state = LoopState()

        prompt = state.get_full_prompt(
            base_prompt="Test prompt",
            iteration=2,
            iterations=5,
            change_name="test-change",
            quick=False,
            previous_summary="Previous work completed."
        )

        assert "Previous Iteration Summary" in prompt
        assert "Previous work completed." in prompt

    def test_get_full_prompt_quick_mode(self):
        """Test prompt building in quick mode."""
        state = LoopState()

        prompt = state.get_full_prompt(
            base_prompt="Test prompt",
            iteration=1,
            iterations=5,
            quick=True
        )

        assert "/se3:fc Test prompt" in prompt
        assert "se3:work" not in prompt


class TestStdinPromptDelivery:
    """Test that prompts are delivered via stdin, not temp files."""

    @patch('subprocess.Popen')
    @patch('signal.signal')
    def test_prompt_sent_via_stdin(self, mock_signal, mock_popen):
        """Verify prompt is written to stdin, not temp file."""
        # Setup mock process
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # Setup mock signal handler
        mock_signal.return_value = None

        # Run the function
        test_prompt = "Test prompt content"
        run_claude_with_renderer("claude", test_prompt, timeout_sec=1)

        # Verify Popen was called with stdin=PIPE
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs['stdin'] == subprocess.PIPE

        # Verify prompt was written to stdin
        mock_proc.stdin.write.assert_called_once_with(test_prompt)
        mock_proc.stdin.close.assert_called_once()


class TestLoopCollabIntegration:
    """Test loop collab integration."""

    def test_run_loop_collab_imports(self):
        """Test that run_loop_collab function can be imported."""
        from se3_tools.commands.loop import run_loop_collab
        assert callable(run_loop_collab)

    def test_run_loop_collab_function_signature(self):
        """Test that run_loop_collab has correct function signature."""
        from se3_tools.commands.loop import run_loop_collab
        import inspect

        sig = inspect.signature(run_loop_collab)
        params = list(sig.parameters.keys())

        assert 'prompt' in params
        assert 'project_root' in params
        assert 'iterations' in params
        assert 'quick' in params
        assert 'no_summary' in params


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
