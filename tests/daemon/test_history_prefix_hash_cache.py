"""Prefix-verification tests: linear rewrite detection over a draining backlog.

The defect these lock down is the *second* quadratic term in the history read
path. Before every incremental read, ``read_flow`` must prove the bytes it
already consumed are still on disk unchanged — otherwise a step retried in place
would be delivered from a stale offset and the replacement's leading records
would silently vanish (issue #209). That proof used to re-hash the WHOLE consumed
prefix from disk on every round. A capped drain advances by one byte budget per
round, so draining a 253 MB step file ran ~1000 rounds and re-read ~125 GB just
to verify (issue #287) — linearizing the record reads alone would not have
helped.

:class:`~tianluo.daemon.history._PrefixVerifier` keeps the hash of the
already-verified prefix, so a round only re-reads the span the PREVIOUS round
consumed, and re-hashes from byte 0 only when the offset has doubled. These tests
assert both halves of that bargain:

* the **cost** — a ~500-round drain's verification I/O stays within a small
  multiple of the file size, and the full re-hashes really do land on a doubling
  schedule (so their total is bounded by ~2n rather than one-per-round);
* the **detection strength** — the three #209 rewrite shapes still set
  ``rewritten`` and force a re-read from line 0, an in-place mutation of an
  already-verified region is caught no later than the next doubling point (the
  exactly bounded weakening the verifier's INVARIANT: note records), and the
  cache is dropped on every event that makes an offset untrustworthy: a file
  identity change, a shortened file, a detected rewrite, a copy switch.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os

import pytest

from tianluo.daemon import history as history_mod
from tianluo.daemon.history import DaemonHistoryReader, _PrefixVerifier


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: [str(r) for r in roots])


def _flow_dir(root, flow_id):
    d = root / "tianluo" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _line(tag, payload=0):
    """One jsonl record line (bytes) whose length is stable for a given payload."""
    return (
        json.dumps(
            {
                "role": "assistant",
                "content": f"{tag}" + "x" * payload,
                "raw_json": [],
                "step_type": "implement",
            }
        )
        + "\n"
    ).encode()


def _keys(records):
    return [(r["step_id"], r["ordinal"]) for r in records]


@pytest.fixture()
def hash_io(monkeypatch):
    """Record every prefix-verification disk read.

    Both seams are billed: ``_hash_span`` (the span reads whose cost tracks the
    prefix size) and ``_window_signatures`` (the constant-cost head/boundary
    windows a no-fresh-span round re-checks). ``bytes`` is the span total —
    what the quadratic assertions are about — while ``total_bytes`` adds the
    windows, so no verification read escapes the cost budget.
    """
    calls = []
    window_calls = []
    real = history_mod._hash_span
    real_windows = history_mod._window_signatures

    def spying(path, start, end, hasher):
        calls.append((str(path), start, end))
        return real(path, start, end, hasher)

    def spying_windows(path, offset):
        window_calls.append((str(path), offset))
        return real_windows(path, offset)

    monkeypatch.setattr(history_mod, "_hash_span", spying)
    monkeypatch.setattr(history_mod, "_window_signatures", spying_windows)

    class _Spy:
        @property
        def calls(self):
            return list(calls)

        @property
        def window_calls(self):
            return list(window_calls)

        @property
        def bytes(self):
            return sum(end - start for _p, start, end in calls)

        @property
        def window_bytes(self):
            return sum(
                2 * min(offset, history_mod.HEAD_SIGNATURE_BYTES)
                for _p, offset in window_calls
            )

        @property
        def total_bytes(self):
            return self.bytes + self.window_bytes

        @property
        def full_offsets(self):
            """Offsets at which a from-byte-0 re-hash ran."""
            return [end for _p, start, end in calls if start == 0]

        def full_offsets_for(self, path):
            return [
                end for p, start, end in calls if start == 0 and p == str(path)
            ]

        def bytes_for(self, path):
            return sum(end - start for p, start, end in calls if p == str(path))

        def reset(self):
            calls.clear()
            window_calls.clear()

    return _Spy()


def _read_all(reader, flow_id, cursor=None, project_root=None):
    """One ``read_flow`` round, returning the frame."""
    return reader.read_flow(flow_id, cursor=cursor, project_root=project_root)


# --------------------------------------------------------------------------
# cost: the drain stays linear and the full re-hashes are geometric
# --------------------------------------------------------------------------


def _write_step_file(flow_dir, name, records, payload=4096):
    jsonl = flow_dir / name
    with jsonl.open("wb") as fh:
        for i in range(records):
            fh.write(_line(f"{name[:2]}{i:06d}", payload=payload))
    return jsonl


def _drain(reader, flow_id, limit=4000):
    """Run ``read_flow`` until a frame is no longer truncated."""
    cursor = None
    rounds = 0
    records = []
    while True:
        frame = reader.read_flow(flow_id, cursor=cursor)
        records.extend(frame.records)
        cursor = frame.cursor
        rounds += 1
        if not frame.truncated:
            return rounds, records
        assert rounds < limit, "drain did not terminate"


def test_long_drain_verification_io_stays_linear(tmp_path, monkeypatch, hash_io):
    """A many-hundred-round multi-file drain verifies with ~3n, not ~n·rounds/2.

    The byte budget is shrunk so a small fixture still produces the many-hundred
    -round drain shape a multi-hundred-MB step file has in production — that
    round count is exactly what turned the per-round whole-prefix re-hash into a
    quadratic term. With the verifier, each round re-reads only the span the
    previous round consumed (~n in total) and the periodic from-scratch re-hashes
    follow a doubling schedule (~2n in total).

    The flow deliberately holds TWO step files. ``read_flow`` runs the rewrite
    check for EVERY step file of the flow before it decides that file has no new
    bytes, so once ``01_discovery`` has drained it spends the entire remaining
    drain of ``06_implement`` in the no-fresh-span state. A fixture with a single
    file cannot observe that term at all — and it is the DOMINANT one: on the
    real 50-file flow of issue #287 the already-drained files turned a 490-round
    drain into 215 GB of verification reads (255x the flow's own size).
    """
    monkeypatch.setattr(history_mod, "MAX_BYTES_PER_REPORT", 4096)

    flow_dir = _flow_dir(tmp_path, "big")
    first = _write_step_file(flow_dir, "01_discovery.jsonl", 250)
    second = _write_step_file(flow_dir, "06_implement.jsonl", 250)
    size = first.stat().st_size + second.stat().st_size

    reader = _make_reader(tmp_path)
    hash_io.reset()

    rounds, records = _drain(reader, "big")

    assert rounds > 400, "fixture must reproduce the many-round drain shape"
    assert len(records) == 500

    # What the per-round whole-prefix re-hash would have cost: every file's
    # consumed offset, every round — i.e. ~size·rounds/2 overall.
    quadratic = size * rounds / 2
    assert hash_io.total_bytes <= 3.5 * size, (
        f"{rounds}-round drain verified with {hash_io.total_bytes} bytes "
        f"({hash_io.bytes} span + {hash_io.window_bytes} window) over "
        f"{size} bytes of history (a per-round whole-prefix re-hash would "
        f"spend ~{int(quadratic)})"
    )
    assert hash_io.total_bytes < quadratic / 10

    # The already-drained file is the one the defect billed for: it must not
    # cost more than its own re-verification schedule allows, however many
    # rounds it spends idle afterwards.
    assert hash_io.bytes_for(first) <= 3.5 * first.stat().st_size


def test_idle_file_is_not_rehashed_once_more_files_are_draining(
    tmp_path, monkeypatch, hash_io
):
    """A drained step file costs O(1) per round, not a whole re-hash per round.

    This pins the exact shape of issue #287's remaining quadratic: the rewrite
    check runs for every step file before the "no new bytes" short-circuit, so a
    file that finished draining early is re-verified on every one of the hundreds
    of rounds the NEXT file still needs.
    """
    monkeypatch.setattr(history_mod, "MAX_BYTES_PER_REPORT", 4096)

    flow_dir = _flow_dir(tmp_path, "big")
    first = _write_step_file(flow_dir, "01_discovery.jsonl", 120)
    second = _write_step_file(flow_dir, "06_implement.jsonl", 300)

    reader = _make_reader(tmp_path)

    # Drain the first file, then measure only what the rest of the drain spends.
    cursor = None
    while True:
        frame = reader.read_flow("big", cursor=cursor)
        cursor = frame.cursor
        if cursor.get("06_implement.jsonl", 0) > 0:
            break
    hash_io.reset()

    rounds = 0
    while frame.truncated:
        frame = reader.read_flow("big", cursor=cursor)
        cursor = frame.cursor
        rounds += 1
        assert rounds < 4000, "drain did not terminate"

    assert rounds > 100, "fixture must keep the drained file idle for many rounds"
    idle_cost = hash_io.bytes_for(first)
    assert idle_cost == 0, (
        f"the drained file was re-hashed for {idle_cost} bytes over {rounds} "
        f"idle rounds ({first.stat().st_size} bytes on disk) — the whole-prefix "
        "re-hash on a no-fresh-span round is back"
    )
    # It is still checked every round, just at a bounded constant cost.
    per_file_windows = [p for p, _o in hash_io.window_calls if p == str(first)]
    assert len(per_file_windows) >= rounds - 1


def test_full_reverifications_follow_a_doubling_schedule(tmp_path, monkeypatch, hash_io):
    """From-byte-0 re-hashes happen, and each one is at ≥2x the previous offset.

    This is what bounds BOTH sides of the trade: the whole prefix is still
    re-verified periodically (so an in-place change to an old region cannot hide
    forever), while the geometric spacing keeps their total cost at ~2n instead
    of one whole prefix per round. Asserted per file over a two-file flow, so an
    already-drained file's idle rounds are inside the measured window too.
    """
    monkeypatch.setattr(history_mod, "MAX_BYTES_PER_REPORT", 4096)

    flow_dir = _flow_dir(tmp_path, "big")
    first = _write_step_file(flow_dir, "01_discovery.jsonl", 150)
    second = _write_step_file(flow_dir, "06_implement.jsonl", 150)

    reader = _make_reader(tmp_path)
    hash_io.reset()

    _drain(reader, "big")

    for jsonl in (first, second):
        fulls = hash_io.full_offsets_for(jsonl)
        assert len(fulls) >= 3, (
            f"{jsonl.name}: the whole prefix must still be re-verified regularly"
        )
        for previous, current in zip(fulls, fulls[1:]):
            assert current >= 2 * previous, (
                f"{jsonl.name}: full re-verification at offset {current} followed "
                f"one at {previous} — the doubling schedule that bounds their "
                "total cost is broken"
            )
        # Geometric spacing means logarithmically many of them, not one per round.
        assert len(fulls) < 30


# --------------------------------------------------------------------------
# detection strength: the three #209 rewrite shapes
# --------------------------------------------------------------------------


def _warm_cached_verifier(reader, jsonl, flow_id="f1"):
    """Read *jsonl* over several rounds so a WARM verifier cache exists.

    The rewrite tests must face the verifier in its cached state — a rewrite
    detected only because the cache happened to be cold would prove nothing.
    """
    with jsonl.open("wb") as fh:
        for i in range(6):
            fh.write(_line(f"a{i}", payload=200))
    frame = _read_all(reader, flow_id)
    for round_no in range(3):
        with jsonl.open("ab") as fh:
            fh.write(_line(f"b{round_no}", payload=200))
        frame = _read_all(reader, flow_id, cursor=frame.cursor)
    verifier = reader._prefix_verifiers[str(jsonl)]
    assert verifier._verified_offset > 0, "fixture failed to warm the cache"
    return frame


def test_from_scratch_rewrite_is_detected_with_a_warm_cache(tmp_path):
    """A retry that re-runs the step from the start is caught on the next round."""
    jsonl = _flow_dir(tmp_path, "f1") / "01_discovery.jsonl"
    reader = _make_reader(tmp_path)
    frame = _warm_cached_verifier(reader, jsonl)

    # The retry rewrites every record and grows the file past the old offset —
    # neither the size nor the line count betrays it.
    with jsonl.open("wb") as fh:
        for i in range(12):
            fh.write(_line(f"retry{i}", payload=200))

    after = _read_all(reader, "f1", cursor=frame.cursor)
    assert after.cursor_base["01_discovery.jsonl"] == 0, "must re-read from line 0"
    assert _keys(after.records)[0] == ("01_discovery", 0)
    assert after.records[0]["message"]["content"].startswith("retry0")
    # The stale verifier went with the stale offset.
    assert str(jsonl) not in reader._prefix_verifiers
    assert str(jsonl) in reader._prefix_hashers


def test_equal_size_inplace_rewrite_is_detected_with_a_warm_cache(tmp_path):
    """The #209 shape: same size, same mtime tick, different content."""
    jsonl = _flow_dir(tmp_path, "f1") / "01_discovery.jsonl"
    reader = _make_reader(tmp_path)
    frame = _warm_cached_verifier(reader, jsonl)

    before = jsonl.stat()
    original = jsonl.read_bytes()
    # The retry shape: every record replaced, same total size, same mtime tick.
    replacement = b"".join(_line(f"z{i}", payload=200) for i in range(9))
    assert len(replacement) == len(original) and replacement != original
    jsonl.write_bytes(replacement)
    # Pin the stat back so neither size nor mtime can be the discriminator.
    os.utime(jsonl, ns=(before.st_mtime_ns, before.st_mtime_ns))
    assert jsonl.stat().st_size == before.st_size

    after = _read_all(reader, "f1", cursor=frame.cursor)
    assert after.cursor_base["01_discovery.jsonl"] == 0
    contents = [r["message"]["content"] for r in after.records]
    assert contents[0].startswith("z0") and contents[1].startswith("z1")
    assert str(jsonl) not in reader._prefix_verifiers


def test_middle_of_prefix_rewrite_in_the_fresh_span_is_detected_at_once(tmp_path):
    """A mutation inside the span the last round consumed is caught immediately.

    That span is precisely what a cached round re-reads from disk, so the shape a
    real retry produces — it re-runs from the start and overwrites its own most
    recent records — never benefits from the cache.
    """
    jsonl = _flow_dir(tmp_path, "f1") / "01_discovery.jsonl"
    reader = _make_reader(tmp_path)
    frame = _warm_cached_verifier(reader, jsonl)

    original = jsonl.read_bytes()
    # ``b2`` is the record the most recent round consumed; keep the head, the
    # boundary and the size identical so only a content hash can see it.
    replacement = original.replace(b"b2", b"Q2")
    assert len(replacement) == len(original) and replacement != original
    jsonl.write_bytes(replacement)
    with jsonl.open("ab") as fh:
        fh.write(_line("tail", payload=200))

    after = _read_all(reader, "f1", cursor=frame.cursor)
    assert after.cursor_base["01_discovery.jsonl"] == 0
    assert _keys(after.records)[0] == ("01_discovery", 0)


def test_middle_of_prefix_rewrite_in_an_old_region_is_caught_by_the_doubling(tmp_path):
    """An old-region mutation is detected no later than the next doubling point.

    This is the exact weakening the verifier documents: a region verified in an
    earlier round is not re-read every round, so a surgical in-place change to it
    (head, boundary and size all preserved, no writer here produces this shape)
    can survive a bounded number of rounds — but the from-scratch re-hash that
    fires once the offset doubles must still catch it, and the delivery that
    follows must start at line 0.
    """
    jsonl = _flow_dir(tmp_path, "f1") / "01_discovery.jsonl"
    reader = _make_reader(tmp_path)
    frame = _warm_cached_verifier(reader, jsonl)

    # Mutate the very first record — long since verified, so no round re-reads it
    # until a from-scratch verification is due.
    original = jsonl.read_bytes()
    jsonl.write_bytes(original.replace(b"a0", b"Q0", 1))
    offset_at_mutation = reader._read_offsets[str(jsonl)][1]

    detected_round = None
    for round_no in range(40):
        with jsonl.open("ab") as fh:
            fh.write(_line(f"c{round_no}", payload=200))
        frame = _read_all(reader, "f1", cursor=frame.cursor)
        if frame.cursor_base["01_discovery.jsonl"] == 0:
            detected_round = round_no
            break

    assert detected_round is not None, "the doubling re-verification never fired"
    # Bounded delay: caught by the time the consumed prefix has doubled.
    assert jsonl.stat().st_size <= 2 * offset_at_mutation + len(_line("c", 200)), (
        "detection was later than the doubling point the INVARIANT promises"
    )
    assert frame.records[0]["ordinal"] == 0
    assert frame.records[0]["message"]["content"].startswith("Q0")


def test_idle_file_is_verified_by_bounded_windows(tmp_path, hash_io):
    """Polling a settled file costs two bounded windows, and still catches a retry.

    An idle file is where the #209 equal-size, same-mtime-tick retry hides:
    neither its size nor its stat moves, and it never appends a fresh span for a
    cached round to re-read. Verification lags consumption by one round, so the
    first idle poll still re-reads the span the last consuming round took; from
    the second poll on there is no fresh span at all, and re-hashing the whole
    prefix there is what pinned the daemon (#287). Instead the head and boundary
    windows are re-read at a fixed cost — which is exactly what a retry moves,
    since it re-runs the step from record 0.
    """
    jsonl = _flow_dir(tmp_path, "f1") / "01_discovery.jsonl"
    reader = _make_reader(tmp_path)
    frame = _warm_cached_verifier(reader, jsonl)

    hash_io.reset()
    frame = _read_all(reader, "f1", cursor=frame.cursor)  # first idle poll
    assert hash_io.calls and not hash_io.full_offsets
    for _ in range(5):
        hash_io.reset()
        frame = _read_all(reader, "f1", cursor=frame.cursor)
        assert hash_io.bytes == 0, "a settled file must not be re-hashed"
        assert hash_io.window_calls, "…but it must still be checked"
        assert hash_io.window_bytes <= 4 * history_mod.HEAD_SIGNATURE_BYTES

    # An equal-size in-place retry — same size, same line count, rewritten from
    # the first record — is therefore still caught on the very next poll, with no
    # growth to trigger a doubling.
    original = jsonl.read_bytes()
    jsonl.write_bytes(original.replace(b"a0", b"Z0", 1))
    after = _read_all(reader, "f1", cursor=frame.cursor)
    assert after.cursor_base["01_discovery.jsonl"] == 0
    assert any(r["message"]["content"].startswith("Z0") for r in after.records)
    assert str(jsonl) not in reader._prefix_verifiers


# --------------------------------------------------------------------------
# cache lifetime
# --------------------------------------------------------------------------


def test_copy_switch_discards_the_verifier_for_the_new_key(tmp_path):
    """A copy switch drops any verifier state held for the file it switches to.

    The bare filename's cursor belongs to the OTHER physical copy, so this file
    is re-read from line 0 anyway; whatever a verifier held for it describes a
    stretch nobody watched in between and must not vouch for anything.
    """
    main = tmp_path / "main"
    (main / "tianluo").mkdir(parents=True)
    wt = main / "tianluo" / "worktrees" / "wt__b"
    wt.mkdir(parents=True)

    main_file = _flow_dir(main, "f1") / "01_discovery.jsonl"
    main_file.write_bytes(_line("m0") + _line("m1"))

    reader = _make_reader(main, wt)
    first = _read_all(reader, "f1", project_root=str(wt))
    assert _keys(first.records) == [("01_discovery", 0), ("01_discovery", 1)]

    wt_file = _flow_dir(wt, "f1") / "01_discovery.jsonl"
    wt_file.write_bytes(_line("m0") + _line("m1") + _line("w2"))

    # Stale state for the copy we are about to switch TO, as an earlier stretch
    # on that same path would have left behind.
    stale = _PrefixVerifier()
    reader._prefix_verifiers[str(wt_file)] = stale

    second = _read_all(reader, "f1", cursor=first.cursor, project_root=str(wt))
    assert _keys(second.records) == [
        ("01_discovery", 0),
        ("01_discovery", 1),
        ("01_discovery", 2),
    ]
    assert second.cursor_base["01_discovery.jsonl"] == 0
    assert reader._prefix_verifiers.get(str(wt_file)) is not stale


def test_verifier_and_prefix_hasher_share_one_lifetime(tmp_path):
    """Every path that installs a prefix hasher leaves the verifier consistent.

    The two are the consumed-side and disk-side halves of the same comparison;
    a path that kept one and dropped the other would either re-verify content the
    reader no longer believes in, or vouch for a prefix nobody hashed.
    """
    jsonl = _flow_dir(tmp_path, "f1") / "01_discovery.jsonl"
    reader = _make_reader(tmp_path)
    key = str(jsonl)

    # Full read: hasher installed, verifier deliberately absent (next round
    # re-hashes the new content from byte 0 and re-anchors the doubling).
    jsonl.write_bytes(_line("a0") + _line("a1"))
    frame = _read_all(reader, "f1")
    assert key in reader._prefix_hashers
    assert key not in reader._prefix_verifiers

    # Incremental read: both halves present and mutually consistent.
    with jsonl.open("ab") as fh:
        fh.write(_line("a2"))
    frame = _read_all(reader, "f1", cursor=frame.cursor)
    assert key in reader._prefix_hashers
    verifier = reader._prefix_verifiers[key]
    assert verifier._verified_offset == reader._read_offsets[key][1] - len(_line("a2"))

    # Detected rewrite: the verifier goes with the offset it vouched for.
    jsonl.write_bytes(_line("z0") + _line("z1") + _line("z2") + _line("z3"))
    _read_all(reader, "f1", cursor=frame.cursor)
    assert key in reader._prefix_hashers
    assert key not in reader._prefix_verifiers


# --------------------------------------------------------------------------
# _PrefixVerifier in isolation
# --------------------------------------------------------------------------


def _digest(data):
    h = hashlib.blake2b(digest_size=16)
    h.update(data)
    return h.digest()


def test_verifier_falls_back_to_a_full_hash_without_a_cache(tmp_path, hash_io):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"a" * 4096)

    verifier = _PrefixVerifier()
    assert verifier.verify(path, 4096, _digest(b"a" * 4096)) is True
    assert hash_io.full_offsets == [4096]

    # Warm now: appending and re-verifying only re-reads the appended span.
    path.write_bytes(b"a" * 4096 + b"b" * 512)
    hash_io.reset()
    assert verifier.verify(path, 4608, _digest(b"a" * 4096 + b"b" * 512)) is True
    assert hash_io.calls == [(str(path), 4096, 4608)]


