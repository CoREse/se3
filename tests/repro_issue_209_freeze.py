"""Deterministic end-to-end reproduction of the issue-#209 WebUI freeze.

issue #209: after a `se3 run` confirms its discovery plan and steps into
`analyze` (discovery→analyze transition), or when a later step errors and is
manually retried, the WebUI conversation area stops appending new content — the
flow keeps advancing (the left status bar updates) but the chat never shows the
next step until you exit and re-enter the session.

This module is the **self-run acceptance harness** for that bug (NOT collected
by the normal pytest run — it stands up real subprocesses and is timing-heavy;
run it explicitly). It stands up a fully isolated instance — a real
`se3-server`, a real `se3 daemon`, a throwaway `se3 init` project, and a real
fake-agent `se3 run --discover` — connects a real `/ws/ui` WebSocket client
(the browser stand-in), drives the discovery→analyze transition, and asserts
whether the live `history_data` append for `analyze` ever crosses the wire.

It is parameterised by **daemon load**:

* `--clean`  — daemon tracks only the tiny temp project. The analyze append IS
  delivered over `/ws/ui` (no freeze).
* `--loaded` — the daemon additionally tracks a **heavy** project root (a large
  `engine.json` + many history dirs + a busy active flow). The push loop is
  starved and the analyze append is **never** delivered over `/ws/ui`, while a
  REST `GET /api/history/{flow}` (exit/re-enter) still returns it — reproducing
  the user-reported freeze.

The G1 diagnosis (see `tests/ISSUE_209_FREEZE_DIAGNOSIS.md`) uses this harness
to坐实 the layer: the freeze is **daemon push-loop starvation under load**, not
the frontend / server-cache / read_flow / dedupe layers (all proven correct on
the real frames in `tests/frontend/fixtures/issue_209/`).

Usage:
    SE3_HISTORY_DIAG=1 python tests/repro_issue_209_freeze.py --loaded --heavy-root /path/to/big/se3project
    python tests/repro_issue_209_freeze.py --clean

When `--heavy-root` is omitted in `--loaded` mode the harness synthesises a
heavy root under the temp tree (large engine.json + a busy active flow).
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import os
import pty
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

WT = Path(__file__).resolve().parents[1]
SRC = WT / "src"
PY = sys.executable


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _poll(fn, n=120, d=0.25):
    for _ in range(n):
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(d)
    return None


FAKE_AGENT = """#!/usr/bin/env python3
import json, os, sys
prompt = ''
argv = sys.argv[1:]
for i, a in enumerate(argv):
    if a == '-p' and i + 1 < len(argv):
        prompt = argv[i + 1]; break
if not prompt:
    try: prompt = sys.stdin.read()
    except Exception: prompt = ''
is_disc = 'DISCOVERY mode' in prompt
if is_disc:
    obj = {'mode': 'confirmation', 'content': 'Proposed.',
           'refined_description': 'Add a /health endpoint', 'questions': []}
else:
    obj = {'summary': 'noop', 'content': 'noop body', 'task_type': 'feature',
           'complexity': 'low', 'scope': 'src', 'reasoning': 'ok', 'relevant_specs': []}
