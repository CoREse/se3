"""Tests for the three-tier assertion ladder.

Nothing here touches a container, a browser or a network: the backend is the
shared :class:`FakeBackend`, host-side HTTP is monkeypatched at ``urlopen``, and
the tier-3 LLM arrives through the injected ``llm_factory``. What *is* real is
the tier-2 pixel comparison — Pillow runs against PNGs written into ``tmp_path``,
because a fake diff would prove nothing about the assertion that matters most.

Pillow is the whole content of the optional ``tianluo[e2e]`` extra, so the few
cases that reach the real comparison carry ``@requires_pillow`` and skip on a
core-only install. Everything else here — including the ladder's config-error
and dependency-missing checks — runs unconditionally, so the extra's absence can
never hide a regression in the parts that do not need it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from tianluo.e2e.assertions import (
    RESULT_MARKER,
    AssertionContext,
    BrowserBridge,
    compare_images,
    evaluate,
    fetch_http,
    tier_of,
)
from tianluo.e2e.backend import EnvironmentHandle, EnvironmentSpec, ExecResult
from tianluo.e2e.errors import E2EConfigError, E2EDependencyMissingError

from ._stubs import FakeBackend, assertion, marked, requires_pillow, write_png


def make_ctx(backend: FakeBackend, tmp_path: Path, **kwargs) -> AssertionContext:
    spec = EnvironmentSpec(project_root=tmp_path, network="net")
    handle = EnvironmentHandle(runtime="fake", spec=spec)
    kwargs.setdefault("scenario", "smoke")
    kwargs.setdefault("project_root", tmp_path)
    kwargs.setdefault("baselines_dir", tmp_path / "baselines")
    kwargs.setdefault("artifacts_dir", tmp_path / "artifacts")
    return AssertionContext(
        backend=backend, handle=handle, driver="app", **kwargs
    )


# ---------------------------------------------------------------------------
# tier 1 — exit code and streams
# ---------------------------------------------------------------------------


class TestExitCodeAndStreams:
    def test_exit_code_matches_default_zero(self, tmp_path):
        backend = FakeBackend()
        ctx = make_ctx(backend, tmp_path)
        ctx.record_exec("app", ["true"], ExecResult(exit_code=0))

        result = evaluate(assertion("exit_code"), ctx)

        assert result.passed
        assert result.tier == 1
        assert result.expected == "exit_code == 0"

    def test_exit_code_failure_reports_both_sides(self, tmp_path):
        backend = FakeBackend()
        ctx = make_ctx(backend, tmp_path)
        ctx.record_exec("app", ["pytest"], ExecResult(exit_code=2))

        result = evaluate(assertion("exit_code", equals=0), ctx)

        assert not result.passed
        assert result.expected == "exit_code == 0"
        assert result.actual == "exit_code == 2"
        assert result.details["command"] == "pytest"

    def test_exit_code_non_zero_expectation(self, tmp_path):
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec("app", ["false"], ExecResult(exit_code=1))

        assert evaluate(assertion("exit_code", equals=1), ctx).passed

    def test_timed_out_command_never_passes(self, tmp_path):
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec("app", ["hang"], ExecResult(exit_code=0, timed_out=True))

        result = evaluate(assertion("exit_code", equals=0), ctx)

        assert not result.passed
        assert "timed out" in result.actual

    def test_timed_out_command_never_passes_a_stream_match(self, tmp_path):
        """Partial output from a killed command is not evidence it worked."""
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec(
            "app",
            ["serve"],
            ExecResult(exit_code=-1, stdout="Server started\n", timed_out=True),
        )

        result = evaluate(assertion("stdout", contains="Server started"), ctx)

        assert not result.passed
        assert result.details["timed_out"] is True

    def test_internal_helper_is_not_the_exec_under_assertion(self, tmp_path):
        """tianluo's own helper commands must not take the `last_exec` slot."""
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec("app", ["run-me"], ExecResult(exit_code=3, stdout="broke"))
        ctx.record_exec(
            "app", ["python3", "-c", "<http fetch>"], ExecResult(exit_code=0),
            internal=True,
        )

        assert evaluate(assertion("exit_code", equals=3), ctx).passed
        assert evaluate(assertion("stdout", contains="broke"), ctx).passed

    def test_without_any_command_the_assertion_fails_loudly(self, tmp_path):
        result = evaluate(assertion("exit_code"), make_ctx(FakeBackend(), tmp_path))

        assert not result.passed
        assert "no command" in result.message

    def test_bad_expectation_is_a_config_error(self, tmp_path):
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec("app", ["x"], ExecResult(exit_code=0))

        with pytest.raises(E2EConfigError):
            evaluate(assertion("exit_code", equals="soon"), ctx)

    @pytest.mark.parametrize(
        "params,passes",
        [
            ({"contains": "ready"}, True),
            ({"contains": "missing"}, False),
            ({"equals": "server ready\n"}, True),
            ({"equals": "server ready"}, False),
            ({"matches": r"^server\s+\w+$"}, True),
            ({"matches": r"^nope"}, False),
        ],
    )
    def test_stdout_matchers(self, tmp_path, params, passes):
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec("app", ["run"], ExecResult(exit_code=0, stdout="server ready\n"))

        assert evaluate(assertion("stdout", **params), ctx).passed is passes

    def test_stderr_reads_the_other_stream(self, tmp_path):
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec(
            "app", ["run"], ExecResult(exit_code=1, stdout="fine", stderr="boom")
        )

        assert evaluate(assertion("stderr", contains="boom"), ctx).passed
        assert not evaluate(assertion("stdout", contains="boom"), ctx).passed

    def test_invalid_regex_is_a_config_error(self, tmp_path):
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec("app", ["run"], ExecResult(exit_code=0, stdout="x"))

        with pytest.raises(E2EConfigError):
            evaluate(assertion("stdout", matches="(unclosed"), ctx)


