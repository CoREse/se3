"""Tests for ``tianluo.e2e.readiness`` — the four probe kinds and their budget.

Time is injected (``clock`` / ``sleeper``), so the polling loop is exercised for
real without a test spending the probe's timeout in wall clock.
"""

from __future__ import annotations

import urllib.error

import pytest

from tianluo.e2e import readiness
from tianluo.e2e.backend import EnvironmentHandle, ExecResult, ReadinessProbe
from tianluo.e2e.errors import E2EConfigError, E2EEnvironmentError

from ._stubs import FakeBackend, FakeClock, sample_spec


def make_handle(tmp_path):
    return EnvironmentHandle(runtime="fake", spec=sample_spec(tmp_path))


def wait(backend, handle, probe, *, service="app", clock=None):
    clock = clock or FakeClock()
    return readiness.wait_ready(
        backend, handle, service, probe, clock=clock, sleeper=clock.sleep
    )


class TestNoProbe:
    def test_a_service_without_a_probe_is_ready_immediately(self, tmp_path):
        backend = FakeBackend()

        assert wait(backend, make_handle(tmp_path), None) is True


class TestCommandProbe:
    def test_passes_once_the_command_exits_zero(self, tmp_path):
        backend = FakeBackend(
            exec_results=[
                ExecResult(exit_code=1),
                ExecResult(exit_code=1),
                ExecResult(exit_code=0),
            ]
        )
        probe = ReadinessProbe(
            kind="command", command=("pg_isready",), timeout=30, interval=0.5
        )

        assert wait(backend, make_handle(tmp_path), probe) is True
        assert len(backend.exec_calls) == 3

    def test_runs_the_command_in_the_named_service(self, tmp_path):
        backend = FakeBackend(exec_results=[ExecResult(exit_code=0)])
        probe = ReadinessProbe(kind="command", command=("true",))

        wait(backend, make_handle(tmp_path), probe, service="db")

        assert backend.exec_calls[0][0] == "db"
        assert backend.exec_calls[0][1] == ["true"]

    def test_a_command_probe_without_a_command_is_a_config_error(self, tmp_path):
        probe = ReadinessProbe(kind="command")

        with pytest.raises(E2EConfigError):
            wait(FakeBackend(), make_handle(tmp_path), probe)


