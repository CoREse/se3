"""Isolated end-to-end acceptance test for the web-console message-rendering
paradigm on **real daemon records**, plus CLI-pause → web-respond → resume.

This is the self-run acceptance harness for the "web console rendering paradigm
works on real daemon data" fix. It stands up a fully isolated instance —

* a private ``SE3_DAEMON_DIR`` (temp dir, never ``~/.se3``),
* a dedicated free ``127.0.0.1`` port (never the user's ``ws://192.168.1.10:4573``
  ``.se3-stable`` daemon, pid 2164255),
* a real ``se3-server`` **and** a real ``se3 daemon`` started as subprocesses
  **from the current worktree** (``PYTHONPATH`` pinned to ``<worktree>/src``),
* a throwaway ``se3 init`` project,

and then proves, against records the *real* daemon produced (file-name →
``step_type`` injection in :mod:`se3.daemon.history`), that:

(a) the rendering paradigm takes effect — step headers read the paradigm names
    (DISCOVERY / IMPLEMENT / VERSION ANALYZE …) rather than the raw
    ``NN_<type>_<hash>`` file stem, and a discovery assistant turn renders its
    structured fields instead of a raw ``json`` blob — verified over real HTTP
    (``GET /api/history/{flow_id}``) and through the production ``app.js``
    conversation renderer; and

(b) a CLI-started ``se3 run --discover`` that pauses at the programmatic
    confirmation gate is visible from the web console as a pending interaction
    chip, can be answered through ``POST /api/flows/{id}/respond``, and the
    *same live process* consumes the answer and advances — with the stale chip
    clearing afterwards.

**Isolation guarantees.** The harness asserts every invariant that keeps it
away from the user's real daemon: the daemon dir is the temp dir, the server
host/port is our chosen ``127.0.0.1:<free>``, no path references
``.se3-stable`` / ``192.168.1.10``, and the launched daemon has its external
``se3 run`` process scan disabled so it can *never* aggregate the user's real
flows. Teardown stops both subprocesses and removes the temp tree.

**Graceful degradation.** When the ``server`` extra (fastapi/uvicorn/websockets)
is missing the whole module skips. The headless-browser sub-case skips when
Playwright/Chromium cannot launch, but the HTTP-layer and node-renderer
assertions (which together prove the paradigm on real records) still run — the
acceptance is never reduced to a faked-``step_type`` unit test.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# --------------------------------------------------------------------------
# Environment / capability probes
# --------------------------------------------------------------------------

_WORKTREE = Path(__file__).resolve().parents[1]
_SRC = _WORKTREE / "src"
_PY = sys.executable

#: The user's real stable daemon endpoint — the harness must NEVER touch it.
_FORBIDDEN_HOST = "192.168.1.10"
_FORBIDDEN_PORT = 4573
_FORBIDDEN_MARKER = ".se3-stable"


def _server_deps_available() -> bool:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        import websockets  # noqa: F401
    except Exception:
        return False
    return True


def _node_available() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _server_deps_available(),
    reason="server extra (fastapi/uvicorn/websockets) not installed",
)


# --------------------------------------------------------------------------
# Small HTTP helpers
# --------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _http_get(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post(url: str, payload: Dict[str, Any], timeout: float = 5.0) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _poll(fn, *, attempts: int = 80, delay: float = 0.25):
    """Call ``fn`` until it returns a truthy value or attempts run out."""
    last = None
    for _ in range(attempts):
        try:
            last = fn()
        except Exception:
            last = None
        if last:
            return last
        time.sleep(delay)
    return last


# --------------------------------------------------------------------------
# Isolated instance harness (Task 1)
# --------------------------------------------------------------------------


class _IsolatedConsole:
    """An isolated se3-server + se3 daemon + temp project, from the worktree."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="se3_console_e2e_"))
        self.daemon_dir = self.tmp / "daemon"
        self.daemon_dir.mkdir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.machine_id = "e2e-isolated-machine"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.server: Optional[subprocess.Popen] = None
        self.daemon: Optional[subprocess.Popen] = None
        self._server_log = self.tmp / "server.out"
        self._daemon_log = self.tmp / "daemon.out"
        self._sfh = None
        self._dfh = None

    # -- environment -------------------------------------------------------

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["SE3_DAEMON_DIR"] = str(self.daemon_dir)
        # Force HOME into the temp tree so even a fallback ``~/.se3`` lookup
        # can never land on the user's real daemon dir.
        env["HOME"] = str(self.tmp)
        env.pop("CLAUDECODE", None)
        return env

    # -- isolation guards --------------------------------------------------

    def assert_isolation_guards(self) -> None:
        """Fail loudly if anything could touch the user's real daemon."""
        # The daemon dir is the temp dir, never ~/.se3.
        resolved = self.daemon_dir.resolve()
        assert str(resolved).startswith(str(self.tmp.resolve())), resolved
        assert resolved != (Path.home() / ".se3"), resolved
        # The port is our chosen free port, never the forbidden stable port.
        assert self.port != _FORBIDDEN_PORT, self.port
        # No path / endpoint references the user's stable instance.
        for blob in (str(self.tmp), self.base, str(self.daemon_dir), str(self.project)):
            assert _FORBIDDEN_MARKER not in blob, blob
            assert _FORBIDDEN_HOST not in blob, blob
        # The server is bound to loopback only.
        assert self.base.startswith("http://127.0.0.1:"), self.base

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.assert_isolation_guards()
        env = self._env()

        # 1. Initialise a throwaway SE3 project from the worktree.
        init = subprocess.run(
            [_PY, "-m", "se3.cli", "init", "-p", str(self.project)],
            env=env,
            cwd=str(self.project),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert init.returncode == 0, f"se3 init failed: {init.stdout}\n{init.stderr}"
        assert (self.project / "se3" / "specs" / "base" / "spec.md").exists()

        # 2. Start se3-server (worktree) bound to our free loopback port.
        self._sfh = open(self._server_log, "wb")
        self.server = subprocess.Popen(
            [
                _PY,
                "-c",
                "from se3.server import main; main(['--host','127.0.0.1',"
                f"'--port','{self.port}','--log-level','warning'])",
            ],
            env=env,
            cwd=str(self.tmp),
            stdout=self._sfh,
            stderr=subprocess.STDOUT,
        )
        ready = _poll(lambda: _http_get(self.base + "/api/health"), attempts=60, delay=0.25)
        assert ready and ready.get("status") == "ok", "se3-server never became healthy"

        # 3. Start the daemon (worktree) dialing our server, scoped strictly to
        #    the temp project. External ``se3 run`` process scanning is disabled
        #    so the daemon can never aggregate the user's real flows.
        launcher = (
            "from se3.daemon.daemon import Daemon, DaemonConfig;"
            "d=Daemon(DaemonConfig("
            f"server_url='ws://127.0.0.1:{self.port}',"
            f"pid_dir=r'{self.daemon_dir}',"
            f"project_roots=[r'{self.project}'],"
            f"machine_id='{self.machine_id}',"
            "poll_interval=0.5));"
            # Hard isolation: never discover externally-running se3 run procs.
            "d.supervisor.discover_flows=lambda *a, **k: [];"
            "d.run_forever()"
        )
        self._dfh = open(self._daemon_log, "wb")
        self.daemon = subprocess.Popen(
            [_PY, "-c", launcher],
            env=env,
            cwd=str(self.tmp),
            stdout=self._dfh,
            stderr=subprocess.STDOUT,
        )
        online = _poll(self._machine_online, attempts=100, delay=0.25)
        assert online, "daemon never connected to the isolated server"

    def _machine_online(self) -> bool:
        data = _http_get(self.base + "/api/machines")
        for m in data.get("machines", []):
            if m.get("machine_id") == self.machine_id and m.get("online"):
                return True
        return False

    def stop(self) -> None:
        for proc in (self.daemon, self.server):
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
        for fh in (self._dfh, self._sfh):
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass

    def logs(self) -> str:
        out = []
        for name, path in (("server", self._server_log), ("daemon", self._daemon_log)):
            try:
                out.append(f"--- {name} ---\n{path.read_text(errors='replace')[-2000:]}")
            except Exception:
                pass
        return "\n".join(out)

    # -- convenience -------------------------------------------------------

    def flow_history(self, flow_id: str) -> List[Dict[str, Any]]:
        data = _http_get(self.base + f"/api/history/{flow_id}")
        return list(data.get("records") or [])


@pytest.fixture(scope="module")
def console() -> "_IsolatedConsole":
    inst = _IsolatedConsole()
    try:
        inst.start()
    except Exception:
        sys.stderr.write(inst.logs() + "\n")
        inst.stop()
        shutil.rmtree(inst.tmp, ignore_errors=True)
        raise
    try:
        yield inst
    finally:
        tmp = inst.tmp
        daemon_dir = inst.daemon_dir
        inst.stop()
        # Teardown must be clean: no lingering pidfile, dir removable.
        shutil.rmtree(tmp, ignore_errors=True)
        assert not tmp.exists(), "temp tree was not removed on teardown"
        assert not daemon_dir.exists(), "daemon dir lingered after teardown"


# --------------------------------------------------------------------------
# Real-record fixtures
# --------------------------------------------------------------------------

# Sentinel markers used by the role-based collapse split (prompt_markers.py),
# so the user record carries a real prefix / user-content / suffix shape.
_TPL_END = "<!--SE3:TEMPLATE_END-->"
_UC_BEGIN = "<!--SE3:USER_CONTENT-->"
_UC_END = "<!--SE3:USER_CONTENT_END-->"


def _write_real_history_flow(project: Path, flow_id: str) -> Dict[str, str]:
    """Write a real-shaped, multi-step history flow into ``project``.

    The on-disk records mirror exactly what the engine's chat-history writer
    produces: each ``message`` carries only ``{role, content, timestamp}`` —
    crucially **no** ``step_type`` field (real daemon payloads never have one).
    The authoritative ``step_type`` is left for the real daemon to derive from
    the file-name convention ``NN_<step_type>_<hash>(_Gk)``. The chosen names
    exercise the full parser matrix: a plain ``discovery``, a group-suffixed
    ``implement`` (``_G1``), and an underscore-bearing ``version_analyze``.
    """
    hist = project / "se3" / "history" / flow_id
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "_meta.json").write_text(
        json.dumps(
            {
                "project_root": str(project),
                "type": "feature",
                "created_at": "2026-05-21T10:00:00",
            }
        ),
        encoding="utf-8",
    )

    user_body = (
        f"You are an expert software engineer.{_TPL_END}{_UC_BEGIN}"
        "Add a /health endpoint to the server"
        f"{_UC_END}Available Specs / Guidelines / READ-ONLY CONSTRAINT"
    )
    discovery_json = json.dumps(
        {
            "mode": "confirmation",
            "content": "I understand the requirement and have a refined description.",
            "refined_description": "Add a GET /health endpoint returning service status",
            "questions": [],
        }
    )
    discovery_records = [
        {"role": "user", "content": user_body, "timestamp": "2026-05-21T10:00:01"},
        {
            "role": "assistant",
            "content": "Here is my analysis.\n```json\n" + discovery_json + "\n```",
            "timestamp": "2026-05-21T10:00:02",
        },
    ]
    (hist / "01_discovery_975607bb.jsonl").write_text(
        "\n".join(json.dumps(r) for r in discovery_records), encoding="utf-8"
    )

    # A ``step_completed`` event — the per-step jsonl line the engine's
    # HistorySink persists. The frontend turns it into a default-expanded,
    # no-max-height report card (the Per-Step Report Cards paradigm), so the
    # in-browser render path can assert that card on real-shaped data. Its
    # inner ``message`` is the raw event object (``type`` = step_completed,
    # nested ``data.step.outputs``) and still carries no envelope-level
    # ``step_type`` — that stays the daemon's file-name-derived injection.
    analyze_event = {
        "type": "step_completed",
        "step_id": "03_analyze_a1b2c3d4",
        "timestamp": "2026-05-21T10:00:30",
        "data": {
            "step": {
                "step_type": "analyze",
                "step_id": "03_analyze_a1b2c3d4",
                "status": "COMPLETED",
                "outputs": {
                    "task_type": "feature",
                    "complexity": "medium",
                    "scope": "src/se3/server",
                    "reasoning": "Add a GET /health endpoint to the server module.",
                    "relevant_specs": ["base:Server Modules"],
                },
            }
        },
    }
    (hist / "03_analyze_a1b2c3d4.jsonl").write_text(
        json.dumps(analyze_event), encoding="utf-8"
    )

    # Group-suffixed implement step — exercises the ``_G1`` suffix peeling.
    impl_record = {
        "role": "assistant",
        "content": "Implemented the endpoint.",
        "timestamp": "2026-05-21T10:01:00",
    }
    (hist / "05_implement_61605e42_G1.jsonl").write_text(
        json.dumps(impl_record), encoding="utf-8"
    )

    # Underscore-bearing type name — exercises the middle-segment preservation.
    va_record = {
        "role": "assistant",
        "content": "Determined the version bump.",
        "timestamp": "2026-05-21T10:02:00",
    }
    (hist / "13_version_analyze_def45678.jsonl").write_text(
        json.dumps(va_record), encoding="utf-8"
    )

    return {
        "refined": "Add a GET /health endpoint returning service status",
        "content": "I understand the requirement and have a refined description.",
    }