# ---------------------------------------------------------------------------
# tier 1 — HTTP
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def stub_urlopen(monkeypatch, response=None, error=None, recorder=None):
    import urllib.request

    def fake_urlopen(url, timeout=None):
        if recorder is not None:
            recorder.append((url, timeout))
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


class TestHttpAssertions:
    def test_status_from_the_host(self, tmp_path, monkeypatch):
        calls = []
        stub_urlopen(monkeypatch, _Response(200, b"ok"), recorder=calls)
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(
            assertion("http_status", url="http://127.0.0.1:18000/health"), ctx
        )

        assert result.passed
        assert result.details["status"] == 200
        assert calls[0][0] == "http://127.0.0.1:18000/health"

    def test_status_mismatch_reports_both_sides(self, tmp_path, monkeypatch):
        stub_urlopen(monkeypatch, _Response(500, b""))
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(assertion("http_status", url="http://h/x", equals=200), ctx)

        assert not result.passed
        assert "200" in result.expected and "500" in result.actual

    def test_http_error_status_is_an_observation_not_a_crash(self, tmp_path, monkeypatch):
        error = urllib.error.HTTPError(
            "http://h/missing", 404, "Not Found", {}, None
        )
        stub_urlopen(monkeypatch, error=error)
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(
            assertion("http_status", url="http://h/missing", equals=404), ctx
        )

        assert result.passed

    def test_unreachable_url_fails_with_guidance(self, tmp_path, monkeypatch):
        stub_urlopen(monkeypatch, error=urllib.error.URLError("refused"))
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(assertion("http_status", url="http://app:8000/"), ctx)

        assert not result.passed
        assert "refused" in result.actual
        assert "from:" in result.message

    def test_body_matcher(self, tmp_path, monkeypatch):
        stub_urlopen(monkeypatch, _Response(200, b"<h1>Welcome</h1>"))
        ctx = make_ctx(FakeBackend(), tmp_path)

        assert evaluate(
            assertion("http_body", url="http://h/", contains="Welcome"), ctx
        ).passed

    def test_json_path_reads_a_field(self, tmp_path, monkeypatch):
        stub_urlopen(
            monkeypatch, _Response(200, b'{"items": [{"name": "alpha"}], "n": 1}')
        )
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(
            assertion(
                "http_body",
                url="http://h/api",
                json_path="items[0].name",
                equals="alpha",
            ),
            ctx,
        )

        assert result.passed, result.actual

    def test_missing_json_path_fails_with_the_body(self, tmp_path, monkeypatch):
        stub_urlopen(monkeypatch, _Response(200, b'{"items": []}'))
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(
            assertion("http_body", url="http://h/api", json_path="items[3].name"), ctx
        )

        assert not result.passed
        assert "items[3].name" in result.message

    def test_from_service_routes_through_the_container(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0, stdout=marked({"status": 201, "body": "created"})
            )
        )
        ctx = make_ctx(backend, tmp_path)

        result = evaluate(
            assertion(
                "http_status", url="http://app:8000/health", equals=201, **{"from": "driver"}
            ),
            ctx,
        )

        assert result.passed
        service, argv, _ = backend.exec_calls[0]
        assert service == "driver"
        assert argv[0] == "python3" and argv[1] == "-c"
        assert "urllib.request" in argv[2]
        assert "http://app:8000/health" in argv

    def test_in_container_fetch_without_payload_is_reported(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=127, stderr="python3: not found"
            )
        )
        record = fetch_http(
            "http://app/x", ctx=make_ctx(backend, tmp_path), from_service="driver"
        )

        assert record.error
        assert "not found" in record.error

    def test_in_container_fetch_snippet_is_valid_python(self):
        """It runs via ``python3 -c``, so a syntax slip would only show in a
        container. Compiling it here is the cheap equivalent."""
        from tianluo.e2e.assertions import _IN_CONTAINER_FETCH

        compile(_IN_CONTAINER_FETCH, "<fetch>", "exec")
        assert "urllib.request" in _IN_CONTAINER_FETCH
        assert "requests" not in _IN_CONTAINER_FETCH

    def test_host_fetch_uses_stdlib_only(self):
        """No third-party HTTP client may sneak into the e2e path."""
        import tianluo.e2e.assertions as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import requests" not in source
        assert "httpx" not in source