t = json.dumps(obj)
sys.stdout.write(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': t}]}}) + chr(10))
sys.stdout.write(json.dumps({'type': 'result', 'result': t}) + chr(10))
sys.stdout.flush()
"""


def _synth_heavy_root(base: Path) -> Path:
    """Build a heavy se3 project root: a large active engine.json + many history
    dirs + a multi-MB busy active flow — the realistic load that starves the
    daemon push loop."""
    root = base / "heavy_project"
    (root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
    flow_id = "20260101-000000_heavyflow"
    # ~1MB engine.json (parsed SYNC on the event loop by active_flow_signature
    # every tick).
    big_steps = {f"{i:02d}_step_{i:08x}": {"status": "RUNNING", "blob": "x" * 600}
                 for i in range(1200)}
    (root / "tianluo" / "state" / "engine.json").write_text(json.dumps(
        {"flow_id": flow_id, "status": "RUNNING", "state": {"steps": big_steps}}))
    hist = root / "tianluo" / "history"
    # the busy active flow with multi-MB jsonl
    af = hist / flow_id
    af.mkdir(parents=True, exist_ok=True)
    (af / "_meta.json").write_text(json.dumps(
        {"project_root": str(root), "type": "feature", "created_at": "2026-01-01T00:00:00"}))
    for name in ("01_discovery_aaaa", "02_analyze_bbbb", "03_plan_cccc"):
        lines = [json.dumps({"role": "assistant", "content": "y" * 800,
                             "timestamp": "2026-01-01T00:00:01"}) for _ in range(600)]
        (af / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    # many history-only dirs (build_index walk cost)
    for k in range(300):
        d = hist / f"20260101-0000{k:03d}_old{k:06x}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "_meta.json").write_text(json.dumps(
            {"project_root": str(root), "type": "feature", "created_at": "2026-01-01T00:00:00"}))
        (d / f"01_discovery_z{k:04d}.jsonl").write_text(
            "\n".join(json.dumps({"role": "assistant", "content": "z" * 300,
                                  "timestamp": "2026-01-01T00:00:01"}) for _ in range(40)) + "\n")
    return root


def run(loaded: bool, heavy_root: str | None, wait_after: float) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="se3_repro209_"))
    daemon_dir = tmp / "daemon"
    daemon_dir.mkdir()
    project = tmp / "project"
    project.mkdir()
    db_path = tmp / "server.db"
    machine_id = "repro209-machine"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    def env():
        e = dict(os.environ)
        e["PYTHONPATH"] = str(SRC) + os.pathsep + e.get("PYTHONPATH", "")
        e["SE3_DAEMON_DIR"] = str(daemon_dir)
        e["HOME"] = str(tmp)
        e.pop("CLAUDECODE", None)
        return e

    cj = http.cookiejar.CookieJar()
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)))

    def hget(u, t=10):
        with urllib.request.urlopen(u, timeout=t) as r:
            return json.loads(r.read())

    def hpost(u, p, t=10):
        req = urllib.request.Request(u, data=json.dumps(p).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.loads(r.read())

    procs = []
    try:
        subprocess.run([PY, "-m", "tianluo.cli", "init", "-p", str(project)],
                       env=env(), cwd=str(project), capture_output=True, timeout=120)
        fa = tmp / "fake_agent.py"
        fa.write_text(FAKE_AGENT)
        fa.chmod(0o755)
        y = project / "tianluo.yaml"
        y.write_text(y.read_text() +
                     f"\nagents:\n  fake: {{type: claude-code, cmd: {fa}, priority: 10}}\n"
                     "llm_caller:\n  defaults: [fake]\n")

        roots = [str(project)]
        if loaded:
            hr = heavy_root or str(_synth_heavy_root(tmp))
            roots.append(hr)
            print(f"[repro] LOADED mode — heavy root: {hr}")
        else:
            print("[repro] CLEAN mode — temp project only")

        import tianluo.server.crypto as crypto
        from tianluo.server.persistence import Store
        store = Store(str(db_path))
        owner = store.create_owner("admin", is_admin=True)
        store.link_identity(owner, "local", "admin")
        store.set_password(owner, crypto.hash_password("pw"))
        kp, kh = crypto.generate_token("dk")
        store.issue_daemon_key(owner, kh)

        sfh = open(tmp / "server.out", "wb")
        launcher = ("import uvicorn;from tianluo.server.app import create_app;"
                    "from tianluo.server.auth.session import SessionStore,CookieConfig;"
                    f"app=create_app(db_path={str(db_path)!r},"
                    "session_store=SessionStore(cookie_config=CookieConfig(secure=False)));"
                    f"uvicorn.run(app,host='127.0.0.1',port={port},log_level='warning')")
        procs.append(subprocess.Popen([PY, "-c", launcher], env=env(), cwd=str(tmp),
                                      stdout=sfh, stderr=subprocess.STDOUT))
        assert _poll(lambda: hget(base + "/api/health").get("status") == "ok", 60, 0.25)
        hpost(base + "/api/auth/login", {"username": "admin", "password": "pw"})
        cookie = next(c.value for c in cj if c.name == "se3_session")

        roots_lit = ", ".join(repr(r) for r in roots)
        dfh = open(tmp / "daemon.out", "wb")
        dl = ("from tianluo.daemon.daemon import Daemon,DaemonConfig;"
              f"d=Daemon(DaemonConfig(server_url='ws://127.0.0.1:{port}',pid_dir=r'{daemon_dir}',"
              f"project_roots=[{roots_lit}],machine_id='{machine_id}',daemon_key={kp!r},poll_interval=0.4));"
              "d.supervisor.discover_flows=lambda *a,**k: [];"
              "d.run_forever()")
        procs.append(subprocess.Popen([PY, "-c", dl], env=env(), cwd=str(tmp),
                                      stdout=dfh, stderr=subprocess.STDOUT))

        def online():
            for m in hget(base + "/api/machines").get("machines", []):
                if m.get("machine_id") == machine_id and m.get("online"):
                    return True
            return False
        assert _poll(online, 150, 0.25), "daemon never came online"

        ws_frames = []
        stop = threading.Event()

        def ws_thread():
            import websockets

            async def runws():
                url = f"ws://127.0.0.1:{port}/ws/ui"
                # Emulate the real browser/server frame budget: lift the
                # ``websockets`` 1 MiB default to the shared protocol cap, the
                # same ``MAX_WS_MESSAGE_BYTES`` the server passes to uvicorn and
                # the daemon passes to ``websockets.connect``. Without this the
                # heavy root's first ``mode: full`` push (a multi-MB frame) would
                # exceed the default limit and *close this stand-in client*
                # (1009), so it would receive nothing thereafter — a harness
                # artifact that masks the daemon-side fix as a false "freeze".
                from tianluo.daemon import protocol as _protocol

                async with websockets.connect(
                    url,
                    additional_headers={"Cookie": f"se3_session={cookie}"},
                    max_size=_protocol.MAX_WS_MESSAGE_BYTES,
                ) as ws:
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if msg.get("type") == "history_data":
                            recs = msg.get("records") or []
                            ws_frames.append({
                                "mode": msg.get("mode"), "flow_id": msg.get("flow_id"),
                                "n": len(recs),
                                "steps": sorted({r.get("step_id") for r in recs}),
                                "t": time.time()})
            asyncio.run(runws())

        threading.Thread(target=ws_thread, daemon=True).start()
        time.sleep(1.0)

        e = env()
        e["FAKE_AGENT_LOG"] = str(tmp / "agent.log")
        mfd, sfd = pty.openpty()
        run_p = subprocess.Popen([PY, "-m", "tianluo.cli", "run", "--discover", "Add a health endpoint"],
                                 env=e, cwd=str(project), stdin=sfd, stdout=sfd, stderr=sfd, close_fds=True)
        procs.append(run_p)
        os.close(sfd)
        dstop = threading.Event()

        def bgdrain():
            while not dstop.is_set():
                r, _, _ = select.select([mfd], [], [], 0.2)
                if not r:
                    continue
                try:
                    os.read(mfd, 8192)
                except OSError:
                    break
        threading.Thread(target=bgdrain, daemon=True).start()

        ej = project / "tianluo" / "state" / "engine.json"

        def flow_id():
            if not ej.exists():
                return None
            try:
                return json.loads(ej.read_text()).get("flow_id")
            except Exception:
                return None
        fid = _poll(flow_id, 200, 0.2)
        print("[repro] flow:", fid)
        snap = _poll(lambda: (hget(base + f"/api/history/{fid}") if fid else None), 150, 0.5)
        assert snap is not None, "flow never visible via /api/history"
        frames_at_open = len(ws_frames)

        def pending():
            try:
                det = hget(base + f"/api/flows/{fid}")
            except Exception:
                return None
            for c in (det.get("flow") or {}).get("pending_calls", []):
                if c.get("kind") == "discovery_confirm":
                    return c
            return None
        chip = _poll(pending, 200, 0.3)
        assert chip, "web never saw the discovery_confirm chip"
        hpost(base + f"/api/flows/{fid}/respond", {"call_id": chip["call_id"], "response": "1"})
        confirm_t = time.time()

        def analyze_on_disk():
            hd = project / "tianluo" / "history" / fid
            return hd.exists() and any("analyze" in f.name for f in hd.iterdir())
        _poll(analyze_on_disk, 200, 0.2)
        time.sleep(wait_after)
        stop.set()
        dstop.set()
        time.sleep(0.8)

        post = ws_frames[frames_at_open:]
        got_analyze = any(any("analyze" in (s or "") for s in f["steps"]) for f in post)
        final = hget(base + f"/api/history/{fid}")
        fsteps = sorted({r.get("step_id") for r in (final.get("records") or [])})
        full_has_analyze = any("analyze" in s for s in fsteps)

        print("\n[repro] WS history_data frames after open:")
        for f in post:
            print(f"   mode={f['mode']} n={f['n']} dt={f['t']-confirm_t:+.1f}s steps={f['steps']}")
        print(f"\n[repro] analyze over live WS: {got_analyze}")
        print(f"[repro] analyze via exit/re-enter (REST full): {full_has_analyze}")
        frozen = (not got_analyze) and full_has_analyze
        print("\n[repro] RESULT:",
              "FREEZE REPRODUCED (live WS missing analyze, exit/re-enter shows it)"
              if frozen else "no freeze (live WS carried analyze)")
        return 1 if frozen else 0
    finally:
        for p in reversed(procs):
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--clean", action="store_true", help="daemon tracks only the temp project")
    g.add_argument("--loaded", action="store_true", help="daemon also tracks a heavy root")
    ap.add_argument("--heavy-root", default=None, help="path to a heavy real se3 project root")
    ap.add_argument("--wait-after", type=float, default=30.0,
                    help="seconds to keep watching the WS after the analyze file lands")
    a = ap.parse_args()
    loaded = a.loaded or (a.heavy_root is not None)
    return run(loaded=loaded, heavy_root=a.heavy_root, wait_after=a.wait_after)


if __name__ == "__main__":
    raise SystemExit(main())