def test_verifier_falls_back_when_the_file_identity_changes(tmp_path, hash_io):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"a" * 4096)
    verifier = _PrefixVerifier()
    assert verifier.verify(path, 4096, _digest(b"a" * 4096)) is True
    path.write_bytes(b"a" * 4096 + b"b" * 512)
    assert verifier.verify(path, 4608, _digest(b"a" * 4096 + b"b" * 512)) is True

    # Same bytes, new inode (the atomic-replace shape) — the cache is keyed by
    # path, so identity is what tells the two files apart.
    replacement = tmp_path / "s.new"
    replacement.write_bytes(b"a" * 4096 + b"b" * 512 + b"c" * 128)
    os.replace(replacement, path)
    hash_io.reset()

    payload = b"a" * 4096 + b"b" * 512 + b"c" * 128
    assert verifier.verify(path, len(payload), _digest(payload)) is True
    assert hash_io.full_offsets == [len(payload)], "identity change must re-hash"


def test_verifier_rejects_a_file_shorter_than_the_offset(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"a" * 4096)
    verifier = _PrefixVerifier()
    assert verifier.verify(path, 4096, _digest(b"a" * 4096)) is True

    path.write_bytes(b"a" * 100)
    assert verifier.verify(path, 4096, _digest(b"a" * 4096)) is False
    assert verifier._hasher is None, "a rejected verification must drop the cache"


def test_verifier_rejects_changed_and_unreadable_prefixes(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"a" * 4096)
    verifier = _PrefixVerifier()

    assert verifier.verify(path, 4096, _digest(b"b" * 4096)) is False
    assert verifier.verify(path, 4096, None) is False
    assert verifier.verify(tmp_path / "missing.jsonl", 10, _digest(b"x")) is False

    assert verifier.verify(path, 4096, _digest(b"a" * 4096)) is True
    path.write_bytes(b"a" * 4096 + b"b" * 512)

    real_open = builtins.open

    def failing_open(file, *args, **kwargs):
        if str(file) == str(path):
            raise OSError("unreadable")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing_open)
    assert verifier.verify(path, 4608, _digest(b"a" * 4096 + b"b" * 512)) is False