# ---------------------------------------------------------------------------
# tier 1 — files
# ---------------------------------------------------------------------------


class TestDeclaredTimeBudgets:
    """A declared `timeout` reaches arithmetic, so its shape has to be checked.

    The scenario-level budget is validated by the schema; these per-assertion and
    per-action ones were not, and went uncoerced into ``min(default, remaining)``
    and bare ``float(...)`` — a quoted ``"5"`` surfaced as a TypeError from inside
    a comparison and ``timeout: fast`` as a ValueError, neither naming the
    scenario or the field.
    """

    def test_a_quoted_number_is_accepted(self, tmp_path, monkeypatch):
        calls = []
        stub_urlopen(monkeypatch, _Response(200, b"ok"), recorder=calls)
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(
            assertion("http_status", url="http://h/x", timeout="5"), ctx
        )

        assert result.passed
        assert calls[0][1] == 5.0

    @pytest.mark.parametrize("bad", ["fast", 0, -1, True, [3]])
    def test_a_non_numeric_timeout_is_a_located_config_error(
        self, tmp_path, monkeypatch, bad
    ):
        stub_urlopen(monkeypatch, _Response(200, b"ok"))
        ctx = make_ctx(FakeBackend(), tmp_path)

        with pytest.raises(E2EConfigError) as excinfo:
            evaluate(assertion("http_status", url="http://h/x", timeout=bad), ctx)

        assert "smoke" in str(excinfo.value)

    def test_a_file_assertion_timeout_is_coerced_too(self, tmp_path):
        backend = FakeBackend(exec_results={("app", "test"): ExecResult(exit_code=0)})
        ctx = make_ctx(backend, tmp_path)

        with pytest.raises(E2EConfigError):
            evaluate(assertion("file_exists", path="/tmp/x", timeout="soon"), ctx)


class TestFileAssertions:
    def test_file_exists_uses_test_e(self, tmp_path):
        backend = FakeBackend()
        ctx = make_ctx(backend, tmp_path)

        result = evaluate(assertion("file_exists", path="/workspace/out.txt"), ctx)

        assert result.passed
        assert backend.exec_calls[0][1] == ["test", "-e", "/workspace/out.txt"]

    def test_missing_file_fails(self, tmp_path):
        backend = FakeBackend(exec_results=[ExecResult(exit_code=1)])
        result = evaluate(
            assertion("file_exists", path="/nope"), make_ctx(backend, tmp_path)
        )

        assert not result.passed
        assert "absent" in result.actual

    def test_absent_expectation_inverts_the_check(self, tmp_path):
        backend = FakeBackend(exec_results=[ExecResult(exit_code=1)])
        result = evaluate(
            assertion("file_exists", path="/tmp/lock", absent=True),
            make_ctx(backend, tmp_path),
        )

        assert result.passed

    @pytest.mark.parametrize(
        "probe",
        [
            ExecResult(exit_code=126, stderr="container not running"),
            ExecResult(exit_code=1, timed_out=True),
        ],
    )
    def test_a_probe_that_could_not_run_is_not_evidence_of_absence(
        self, tmp_path, probe
    ):
        """`absent: true` must not be satisfied by a probe that never executed."""
        backend = FakeBackend(exec_results=[probe])
        result = evaluate(
            assertion("file_exists", path="/out/stale.lock", absent=True),
            make_ctx(backend, tmp_path),
        )

        assert not result.passed
        assert "probe" in result.message.lower()

    def test_file_content_matches(self, tmp_path):
        backend = FakeBackend(
            exec_results=[ExecResult(exit_code=0, stdout="version: 3\n")]
        )
        result = evaluate(
            assertion("file_content", path="/workspace/v.yaml", contains="version: 3"),
            make_ctx(backend, tmp_path),
        )

        assert result.passed
        assert backend.exec_calls[0][1] == ["cat", "/workspace/v.yaml"]

    def test_unreadable_file_fails_with_the_error(self, tmp_path):
        backend = FakeBackend(
            exec_results=[ExecResult(exit_code=1, stderr="No such file")]
        )
        result = evaluate(
            assertion("file_content", path="/gone", contains="x"),
            make_ctx(backend, tmp_path),
        )

        assert not result.passed
        assert "No such file" in result.actual

    def test_service_override_targets_another_container(self, tmp_path):
        backend = FakeBackend()
        evaluate(
            assertion("file_exists", path="/data/db", service="db"),
            make_ctx(backend, tmp_path),
        )

        assert backend.exec_calls[0][0] == "db"


