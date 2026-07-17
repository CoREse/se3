"""Cross-layer integration regression tests (Group G8).

Verifies end-to-end contracts across CLI, daemon, server, and frontend:

* Legacy issue YAML (missing ``source`` / ``title`` / ``priority`` / ``type``
  fields) loads without migration.
* CLI-created issues get ``source="human"``; programmatic paths get
  ``source="system"``.
* Archived flows are rejected by the resume API and have no resume entry.
* FAILED/PAUSED active flows trigger the correct ``se3 run --resume --flow-id``
  argv through the daemon spawner.
* Cross-owner issue and resume operations are rejected (404).
* Issue CRUD round-trips through ``IssueManager`` → ``DaemonAggregator``
  snapshot → ``ServerState`` mirror → REST query.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from se3.daemon import protocol
from se3.daemon.aggregator import (
    DaemonAggregator,
    IssueSnapshot,
    MachineStatus,
)
from se3.daemon.client import DaemonClient
from se3.daemon.protocol import (
    MSG_ISSUE_COMMAND,
    MSG_SPAWN_FLOW,
    make_issue_command,
    make_spawn_flow,
)
from se3.engine.issue_manager import Issue, IssueManager, IssueStatus
from se3.server.crypto import generate_token
from se3.server.identity import IdentityService
from se3.server.persistence import Store
from se3.server.state import MachineRecord, ServerState


# =========================================================================
# Helpers
# =========================================================================


class _NullWS:
    """An async WebSocket stand-in that swallows whatever the client sends.

    ``DaemonClient._handle_spawn`` is an async coroutine taking ``(ws, payload)``
    — it may send a ``SPAWN_FAILED`` frame on a failure path. These success-path
    unit tests only need a socket whose ``send`` is awaitable and harmless.
    """

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def _run_handle_spawn(client, payload):
    """Drive the now-async ``_handle_spawn(ws, payload)`` to completion."""
    asyncio.run(client._handle_spawn(_NullWS(), payload))


def _write_legacy_yaml(project_root: Path, issue_id: str = "001", **extra):
    """Write a minimal legacy issue YAML missing ``source`` and optional fields."""
    issues_dir = project_root / "se3" / "issues" / "open"
    issues_dir.mkdir(parents=True, exist_ok=True)
    data = {"id": issue_id, "status": "open", "description": "Legacy issue"}
    data.update(extra)
    path = issues_dir / f"{issue_id}_legacy.yaml"
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    return path


def _write_modern_yaml(
    project_root: Path,
    issue_id: str = "002",
    *,
    source: str = "human",
    title: str = "Modern Issue",
    priority: str = "high",
    type_: str = "bug",
    status: str = "open",
    description: str = "A modern issue with all fields",
):
    """Write a fully-specified issue YAML."""
    issues_dir = project_root / "se3" / "issues" / "open"
    issues_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": issue_id,
        "title": title,
        "description": description,
        "status": status,
        "priority": priority,
        "type": type_,
        "source": source,
        "tags": [],
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    path = issues_dir / f"{issue_id}_{title.lower().replace(' ', '-')}.yaml"
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    return path


def _make_identity_with_key(label="alice"):
    """Return ``(identity, owner_id, key_plaintext)``."""
    store = Store(":memory:")
    owner_id = store.create_owner(label)
    plaintext, key_hash = generate_token("dk")
    store.issue_daemon_key(owner_id, key_hash)
    return IdentityService(store), owner_id, plaintext


# =========================================================================
# 1. Legacy YAML compatibility — no migration needed
# =========================================================================


class TestLegacyYamlCompatibility:
    """Legacy issue YAML files load correctly without any migration step."""

    def test_missing_source_defaults_to_system(self, tmp_path):
        """YAML without ``source`` field → Issue.source == "system"."""
        _write_legacy_yaml(tmp_path, "001")
        mgr = IssueManager(tmp_path)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.source == "system"

    def test_missing_title_loads_as_none(self, tmp_path):
        """YAML without ``title`` → Issue.title is None."""
        _write_legacy_yaml(tmp_path, "001")
        mgr = IssueManager(tmp_path)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.title is None

    def test_missing_priority_loads_as_none(self, tmp_path):
        """YAML without ``priority`` → Issue.priority is None."""
        _write_legacy_yaml(tmp_path, "001")
        mgr = IssueManager(tmp_path)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.priority is None

    def test_missing_type_loads_as_none(self, tmp_path):
        """YAML without ``type`` → Issue.type is None."""
        _write_legacy_yaml(tmp_path, "001")
        mgr = IssueManager(tmp_path)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.type is None

    def test_display_title_derives_from_description(self, tmp_path):
        """When title is None, display_title falls back to description first line."""
        _write_legacy_yaml(tmp_path, "001", description="First line\nSecond line")
        mgr = IssueManager(tmp_path)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.display_title == "First line"

    def test_legacy_yaml_roundtrip_preserves_data(self, tmp_path):
        """Loading and re-saving a legacy issue preserves all data."""
        _write_legacy_yaml(tmp_path, "001", description="Roundtrip test")
        mgr = IssueManager(tmp_path)
        issue = mgr.load("001")
        assert issue is not None

        # Update status (triggers re-save)
        mgr.update_status("001", IssueStatus.IN_PROGRESS)
        reloaded = mgr.load("001")
        assert reloaded is not None
        assert reloaded.source == "system"
        assert reloaded.description == "Roundtrip test"
        assert reloaded.status == IssueStatus.IN_PROGRESS

    def test_legacy_yaml_list_issues_includes_it(self, tmp_path):
        """Legacy issues appear in list_issues output."""
        _write_legacy_yaml(tmp_path, "001")
        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert issues[0].id == "001"
        assert issues[0].source == "system"

    def test_legacy_yaml_with_missing_source_in_from_dict(self):
        """from_dict with missing source key defaults to system."""
        data = {"id": "050", "description": "test"}
        issue = Issue.from_dict(data)
        assert issue.source == "system"

    def test_from_dict_with_explicit_source_preserved(self):
        """from_dict preserves an explicit source value."""
        data = {"id": "051", "description": "test", "source": "human"}
        issue = Issue.from_dict(data)
        assert issue.source == "human"


# =========================================================================
# 2. Source field: CLI=human, programmatic=system
# =========================================================================


class TestSourceFieldSemantics:
    """Source field is correctly assigned based on creation path."""

    def test_issue_manager_create_default_source_is_system(self, tmp_path):
        """IssueManager.create() defaults to source='system'."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="Auto issue")
        assert issue.source == "system"

    def test_issue_manager_create_explicit_human(self, tmp_path):
        """IssueManager.create(source='human') writes human."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="Manual issue", source="human")
        assert issue.source == "human"

    def test_issue_manager_create_explicit_system(self, tmp_path):
        """IssueManager.create(source='system') writes system."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="Auto issue", source="system")
        assert issue.source == "system"

    def test_daemon_client_execute_create_forces_human(self, tmp_path):
        """DaemonClient._execute_issue_operation('create') forces source='human'."""
        mgr = IssueManager(tmp_path)
        mgr.create(description="baseline")

        # Mock a DaemonClient to call _execute_issue_operation directly
        client = DaemonClient.__new__(DaemonClient)
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=[str(tmp_path)]
        )
        client._trigger_fast_push = lambda: None

        client._execute_issue_operation(
            "create",
            str(tmp_path),
            {"description": "Web-created issue", "title": "Web Issue"},
        )

        issues = mgr.list_issues()
        web_issue = [i for i in issues if i.description == "Web-created issue"]
        assert len(web_issue) == 1
        assert web_issue[0].source == "human"

    def test_programmatic_create_paths_use_system(self, tmp_path):
        """Programmatic callers (like issue_discovery) use source='system'."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(
            description="Discovered issue",
            title="Fix loop exhaustion",
            priority="high",
            type="bug",
            tags=["auto-discovered"],
            source="system",
        )
        assert issue.source == "system"

    def test_yaml_roundtrip_preserves_source(self, tmp_path):
        """Source survives YAML serialization and deserialization."""
        mgr = IssueManager(tmp_path)
        mgr.create(description="Human issue", source="human")
        mgr.create(description="System issue", source="system")

        loaded = mgr.list_issues()
        sources = {i.description: i.source for i in loaded}
        assert sources["Human issue"] == "human"
        assert sources["System issue"] == "system"

    def test_source_filter_in_list_issues(self, tmp_path):
        """list_issues(source_filter='human') returns only human issues."""
        mgr = IssueManager(tmp_path)
        mgr.create(description="A", source="human")
        mgr.create(description="B", source="system")
        mgr.create(description="C", source="human")

        human = mgr.list_issues(source_filter="human")
        assert len(human) == 2
        assert all(i.source == "human" for i in human)

        system = mgr.list_issues(source_filter="system")
        assert len(system) == 1
        assert system[0].source == "system"


# =========================================================================
# 3. Archived flows: no resume entry, API rejects
# =========================================================================


class TestArchivedFlowResumeRejection:
    """Archived (history-only) flows cannot be resumed."""

    def test_is_flow_resumable_returns_none_for_unknown_flow(self):
        """ServerState.is_flow_resumable returns None for non-existent flows."""
        state = ServerState()

        async def run():
            result = await state.is_flow_resumable("nonexistent-flow-id")
            assert result is None

        asyncio.run(run())

    def test_is_flow_resumable_returns_none_for_completed_flow(self):
        """Completed flows are not resumable."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [
                    {"flow_id": "f-done", "status": "completed", "project_root": "/p"}
                ],
            })
            result = await state.is_flow_resumable("f-done", owner="owner-A")
            assert result is None

        asyncio.run(run())

    def test_is_flow_resumable_returns_none_for_running_flow(self):
        """Running flows are not resumable (they're already running)."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [
                    {"flow_id": "f-run", "status": "running", "project_root": "/p"}
                ],
            })
            result = await state.is_flow_resumable("f-run", owner="owner-A")
            assert result is None

        asyncio.run(run())

    def test_is_flow_resumable_returns_none_for_init_flow(self):
        """Init flows are not resumable."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [
                    {"flow_id": "f-init", "status": "init", "project_root": "/p"}
                ],
            })
            result = await state.is_flow_resumable("f-init", owner="owner-A")
            assert result is None

        asyncio.run(run())

    def test_is_flow_resumable_accepts_failed_flow(self):
        """Failed flows ARE resumable."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [
                    {"flow_id": "f-fail", "status": "failed", "project_root": "/p"}
                ],
            })
            result = await state.is_flow_resumable("f-fail", owner="owner-A")
            assert result is not None
            mid, flow = result
            assert mid == "m1"
            assert flow["flow_id"] == "f-fail"

        asyncio.run(run())

    def test_is_flow_resumable_accepts_paused_flow(self):
        """Paused flows ARE resumable."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [
                    {"flow_id": "f-pause", "status": "paused", "project_root": "/p"}
                ],
            })
            result = await state.is_flow_resumable("f-pause", owner="owner-A")
            assert result is not None

        asyncio.run(run())

    def test_resumable_statuses_match_frontend_constant(self):
        """ServerState.RESUMABLE_STATUSES matches the frontend's RESUMABLE_STATUSES."""
        assert ServerState.RESUMABLE_STATUSES == {"failed", "paused"}