class TestHttpProbe:
    def test_passes_on_a_2xx_answer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            readiness.urllib.request, "urlopen", _fake_urlopen([200])
        )
        probe = ReadinessProbe(kind="http", url="http://127.0.0.1:18000/healthz")

        assert wait(FakeBackend(), make_handle(tmp_path), probe) is True

    def test_retries_until_the_server_answers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            readiness.urllib.request,
            "urlopen",
            _fake_urlopen([urllib.error.URLError("refused"), 200]),
        )
        probe = ReadinessProbe(kind="http", url="http://127.0.0.1:18000/", timeout=30)

        assert wait(FakeBackend(), make_handle(tmp_path), probe) is True

    def test_a_404_does_not_count_as_ready(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            readiness.urllib.request,
            "urlopen",
            _fake_urlopen(
                [urllib.error.HTTPError("u", 404, "nf", None, None)] * 50
            ),
        )
        probe = ReadinessProbe(kind="http", url="http://127.0.0.1:18000/", timeout=3)

        with pytest.raises(E2EEnvironmentError):
            wait(FakeBackend(), make_handle(tmp_path), probe)

    def test_a_declared_status_is_what_counts_as_ready(self, tmp_path, monkeypatch):
        """An auth-guarded root answers 401 when perfectly healthy."""
        monkeypatch.setattr(
            readiness.urllib.request,
            "urlopen",
            _fake_urlopen([urllib.error.HTTPError("u", 401, "auth", None, None)]),
        )
        probe = ReadinessProbe(
            kind="http", url="http://127.0.0.1:18000/", status=401, timeout=3
        )

        assert wait(FakeBackend(), make_handle(tmp_path), probe) is True

    def test_a_declared_3xx_is_observed_instead_of_followed(
        self, tmp_path, monkeypatch
    ):
        """A redirect-to-login can itself be the "service is up" signal.

        The default opener follows redirects transparently, so the status a probe
        sees is always the terminal one — a declared 302 could never match, and a
        healthy service burned the whole budget before being reported as an
        environment failure.
        """
        openers = []

        class _Opener:
            def open(self, url, timeout=None):
                openers.append((url, timeout))
                raise urllib.error.HTTPError(url, 302, "Found", None, None)

        def _build_opener(*handlers):
            openers.append(handlers)
            return _Opener()

        monkeypatch.setattr(readiness.urllib.request, "build_opener", _build_opener)
        monkeypatch.setattr(
            readiness.urllib.request, "urlopen", _fake_urlopen([200] * 50)
        )
        probe = ReadinessProbe(
            kind="http", url="http://127.0.0.1:18000/", status=302, timeout=3
        )

        assert wait(FakeBackend(), make_handle(tmp_path), probe) is True
        # The redirect handler is what makes the 302 observable at all.
        assert readiness._NoRedirectHandler in openers[0]

    def test_a_non_redirect_probe_keeps_the_default_opener(
        self, tmp_path, monkeypatch
    ):
        """Only the 3xx case needs a private opener; everything else must not."""

        def _refuse(*handlers):  # pragma: no cover - must never be called
            raise AssertionError("build_opener used for a non-redirect probe")

        monkeypatch.setattr(readiness.urllib.request, "build_opener", _refuse)
        monkeypatch.setattr(readiness.urllib.request, "urlopen", _fake_urlopen([200]))
        probe = ReadinessProbe(kind="http", url="http://127.0.0.1:18000/")

        assert wait(FakeBackend(), make_handle(tmp_path), probe) is True

    def test_a_declared_status_also_excludes_other_answers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            readiness.urllib.request, "urlopen", _fake_urlopen([200] * 50)
        )
        probe = ReadinessProbe(
            kind="http", url="http://127.0.0.1:18000/", status=401, timeout=3
        )

        with pytest.raises(E2EEnvironmentError):
            wait(FakeBackend(), make_handle(tmp_path), probe)

    def test_an_http_probe_without_a_url_is_a_config_error(self, tmp_path):
        with pytest.raises(E2EConfigError):
            wait(FakeBackend(), make_handle(tmp_path), ReadinessProbe(kind="http"))


class TestTcpProbe:
    def test_passes_when_the_port_accepts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            readiness.socket, "create_connection", _fake_connect([True])
        )
        probe = ReadinessProbe(kind="tcp", port=5432)

        assert wait(FakeBackend(), make_handle(tmp_path), probe) is True

    def test_retries_a_refused_connection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            readiness.socket, "create_connection", _fake_connect([False, True])
        )
        probe = ReadinessProbe(kind="tcp", port=5432, timeout=30)

        assert wait(FakeBackend(), make_handle(tmp_path), probe) is True

    def test_dials_the_host_named_in_the_url_when_given(self, tmp_path, monkeypatch):
        connect = _fake_connect([True])
        monkeypatch.setattr(readiness.socket, "create_connection", connect)
        probe = ReadinessProbe(kind="tcp", port=5432, url="db.example:5432")

        wait(FakeBackend(), make_handle(tmp_path), probe)

        assert connect.addresses[0] == ("db.example", 5432)

    def test_defaults_to_the_published_port_on_localhost(self, tmp_path, monkeypatch):
        connect = _fake_connect([True])
        monkeypatch.setattr(readiness.socket, "create_connection", connect)

        wait(
            FakeBackend(), make_handle(tmp_path), ReadinessProbe(kind="tcp", port=15432)
        )

        assert connect.addresses[0] == ("127.0.0.1", 15432)

    def test_a_tcp_probe_without_a_port_is_a_config_error(self, tmp_path):
        with pytest.raises(E2EConfigError):
            wait(FakeBackend(), make_handle(tmp_path), ReadinessProbe(kind="tcp"))