# --------------------------------------------------------------------------
# Task 1 — harness isolation guards & connectivity
# --------------------------------------------------------------------------


def test_harness_is_isolated_and_connected(console: "_IsolatedConsole") -> None:
    """Task 1: the instance is isolated (temp dir + dedicated port, never the
    real daemon) and the worktree-launched daemon/server are talking."""
    console.assert_isolation_guards()

    # Daemon online on our isolated server.
    machines = _http_get(console.base + "/api/machines")["machines"]
    ours = [m for m in machines if m["machine_id"] == console.machine_id]
    assert ours and ours[0]["online"], machines
    # Only our isolated machine — no bleed-through from the real daemon.
    assert all(m["machine_id"] == console.machine_id for m in machines), machines

    # The daemon's runtime files live under the temp daemon dir, not ~/.se3.
    assert console.daemon_dir.exists()
    assert any(console.daemon_dir.iterdir()), "daemon wrote no runtime files"
    assert console.daemon_dir.resolve() != (Path.home() / ".se3")


# --------------------------------------------------------------------------
# Task 2 — rendering paradigm on real daemon records
# --------------------------------------------------------------------------


def test_real_daemon_injects_paradigm_step_type(console: "_IsolatedConsole") -> None:
    """Task 2 (HTTP layer): the real daemon injects an authoritative
    ``step_type`` (parsed from the file-name) into every record while the inner
    ``message`` keeps the real shape (no ``step_type``)."""
    flow_id = "t2_render_flow"
    expect = _write_real_history_flow(console.project, flow_id)

    records = _poll(
        lambda: console.flow_history(flow_id) or None, attempts=60, delay=0.3
    )
    assert records, f"flow history never surfaced over HTTP:\n{console.logs()}"

    by_type = {r.get("step_type") for r in records}
    assert {"discovery", "implement", "version_analyze"} <= by_type, by_type

    # The step ids are the raw file stems; the type is the daemon-derived value.
    pairs = {(r["step_id"], r["step_type"]) for r in records}
    assert ("01_discovery_975607bb", "discovery") in pairs, pairs
    assert ("05_implement_61605e42_G1", "implement") in pairs, pairs
    assert ("13_version_analyze_def45678", "version_analyze") in pairs, pairs

    # Real daemon payloads must NOT carry an inner message.step_type — that
    # would be the faked shape this whole fix exists to remove.
    for r in records:
        assert "step_type" not in (r.get("message") or {}), r

    # The discovery assistant turn carries the structured refined_description.
    disc = [
        r
        for r in records
        if r["step_type"] == "discovery" and (r["message"] or {}).get("role") == "assistant"
    ]
    assert disc and expect["refined"] in disc[0]["message"]["content"]