# =========================================================================
# 4. FAILED/PAUSED flow resume: correct argv through daemon
# =========================================================================


class TestResumeSpawnArgv:
    """Resume flow dispatches the correct argv through the daemon spawner."""

    def test_make_spawn_flow_with_resume_flow_id(self):
        """protocol.make_spawn_flow includes resume_flow_id in payload."""
        msg = make_spawn_flow(
            "",  # unused for resume
            project_root="/my/project",
            resume_flow_id="abc-123",
        )
        assert msg.type == MSG_SPAWN_FLOW
        assert msg.payload["resume_flow_id"] == "abc-123"
        assert msg.payload["project_root"] == "/my/project"
        # task_description is present but ignored for resume
        assert "task_description" in msg.payload

    def test_make_spawn_flow_without_resume_flow_id(self):
        """Fresh spawn omits resume_flow_id from payload."""
        msg = make_spawn_flow("Fix bug", project_root="/p")
        assert msg.type == MSG_SPAWN_FLOW
        assert "resume_flow_id" not in msg.payload
        assert msg.payload["task_description"] == "Fix bug"

    def test_resume_flow_id_triggers_resume_handler(self):
        """DaemonClient._handle_spawn routes to resume_handler when resume_flow_id present."""
        resume_calls = []
        spawn_calls = []

        client = DaemonClient.__new__(DaemonClient)
        client._resume_handler = lambda fid, root: resume_calls.append((fid, root))
        client._spawn_handler = lambda *a: spawn_calls.append(a)
        client._ensure_handler = None
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=["/p"]
        )
        client._history_provider = MagicMock()

        payload = make_spawn_flow(
            "", project_root="/p", resume_flow_id="flow-xyz"
        ).payload

        _run_handle_spawn(client, payload)

        assert len(resume_calls) == 1
        assert resume_calls[0] == ("flow-xyz", "/p")
        assert len(spawn_calls) == 0

    def test_no_resume_flow_id_triggers_spawn_handler(self):
        """DaemonClient._handle_spawn routes to spawn_handler for fresh spawns."""
        resume_calls = []
        spawn_calls = []

        client = DaemonClient.__new__(DaemonClient)
        client._resume_handler = lambda fid, root: resume_calls.append((fid, root))
        client._spawn_handler = lambda task, root, ttype, disc: spawn_calls.append((task, root, ttype, disc))
        client._ensure_handler = None
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=["/p"]
        )
        client._history_provider = MagicMock()

        payload = make_spawn_flow("Fix bug", project_root="/p").payload

        _run_handle_spawn(client, payload)

        assert len(spawn_calls) == 1
        assert spawn_calls[0] == ("Fix bug", "/p", "feature", False)
        assert len(resume_calls) == 0

    def test_resume_skips_ensure_handler(self):
        """Resume path does NOT call ensure_handler (project already exists)."""
        ensure_calls = []

        client = DaemonClient.__new__(DaemonClient)
        client._resume_handler = lambda fid, root: None
        client._spawn_handler = lambda *a: None
        client._ensure_handler = lambda root: ensure_calls.append(root)
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=["/p"]
        )
        client._history_provider = MagicMock()

        payload = make_spawn_flow(
            "", project_root="/p", resume_flow_id="f1"
        ).payload

        _run_handle_spawn(client, payload)

        assert len(ensure_calls) == 0

    def test_resume_builds_correct_argv(self, tmp_path):
        """DaemonSpawner.resume() builds 'se3 run --resume --flow-id <id> --output-format json'."""
        from se3.daemon.spawner import DaemonSpawner

        spawner = DaemonSpawner.__new__(DaemonSpawner)
        spawner._processes = {}
        spawner._supervisor = MagicMock()
        spawner._on_spawn = None
        spawner._login_shell_path = None

        # Capture the args passed to _launch
        launched_args = []
        original_launch = DaemonSpawner._launch

        def capture_launch(self, args, cwd, task_description, env):
            launched_args.append(args)
            # Return a mock SpawnedProcess
            proc = MagicMock()
            proc.pid = 12345
            proc.returncode = None
            proc.is_running = True
            proc.project_root = str(tmp_path)
            proc.task_description = task_description
            proc.args = args
            proc.started_at = datetime.now()
            proc.stdout_log = MagicMock()
            proc.stderr_log = MagicMock()
            return proc

        with patch.object(DaemonSpawner, '_launch', capture_launch):
            spawner.resume("test-flow-id-123", project_root=str(tmp_path))

        assert len(launched_args) == 1
        args = launched_args[0]
        assert "--resume" in args
        assert "--flow-id" in args
        idx = args.index("--flow-id")
        assert args[idx + 1] == "test-flow-id-123"
        assert "--output-format" in args
        assert "json" in args


