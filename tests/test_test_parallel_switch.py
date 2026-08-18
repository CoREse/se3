"""Tests for the ``test.parallel`` switch (pytest-xdist parallelism).

Covers:
- TestConfig.parallel parsing: 'auto' / positive int, and the warn-and-default
  fallback to serial for every malformed shape.
- _apply_parallel: which flags are appended, the flag spellings that suppress
  them, non-pytest commands, and the switch being off.
- run_and_classify_tests / test_handler: the primary command carries the flags
  while configured phases stay verbatim.
- Both xdist-missing paths (interpreter pre-flight and post-hoc output
  signature) end the step as FAILED with an install instruction, never as a
  REVISION_NEEDED that would enter the fix loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tianluo.config import TestConfig
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.test import (
    XdistUnavailableError,
    _apply_parallel,
    _is_missing_xdist_result,
    _pytest_module_launcher,
    test_handler as run_test_step,
)


# ---------------------------------------------------------------------------
# TestConfig.parallel loading
# ---------------------------------------------------------------------------

class TestParallelConfigLoading:
    def test_default_is_serial(self, tmp_path):
        assert TestConfig.load(tmp_path).parallel is None

    def test_absent_key_is_serial(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("test:\n  timeout: 60\n")
        assert TestConfig.load(tmp_path).parallel is None

    @pytest.mark.parametrize("raw", ["auto", "AUTO", '" auto "'])
    def test_auto_accepted_case_and_space_insensitive(self, tmp_path, raw):
        (tmp_path / "tianluo.yaml").write_text(f"test:\n  parallel: {raw}\n")
        assert TestConfig.load(tmp_path).parallel == "auto"

    def test_positive_int_accepted(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text("test:\n  parallel: 4\n")
        assert TestConfig.load(tmp_path).parallel == 4

    def test_quoted_positive_int_accepted(self, tmp_path):
        (tmp_path / "tianluo.yaml").write_text('test:\n  parallel: "4"\n')
        assert TestConfig.load(tmp_path).parallel == 4

    @pytest.mark.parametrize(
        "raw", ["0", "-2", "3.5", "sometimes", "true", "[1, 2]", "{a: 1}"],
    )
    def test_invalid_values_warn_and_stay_serial(self, tmp_path, raw, caplog):
        (tmp_path / "tianluo.yaml").write_text(f"test:\n  parallel: {raw}\n")
        with caplog.at_level("WARNING"):
            cfg = TestConfig.load(tmp_path)
        assert cfg.parallel is None
        assert any("test.parallel" in rec.message for rec in caplog.records)

    def test_invalid_value_keeps_the_rest_of_the_block(self, tmp_path):
        # warn-and-default, not "discard the whole test: block".
        (tmp_path / "tianluo.yaml").write_text(
            "test:\n  parallel: nope\n  command: pytest -x\n  timeout: 99\n"
        )
        cfg = TestConfig.load(tmp_path)
        assert cfg.parallel is None
        assert cfg.command == "pytest -x"
        assert cfg.timeout == 99

    def test_warning_names_the_config_source(self, tmp_path, caplog):
        (tmp_path / "tianluo.yaml").write_text("test:\n  parallel: nope\n")
        with caplog.at_level("WARNING"):
            TestConfig.load(tmp_path)
        assert any(
            "tianluo.yaml" in rec.getMessage() for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# _apply_parallel
# ---------------------------------------------------------------------------

class TestApplyParallel:
    def test_auto_appends_workers_and_loadgroup(self):
        assert _apply_parallel(["python", "-m", "pytest", "-v"], "auto") == [
            "python", "-m", "pytest", "-v", "-n", "auto", "--dist", "loadgroup",
        ]

    def test_worker_count_appended(self):
        assert _apply_parallel(["pytest"], 4) == [
            "pytest", "-n", "4", "--dist", "loadgroup",
        ]

    def test_off_leaves_command_untouched(self):
        cmd = ["python", "-m", "pytest", "-v"]
        assert _apply_parallel(cmd, None) == cmd

    def test_non_pytest_command_untouched(self, caplog):
        with caplog.at_level("DEBUG", logger="tianluo.engine.steps.test"):
            assert _apply_parallel(["npm", "test"], "auto") == ["npm", "test"]
        assert any("not pytest" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.parametrize(
        "existing",
        [["-n", "2"], ["-n2"], ["-nauto"], ["--numprocesses", "2"],
         ["--numprocesses=2"]],
    )
    def test_existing_worker_flag_is_not_duplicated(self, existing):
        result = _apply_parallel(["pytest", *existing], "auto")
        assert result == ["pytest", *existing, "--dist", "loadgroup"]

    @pytest.mark.parametrize(
        "existing", [["--dist", "loadscope"], ["--dist=loadfile"]],
    )
    def test_existing_dist_flag_is_not_duplicated(self, existing):
        result = _apply_parallel(["pytest", *existing], "auto")
        assert result == ["pytest", *existing, "-n", "auto"]

    def test_both_flags_present_is_a_noop(self):
        cmd = ["pytest", "-n", "2", "--dist", "loadfile"]
        assert _apply_parallel(cmd, "auto") == cmd

    def test_does_not_mutate_the_input_command(self):
        cmd = ["pytest"]
        _apply_parallel(cmd, "auto")
        assert cmd == ["pytest"]


# ---------------------------------------------------------------------------
# xdist detection helpers
# ---------------------------------------------------------------------------

class TestPytestModuleLauncher:
    def test_module_form_yields_interpreter(self):
        assert _pytest_module_launcher(
            ["/venv/bin/python", "-m", "pytest", "-v"]
        ) == ["/venv/bin/python"]

    @pytest.mark.parametrize(
        "prefix",
        [
            ["uv", "run", "python"],
            ["poetry", "run", "python"],
            ["pixi", "run", "python"],
            ["/usr/bin/env", "/venv/bin/python"],
        ],
    )
    def test_wrapper_form_yields_the_whole_prefix(self, prefix):
        """The wrapper owns the environment; probing its first token asks the
        wrong program, and probing a bare ``python`` asks the wrong PATH."""
        assert _pytest_module_launcher([*prefix, "-m", "pytest", "-v"]) == prefix

    def test_bare_pytest_has_no_knowable_interpreter(self):
        assert _pytest_module_launcher(["pytest", "-v"]) is None

    def test_leading_dash_m_has_no_launcher(self):
        assert _pytest_module_launcher(["-m", "pytest"]) is None

    def test_non_pytest_module_is_not_matched(self):
        assert _pytest_module_launcher(["python", "-m", "unittest"]) is None


class TestIsMissingXdistResult:
    def test_recognises_unrecognized_arguments(self):
        assert _is_missing_xdist_result({
            "passed": False,
            "stdout": "",
            "stderr": "error: unrecognized arguments: -n auto --dist loadgroup\n",
        })

    def test_recognises_it_on_stdout_too(self):
        assert _is_missing_xdist_result({
            "passed": False,
            "stdout": "ERROR: unrecognized arguments: --numprocesses=4\n",
            "stderr": "",
        })

    def test_passing_run_is_never_missing_xdist(self):
        assert not _is_missing_xdist_result({
            "passed": True,
            "stdout": "unrecognized arguments: -n auto",
            "stderr": "",
        })

    def test_ordinary_failure_is_not_missing_xdist(self):
        assert not _is_missing_xdist_result({
            "passed": False,
            "stdout": "1 failed, 2 passed\nassert 1 == 2\n",
            "stderr": "",
        })

    def test_unrelated_unrecognized_argument_is_not_missing_xdist(self):
        assert not _is_missing_xdist_result({
            "passed": False,
            "stdout": "error: unrecognized arguments: --nonsense\n",
            "stderr": "",
        })

    def test_a_run_that_produced_per_test_results_is_not_missing_xdist(self):
        """xdist absence aborts at argument parsing, so nothing can be collected.

        A suite that ran under xdist and merely *printed* the signature (an
        argparse/CLI test, or pytest echoing a failing assertion whose source
        quotes it) must stay a real failure and reach the fix loop.
        """
        assert not _is_missing_xdist_result({
            "passed": False,
            "returncode": 1,
            "stdout": (
                "[gw0] [ 50%] PASSED tests/test_cli.py::test_a\n"
                "[gw1] [100%] FAILED tests/test_cli.py::test_b\n"
                "E       assert \"error: unrecognized arguments: -n auto "
                "--dist loadgroup\" in out\n"
            ),
            "stderr": "",
        })

    def test_a_run_with_only_an_aggregate_summary_is_not_missing_xdist(self):
        """Quiet (non-verbose) runs have no per-test lines; the summary still proves
        the suite ran."""
        assert not _is_missing_xdist_result({
            "passed": False,
            "returncode": 1,
            "stdout": (
                "E   assert 'unrecognized arguments: --numprocesses=4' in out\n"
                "=== 1 failed, 4021 passed in 95.10s ===\n"
            ),
            "stderr": "",
        })

    def test_non_usage_error_exit_code_is_not_missing_xdist(self):
        """pytest rejects an unknown option during argument parsing, which is exit
        code 4; anything else means it got further than that."""
        assert not _is_missing_xdist_result({
            "passed": False,
            "returncode": 1,
            "stdout": "error: unrecognized arguments: -n auto\n",
            "stderr": "",
        })

    def test_usage_error_exit_code_with_no_results_is_missing_xdist(self):
        assert _is_missing_xdist_result({
            "passed": False,
            "returncode": 4,
            "stdout": "",
            "stderr": "error: unrecognized arguments: -n auto --dist loadgroup\n",
        })


# ---------------------------------------------------------------------------
# test_handler / run_and_classify_tests integration
# ---------------------------------------------------------------------------

def _make_flow_and_step(tmp_path):
    flow = FlowInstance(task_description="parallel switch test")
    flow.change_path = tmp_path / "dummy"
    step = Step(step_type=StepType.TEST)
    return flow, step


def _mock_process(returncode: int, stdout: str, stderr: str = ""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


def _commands_run(mock_popen) -> list[list[str]]:
    return [call.args[0] for call in mock_popen.call_args_list]


@patch("tianluo.config.TestConfig.load")
@patch("subprocess.Popen")
class TestHandlerParallelCommand:
    def test_primary_command_carries_parallel_flags(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="python -m pytest -v", parallel="auto",
        )
        mock_popen.return_value = _mock_process(
            0, "tests/test_a.py::test_x PASSED\n1 passed in 1.0s\n",
        )
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            result = run_test_step(step, flow)

        assert result == StepStatus.COMPLETED
        assert _commands_run(mock_popen)[0] == [
            "python", "-m", "pytest", "-v", "-n", "auto", "--dist", "loadgroup",
        ]

    def test_phases_run_verbatim(self, mock_popen, mock_load, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="python -m pytest -v",
            parallel=4,
            phases=[{"name": "smoke", "command": "python -m pytest tests/smoke -v"}],
        )
        mock_popen.return_value = _mock_process(0, "1 passed in 1.0s\n")
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            assert run_test_step(step, flow) == StepStatus.COMPLETED

        primary, phase = _commands_run(mock_popen)
        assert primary[-4:] == ["-n", "4", "--dist", "loadgroup"]
        # The user's phase command is executed exactly as written.
        assert phase == ["python", "-m", "pytest", "tests/smoke", "-v"]

    def test_serial_by_default(self, mock_popen, mock_load, tmp_path, monkeypatch):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(command="python -m pytest -v")
        mock_popen.return_value = _mock_process(0, "1 passed in 1.0s\n")
        flow, step = _make_flow_and_step(tmp_path)

        assert run_test_step(step, flow) == StepStatus.COMPLETED
        assert _commands_run(mock_popen)[0] == ["python", "-m", "pytest", "-v"]

    def test_non_pytest_command_is_not_rewritten(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(command="npm test", parallel="auto")
        mock_popen.return_value = _mock_process(0, "ok\n")
        flow, step = _make_flow_and_step(tmp_path)

        assert run_test_step(step, flow) == StepStatus.COMPLETED
        assert _commands_run(mock_popen)[0] == ["npm", "test"]


@patch("tianluo.config.TestConfig.load")
@patch("subprocess.Popen")
class TestHandlerXdistMissing:
    def test_preflight_path_fails_with_install_hint(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """`<python> -m pytest`: no xdist in that interpreter -> nothing runs."""
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="/venv/bin/python -m pytest -v", parallel="auto",
        )
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(
                returncode=1, stdout="", stderr="ModuleNotFoundError: xdist",
            ),
        ) as mock_run:
            result = run_test_step(step, flow)

        # Probed the interpreter that would run the tests, not our own.
        assert mock_run.call_args.args[0][0] == "/venv/bin/python"
        # An environment problem, NOT a fix-loop trigger.
        assert result == StepStatus.FAILED
        assert "fix_needed" not in step.outputs
        assert step.outputs["tests_passed"] is False
        assert "pytest-xdist" in step.error_message
        assert "pip install pytest-xdist" in step.error_message
        assert "/venv/bin/python" in step.error_message
        # The suite never ran.
        mock_popen.assert_not_called()

    def test_bare_pytest_path_fails_with_install_hint(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """Bare `pytest`: no interpreter to probe, so diagnose from the output."""
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(command="pytest -v", parallel="auto")
        mock_popen.return_value = _mock_process(
            4, "", "error: unrecognized arguments: -n auto --dist loadgroup\n",
        )
        flow, step = _make_flow_and_step(tmp_path)

        result = run_test_step(step, flow)

        assert result == StepStatus.FAILED
        assert "fix_needed" not in step.outputs
        assert "pytest-xdist" in step.error_message
        assert "pip install pytest-xdist" in step.error_message

    def test_preflight_covers_a_user_written_worker_flag(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """The switch is on and the command already pins -n: still xdist-dependent."""
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="/venv/bin/python -m pytest -v -n 4 --dist loadfile",
            parallel="auto",
        )
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stdout="",
                stderr="ModuleNotFoundError: No module named 'xdist'\n",
            ),
        ):
            result = run_test_step(step, flow)

        assert result == StepStatus.FAILED
        assert "pytest-xdist" in step.error_message
        mock_popen.assert_not_called()

    def test_wrapper_command_probes_the_wrapped_environment(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """`uv run python -m pytest`: probe through the wrapper, not `uv` alone."""
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="uv run python -m pytest -v", parallel="auto",
        )
        mock_popen.return_value = _mock_process(0, "1 passed in 1.0s\n")
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as mock_run:
            assert run_test_step(step, flow) == StepStatus.COMPLETED

        probe = next(
            call.args[0] for call in mock_run.call_args_list
            if call.args and "import xdist" in " ".join(call.args[0])
        )
        assert probe == ["uv", "run", "python", "-c", "import xdist"]

    def test_probe_runs_in_the_same_directory_as_the_test_command(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """The cwd is part of the environment being probed.

        Wrapper launchers pick their project/virtualenv from the working
        directory, and tianluo never chdirs (under ``--worktree`` its own cwd is
        the main checkout while the tests run in the worktree). A probe run from
        anywhere else answers a question about a different environment — and a
        false "missing" there ends the step FAILED with no fix loop to recover
        through.
        """
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="uv run python -m pytest -v", parallel="auto",
        )
        mock_popen.return_value = _mock_process(0, "1 passed in 1.0s\n")
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as mock_run:
            assert run_test_step(step, flow) == StepStatus.COMPLETED

        probe_call = next(
            call for call in mock_run.call_args_list
            if call.args and "import xdist" in " ".join(call.args[0])
        )
        assert probe_call.kwargs["cwd"] == mock_popen.call_args_list[0].kwargs["cwd"]

    def test_probe_failing_for_an_unrelated_reason_is_inconclusive(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """A wrapper that refuses to run (no lockfile, no TTY) has said nothing
        about xdist; ending the step as FAILED there is unrecoverable."""
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(
            command="uv run python -m pytest -v", parallel="auto",
        )
        mock_popen.return_value = _mock_process(0, "1 passed in 1.0s\n")
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(
                returncode=2, stdout="", stderr="error: No `project` found\n",
            ),
        ):
            assert run_test_step(step, flow) == StepStatus.COMPLETED

        # The suite ran, in parallel, as configured.
        assert _commands_run(mock_popen)[0][-4:] == [
            "-n", "auto", "--dist", "loadgroup",
        ]

    def test_ordinary_failure_still_enters_the_fix_loop(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(command="pytest -v", parallel="auto")
        mock_popen.return_value = _mock_process(
            1,
            "[gw0] [100%] FAILED tests/test_a.py::test_x\n1 failed in 1.0s\n",
        )
        flow, step = _make_flow_and_step(tmp_path)

        assert run_test_step(step, flow) == StepStatus.REVISION_NEEDED
        assert step.outputs["fix_needed"] is True

    def test_suite_that_quotes_the_signature_still_enters_the_fix_loop(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """A real regression must not be answered with "install pytest-xdist".

        This repository runs with ``test.parallel: auto`` and its own tests
        assert on pytest's ``unrecognized arguments`` wording; when one of them
        fails, pytest prints that source line into the suite output. Diagnosing
        the whole run as a missing plugin would abort the flow and hide the
        genuine failure.
        """
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(command="pytest -v", parallel="auto")
        mock_popen.return_value = _mock_process(
            1,
            "[gw0] [ 50%] PASSED tests/test_a.py::test_ok\n"
            "[gw1] [100%] FAILED tests/test_test_parallel_switch.py::test_sig\n"
            "E       assert \"error: unrecognized arguments: -n auto "
            "--dist loadgroup\"\n"
            "=== 1 failed, 1 passed in 2.0s ===\n",
        )
        flow, step = _make_flow_and_step(tmp_path)

        assert run_test_step(step, flow) == StepStatus.REVISION_NEEDED
        assert step.outputs["fix_needed"] is True

    def test_no_preflight_when_switch_is_off(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(command="python -m pytest -v")
        mock_popen.return_value = _mock_process(0, "1 passed in 1.0s\n")
        flow, step = _make_flow_and_step(tmp_path)

        with patch(
            "tianluo.engine.steps.test.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as mock_run:
            assert run_test_step(step, flow) == StepStatus.COMPLETED
        # No xdist probe: the switch is off, so there is nothing to check for.
        # (subprocess.run itself is used elsewhere, e.g. git introspection.)
        assert not any(
            "import xdist" in " ".join(call.args[0])
            for call in mock_run.call_args_list
            if call.args
        )

    def test_unrecognized_arguments_without_the_switch_is_a_plain_failure(
        self, mock_popen, mock_load, tmp_path, monkeypatch,
    ):
        """The signature only means "missing xdist" for a command we rewrote."""
        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        mock_load.return_value = TestConfig(command="pytest -v")
        mock_popen.return_value = _mock_process(
            4, "", "error: unrecognized arguments: -n 4\n",
        )
        flow, step = _make_flow_and_step(tmp_path)

        assert run_test_step(step, flow) == StepStatus.REVISION_NEEDED


class TestGeneratedCommandUnderRealXdist:
    """The generated command is exercised against a real pytest + xdist run.

    WHY a subprocess test rather than a pure assertion on the argv: the point of
    appending ``--dist loadgroup`` is a *runtime* property — same ``xdist_group``
    means same worker, hence sequential — which no amount of string comparison
    can demonstrate. Skipped when xdist is not installed, since parallelism is a
    development convenience and not a packaging dependency.
    """

    @pytest.fixture
    def grouped_project(self, tmp_path):
        # Each test records the worker that ran it by touching a file named
        # after that worker — worker stdout is captured by xdist and would not
        # reach our subprocess's output.
        body = (
            "import os\n"
            "import pytest\n"
            "pytestmark = pytest.mark.xdist_group(name='serial')\n"
            "MARKS = os.path.join(os.path.dirname(__file__), 'workers')\n"
            "def _record():\n"
            "    os.makedirs(MARKS, exist_ok=True)\n"
            "    worker = os.environ.get('PYTEST_XDIST_WORKER', 'none')\n"
            "    open(os.path.join(MARKS, worker), 'a').close()\n"
        )
        for name in ("test_group_a.py", "test_group_b.py"):
            (tmp_path / name).write_text(
                body
                + "".join(
                    f"def test_{name[5:-3]}_{i}():\n    _record()\n"
                    for i in range(4)
                )
            )
        (tmp_path / "pytest.ini").write_text(
            "[pytest]\nmarkers =\n    xdist_group: serial group\n"
        )
        return tmp_path

    def test_loadgroup_keeps_a_group_on_one_worker(self, grouped_project):
        pytest.importorskip("xdist")
        import subprocess
        import sys

        command = _apply_parallel(
            [sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider"], 2,
        )
        assert command[-4:] == ["-n", "2", "--dist", "loadgroup"]

        proc = subprocess.run(
            command, cwd=grouped_project, capture_output=True, text=True,
            timeout=300,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        workers = sorted(p.name for p in (grouped_project / "workers").iterdir())
        # Eight tests across two files, one shared group -> exactly one worker.
        assert workers != ["none"], "xdist did not actually parallelise the run"
        assert len(workers) == 1, workers


class TestXdistUnavailableError:
    def test_str_joins_message_and_remediation(self):
        exc = XdistUnavailableError("missing", "install it")
        assert str(exc) == "missing\ninstall it"

    def test_str_without_remediation(self):
        assert str(XdistUnavailableError("missing")) == "missing"