# ---------------------------------------------------------------------------
# tier 1 — DOM
# ---------------------------------------------------------------------------


class TestDomAssertions:
    def test_observation_supplied_by_the_executor(self, tmp_path):
        ctx = make_ctx(FakeBackend(), tmp_path)

        result = evaluate(
            assertion("dom", selector="h1", contains="Welcome"),
            ctx,
            observation={"count": 1, "text": "Welcome back"},
        )

        assert result.passed
        assert result.details["count"] == 1

    def test_text_mismatch_reports_both_sides(self, tmp_path):
        result = evaluate(
            assertion("dom", selector="h1", equals="Welcome"),
            make_ctx(FakeBackend(), tmp_path),
            observation={"count": 1, "text": "Goodbye"},
        )

        assert not result.passed
        assert "Welcome" in result.expected and "Goodbye" in result.actual

    def test_absent_element_fails_a_presence_check(self, tmp_path):
        result = evaluate(
            assertion("dom", selector=".error"),
            make_ctx(FakeBackend(), tmp_path),
            observation={"count": 0, "text": ""},
        )

        assert not result.passed
        assert "no element" in result.actual

    def test_absent_expectation(self, tmp_path):
        result = evaluate(
            assertion("dom", selector=".error", absent=True),
            make_ctx(FakeBackend(), tmp_path),
            observation={"count": 0},
        )

        assert result.passed

    def test_count_expectation(self, tmp_path):
        result = evaluate(
            assertion("dom", selector="li", count=3),
            make_ctx(FakeBackend(), tmp_path),
            observation={"count": 2},
        )

        assert not result.passed
        assert "3 element" in result.expected and "2 element" in result.actual

    def test_attribute_expectation(self, tmp_path):
        result = evaluate(
            assertion("dom", selector="a", attribute="href", contains="/docs"),
            make_ctx(FakeBackend(), tmp_path),
            observation={"count": 1, "attribute": "https://x/docs/intro"},
        )

        assert result.passed

    def test_browser_side_error_fails_the_assertion(self, tmp_path):
        result = evaluate(
            assertion("dom", selector="#late"),
            make_ctx(FakeBackend(), tmp_path),
            observation={"count": 0, "error": "Timeout 30000ms exceeded"},
        )

        assert not result.passed
        assert "Timeout" in result.actual

    def test_lazy_single_query_run_without_browser_actions(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0,
                stdout=marked({"ops": [], "queries": [{"count": 1, "text": "Hi"}]}),
            )
        )
        ctx = make_ctx(backend, tmp_path)
        ctx.browser = BrowserBridge(backend, ctx.handle, "driver")

        result = evaluate(assertion("dom", selector="h1", contains="Hi"), ctx)

        assert result.passed
        service, argv, _ = backend.exec_calls[0]
        assert service == "driver"
        assert argv[:2] == ["node", "-e"]
        assert "playwright" in argv[2] and "h1" in argv[2]

    def test_dom_without_a_browser_driver_is_a_config_error(self, tmp_path):
        with pytest.raises(E2EConfigError) as excinfo:
            evaluate(assertion("dom", selector="h1"), make_ctx(FakeBackend(), tmp_path))

        assert "playwright" in str(excinfo.value)