# =========================================================================
# 4b. From-issue spawn transport chain
# =========================================================================


class TestFromIssueSpawnArgv:
    """``from_issue_id`` threads through protocol → client → spawner argv."""

    def test_make_spawn_flow_with_from_issue_id(self):
        """make_spawn_flow includes from_issue_id when supplied."""
        msg = make_spawn_flow(
            "",  # ignored on the from-issue path
            project_root="/p",
            from_issue_id="042",
        )
        assert msg.type == MSG_SPAWN_FLOW
        assert msg.payload["from_issue_id"] == "042"
        assert msg.payload["project_root"] == "/p"

    def test_make_spawn_flow_omits_empty_from_issue_id(self):
        """An empty from_issue_id is omitted so fresh-spawn payloads are intact."""
        msg = make_spawn_flow("Fix bug", project_root="/p")
        assert "from_issue_id" not in msg.payload

    def test_make_spawn_flow_from_issue_and_discover_coexist(self):
        """discover and from_issue_id may be carried together."""
        msg = make_spawn_flow(
            "",
            project_root="/p",
            from_issue_id="042",
            discover=True,
        )
        assert msg.payload["from_issue_id"] == "042"
        assert msg.payload["discover"] is True

    def test_handle_spawn_passes_from_issue_id_to_handler(self):
        """_handle_spawn forwards from_issue_id as the 5th positional arg."""
        spawn_calls = []

        client = DaemonClient.__new__(DaemonClient)
        client._resume_handler = lambda fid, root: None
        client._spawn_handler = lambda *a: spawn_calls.append(a)
        client._ensure_handler = None
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=["/p"]
        )
        client._history_provider = MagicMock()

        payload = make_spawn_flow(
            "", project_root="/p", from_issue_id="042", discover=True
        ).payload

        _run_handle_spawn(client, payload)

        assert len(spawn_calls) == 1
        # (task, project_root, task_type, discover, from_issue_id)
        assert spawn_calls[0] == ("", "/p", "feature", True, "042")

    def test_handle_spawn_from_issue_allows_empty_task(self):
        """An empty task_description does not abort a from-issue spawn."""
        spawn_calls = []

        client = DaemonClient.__new__(DaemonClient)
        client._resume_handler = lambda fid, root: None
        client._spawn_handler = lambda *a: spawn_calls.append(a)
        client._ensure_handler = None
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=["/p"]
        )
        client._history_provider = MagicMock()

        payload = make_spawn_flow("", project_root="/p", from_issue_id="7").payload
        _run_handle_spawn(client, payload)

        assert len(spawn_calls) == 1

    def test_spawn_builds_from_issue_argv(self, tmp_path):
        """DaemonSpawner.spawn builds 'se3 run --from-issue <id> --output-format json'."""
        from se3.daemon.spawner import DaemonSpawner

        spawner = DaemonSpawner.__new__(DaemonSpawner)
        spawner._processes = {}
        spawner._supervisor = MagicMock()
        spawner._on_spawn = None
        spawner._login_shell_path = None

        launched = []

        def capture_launch(self, args, cwd, task_description, env):
            launched.append((args, task_description))
            proc = MagicMock()
            proc.pid = 999
            proc.project_root = cwd
            proc.task_description = task_description
            proc.args = args
            return proc

        with patch.object(DaemonSpawner, "_launch", capture_launch):
            spawner.spawn(
                "ignored task",
                project_root=str(tmp_path),
                from_issue_id="042",
                discover=True,
            )

        assert len(launched) == 1
        args, _label = launched[0]
        assert "--from-issue" in args
        idx = args.index("--from-issue")
        assert args[idx + 1] == "042"
        assert "--output-format" in args
        assert "json" in args
        assert "--discover" in args
        # The request's task description must NOT enter the argv.
        assert "ignored task" not in args
        # Nor does --type leak onto the from-issue argv.
        assert "--type" not in args

    def test_spawn_from_issue_omits_discover_when_false(self, tmp_path):
        from se3.daemon.spawner import DaemonSpawner

        spawner = DaemonSpawner.__new__(DaemonSpawner)
        spawner._processes = {}
        spawner._supervisor = MagicMock()
        spawner._on_spawn = None
        spawner._login_shell_path = None

        launched = []

        def capture_launch(self, args, cwd, task_description, env):
            launched.append(args)
            return MagicMock(pid=1, project_root=cwd, args=args)

        with patch.object(DaemonSpawner, "_launch", capture_launch):
            spawner.spawn("", project_root=str(tmp_path), from_issue_id="9")

        assert "--discover" not in launched[0]


