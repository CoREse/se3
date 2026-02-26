"""Tests for se3 loop command."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import signal
import threading
import queue
import subprocess
import time

from se3.commands.loop import (
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
        from se3.commands.loop import run_loop_collab
        assert callable(run_loop_collab)

    def test_run_loop_collab_function_signature(self):
        """Test that run_loop_collab has correct function signature."""
        from se3.commands.loop import run_loop_collab
        import inspect

        sig = inspect.signature(run_loop_collab)
        params = list(sig.parameters.keys())

        assert 'prompt' in params
        assert 'project_root' in params
        assert 'iterations' in params
        assert 'quick' in params
        assert 'no_summary' in params


class TestCreateLoopBranch:
    """Test create_loop_branch function."""

    def test_create_loop_branch_imports(self):
        """Test that create_loop_branch function can be imported."""
        from se3.commands.loop import create_loop_branch
        assert callable(create_loop_branch)

    def test_create_loop_branch_creates_branch_with_timestamp(self):
        """Test that create_loop_branch creates a branch with timestamp."""
        from se3.commands.loop import create_loop_branch, is_loop_branch
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            # Mock successful branch creation
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            # Return same mock for all calls
            mock_run.return_value = mock_result

            result = create_loop_branch(Path("/tmp"), "master")

            # Should create a se3-loop/ timestamp branch
            assert is_loop_branch(result)
            assert result.startswith("se3-loop/")

    def test_create_loop_branch_sets_config(self):
        """Test that create_loop_branch records base branch in git config."""
        from se3.commands.loop import create_loop_branch
        from pathlib import Path
        from unittest.mock import patch, MagicMock, call

        with patch('subprocess.run') as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            create_loop_branch(Path("/tmp"), "feature-branch")

            # Check that git config was called to record base branch
            calls = mock_run.call_args_list
            config_calls = [c for c in calls if 'config' in str(c)]
            assert len(config_calls) > 0
            assert 'feature-branch' in str(config_calls[-1])

    def test_create_loop_branch_handles_existing_branch(self):
        """Test that create_loop_branch handles branch name collision."""
        from se3.commands.loop import create_loop_branch
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            # git status --porcelain (no uncommitted changes)
            mock_status = MagicMock()
            mock_status.returncode = 0
            mock_status.stdout = ""

            # First git branch call fails (branch exists)
            mock_fail = MagicMock()
            mock_fail.returncode = 1
            mock_fail.stderr = "branch already exists"

            mock_success = MagicMock()
            mock_success.returncode = 0
            mock_success.stdout = ""
            mock_success.stderr = ""

            # status, branch(fail), branch(success), checkout, config
            mock_run.side_effect = [mock_status, mock_fail, mock_success, mock_success, mock_success]

            result = create_loop_branch(Path("/tmp"), "master")

            assert result.startswith("se3-loop/")
            # Should have tried multiple times with different names
            assert mock_run.call_count >= 3


class TestInferLoopBranchBase:
    """Test infer_loop_loop_branch_base function."""

    def test_infer_loop_branch_base_imports(self):
        """Test that infer_loop_branch_base function can be imported."""
        from se3.commands.loop import infer_loop_branch_base
        assert callable(infer_loop_branch_base)

    def test_infer_loop_branch_base_returns_master_when_ancestor(self):
        """Test that infer_loop_branch_base returns master when it's an ancestor."""
        from se3.commands.loop import infer_loop_branch_base
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            # Mock master exists
            mock_master_rev = MagicMock()
            mock_master_rev.returncode = 0
            mock_master_rev.stdout = "abc123\n"

            # Mock merge-base returns same as master HEAD
            mock_merge_base = MagicMock()
            mock_merge_base.returncode = 0
            mock_merge_base.stdout = "abc123\n"

            mock_run.side_effect = [mock_master_rev, mock_merge_base]

            result = infer_loop_branch_base(Path("/tmp"), "se3-loop/1234567890")
            assert result == "master"

    def test_infer_loop_branch_base_returns_main_when_master_not_found(self):
        """Test that infer_loop_branch_base returns main when master not found but main is."""
        from se3.commands.loop import infer_loop_branch_base
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            # Mock master doesn't exist
            mock_master_rev = MagicMock()
            mock_master_rev.returncode = 1

            # Mock main exists
            mock_main_rev = MagicMock()
            mock_main_rev.returncode = 0
            mock_main_rev.stdout = "def456\n"

            # Mock merge-base returns same as main HEAD
            mock_merge_base = MagicMock()
            mock_merge_base.returncode = 0
            mock_merge_base.stdout = "def456\n"

            mock_run.side_effect = [mock_master_rev, mock_main_rev, mock_merge_base]

            result = infer_loop_branch_base(Path("/tmp"), "se3-loop/1234567890")
            assert result == "main"

    def test_infer_loop_branch_base_returns_none_when_no_match(self):
        """Test that infer_loop_branch_base returns None when no base branch matches."""
        from se3.commands.loop import infer_loop_branch_base
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            # All common branches don't exist
            mock_fail = MagicMock()
            mock_fail.returncode = 1

            mock_run.return_value = mock_fail

            result = infer_loop_branch_base(Path("/tmp"), "se3-loop/1234567890")
            assert result is None