class TestBrowserBridge:
    def test_program_carries_ops_and_queries_in_order(self, tmp_path):
        backend = FakeBackend()
        bridge = BrowserBridge(backend, None, "driver")
        bridge.add_op("goto", {"url": "http://app:8000/"})
        bridge.add_op("fill", {"selector": "#user", "value": "alice"})
        bridge.add_op("click", {"selector": "button[type=submit]"})
        bridge.add_query({"selector": ".greeting"})

        program = bridge.render_program()

        assert program.index("http://app:8000/") < program.index("alice")
        assert program.index("alice") < program.index("button[type=submit]")
        assert ".greeting" in program
        assert RESULT_MARKER in program

    def test_one_program_per_scenario(self, tmp_path):
        """The whole flow runs in a single exec, so no action is replayed."""
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0,
                stdout=marked(
                    {
                        "ops": [{"op": "goto", "ok": True}, {"op": "click", "ok": True}],
                        "queries": [{"count": 1, "text": "ok"}],
                    }
                ),
            )
        )
        bridge = BrowserBridge(backend, None, "driver")
        bridge.add_op("goto", {"url": "http://app/"})
        bridge.add_op("click", {"selector": "#go"})
        index = bridge.add_query({"selector": "#result"})

        bridge.run()
        bridge.run()  # idempotent: a second call must not re-drive the UI

        assert len(backend.exec_calls) == 1
        assert bridge.observation(index) == {"count": 1, "text": "ok"}
        assert bridge.failed_ops() == []

    def test_failed_op_is_reported(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=0,
                stdout=marked(
                    {
                        "ops": [{"op": "click", "ok": False, "error": "no such element"}],
                        "queries": [],
                    }
                ),
            )
        )
        bridge = BrowserBridge(backend, None, "driver")
        bridge.add_op("click", {"selector": "#gone"})

        bridge.run()

        assert bridge.failed_ops()[0]["error"] == "no such element"

    def test_rendered_program_is_valid_javascript(self, tmp_path):
        """The JS is generated, so nothing else would catch a template typo.

        Skipped when node is absent: the point is to validate *our* template, and
        e2e's own container runtime is not a test dependency.
        """
        node = shutil.which("node")
        if node is None:
            pytest.skip("node is not installed on this host")

        bridge = BrowserBridge(FakeBackend(), None, "driver")
        bridge.add_op("goto", {"url": "http://app:8000/"})
        bridge.add_op("fill", {"selector": "#user", "value": "o'brien \"quoted\""})
        bridge.add_op("screenshot", {"path": "/tmp/x.png", "full_page": True})
        bridge.add_query({"selector": ".greeting", "attribute": "title"})
        program = tmp_path / "program.js"
        program.write_text(bridge.render_program(), encoding="utf-8")

        checked = subprocess.run(
            [node, "--check", str(program)], capture_output=True, text=True
        )

        assert checked.returncode == 0, checked.stderr

    def test_unparseable_output_becomes_an_error_not_a_crash(self, tmp_path):
        backend = FakeBackend(
            exec_handler=lambda service, argv: ExecResult(
                exit_code=1, stderr="node: command not found"
            )
        )
        bridge = BrowserBridge(backend, None, "driver")
        bridge.add_query({"selector": "h1"})

        bridge.run()

        assert "command not found" in bridge.error
        assert bridge.observation(0) is None


# ---------------------------------------------------------------------------
# tier 2 — baseline screenshot diff
# ---------------------------------------------------------------------------