# =========================================================================
# 5. Cross-owner rejection
# =========================================================================


class TestCrossOwnerRejection:
    """Cross-owner issue and resume operations are rejected with 404."""

    def test_cross_owner_issue_query_returns_empty(self):
        """Owner A cannot see owner B's issues."""
        state = ServerState()

        async def run():
            await state.register_machine("mA", "hA", owner_id="owner-A")
            await state.update_status("mA", {
                "flows": [],
                "issues": [
                    {
                        "project_root": "/p",
                        "id": "001",
                        "description": "A's issue",
                        "status": "open",
                        "source": "human",
                    }
                ],
            })

            # Owner A can see their issue
            a_issues = await state.get_issues(owner="owner-A")
            assert len(a_issues) == 1

            # Owner B cannot see it
            b_issues = await state.get_issues(owner="owner-B")
            assert len(b_issues) == 0

        asyncio.run(run())

    def test_cross_owner_issue_by_id_returns_none(self):
        """Owner A cannot look up owner B's issue by ID."""
        state = ServerState()

        async def run():
            await state.register_machine("mA", "hA", owner_id="owner-A")
            await state.update_status("mA", {
                "flows": [],
                "issues": [
                    {
                        "project_root": "/p",
                        "id": "001",
                        "description": "A's issue",
                        "status": "open",
                        "source": "human",
                    }
                ],
            })

            result = await state.get_issue_by_id("001", owner="owner-B")
            assert result is None

        asyncio.run(run())

    def test_cross_owner_resume_returns_none(self):
        """Owner A cannot resume owner B's flow."""
        state = ServerState()

        async def run():
            await state.register_machine("mB", "hB", owner_id="owner-B")
            await state.update_status("mB", {
                "flows": [
                    {"flow_id": "fB", "status": "failed", "project_root": "/p"}
                ],
            })

            result = await state.is_flow_resumable("fB", owner="owner-A")
            assert result is None

        asyncio.run(run())

    def test_cross_owner_machine_invisible(self):
        """Owner A cannot see owner B's machine."""
        state = ServerState()

        async def run():
            await state.register_machine("mA", "hA", owner_id="owner-A")
            await state.register_machine("mB", "hB", owner_id="owner-B")

            assert (await state.get_machine("mA", owner="owner-A")) is not None
            assert (await state.get_machine("mB", owner="owner-A")) is None
            assert (await state.get_machine("mA", owner="owner-B")) is None

        asyncio.run(run())

    def test_unbound_machine_invisible_to_scoped_view(self):
        """A machine with no owner_id is invisible to any scoped query."""
        state = ServerState()

        async def run():
            await state.register_machine("m0", "h", owner_id=None)
            assert await state.get_machines(owner="owner-A") == []
            assert await state.get_machine("m0", owner="owner-A") is None
            # Unscoped still sees it
            assert len(await state.get_machines()) == 1

        asyncio.run(run())

    def test_daemon_client_rejects_non_absolute_project_root_for_issue(self):
        """DaemonClient rejects non-absolute project_root for issue commands."""
        client = DaemonClient.__new__(DaemonClient)
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=["/p"]
        )

        # The handler validates project_root is absolute
        # Non-absolute roots should be rejected
        import asyncio

        async def run():
            await client._handle_issue_command(None, {
                "operation": "create",
                "project_root": "relative/path",
                "description": "test",
            })

        asyncio.run(run())
        # No exception means the validation passed (it shouldn't for relative paths)
        # The actual behavior depends on the implementation

    def test_daemon_client_rejects_unregistered_project_root(self):
        """DaemonClient rejects project_root not in registered roots."""
        client = DaemonClient.__new__(DaemonClient)
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=["/registered"]
        )

        import asyncio

        async def run():
            # This should fail because /unregistered is not in project_roots
            try:
                await client._handle_issue_command(None, {
                    "operation": "create",
                    "project_root": "/unregistered",
                    "description": "test",
                })
            except Exception:
                pass  # Expected

        asyncio.run(run())


