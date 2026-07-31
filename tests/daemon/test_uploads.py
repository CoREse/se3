"""Daemon-side upload channel: on-disk semantics and frame-level behaviour.

Two layers are covered here and nothing in between: :mod:`tianluo.daemon.uploads`
against a real filesystem (naming, dedup, containment, atomicity), and
``DaemonClient._handle_upload_command`` against a fake WebSocket (decode, size,
registry gate, ack shape). No real socket and no ``websockets`` import.
"""

import asyncio
import base64

import pytest

from tianluo.daemon import protocol, uploads
from tianluo.daemon.client import DaemonClient
from tianluo.daemon.uploads import UploadError, sanitize_upload_filename, store_upload


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _project(tmp_path, runtime_name="tianluo"):
    """Create a project root whose runtime dir is *runtime_name*."""
    root = tmp_path / "proj"
    (root / runtime_name).mkdir(parents=True)
    return root


def _stored_files(root, runtime_name="tianluo"):
    up = root / runtime_name / "uploads"
    if not up.is_dir():
        return []
    return sorted(p.name for p in up.iterdir())


# --------------------------------------------------------------------------
# uploads_dir / layout
# --------------------------------------------------------------------------


def test_uploads_dir_uses_canonical_runtime_dir(tmp_path):
    root = _project(tmp_path)
    assert uploads.uploads_dir(root) == root / "tianluo" / "uploads"


def test_uploads_dir_follows_legacy_se3_layout(tmp_path):
    root = _project(tmp_path, runtime_name="se3")
    assert uploads.uploads_dir(root) == root / "se3" / "uploads"


def test_store_upload_lands_under_legacy_se3_layout(tmp_path):
    root = _project(tmp_path, runtime_name="se3")

    stored = store_upload(root, "shot.png", b"legacy bytes")

    assert stored.path.startswith("se3/uploads/")
    assert (root / stored.path).read_bytes() == b"legacy bytes"


# --------------------------------------------------------------------------
# store_upload — naming, dedup, size
# --------------------------------------------------------------------------


def test_store_upload_returns_relative_posix_path_and_writes_content(tmp_path):
    root = _project(tmp_path)

    stored = store_upload(root, "diagram.png", b"\x89PNG fake")

    assert stored.path.startswith("tianluo/uploads/")
    assert "\\" not in stored.path
    assert not stored.deduplicated
    assert stored.size == len(b"\x89PNG fake")
    assert (root / stored.path).read_bytes() == b"\x89PNG fake"


def test_store_upload_name_is_hash_prefix_plus_original_name(tmp_path):
    root = _project(tmp_path)

    stored = store_upload(root, "notes.txt", b"hello")

    name = stored.path.rsplit("/", 1)[-1]
    prefix, _, rest = name.partition("_")
    assert len(prefix) == uploads.HASH_PREFIX_LEN
    assert all(c in "0123456789abcdef" for c in prefix)
    assert rest == "notes.txt"


def test_store_upload_same_content_twice_is_deduplicated(tmp_path):
    root = _project(tmp_path)

    first = store_upload(root, "a.bin", b"same bytes")
    second = store_upload(root, "a.bin", b"same bytes")

    assert first.path == second.path
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert len(_stored_files(root)) == 1


def test_store_upload_same_name_different_content_keeps_both(tmp_path):
    root = _project(tmp_path)

    first = store_upload(root, "shot.png", b"version one")
    second = store_upload(root, "shot.png", b"version two")

    assert first.path != second.path
    assert (root / first.path).read_bytes() == b"version one"
    assert (root / second.path).read_bytes() == b"version two"
    assert len(_stored_files(root)) == 2


def test_store_upload_rejects_oversized_content_without_writing(tmp_path):
    root = _project(tmp_path)
    oversized = b"x" * (protocol.MAX_UPLOAD_BYTES + 1)

    with pytest.raises(UploadError) as excinfo:
        store_upload(root, "big.bin", oversized)

    assert excinfo.value.code == protocol.UPLOAD_ERR_TOO_LARGE
    # Not merely "no file stored" — the uploads directory must not even have
    # been created, proving the limit is checked before any disk work.
    assert not (root / "tianluo" / "uploads").exists()


def test_store_upload_accepts_content_exactly_at_the_limit(tmp_path):
    root = _project(tmp_path)
    at_limit = b"y" * protocol.MAX_UPLOAD_BYTES

    stored = store_upload(root, "edge.bin", at_limit)

    assert stored.size == protocol.MAX_UPLOAD_BYTES


def test_store_upload_handles_empty_file(tmp_path):
    root = _project(tmp_path)

    stored = store_upload(root, "empty.txt", b"")

    assert stored.size == 0
    assert (root / stored.path).read_bytes() == b""