class TestScreenshotDiff:
    def _ctx(self, tmp_path, *, shot: Path, **kwargs):
        backend = FakeBackend(screenshot_bytes=shot.read_bytes())
        ctx = make_ctx(backend, tmp_path, **kwargs)
        return backend, ctx

    def test_undeclared_tier_two_is_refused(self, tmp_path):
        shot = write_png(tmp_path / "shot.png")
        _, ctx = self._ctx(tmp_path, shot=shot)

        with pytest.raises(E2EConfigError) as excinfo:
            evaluate(assertion("screenshot_diff", baseline="home.png"), ctx)

        assert "visual_regression" in str(excinfo.value)

    @pytest.mark.parametrize(
        "baseline",
        [
            "/etc/hosts",
            "../../outside.png",
            "sub/../../out.png",
            "C:/Windows/out.png",
            # Rooted-but-driveless and drive-relative: neither reports as absolute
            # to PureWindowsPath, but both discard the baselines directory when
            # joined onto it. The runtime twin of the schema check must agree.
            "\\out.png",
            "C:out.png",
        ],
    )
    def test_a_baseline_outside_the_directory_is_refused(self, tmp_path, baseline):
        """Joining an anchored or `..` name would read/write outside git."""
        shot = write_png(tmp_path / "shot.png")
        _, ctx = self._ctx(tmp_path, shot=shot)

        with pytest.raises(E2EConfigError) as excinfo:
            evaluate(
                assertion(
                    "screenshot_diff", baseline=baseline, visual_regression=True
                ),
                ctx,
            )

        assert baseline in str(excinfo.value)

    @requires_pillow
    def test_identical_images_pass(self, tmp_path):
        shot = write_png(tmp_path / "shot.png")
        write_png(tmp_path / "baselines" / "home.png")
        _, ctx = self._ctx(tmp_path, shot=shot)

        result = evaluate(
            assertion("screenshot_diff", baseline="home.png", visual_regression=True),
            ctx,
        )

        assert result.passed, result.actual
        assert result.tier == 2
        assert result.details["ratio"] == 0.0

    @requires_pillow
    def test_difference_ratio_is_reported(self, tmp_path):
        write_png(tmp_path / "baselines" / "home.png", size=(4, 4))
        shot = write_png(
            tmp_path / "shot.png", size=(4, 4), pixels={(0, 0): (255, 0, 0)}
        )
        _, ctx = self._ctx(tmp_path, shot=shot)

        result = evaluate(
            assertion("screenshot_diff", baseline="home.png", visual_regression=True),
            ctx,
        )

        assert not result.passed
        # One pixel of sixteen.
        assert result.details["differing"] == 1
        assert result.details["total"] == 16
        assert result.details["ratio"] == pytest.approx(0.0625)
        assert "6.25" in result.actual
        assert result.evidence

    @requires_pillow
    def test_threshold_admits_a_small_difference(self, tmp_path):
        write_png(tmp_path / "baselines" / "home.png", size=(4, 4))
        shot = write_png(
            tmp_path / "shot.png", size=(4, 4), pixels={(0, 0): (255, 0, 0)}
        )
        _, ctx = self._ctx(tmp_path, shot=shot)

        result = evaluate(
            assertion(
                "screenshot_diff",
                baseline="home.png",
                threshold=0.1,
                visual_regression=True,
            ),
            ctx,
        )

        assert result.passed

    @requires_pillow
    def test_geometry_change_is_called_out(self, tmp_path):
        write_png(tmp_path / "baselines" / "home.png", size=(4, 4))
        shot = write_png(tmp_path / "shot.png", size=(8, 4))
        _, ctx = self._ctx(tmp_path, shot=shot)

        result = evaluate(
            assertion("screenshot_diff", baseline="home.png", visual_regression=True),
            ctx,
        )

        assert not result.passed
        assert result.details["size_mismatch"] is True
        assert "4x4" in result.expected and "8x4" in result.actual

    def test_missing_baseline_fails_and_explains_how_to_make_one(self, tmp_path):
        shot = write_png(tmp_path / "shot.png")
        _, ctx = self._ctx(tmp_path, shot=shot)

        result = evaluate(
            assertion("screenshot_diff", baseline="home.png", visual_regression=True),
            ctx,
        )

        assert not result.passed
        assert "--write-baselines" in result.message
        assert not (tmp_path / "baselines" / "home.png").exists()

    def test_write_missing_baselines_captures_but_does_not_pass(self, tmp_path):
        shot = write_png(tmp_path / "shot.png")
        _, ctx = self._ctx(tmp_path, shot=shot, write_missing_baselines=True)

        result = evaluate(
            assertion("screenshot_diff", baseline="home.png", visual_regression=True),
            ctx,
        )

        assert (tmp_path / "baselines" / "home.png").is_file()
        # Not passed: nobody has reviewed the rendering yet, so it must not
        # become the reference simply by being produced first.
        assert not result.passed
        assert result.details["baseline_created"] is True

    @requires_pillow
    def test_an_earlier_screenshot_action_is_reused(self, tmp_path):
        write_png(tmp_path / "baselines" / "home.png")
        captured = write_png(tmp_path / "already.png")
        backend = FakeBackend()
        ctx = make_ctx(backend, tmp_path)
        ctx.screenshots["home"] = captured

        result = evaluate(
            assertion(
                "screenshot_diff",
                baseline="home.png",
                screenshot="home",
                visual_regression=True,
            ),
            ctx,
        )

        assert result.passed
        assert backend.snapshot_calls == []

    @requires_pillow
    def test_browser_written_shot_is_copied_out_not_recaptured(self, tmp_path):
        """A Playwright shot lives inside the container: copy it, don't re-shoot."""
        write_png(tmp_path / "baselines" / "home.png")
        shot = write_png(tmp_path / "src.png")
        backend = FakeBackend(screenshot_bytes=shot.read_bytes())
        ctx = make_ctx(backend, tmp_path)
        ctx.remote_screenshots["home"] = "/tmp/tianluo-e2e-home.png"

        result = evaluate(
            assertion(
                "screenshot_diff",
                baseline="home.png",
                screenshot="home",
                visual_regression=True,
            ),
            ctx,
        )

        assert result.passed, result.actual
        service, target, kind = backend.snapshot_calls[0]
        assert kind == "file", "must copy the existing image, not run scrot"
        assert target == "/tmp/tianluo-e2e-home.png"

    def test_baseline_without_a_name_is_a_config_error(self, tmp_path):
        _, ctx = self._ctx(tmp_path, shot=write_png(tmp_path / "shot.png"))

        with pytest.raises(E2EConfigError):
            evaluate(assertion("screenshot_diff", visual_regression=True), ctx)