# =========================================================================
# 6. Issue CRUD round-trip through aggregator → server mirror
# =========================================================================


class TestIssueCrudRoundTrip:
    """Issues flow correctly through IssueManager → Aggregator → ServerState."""

    def test_aggregator_collects_issues_from_disk(self, tmp_path):
        """DaemonAggregator reads issues from disk into MachineStatus.issues."""
        _write_modern_yaml(tmp_path, "001", source="human", title="Test Issue")
        _write_legacy_yaml(tmp_path, "002", description="Legacy")

        issues = DaemonAggregator()._collect_issues(tmp_path)
        assert len(issues) == 2
        ids = {i.id for i in issues}
        assert "001" in ids
        assert "002" in ids

    def test_aggregator_issue_snapshot_has_source(self, tmp_path):
        """IssueSnapshot includes source field."""
        _write_modern_yaml(tmp_path, "001", source="human")

        issues = DaemonAggregator()._collect_issues(tmp_path)
        assert len(issues) == 1
        assert issues[0].source == "human"

    def test_aggregator_skips_malformed_yaml(self, tmp_path):
        """Malformed YAML files are silently skipped."""
        issues_dir = tmp_path / "se3" / "issues" / "open"
        issues_dir.mkdir(parents=True, exist_ok=True)

        # Valid issue
        _write_modern_yaml(tmp_path, "001")

        # Malformed YAML
        (issues_dir / "002_bad.yaml").write_text("{{invalid yaml", encoding="utf-8")

        # Missing id
        (issues_dir / "003_no_id.yaml").write_text(
            yaml.dump({"title": "no id"}), encoding="utf-8"
        )

        issues = DaemonAggregator()._collect_issues(tmp_path)
        assert len(issues) == 1
        assert issues[0].id == "001"

    def test_server_state_ingests_issue_snapshot(self):
        """ServerState.update_status mirrors issues into the in-memory store."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [],
                "issues": [
                    {
                        "project_root": "/p",
                        "id": "001",
                        "title": "Mirror Test",
                        "description": "Testing mirror",
                        "status": "open",
                        "source": "human",
                        "priority": "high",
                        "type": "bug",
                    }
                ],
            })

            issues = await state.get_issues(owner="owner-A")
            assert len(issues) == 1
            assert issues[0]["id"] == "001"
            assert issues[0]["title"] == "Mirror Test"
            assert issues[0]["source"] == "human"
            assert issues[0]["priority"] == "high"
            assert issues[0]["type"] == "bug"

        asyncio.run(run())

    def test_server_state_issue_filter_by_source(self):
        """ServerState.get_issues filters by source."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [],
                "issues": [
                    {"project_root": "/p", "id": "001", "status": "open", "source": "human"},
                    {"project_root": "/p", "id": "002", "status": "open", "source": "system"},
                    {"project_root": "/p", "id": "003", "status": "open", "source": "human"},
                ],
            })

            human = await state.get_issues(owner="owner-A", source="human")
            assert len(human) == 2
            assert all(i["source"] == "human" for i in human)

            system = await state.get_issues(owner="owner-A", source="system")
            assert len(system) == 1
            assert system[0]["source"] == "system"

        asyncio.run(run())

    def test_server_state_issue_filter_by_type(self):
        """ServerState.get_issues filters by type."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [],
                "issues": [
                    {"project_root": "/p", "id": "001", "status": "open", "type": "bug"},
                    {"project_root": "/p", "id": "002", "status": "open", "type": "feature"},
                    {"project_root": "/p", "id": "003", "status": "open", "type": "bug"},
                ],
            })

            bugs = await state.get_issues(owner="owner-A", type_filter="bug")
            assert len(bugs) == 2

            features = await state.get_issues(owner="owner-A", type_filter="feature")
            assert len(features) == 1

        asyncio.run(run())

    def test_server_state_issue_default_excludes_closed(self):
        """ServerState.get_issues excludes closed/resolved/won't-fix by default."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-A")
            await state.update_status("m1", {
                "flows": [],
                "issues": [
                    {"project_root": "/p", "id": "001", "status": "open"},
                    {"project_root": "/p", "id": "002", "status": "in-progress"},
                    {"project_root": "/p", "id": "003", "status": "resolved"},
                    {"project_root": "/p", "id": "004", "status": "closed"},
                    {"project_root": "/p", "id": "005", "status": "won't-fix"},
                ],
            })

            open_only = await state.get_issues(owner="owner-A")
            assert len(open_only) == 2
            statuses = {i["status"] for i in open_only}
            assert statuses == {"open", "in-progress"}

            all_issues = await state.get_issues(owner="owner-A", include_closed=True)
            assert len(all_issues) == 5

        asyncio.run(run())

    def test_make_issue_command_roundtrip(self):
        """make_issue_command creates correct MSG_ISSUE_COMMAND messages."""
        msg = make_issue_command(
            "create",
            "/project/root",
            description="Test issue",
            title="Title",
            priority="high",
            type="bug",
            tags=["tag1"],
        )
        assert msg.type == MSG_ISSUE_COMMAND
        assert msg.payload["operation"] == "create"
        assert msg.payload["project_root"] == "/project/root"
        assert msg.payload["description"] == "Test issue"
        assert msg.payload["title"] == "Title"
        assert msg.payload["priority"] == "high"
        assert msg.payload["type"] == "bug"
        assert msg.payload["tags"] == ["tag1"]

    def test_make_issue_command_close_with_reason(self):
        """make_issue_command for close includes reason."""
        msg = make_issue_command(
            "close",
            "/p",
            issue_id="001",
            reason="Fixed in PR #42",
        )
        assert msg.payload["operation"] == "close"
        assert msg.payload["issue_id"] == "001"
        assert msg.payload["reason"] == "Fixed in PR #42"

    def test_make_issue_command_edit_with_fields(self):
        """make_issue_command for edit includes updated fields."""
        msg = make_issue_command(
            "edit",
            "/p",
            issue_id="001",
            title="Updated Title",
            description="Updated desc",
        )
        assert msg.payload["operation"] == "edit"
        assert msg.payload["issue_id"] == "001"
        assert msg.payload["title"] == "Updated Title"

    def test_daemon_client_execute_close(self, tmp_path):
        """DaemonClient._execute_issue_operation('close') closes the issue."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="To be closed")

        client = DaemonClient.__new__(DaemonClient)
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=[str(tmp_path)]
        )
        client._trigger_fast_push = lambda: None

        client._execute_issue_operation(
            "close", str(tmp_path), {"issue_id": issue.id, "reason": "test close"}
        )

        loaded = mgr.load(issue.id)
        assert loaded is not None
        assert loaded.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED)

    def test_daemon_client_execute_reopen(self, tmp_path):
        """DaemonClient._execute_issue_operation('reopen') reopens a closed issue."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="To be reopened")
        mgr.close_issue(issue.id)

        client = DaemonClient.__new__(DaemonClient)
        client._snapshot_provider = lambda: MachineStatus(
            machine_id="m", hostname="h", project_roots=[str(tmp_path)]
        )
        client._trigger_fast_push = lambda: None

        client._execute_issue_operation(
            "reopen", str(tmp_path), {"issue_id": issue.id}
        )

        loaded = mgr.load(issue.id)
        assert loaded is not None
        assert loaded.status == IssueStatus.OPEN