class TestLogProbe:
    def test_passes_when_the_pattern_appears(self, tmp_path):
        backend = FakeBackend(log_text="boot\nlistening on 0.0.0.0:8000\n")
        probe = ReadinessProbe(kind="log", pattern=r"listening on .*:8000")

        assert wait(backend, make_handle(tmp_path), probe) is True

    def test_reads_the_log_through_the_backend_snapshot_verb(self, tmp_path):
        backend = FakeBackend(log_text="ready")
        probe = ReadinessProbe(kind="log", pattern="ready")

        wait(backend, make_handle(tmp_path), probe)

        assert backend.snapshot_calls[0][2] == "log"

    def test_an_invalid_pattern_is_a_config_error(self, tmp_path):
        probe = ReadinessProbe(kind="log", pattern="unclosed[")

        with pytest.raises(E2EConfigError):
            wait(FakeBackend(), make_handle(tmp_path), probe)

    def test_a_log_probe_without_a_pattern_is_a_config_error(self, tmp_path):
        with pytest.raises(E2EConfigError):
            wait(FakeBackend(), make_handle(tmp_path), ReadinessProbe(kind="log"))


class TestTimeout:
    def test_timeout_reports_service_kind_and_log_tail(self, tmp_path):
        backend = FakeBackend(
            exec_results=[ExecResult(exit_code=1)] * 200,
            log_text="Traceback\nImportError: no module named app\n",
        )
        probe = ReadinessProbe(
            kind="command", command=("true",), timeout=5, interval=1
        )

        with pytest.raises(E2EEnvironmentError) as excinfo:
            wait(backend, make_handle(tmp_path), probe, service="db")

        message = str(excinfo.value)
        assert "db" in message
        assert "command" in message
        assert "ImportError: no module named app" in message
        assert excinfo.value.remediation

    def test_timeout_without_any_log_still_reports(self, tmp_path):
        backend = FakeBackend(exec_results=[ExecResult(exit_code=1)] * 200)
        probe = ReadinessProbe(kind="command", command=("true",), timeout=2, interval=1)

        with pytest.raises(E2EEnvironmentError):
            wait(backend, make_handle(tmp_path), probe)

    def test_at_least_one_attempt_happens_even_with_a_zero_budget(self, tmp_path):
        backend = FakeBackend(exec_results=[ExecResult(exit_code=1)])
        probe = ReadinessProbe(kind="command", command=("true",), timeout=0)

        with pytest.raises(E2EEnvironmentError):
            wait(backend, make_handle(tmp_path), probe)

        assert len(backend.exec_calls) == 1

    def test_the_sleep_never_outlasts_the_remaining_budget(self, tmp_path):
        """A long interval must not stretch the wait far past its timeout.

        timeout 5 with interval 30 used to sleep the whole 30s after the first
        failed attempt — six times the budget the caller sized the E2E step
        against — before reporting the environment failure.
        """
        backend = FakeBackend(exec_results=[ExecResult(exit_code=1)] * 200)
        probe = ReadinessProbe(
            kind="command", command=("true",), timeout=5, interval=30
        )
        clock = FakeClock()

        with pytest.raises(E2EEnvironmentError):
            wait(backend, make_handle(tmp_path), probe, clock=clock)

        assert clock.slept and max(clock.slept) <= 5
        assert clock.now <= 5.01

    def test_a_short_interval_is_still_slept_in_full(self, tmp_path):
        """The clamp must only ever shorten the last sleep, not every sleep."""
        backend = FakeBackend(
            exec_results=[ExecResult(exit_code=1), ExecResult(exit_code=0)]
        )
        probe = ReadinessProbe(
            kind="command", command=("true",), timeout=60, interval=2
        )
        clock = FakeClock()

        assert wait(backend, make_handle(tmp_path), probe, clock=clock) is True
        assert clock.slept == [2]

    def test_probe_mechanism_errors_do_not_abort_the_wait(self, tmp_path):
        class Flaky(FakeBackend):
            def __init__(self):
                super().__init__()
                self.tries = 0

            def exec(self, handle, service, argv, **kwargs):
                self.tries += 1
                if self.tries < 3:
                    raise RuntimeError("container not accepting exec yet")
                return ExecResult(exit_code=0)

        backend = Flaky()
        probe = ReadinessProbe(kind="command", command=("true",), timeout=60)

        assert wait(backend, make_handle(tmp_path), probe) is True
        assert backend.tries == 3