class TestPillowIsolation:
    def test_module_has_no_top_level_pil_reference(self):
        import tianluo.e2e.assertions as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        top_level = [
            line
            for line in source.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        assert not [line for line in top_level if "PIL" in line]

    def test_missing_pillow_raises_with_the_install_command(self, tmp_path, monkeypatch):
        """The extra is absent -> an actionable message, not ModuleNotFoundError."""

        class Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] == "PIL":
                    raise ModuleNotFoundError("simulated", name=fullname)
                return None

        write_png(tmp_path / "baselines" / "home.png")
        shot = write_png(tmp_path / "shot.png")
        backend = FakeBackend(screenshot_bytes=shot.read_bytes())
        ctx = make_ctx(backend, tmp_path)

        blocker = Blocker()
        removed = {
            name: module for name, module in sys.modules.items()
            if name.split(".")[0] == "PIL"
        }
        for name in removed:
            del sys.modules[name]
        sys.meta_path.insert(0, blocker)
        try:
            with pytest.raises(E2EDependencyMissingError) as excinfo:
                evaluate(
                    assertion(
                        "screenshot_diff", baseline="home.png", visual_regression=True
                    ),
                    ctx,
                )
        finally:
            sys.meta_path.remove(blocker)
            sys.modules.update(removed)

        message = str(excinfo.value)
        assert "pip install 'tianluo[e2e]'" in message
        assert "Pillow" in message

    @requires_pillow
    def test_compare_images_counts_single_channel_drift(self, tmp_path):
        """A one-channel shift must not be rounded away by luminance conversion."""
        write_png(tmp_path / "a.png", size=(2, 2), color=(10, 10, 10))
        write_png(
            tmp_path / "b.png", size=(2, 2), color=(10, 10, 10),
            pixels={(0, 0): (10, 11, 10)},
        )

        diff = compare_images(tmp_path / "a.png", tmp_path / "b.png")

        assert diff["differing"] == 1
        assert diff["ratio"] == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# tier 3 — LLM semantic visual
# ---------------------------------------------------------------------------


class FakeCaller:
    """Records every call and answers with a canned payload."""

    def __init__(self, answer=None, error: BaseException = None) -> None:
        self.answer = answer
        self.error = error
        self.calls = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.answer