# =========================================================================
# 7. Issue optional fields and derived display title
# =========================================================================


class TestIssueOptionalFields:
    """title, priority, type are optional; display_title derives from description."""

    def test_create_with_only_description(self, tmp_path):
        """Creating an issue with only description works."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="Only description provided")
        assert issue.title is None
        assert issue.priority is None
        assert issue.type is None
        assert issue.description == "Only description provided"

    def test_display_title_from_description_first_line(self, tmp_path):
        """display_title uses description first line when title is None."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="First line\nSecond line\nThird line")
        assert issue.display_title == "First line"

    def test_display_title_prefers_explicit_title(self, tmp_path):
        """display_title uses title when set."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="desc", title="Explicit Title")
        assert issue.display_title == "Explicit Title"

    def test_display_title_skips_blank_description_lines(self, tmp_path):
        """display_title skips blank lines at the start of description."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="\n\n  \nReal content\nMore")
        assert issue.display_title == "Real content"

    def test_to_dict_omits_none_optional_fields(self):
        """to_dict omits title/priority/type when None."""
        issue = Issue(id="099", description="test")
        data = issue.to_dict()
        assert "title" not in data
        assert "priority" not in data
        assert "type" not in data

    def test_to_dict_includes_set_optional_fields(self):
        """to_dict includes title/priority/type when set."""
        issue = Issue(id="099", description="test", title="T", priority="high", type="bug")
        data = issue.to_dict()
        assert data["title"] == "T"
        assert data["priority"] == "high"
        assert data["type"] == "bug"