@pytest.mark.skipif(not _node_available(), reason="node not available for app.js render")
def test_render_paradigm_via_appjs_on_real_records(console: "_IsolatedConsole") -> None:
    """Task 2 (rendering): feed the REAL daemon records through the production
    ``app.js`` conversation renderer and assert the paradigm took effect —
    paradigm step headers (DISCOVERY / IMPLEMENT / VERSION ANALYZE) instead of
    file stems, and a structured discovery result card instead of a raw blob."""
    flow_id = "t2_render_flow"
    _write_real_history_flow(console.project, flow_id)
    records = _poll(lambda: console.flow_history(flow_id) or None, attempts=60, delay=0.3)
    assert records, "no records to render"

    recs_file = console.tmp / "records_for_render.json"
    recs_file.write_text(json.dumps(records), encoding="utf-8")
    helper = _WORKTREE / "tests" / "frontend" / "render_real_records.mjs"
    assert helper.exists(), helper

    proc = subprocess.run(
        ["node", str(helper), str(recs_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"render assertion failed:\n{proc.stdout}\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], result
    assert "DISCOVERY" in result["headers"], result
    assert "IMPLEMENT" in result["headers"], result
    assert "VERSION ANALYZE" in result["headers"], result
    assert result["discovery_structured"], result


def test_render_paradigm_in_headless_browser(console: "_IsolatedConsole") -> None:
    """Task 2 (browser): a real headless browser loads the console, drives the
    production ``app.js`` ``renderConversation`` over the real
    ``GET /api/history/{flow_id}`` records, and asserts the rendered DOM
    paradigm — NOT merely the HTTP envelope.

    This is a *critical acceptance test* (registered in ``se3.yaml`` under
    ``test.critical_tests``): it is the only case that exercises the real UI
    render path end to end through a browser, so it MUST actually run rather
    than skip. When Playwright or its Chromium binary is missing the test
    fails loudly with install guidance instead of silently skipping —
    skipping a critical acceptance test is treated by the engine as an
    unverified result, not as "no failures".

    What it proves, inside real Chromium, on real daemon records:
      (1) the page shell boots (``#se3-version`` renders from
          ``GET /api/version``) and the production ``app.js`` is live in-page;
      (2) feeding the real ``/api/history`` records through ``renderConversation``
          produces the message paradigm in the rendered DOM:
            - step-section headers read the paradigm names (DISCOVERY /
              IMPLEMENT / VERSION ANALYZE) — never the raw ``NN_<type>_<hash>``
              file stem;
            - the discovery assistant turn renders structured fields (a
              Proposed Task Description card) instead of a raw ```json``` blob;
            - the marker-split user turn shows ONLY the literal input by default
              (Three-Tier Progressive Disclosure), with no row-level ``查看原始``
              raw toggle leaking into the default view;
            - a ``step_completed`` event renders a default-expanded report card
              whose body has NO ``max-height`` cap (the browser-only CSS
              contract verified via ``getComputedStyle``);
      (3) the daemon-injected paradigm ``step_type`` envelope is intact (the
          discovery/implement/version_analyze types are present and the inner
          ``message`` carries no ``step_type``).

    The rendered-DOM judgement (1)/(2) is the SINGLE shared source
    ``tests/frontend/render_in_browser.mjs::paradigmAssertions`` — the very same
    function the node + DOM-stub path
    (``test_render_paradigm_via_appjs_on_real_records``) runs — injected here as
    a classic ``<script>`` so the in-browser and node checks can never drift."""
    # Keep the isolation invariants enforced even on the browser path.
    console.assert_isolation_guards()

    install_hint = (
        "Install the browser test dependency with `pip install se3[browser]`, "
        "then download the Chromium binary with `playwright install chromium`, "
        "and provision its system libraries with "
        "`scripts/install_browser_test_libs.sh`."
    )
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        pytest.fail(f"Playwright is not installed. {install_hint} ({exc})")

    shared = _WORKTREE / "tests" / "frontend" / "render_in_browser.mjs"
    assert shared.exists(), shared
    shared_js = shared.read_text(encoding="utf-8")

    flow_id = "t2_render_flow"
    _write_real_history_flow(console.project, flow_id)
    _poll(lambda: console.flow_history(flow_id) or None, attempts=60, delay=0.3)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.fail(
                f"Could not launch headless Chromium. {install_hint} ({exc})"
            )
        try:
            page = browser.new_page()
            page.goto(console.base, wait_until="domcontentloaded")
            # The console boots and renders its version label from /api/version,
            # which also proves the production app.js loaded in-page.
            page.wait_for_selector("#se3-version", timeout=15000)
            page.wait_for_function("typeof window.renderConversation === 'function'",
                                   timeout=15000)

            # Inject the shared paradigm judgement (classic <script>; the file
            # has no import/export and self-attaches to window.__se3Paradigm).
            page.add_script_tag(content=shared_js)
            page.wait_for_function(
                "window.__se3Paradigm && "
                "typeof window.__se3Paradigm.paradigmAssertions === 'function'",
                timeout=15000,
            )

            # Fetch the REAL flow history in-page, render it through the
            # production renderConversation, and run the shared DOM-paradigm
            # judgement plus the browser-only CSS (max-height) contract.
            result = page.evaluate(
                """async (fid) => {
                    const r = await fetch(`/api/history/${fid}`);
                    const j = await r.json();
                    const records = j.records || [];

                    const container = document.createElement("div");
                    container.id = "__paradigm_test_container__";
                    document.body.appendChild(container);
                    // Production renderer, real records, full (non-append) build.
                    window.renderConversation(container, records, false);

                    // Shared, single-source DOM-paradigm judgement.
                    const res = window.__se3Paradigm.paradigmAssertions(
                        records, container);

                    // Browser-only CSS contract: every report-card body must be
                    // visible (default-expanded) and carry NO max-height cap, so
                    // long reports scroll with the page, not inside a captive
                    // viewport (running-flow-console: no per-block height caps).
                    res.report_card_maxheight_ok = true;
                    res.report_card_visible = true;
                    res.report_body_count = 0;
                    res.maxheight_value = "";
                    const bodies = Array.from(
                        container.querySelectorAll(".step-report__body"));
                    res.report_body_count = bodies.length;
                    for (const b of bodies) {
                        const cs = getComputedStyle(b);
                        if (cs.maxHeight !== "none") {
                            res.report_card_maxheight_ok = false;
                            res.maxheight_value = cs.maxHeight;
                        }
                        if (cs.display === "none") {
                            res.report_card_visible = false;
                        }
                    }

                    // Envelope step_type integrity, surfaced for the Python
                    // side to assert on real records.
                    res.envelope_types = records.map((x) => x.step_type);
                    res.inner_has_step_type = records.some(
                        (x) => x.message &&
                            Object.prototype.hasOwnProperty.call(
                                x.message, "step_type"));
                    return res;
                }""",
                flow_id,
            )

            # (2) The rendered-DOM paradigm took effect in a real browser.
            assert result["ok"], result
            assert "DISCOVERY" in result["headers"], result
            assert "IMPLEMENT" in result["headers"], result
            assert "VERSION ANALYZE" in result["headers"], result
            assert result["discovery_structured"], result
            assert result["discovery_proposed_card"], result
            assert result["user_literal_only"], result
            assert result["raw_nested"], result
            # Per-step report card present, default-expanded, no max-height cap.
            assert result["report_card_present"], result
            assert result.get("report_body_count", 0) >= 1, result
            assert result["report_card_visible"], result
            assert result["report_card_maxheight_ok"], result

            # (3) The daemon-injected paradigm step_type envelope is intact.
            assert {"discovery", "implement", "version_analyze"} <= set(
                result["envelope_types"]
            ), result
            assert not result["inner_has_step_type"], result
        finally:
            browser.close()


# --------------------------------------------------------------------------
# Task 3 — CLI discovery pause → web respond → same-process resume
# --------------------------------------------------------------------------


def _write_fake_agent(path: Path) -> None:
    """Write a deterministic fake Claude agent.

    It mimics the Claude CLI stream-json interface enough for the engine: reads
    the prompt from ``-p`` argv or stdin, logs each invocation (``discovery``
    vs ``other``) to ``$FAKE_AGENT_LOG``, and emits a stream-json transcript.
    A DISCOVERY-mode prompt gets a confirmation response (``refined_description``
    + no questions) so the step pauses at the programmatic confirmation gate;
    anything else gets a no-op object (its only purpose is to prove the flow
    advanced past discovery in the same process).
    """
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "log = os.environ.get('FAKE_AGENT_LOG')\n"
        "prompt = ''\n"
        "argv = sys.argv[1:]\n"
        "for i, a in enumerate(argv):\n"
        "    if a == '-p' and i + 1 < len(argv):\n"
        "        prompt = argv[i + 1]; break\n"
        "if not prompt:\n"
        "    try: prompt = sys.stdin.read()\n"
        "    except Exception: prompt = ''\n"
        "is_disc = 'DISCOVERY mode' in prompt\n"
        "if log:\n"
        "    with open(log, 'a') as f:\n"
        "        f.write(('discovery' if is_disc else 'other') + '\\n'); f.flush()\n"
        "if is_disc:\n"
        "    obj = {'mode': 'confirmation', 'content': 'Proposed task below.',\n"
        "           'refined_description': 'Add a /health endpoint to the server',\n"
        "           'questions': []}\n"
        "else:\n"
        "    obj = {'summary': 'noop', 'content': 'noop'}\n"
        "text = json.dumps(obj)\n"
        "sys.stdout.write(json.dumps({'type': 'assistant', 'message': {'content': "
        "[{'type': 'text', 'text': text}]}}) + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'result': text}) + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_cli_discovery_pause_answered_from_web(console: "_IsolatedConsole") -> None:
    """Task 3: a CLI-started ``se3 run --discover`` pauses at the confirmation
    gate; the web console sees the pending interaction chip, answers it via
    ``POST /respond``, and the *same live process* consumes the answer and
    advances — with the stale chip clearing afterwards."""
    import pty

    project = console.project
    env = console._env()
    env["FAKE_AGENT_LOG"] = str(console.tmp / "agent.log")
    agent_log = console.tmp / "agent.log"
    if agent_log.exists():
        agent_log.unlink()

    # Point the project's agent registry at the deterministic fake agent.
    fake_agent = console.tmp / "fake_agent.py"
    _write_fake_agent(fake_agent)
    se3_yaml = project / "se3.yaml"
    se3_yaml.write_text(
        se3_yaml.read_text(encoding="utf-8")
        + f"\nagents:\n  fake: {{type: claude-code, cmd: {fake_agent}, priority: 10}}\n"
        + "llm_caller:\n  defaults: [fake]\n",
        encoding="utf-8",
    )

    # Run discovery under a PTY so it takes the interactive (CLI) path: the
    # pause stays RUNNING and dual-waits the terminal + web response file.
    master_fd, slave_fd = pty.openpty()
    run = subprocess.Popen(
        [_PY, "-m", "se3.cli", "run", "--discover", "Add a health endpoint"],
        env=env,
        cwd=str(project),
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    def _drain(seconds: float = 0.2) -> None:
        import select

        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([master_fd], [], [], 0.1)
            if not r:
                continue
            try:
                if not os.read(master_fd, 4096):
                    break
            except OSError:
                break

    try:
        engine_json = project / "se3" / "state" / "engine.json"

        # 1. Wait for the live process to create the flow and pause.
        def _flow_id() -> Optional[str]:
            _drain(0.2)
            if not engine_json.exists():
                return None
            try:
                return json.loads(engine_json.read_text()).get("flow_id")
            except Exception:
                return None

        flow_id = _poll(_flow_id, attempts=150, delay=0.2)
        assert flow_id, f"flow never created:\n{console.logs()}"

        # 2. The web console sees the pending discovery_confirm chip, flow-scoped.
        def _pending_confirm() -> Optional[Dict[str, Any]]:
            _drain(0.2)
            try:
                detail = _http_get(console.base + f"/api/flows/{flow_id}")
            except Exception:
                return None
            for call in (detail.get("flow") or {}).get("pending_calls", []):
                if call.get("kind") == "discovery_confirm":
                    return call
            return None

        chip = _poll(_pending_confirm, attempts=150, delay=0.3)
        assert chip, f"web never saw the pending discovery chip:\n{console.logs()}"
        assert chip.get("call_id"), chip
        # The chip carries the refined description for the GUI confirm panel.
        assert "health" in (chip.get("prompt") or "").lower()
        # It is scoped to this flow.
        assert (chip.get("context") or {}).get("flow_id") == flow_id

        # Sanity: discovery actually ran before we answer.
        assert agent_log.exists() and "discovery" in agent_log.read_text().split()

        # 3. Answer it from the web with the literal confirm token "1".
        resp = _http_post(
            console.base + f"/api/flows/{flow_id}/respond",
            {"call_id": chip["call_id"], "response": "1"},
        )
        assert resp.get("status") == "dispatched", resp

        # 4. The SAME live process consumes the answer and advances past
        #    discovery (a non-discovery LLM call is the proof of advance).
        def _advanced() -> bool:
            _drain(0.2)
            if agent_log.exists() and "other" in agent_log.read_text().split():
                return True
            try:
                steps = json.loads(engine_json.read_text()).get("state", {}).get("steps", {})
            except Exception:
                return False
            return any(
                "discovery" in sid and st.get("status") == "COMPLETED"
                for sid, st in steps.items()
            )

        assert _poll(_advanced, attempts=150, delay=0.2), (
            f"live process did not consume the web answer / advance:\n{console.logs()}"
        )

        # 5. The stale pending chip clears once the flow advanced past the step.
        def _chip_gone() -> bool:
            _drain(0.2)
            try:
                detail = _http_get(console.base + f"/api/flows/{flow_id}")
            except Exception:
                return False
            calls = (detail.get("flow") or {}).get("pending_calls", [])
            return all(c.get("kind") != "discovery_confirm" for c in calls)

        assert _poll(_chip_gone, attempts=150, delay=0.3), (
            f"stale discovery chip never cleared after the answer:\n{console.logs()}"
        )
    finally:
        try:
            run.terminate()
            run.wait(timeout=10)
        except Exception:
            try:
                run.kill()
            except Exception:
                pass
        try:
            os.close(master_fd)
        except Exception:
            pass