class TestIsLoopBranch:
    """Test is_loop_branch function."""

    def test_is_loop_branch_imports(self):
        """Test that is_loop_branch function can be imported."""
        from se3.commands.loop import is_loop_branch
        assert callable(is_loop_branch)

    def test_is_loop_branch_returns_true_for_loop_branch(self):
        """Test that is_loop_branch returns True for loop branch names."""
        from se3.commands.loop import is_loop_branch

        assert is_loop_branch("se3-loop/1234567890") is True
        assert is_loop_branch("se3-loop/1234567890-1") is True

    def test_is_loop_branch_returns_false_for_non_loop_branch(self):
        """Test that is_loop_branch returns False for non-loop branch names."""
        from se3.commands.loop import is_loop_branch

        assert is_loop_branch("master") is False
        assert is_loop_branch("main") is False
        assert is_loop_branch("feature/test") is False
        assert is_loop_branch("collab/task-001") is False


class TestGetLoopBranchBase:
    """Test get_loop_branch_base function."""

    def test_get_loop_branch_base_imports(self):
        """Test that get_loop_branch_base function can be imported."""
        from se3.commands.loop import get_loop_branch_base
        assert callable(get_loop_branch_base)

    def test_get_loop_branch_base_returns_none_when_not_set(self):
        """Test that get_loop_branch_base returns None when base branch not recorded."""
        from se3.commands.loop import get_loop_branch_base
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1  # Config not found
            mock_run.return_value = mock_result

            result = get_loop_branch_base(Path("/tmp"), "se3-loop/1234567890")
            assert result is None

    def test_get_loop_branch_base_returns_value_when_set(self):
        """Test that get_loop_branch_base returns value when base branch is recorded."""
        from se3.commands.loop import get_loop_branch_base
        from pathlib import Path
        from unittest.mock import patch, MagicMock

        with patch('subprocess.run') as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "main\n"
            mock_run.return_value = mock_result

            result = get_loop_branch_base(Path("/tmp"), "se3-loop/1234567890")
            assert result == "main"


class TestAutoMergeWithClaude:
    """Test auto_merge_with_claude function."""

    def test_auto_merge_can_be_imported(self):
        """Test that auto_merge_with_claude can be imported."""
        from se3.commands.loop import auto_merge_with_claude
        assert callable(auto_merge_with_claude)

    @patch('se3.commands.loop.run_claude_with_renderer')
    @patch('se3.commands.loop.load_claude_commands')
    @patch('shutil.which', return_value='/usr/bin/claude')
    def test_auto_merge_success(self, mock_which, mock_load_cmds, mock_run_claude):
        """Test auto_merge_with_claude returns True on success."""
        from se3.commands.loop import auto_merge_with_claude
        from pathlib import Path

        mock_load_cmds.return_value = [{"cmd": "claude"}]
        mock_run_claude.return_value = (0, False)

        result = auto_merge_with_claude("se3-loop/123", "master", Path("/tmp"))
        assert result is True
        mock_run_claude.assert_called_once()

        # Verify the prompt mentions both branches
        call_args = mock_run_claude.call_args
        prompt = call_args[0][1]
        assert "se3-loop/123" in prompt
        assert "master" in prompt

    @patch('se3.commands.loop.run_claude_with_renderer')
    @patch('se3.commands.loop.load_claude_commands')
    @patch('shutil.which', return_value='/usr/bin/claude')
    def test_auto_merge_failure(self, mock_which, mock_load_cmds, mock_run_claude):
        """Test auto_merge_with_claude returns False on failure."""
        from se3.commands.loop import auto_merge_with_claude
        from pathlib import Path

        mock_load_cmds.return_value = [{"cmd": "claude"}]
        mock_run_claude.return_value = (1, False)

        result = auto_merge_with_claude("se3-loop/123", "master", Path("/tmp"))
        assert result is False

    @patch('se3.commands.loop.load_claude_commands')
    @patch('shutil.which', return_value=None)
    def test_auto_merge_no_claude(self, mock_which, mock_load_cmds):
        """Test auto_merge_with_claude returns False when claude not found."""
        from se3.commands.loop import auto_merge_with_claude
        from pathlib import Path

        mock_load_cmds.return_value = [{"cmd": "claude"}]

        result = auto_merge_with_claude("se3-loop/123", "master", Path("/tmp"))
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