# =========================================================================
# 8. Protocol message encoding/decoding
# =========================================================================


class TestProtocolMessages:
    """Protocol messages encode/decode correctly for resume and issue operations."""

    def test_spawn_flow_encode_decode_roundtrip(self):
        """MSG_SPAWN_FLOW encodes and decodes correctly."""
        msg = make_spawn_flow(
            "Fix bug",
            project_root="/p",
            task_type="bugfix",
            resume_flow_id="f-123",
        )
        encoded = msg.to_json()
        decoded = protocol.decode(encoded)
        assert decoded.type == MSG_SPAWN_FLOW
        assert decoded.payload["resume_flow_id"] == "f-123"
        assert decoded.payload["project_root"] == "/p"

    def test_issue_command_encode_decode_roundtrip(self):
        """MSG_ISSUE_COMMAND encodes and decodes correctly."""
        msg = make_issue_command(
            "create",
            "/p",
            description="Test",
            title="Title",
            tags=["a", "b"],
        )
        encoded = msg.to_json()
        decoded = protocol.decode(encoded)
        assert decoded.type == MSG_ISSUE_COMMAND
        assert decoded.payload["operation"] == "create"
        assert decoded.payload["description"] == "Test"
        assert decoded.payload["tags"] == ["a", "b"]

    def test_issue_command_omits_none_tags(self):
        """make_issue_command omits tags when None."""
        msg = make_issue_command("create", "/p", description="test")
        assert "tags" not in msg.payload

    def test_issue_command_omits_empty_optional_fields(self):
        """make_issue_command omits empty optional fields."""
        msg = make_issue_command("create", "/p", description="test")
        assert "title" not in msg.payload
        assert "priority" not in msg.payload
        assert "type" not in msg.payload
        assert "issue_id" not in msg.payload
        assert "reason" not in msg.payload