def test_store_upload_creates_uploads_dir_when_missing(tmp_path):
    root = _project(tmp_path)
    assert not (root / "tianluo" / "uploads").exists()

    store_upload(root, "first.txt", b"data")

    assert (root / "tianluo" / "uploads").is_dir()


def test_store_upload_leaves_no_partial_temp_file_behind(tmp_path):
    root = _project(tmp_path)

    store_upload(root, "atomic.bin", b"payload")

    names = _stored_files(root)
    assert len(names) == 1
    assert not any(n.endswith(".part") or n.startswith(".") for n in names)


# --------------------------------------------------------------------------
# sanitize_upload_filename — the security-critical transform
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..\\..\\windows\\system32\\cfg.sys",
        "C:\\Users\\me\\shot.png",
        "sub/dir/file.txt",
    ],
)
def test_sanitize_strips_every_directory_component(raw):
    safe = sanitize_upload_filename(raw)
    assert "/" not in safe
    assert "\\" not in safe
    assert safe not in {".", ".."}


def test_sanitize_replaces_nul_and_control_characters():
    safe = sanitize_upload_filename("bad\x00name\x07here\n.txt")
    assert "\x00" not in safe
    assert "\x07" not in safe
    assert "\n" not in safe
    assert safe.endswith(".txt")


def test_sanitize_replaces_whitespace():
    assert " " not in sanitize_upload_filename("my report final.pdf")


def test_sanitize_drops_leading_dots_and_dashes():
    assert not sanitize_upload_filename("...hidden.txt").startswith(".")
    assert not sanitize_upload_filename("--rf.sh").startswith("-")


@pytest.mark.parametrize("raw", ["", "..", ".", "...", "\x00\x00", "///"])
def test_sanitize_falls_back_for_names_that_vanish(raw):
    assert sanitize_upload_filename(raw) == uploads.FALLBACK_NAME


def test_sanitize_truncates_long_names_keeping_the_extension():
    safe = sanitize_upload_filename("a" * 300 + ".png")
    assert len(safe) <= uploads.MAX_NAME_LEN
    assert safe.endswith(".png")


def test_sanitize_truncates_extensionless_names():
    safe = sanitize_upload_filename("b" * 400)
    assert len(safe) == uploads.MAX_NAME_LEN


def test_sanitize_preserves_unicode_names():
    assert sanitize_upload_filename("截图.png") == "截图.png"


@pytest.mark.parametrize(
    "raw",
    [
        "../../../etc/passwd",
        "/absolute/evil.sh",
        "..\\..\\evil.exe",
        "\x00../escape",
        "....//..//escape.txt",
    ],
)
def test_store_upload_traversal_attempts_stay_inside_uploads_dir(tmp_path, raw):
    root = _project(tmp_path)
    uploads_root = (root / "tianluo" / "uploads").resolve()

    stored = store_upload(root, raw, b"payload")

    written = (root / stored.path).resolve()
    assert written.parent == uploads_root
    assert written.is_file()
    # And the relative path handed to the prompt is itself contained.
    assert stored.path.startswith("tianluo/uploads/")
    assert ".." not in stored.path.split("/")


def test_store_upload_refuses_a_name_escaping_containment(tmp_path, monkeypatch):
    """If sanitization ever regressed, the containment check must still hold."""
    root = _project(tmp_path)
    monkeypatch.setattr(uploads, "sanitize_upload_filename", lambda name: "../escaped")

    with pytest.raises(UploadError) as excinfo:
        store_upload(root, "innocent.txt", b"payload")

    assert excinfo.value.code == protocol.UPLOAD_ERR_INVALID_FILENAME
    assert list(root.rglob("escaped*")) == []


# --------------------------------------------------------------------------
# _handle_upload_command — frame-level behaviour
# --------------------------------------------------------------------------


class _FakeWS:
    """Minimal WebSocket stand-in capturing what the client sends."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


def _make_client(root=None, **kw):
    roots = [str(root)] if root is not None else []
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="12.0.3",
        snapshot_provider=kw.pop(
            "snapshot_provider",
            lambda: {"machine_id": "m1", "project_roots": roots},
        ),
        **kw,
    )


def _run_upload(client, ws, **kw):
    """Dispatch one UPLOAD_COMMAND through the client's real dispatch path."""

    async def scenario():
        await client._dispatch(ws, protocol.make_upload_command(**kw))

    asyncio.run(scenario())


def _acks(ws):
    return [m for m in ws.sent if m.type == protocol.MSG_UPLOAD_RESULT]


def _upload_kwargs(root, data=b"payload", filename="note.txt", request_id="req-1"):
    return {
        "project_root": str(root),
        "filename": filename,
        "content_b64": base64.b64encode(data).decode("ascii"),
        "size": len(data),
        "request_id": request_id,
    }