class TestSemanticVisual:
    def _ctx(self, tmp_path, caller):
        shot = write_png(tmp_path / "shot.png")
        backend = FakeBackend(screenshot_bytes=shot.read_bytes())
        return backend, make_ctx(backend, tmp_path, llm_factory=lambda: caller)

    def test_undeclared_tier_three_is_refused(self, tmp_path):
        caller = FakeCaller({"verdict": "pass", "evidence": "looks fine"})
        _, ctx = self._ctx(tmp_path, caller)

        with pytest.raises(E2EConfigError) as excinfo:
            evaluate(assertion("visual_semantic", question="is it legible?"), ctx)

        assert "semantic_visual" in str(excinfo.value)
        assert caller.calls == [], "the LLM must not be consulted at all"

    def test_tier_one_assertions_never_reach_the_llm(self, tmp_path):
        caller = FakeCaller({"verdict": "pass", "evidence": "x"})
        _, ctx = self._ctx(tmp_path, caller)
        ctx.record_exec("app", ["true"], ExecResult(exit_code=0))

        evaluate(assertion("exit_code"), ctx)
        evaluate(
            assertion("dom", selector="h1"), ctx, observation={"count": 1, "text": "h"}
        )

        assert caller.calls == []

    def test_pass_with_evidence(self, tmp_path):
        caller = FakeCaller(
            {
                "verdict": "pass",
                "evidence": "A blue 'Save' button sits below the form, fully visible.",
                "confidence": "high",
            }
        )
        _, ctx = self._ctx(tmp_path, caller)

        result = evaluate(
            assertion(
                "visual_semantic",
                question="is the save button visible?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        assert result.passed
        assert result.tier == 3
        assert "Save" in result.evidence
        assert caller.calls[0]["context_files"] == [Path(tmp_path / "artifacts") / next(
            p.name for p in (tmp_path / "artifacts").iterdir()
        )]

    def test_verdict_without_evidence_fails(self, tmp_path):
        caller = FakeCaller({"verdict": "pass", "evidence": "   "})
        _, ctx = self._ctx(tmp_path, caller)

        result = evaluate(
            assertion(
                "visual_semantic",
                question="is it legible?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        assert not result.passed
        assert "evidence" in result.message.lower()

    def test_negative_verdict_fails(self, tmp_path):
        caller = FakeCaller(
            {"verdict": "fail", "evidence": "The label overlaps the chart axis."}
        )
        _, ctx = self._ctx(tmp_path, caller)

        result = evaluate(
            assertion(
                "visual_semantic",
                question="is the label readable?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        assert not result.passed
        assert result.evidence

    def test_raw_json_text_is_parsed(self, tmp_path):
        caller = FakeCaller('{"verdict": "pass", "evidence": "Header reads Welcome."}')
        _, ctx = self._ctx(tmp_path, caller)

        result = evaluate(
            assertion(
                "visual_semantic",
                question="does the header greet?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        assert result.passed
        assert "Welcome" in result.evidence

    def test_llm_failure_fails_the_assertion(self, tmp_path):
        caller = FakeCaller(error=RuntimeError("no agent available"))
        _, ctx = self._ctx(tmp_path, caller)

        result = evaluate(
            assertion(
                "visual_semantic",
                question="anything?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        assert not result.passed
        assert "no usable answer" in result.message

    def test_unparseable_answer_fails(self, tmp_path):
        caller = FakeCaller("I think it looks great!")
        _, ctx = self._ctx(tmp_path, caller)

        result = evaluate(
            assertion(
                "visual_semantic",
                question="anything?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        assert not result.passed

    def test_question_is_mandatory(self, tmp_path):
        caller = FakeCaller({"verdict": "pass", "evidence": "x"})
        _, ctx = self._ctx(tmp_path, caller)

        with pytest.raises(E2EConfigError):
            evaluate(
                assertion(
                    "visual_semantic", semantic_visual=True, require_evidence=True
                ),
                ctx,
            )

    def test_a_hanging_llm_is_abandoned_at_the_scenario_budget(self, tmp_path):
        """Tier 3 is the one blocking call with no natural ceiling.

        Without the clamp a hung agent holds the whole E2E step open long past
        the scenario's declared timeout, and the budget check between assertions
        never runs again if this was the last one.
        """
        import threading
        import time as _time

        release = threading.Event()

        class HangingCaller:
            def __init__(self):
                self.calls = []

            def call(self, **kwargs):
                self.calls.append(kwargs)
                release.wait(30)
                return {"verdict": "pass", "evidence": "eventually"}

        caller = HangingCaller()
        shot = write_png(tmp_path / "shot.png")
        backend = FakeBackend(screenshot_bytes=shot.read_bytes())
        ctx = make_ctx(
            backend,
            tmp_path,
            llm_factory=lambda: caller,
            deadline=_time.monotonic() + 0.2,
        )

        started = _time.monotonic()
        try:
            result = evaluate(
                assertion(
                    "visual_semantic",
                    question="is it legible?",
                    semantic_visual=True,
                    require_evidence=True,
                ),
                ctx,
            )
        finally:
            release.set()

        assert not result.passed
        assert _time.monotonic() - started < 5, "the call was not abandoned"
        assert result.details.get("timed_out") == "true"
        assert caller.calls, "the LLM was consulted, just not awaited forever"

    def test_an_exhausted_budget_never_reaches_the_llm(self, tmp_path):
        import time as _time

        caller = FakeCaller({"verdict": "pass", "evidence": "x"})
        _, ctx = self._ctx(tmp_path, caller)
        ctx.deadline = _time.monotonic() - 1

        result = evaluate(
            assertion(
                "visual_semantic",
                question="is it legible?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        assert not result.passed
        assert caller.calls == []

    def test_prompt_demands_reviewable_evidence(self, tmp_path):
        caller = FakeCaller({"verdict": "pass", "evidence": "x"})
        _, ctx = self._ctx(tmp_path, caller)

        evaluate(
            assertion(
                "visual_semantic",
                question="is the chart legible?",
                semantic_visual=True,
                require_evidence=True,
            ),
            ctx,
        )

        prompt = caller.calls[0]["prompt"]
        assert "evidence field is mandatory" in prompt
        assert "is the chart legible?" in prompt
        assert caller.calls[0]["required_keys"] == ["verdict", "evidence"]


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_unknown_kind_is_a_config_error(self, tmp_path):
        with pytest.raises(E2EConfigError) as excinfo:
            evaluate(assertion("vibes"), make_ctx(FakeBackend(), tmp_path))

        assert "vibes" in str(excinfo.value)

    @pytest.mark.parametrize(
        "kind,tier",
        [
            ("exit_code", 1),
            ("stdout", 1),
            ("http_status", 1),
            ("dom", 1),
            ("screenshot_diff", 2),
            ("visual_semantic", 3),
        ],
    )
    def test_tier_of(self, kind, tier):
        assert tier_of(assertion(kind)) == tier

    def test_describe_includes_both_sides_on_failure(self, tmp_path):
        ctx = make_ctx(FakeBackend(), tmp_path)
        ctx.record_exec("app", ["x"], ExecResult(exit_code=3))

        line = evaluate(assertion("exit_code"), ctx).describe()

        assert line.startswith("[FAIL] exit_code")
        assert "expected: exit_code == 0" in line and "actual: exit_code == 3" in line