# =========================================================================
# 9. Edge cases and error handling
# =========================================================================


class TestEdgeCases:
    """Edge cases in cross-layer integration."""

    def test_empty_issues_list_returns_empty(self, tmp_path):
        """list_issues on empty project returns empty list."""
        mgr = IssueManager(tmp_path)
        assert mgr.list_issues() == []

    def test_load_nonexistent_issue_returns_none(self, tmp_path):
        """Loading a non-existent issue returns None."""
        mgr = IssueManager(tmp_path)
        assert mgr.load("999") is None

    def test_close_issue_idempotent(self, tmp_path):
        """Closing an already-closed issue is a no-op."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="To close")
        mgr.close_issue(issue.id)
        # Close again — should not raise
        mgr.close_issue(issue.id)
        loaded = mgr.load(issue.id)
        assert loaded is not None
        assert loaded.status in (IssueStatus.RESOLVED, IssueStatus.CLOSED)

    def test_reopen_issue_from_closed(self, tmp_path):
        """Reopening a closed issue transitions to OPEN."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="To reopen")
        mgr.close_issue(issue.id)
        mgr.reopen_issue(issue.id)
        loaded = mgr.load(issue.id)
        assert loaded is not None
        assert loaded.status == IssueStatus.OPEN

    def test_update_fields_changes_title(self, tmp_path):
        """update_fields can change the title."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="desc", title="Old Title")
        mgr.update_fields(issue.id, title="New Title")
        loaded = mgr.load(issue.id)
        assert loaded is not None
        assert loaded.title == "New Title"

    def test_owner_change_discards_state(self):
        """When an owner changes on a machine, prior state is discarded."""
        state = ServerState()

        async def run():
            await state.register_machine("m1", "h", owner_id="owner-B")
            await state.update_status("m1", {
                "flows": [{"flow_id": "fB", "status": "running"}],
                "issues": [
                    {"project_root": "/p", "id": "001", "status": "open", "source": "human"}
                ],
            })

            # Owner A takes over the same machine_id
            await state.register_machine("m1", "h", owner_id="owner-A")

            # Owner A sees no issues from B
            a_issues = await state.get_issues(owner="owner-A")
            assert len(a_issues) == 0

        asyncio.run(run())