def test_upload_command_stores_file_and_acks_with_path(tmp_path):
    root = _project(tmp_path)
    client = _make_client(root, upload_handler=store_upload)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(root, b"hello daemon"))

    acks = _acks(ws)
    assert len(acks) == 1
    ack = acks[0].payload
    assert ack["request_id"] == "req-1"
    assert ack["ok"] is True
    assert ack["path"].startswith("tianluo/uploads/")
    assert ack["size"] == len(b"hello daemon")
    assert ack["deduplicated"] is False
    assert (root / ack["path"]).read_bytes() == b"hello daemon"


def test_upload_command_reports_deduplication_on_the_second_attempt(tmp_path):
    root = _project(tmp_path)
    client = _make_client(root, upload_handler=store_upload)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(root, b"same", request_id="req-a"))
    _run_upload(client, ws, **_upload_kwargs(root, b"same", request_id="req-b"))

    acks = _acks(ws)
    assert [a.payload["deduplicated"] for a in acks] == [False, True]
    assert acks[0].payload["path"] == acks[1].payload["path"]


def test_upload_command_rejects_undecodable_base64(tmp_path):
    root = _project(tmp_path)
    calls = []
    client = _make_client(
        root, upload_handler=lambda *a: calls.append(a)
    )
    ws = _FakeWS()

    async def scenario():
        # Hand-built: make_upload_command does not validate the encoding, but
        # only a broken/foreign peer would put this on the wire.
        msg = protocol.Message(
            type=protocol.MSG_UPLOAD_COMMAND,
            payload={
                "project_root": str(root),
                "filename": "x.bin",
                "content_b64": "not!base64!",
                "size": 3,
                "request_id": "req-bad",
            },
        )
        await client._dispatch(ws, msg)

    asyncio.run(scenario())

    assert calls == []
    ack = _acks(ws)[0].payload
    assert ack["ok"] is False
    assert ack["error_code"] == protocol.UPLOAD_ERR_INVALID_PAYLOAD


def test_upload_command_rejects_oversized_decoded_content(tmp_path):
    """A truthful declared size cannot be trusted; the decoded length rules."""
    root = _project(tmp_path)
    calls = []
    client = _make_client(root, upload_handler=lambda *a: calls.append(a))
    ws = _FakeWS()
    oversized = b"z" * (protocol.MAX_UPLOAD_BYTES + 1)

    async def scenario():
        msg = protocol.Message(
            type=protocol.MSG_UPLOAD_COMMAND,
            payload={
                "project_root": str(root),
                "filename": "big.bin",
                "content_b64": base64.b64encode(oversized).decode("ascii"),
                # Under-declared on purpose: make_upload_command would refuse
                # the honest value, so this is the shape a hostile server sends.
                "size": 10,
                "request_id": "req-big",
            },
        )
        await client._dispatch(ws, msg)

    asyncio.run(scenario())

    assert calls == []
    ack = _acks(ws)[0].payload
    assert ack["ok"] is False
    assert ack["error_code"] == protocol.UPLOAD_ERR_TOO_LARGE


def test_upload_command_rejects_relative_project_root(tmp_path):
    root = _project(tmp_path)
    calls = []
    client = _make_client(root, upload_handler=lambda *a: calls.append(a))
    ws = _FakeWS()

    async def scenario():
        msg = protocol.Message(
            type=protocol.MSG_UPLOAD_COMMAND,
            payload={
                "project_root": "relative/path",
                "filename": "x.txt",
                "content_b64": base64.b64encode(b"x").decode("ascii"),
                "size": 1,
                "request_id": "req-rel",
            },
        )
        await client._dispatch(ws, msg)

    asyncio.run(scenario())

    assert calls == []
    ack = _acks(ws)[0].payload
    assert ack["error_code"] == protocol.UPLOAD_ERR_INVALID_PATH


def test_upload_command_rejects_unregistered_project_root(tmp_path):
    """The security gate: an unknown root must not receive a single byte."""
    registered = _project(tmp_path)
    outsider = tmp_path / "outsider"
    (outsider / "tianluo").mkdir(parents=True)
    client = _make_client(registered, upload_handler=store_upload)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(outsider, b"evil"))

    ack = _acks(ws)[0].payload
    assert ack["ok"] is False
    assert ack["error_code"] == protocol.UPLOAD_ERR_NOT_REGISTERED
    assert not (outsider / "tianluo" / "uploads").exists()


def test_upload_command_without_handler_replies_unsupported(tmp_path):
    """No wired handler must fail fast, not leave the REST caller to time out."""
    root = _project(tmp_path)
    client = _make_client(root)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(root))

    ack = _acks(ws)[0].payload
    assert ack["ok"] is False
    assert ack["error_code"] == protocol.UPLOAD_ERR_UNSUPPORTED