class TestUnknownKind:
    def test_unknown_kind_is_a_config_error_not_a_silent_pass(self, tmp_path):
        """A typo'd probe kind must never be treated as 'already ready'."""
        probe = ReadinessProbe(kind="htpp", url="http://x/")

        with pytest.raises(E2EConfigError) as excinfo:
            wait(FakeBackend(), make_handle(tmp_path), probe)

        assert "htpp" in str(excinfo.value)

    def test_all_four_documented_kinds_are_accepted(self):
        assert set(readiness.PROBE_KINDS) == {"command", "http", "tcp", "log"}


class TestLogTail:
    def test_returns_the_last_lines(self, tmp_path):
        backend = FakeBackend(log_text="\n".join(str(i) for i in range(100)))

        tail = readiness.read_log_tail(backend, make_handle(tmp_path), "app", lines=5)

        assert tail.splitlines() == ["95", "96", "97", "98", "99"]

    def test_no_destination_is_requested_so_polling_leaves_no_host_files(
        self, tmp_path
    ):
        """A `log` probe reads this once per poll; a file per call would pile up."""
        backend = FakeBackend(log_text="ready")
        probe = ReadinessProbe(kind="log", pattern="ready")

        wait(backend, make_handle(tmp_path), probe)

        assert backend.snapshot_calls
        assert list(tmp_path.iterdir()) == []

    def test_a_snapshot_without_a_path_still_yields_its_text(self, tmp_path):
        """The real backend answers a destination-less log capture in metadata."""
        from tianluo.e2e.backend import Snapshot

        class TextOnly(FakeBackend):
            def snapshot(self, handle, service, target, *, kind="file", destination=None):
                return Snapshot(
                    kind="log", path=None, service=service, metadata={"text": "up\n"}
                )

        assert readiness.read_log_tail(TextOnly(), make_handle(tmp_path), "app") == "up"

    def test_a_backend_that_cannot_snapshot_yields_no_diagnostic(self, tmp_path):
        class Broken(FakeBackend):
            def snapshot(self, *a, **k):
                raise RuntimeError("container is gone")

        assert readiness.read_log_tail(Broken(), make_handle(tmp_path), "app") == ""

    def test_falls_back_to_the_snapshot_file_when_metadata_is_empty(self, tmp_path):
        log_file = tmp_path / "app.log"
        log_file.write_text("from disk\n", encoding="utf-8")

        class FileOnly(FakeBackend):
            def snapshot(
                self, handle, service, target, *, kind="file", destination=None
            ):
                from tianluo.e2e.backend import Snapshot

                return Snapshot(kind="log", path=log_file, service=service)

        tail = readiness.read_log_tail(FileOnly(), make_handle(tmp_path), "app")

        assert "from disk" in tail


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _fake_urlopen(outcomes):
    """urlopen stub: each outcome is a status code or an exception to raise."""
    queue = list(outcomes)

    class _Response:
        def __init__(self, status):
            self.status = status

        def getcode(self):
            return self.status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(url, timeout=None):
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)

    return _urlopen


def _fake_connect(outcomes):
    """socket.create_connection stub: each outcome is accept (True) or refuse."""
    queue = list(outcomes)

    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _connect(address, timeout=None):
        _connect.addresses.append(address)
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if not outcome:
            raise ConnectionRefusedError("refused")
        return _Socket()

    _connect.addresses = []
    return _connect