def test_upload_command_rejects_empty_filename(tmp_path):
    root = _project(tmp_path)
    calls = []
    client = _make_client(root, upload_handler=lambda *a: calls.append(a))
    ws = _FakeWS()

    async def scenario():
        msg = protocol.Message(
            type=protocol.MSG_UPLOAD_COMMAND,
            payload={
                "project_root": str(root),
                "filename": "   ",
                "content_b64": base64.b64encode(b"x").decode("ascii"),
                "size": 1,
                "request_id": "req-noname",
            },
        )
        await client._dispatch(ws, msg)

    asyncio.run(scenario())

    assert calls == []
    ack = _acks(ws)[0].payload
    assert ack["error_code"] == protocol.UPLOAD_ERR_INVALID_FILENAME


def test_upload_command_relays_upload_error_code(tmp_path):
    root = _project(tmp_path)

    def _failing(project_root, filename, data):
        raise UploadError(protocol.UPLOAD_ERR_WRITE_FAILED, "disk full")

    client = _make_client(root, upload_handler=_failing)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(root))

    ack = _acks(ws)[0].payload
    assert ack["ok"] is False
    assert ack["error_code"] == protocol.UPLOAD_ERR_WRITE_FAILED
    assert "disk full" in ack["error"]


def test_upload_command_maps_unknown_failure_to_write_failed(tmp_path):
    """An unexpected fault must not carry a code make_upload_result rejects."""
    root = _project(tmp_path)

    def _boom(project_root, filename, data):
        raise RuntimeError("something odd")

    client = _make_client(root, upload_handler=_boom)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(root))

    ack = _acks(ws)[0].payload
    assert ack["ok"] is False
    assert ack["error_code"] == protocol.UPLOAD_ERR_WRITE_FAILED


def test_upload_command_without_request_id_sends_nothing(tmp_path):
    root = _project(tmp_path)
    client = _make_client(root, upload_handler=store_upload)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(root, request_id=""))

    assert ws.sent == []  # no request_id -> nothing to ack
    # The file still landed: the missing correlation id only removes the reply.
    assert len(_stored_files(root)) == 1


def test_upload_command_does_not_trigger_fast_push(tmp_path):
    """An upload changes no snapshot state, so it must not force a push."""
    root = _project(tmp_path)
    client = _make_client(root, upload_handler=store_upload)
    ws = _FakeWS()
    pushes = []
    client._trigger_fast_push = lambda: pushes.append(1)

    _run_upload(client, ws, **_upload_kwargs(root))

    assert _acks(ws)[0].payload["ok"] is True
    assert pushes == []


def test_upload_command_runs_the_handler_off_the_event_loop(tmp_path):
    """Blocking disk I/O must not run on the loop serving status pushes."""
    root = _project(tmp_path)
    loop_threads = []

    def _recording(project_root, filename, data):
        import threading

        loop_threads.append(threading.current_thread().name)
        return store_upload(project_root, filename, data)

    client = _make_client(root, upload_handler=_recording)
    ws = _FakeWS()

    async def scenario():
        import threading

        main_thread = threading.current_thread().name
        await client._dispatch(
            ws, protocol.make_upload_command(**_upload_kwargs(root))
        )
        return main_thread

    main_thread = asyncio.run(scenario())

    assert loop_threads and loop_threads[0] != main_thread


def test_daemon_wires_an_upload_handler_into_its_client(tmp_path):
    """The seam must actually be connected, not merely available."""
    from tianluo.daemon.daemon import Daemon, DaemonConfig

    root = _project(tmp_path)
    daemon = Daemon(
        DaemonConfig(
            pid_dir=tmp_path / "rt",
            server_url="ws://server/ws",
            project_roots=[str(root)],
        )
    )

    async def scenario():
        daemon._stop_event = asyncio.Event()
        task = daemon._start_server_client()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert daemon._client is not None
    assert daemon._client._upload_handler is not None
    stored = daemon._client._upload_handler(str(root), "wired.txt", b"bytes")
    assert (root / stored.path).read_bytes() == b"bytes"


def test_upload_command_uses_cached_roots_without_resnapshotting(tmp_path):
    """A paste is latency-sensitive: the hot path must not walk the snapshot."""
    root = _project(tmp_path)
    calls = []

    def _counting_snapshot():
        calls.append(1)
        return {"machine_id": "m1", "project_roots": [str(root)]}

    client = _make_client(upload_handler=store_upload, snapshot_provider=_counting_snapshot)
    ws = _FakeWS()

    _run_upload(client, ws, **_upload_kwargs(root, request_id="req-1"))
    _run_upload(client, ws, **_upload_kwargs(root, b"other", request_id="req-2"))

    assert len(calls) == 1
    assert all(a.payload["ok"] is True for a in _acks(ws))
