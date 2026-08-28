"""Owner-scoped web-console message history: schema v2 + REST surface (G2).

The console's two prompt boxes (the docked reply textarea shared by
respond/interject, and the New Task description) recall text the operator has
actually **sent**. A sent message is a fact about the *owner*, not about the
browser it was typed in, so unlike the draft cache it is persisted server-side
and follows the owner across devices.

What is pinned down here:

* the table arrives through the existing migration mechanism — a fresh database
  lands on v2, and an **already-published v1 database upgrades in place** with
  its rows intact (the v1 script is never edited, so a deployed server that
  already recorded "I ran v1" still gets the new table);
* one owner can never read another's history — the owner is taken from the
  authenticated identity and is not a request parameter at all;
* the per-(owner, channel) cap is the CLI's 500, and overflow drops the
  *oldest* entries;
* blank text and an immediate repeat never enter the stack;
* every entry the client can ever see carries the row's stable, server-assigned
  id, and an append reports the row it *became* — the new one, or the existing
  one an adjacent repeat folded onto — because that id is the browser's only
  sound way to recognise its own send in a list it reads later;
* both routes are fail-closed, and an unknown channel is a 404 like any other
  unknown resource.

Parallel safety: every database lives under the test's own ``tmp_path`` (or is
``:memory:``), each check builds its own app/client, and nothing is shared
between tests.
"""

from __future__ import annotations

import sqlite3

import pytest

from _authsrv import authed_app, login
from tianluo.server import crypto
from tianluo.server.persistence import (
    MESSAGE_HISTORY_CHANNELS,
    MESSAGE_HISTORY_MAX_ENTRIES,
    SCHEMA_VERSION,
    _SCHEMA_V1,
    Store,
)


REPLY = "flow-reply"
NEW_TASK = "new-task"


def _texts(entries) -> list:
    """Just the text of a store read — for the checks that are about ordering."""
    return [e.text for e in entries]


# --------------------------------------------------------------------------- #
# schema / migration                                                           #
# --------------------------------------------------------------------------- #


def _user_version(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_message_history_channels_and_cap_match_the_cli():
    """The two channels are fixed, and the cap is the CLI's MAX_ENTRIES."""
    from tianluo.engine.prompt_history import MAX_ENTRIES as CLI_MAX_ENTRIES

    assert MESSAGE_HISTORY_CHANNELS == (REPLY, NEW_TASK)
    assert MESSAGE_HISTORY_MAX_ENTRIES == CLI_MAX_ENTRIES == 500


def test_fresh_database_is_created_at_v2_with_the_history_table(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    store = Store(str(db))
    try:
        assert SCHEMA_VERSION == 2
        assert _user_version(db) == 2
        owner = store.create_owner("a")
        landed = store.append_message_history(owner, REPLY, "hello")
        assert landed.appended is True and landed.entry_id is not None
        entries = store.list_message_history(owner, REPLY)
        assert _texts(entries) == ["hello"]
        assert entries[0].entry_id == landed.entry_id
    finally:
        store.close()


def test_existing_v1_database_upgrades_in_place_and_keeps_its_data(tmp_path):
    """A deployed v1 DB gains the new table without losing anything.

    The v1 script is never edited in place (a deployed database has already
    recorded that it ran v1 and would never re-run it), so the only way the
    table can reach an existing install is the appended v2 step.
    """
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_SCHEMA_V1)
        conn.execute("PRAGMA user_version=1")
        conn.execute(
            "INSERT INTO owners (owner_id, display_name, is_admin, created_at) "
            "VALUES ('legacy-owner', 'old', 1, 1.0)"
        )
        conn.execute(
            "INSERT INTO identity_bindings (provider, external_id, owner_id, created_at) "
            "VALUES ('local', 'old', 'legacy-owner', 1.0)"
        )
        conn.commit()
    finally:
        conn.close()
    assert _user_version(db) == 1

    store = Store(str(db))
    try:
        assert _user_version(db) == 2
        # Pre-existing rows survive the upgrade...
        owner = store.get_owner("legacy-owner")
        assert owner is not None and owner.is_admin is True
        assert store.resolve_owner_by_identity("local", "old") == "legacy-owner"
        # ...and the new table is usable straight away.
        assert (
            store.append_message_history("legacy-owner", NEW_TASK, "post-upgrade").appended
            is True
        )
        assert _texts(store.list_message_history("legacy-owner", NEW_TASK)) == [
            "post-upgrade"
        ]
    finally:
        store.close()

    # Re-opening an already-migrated database is a no-op, not a re-run.
    again = Store(str(db))
    try:
        assert _user_version(db) == 2
        assert _texts(again.list_message_history("legacy-owner", NEW_TASK)) == [
            "post-upgrade"
        ]
    finally:
        again.close()


def test_deleting_an_owner_takes_their_history_with_it(tmp_path):
    """ON DELETE CASCADE — history never outlives the owner it belongs to."""
    store = Store(str(tmp_path / "cascade.sqlite3"))
    try:
        owner = store.create_owner("gone-soon")
        store.append_message_history(owner, REPLY, "some words")
        assert store.count_message_history(owner, REPLY) == 1
        assert store.delete_owner(owner) is True
        assert store.count_message_history(owner, REPLY) == 0
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Store semantics                                                              #
# --------------------------------------------------------------------------- #


def test_channels_are_separate_stacks(tmp_path):
    store = Store(str(tmp_path / "channels.sqlite3"))
    try:
        owner = store.create_owner("a")
        store.append_message_history(owner, REPLY, "answer to the flow")
        store.append_message_history(owner, NEW_TASK, "build the thing")
        assert _texts(store.list_message_history(owner, REPLY)) == ["answer to the flow"]
        assert _texts(store.list_message_history(owner, NEW_TASK)) == ["build the thing"]
    finally:
        store.close()


def test_blank_and_repeated_entries_never_enter_the_stack(tmp_path):
    store = Store(str(tmp_path / "dedup.sqlite3"))
    try:
        owner = store.create_owner("a")
        first = store.append_message_history(owner, REPLY, "first")
        assert first.appended is True
        # An immediate repeat would push the previous entries out of reach for
        # no gain — holding Enter on the same answer must not cost history.
        # It is folded, and the fold NAMES the row it landed on: that is how the
        # browser tells "my send became this existing entry" apart from "some
        # older entry happens to read the same".
        folded = store.append_message_history(owner, REPLY, "first")
        assert folded.appended is False
        assert folded.entry_id == first.entry_id
        for blank in ("", "   ", "\n\t "):
            blanked = store.append_message_history(owner, REPLY, blank)
            # Blank is not a message at all, so there is no row to point at.
            assert blanked.appended is False and blanked.entry_id is None
        second = store.append_message_history(owner, REPLY, "second")
        assert second.appended is True and second.entry_id != first.entry_id
        # A NON-adjacent repeat is a real entry — only the immediate one is
        # suppressed, exactly like a shell history — and it gets its OWN id, so
        # equal text never collapses into one append.
        again = store.append_message_history(owner, REPLY, "first")
        assert again.appended is True
        assert again.entry_id not in (first.entry_id, second.entry_id)
        entries = store.list_message_history(owner, REPLY)
        assert _texts(entries) == ["first", "second", "first"]
        assert [e.entry_id for e in entries] == [
            first.entry_id,
            second.entry_id,
            again.entry_id,
        ]
    finally:
        store.close()


def test_overflow_drops_the_oldest_entries(tmp_path):
    store = Store(str(tmp_path / "cap.sqlite3"))
    try:
        owner = store.create_owner("a")
        overflow = 7
        for i in range(MESSAGE_HISTORY_MAX_ENTRIES + overflow):
            assert store.append_message_history(owner, REPLY, f"m{i}").appended is True
        assert store.count_message_history(owner, REPLY) == MESSAGE_HISTORY_MAX_ENTRIES
        entries = store.list_message_history(owner, REPLY)
        assert len(entries) == MESSAGE_HISTORY_MAX_ENTRIES
        # Oldest-first ordering, with the first `overflow` messages evicted.
        assert _texts(entries)[0] == f"m{overflow}"
        assert _texts(entries)[-1] == f"m{MESSAGE_HISTORY_MAX_ENTRIES + overflow - 1}"
        # Ids are stable and strictly increasing: truncation renumbers nothing,
        # so an id the browser learned earlier still names the same entry.
        ids = [e.entry_id for e in entries]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        # The cap is per (owner, channel): the other channel is untouched.
        store.append_message_history(owner, NEW_TASK, "only one here")
        assert _texts(store.list_message_history(owner, NEW_TASK)) == ["only one here"]
        assert store.count_message_history(owner, REPLY) == MESSAGE_HISTORY_MAX_ENTRIES
    finally:
        store.close()


def test_store_level_owner_isolation(tmp_path):
    store = Store(str(tmp_path / "owners.sqlite3"))
    try:
        a = store.create_owner("a")
        b = store.create_owner("b")
        store.append_message_history(a, REPLY, "owner A secret")
        store.append_message_history(b, REPLY, "owner B secret")
        assert _texts(store.list_message_history(a, REPLY)) == ["owner A secret"]
        assert _texts(store.list_message_history(b, REPLY)) == ["owner B secret"]
        # A's cap is A's own: B filling their channel evicts nothing of A's.
        for i in range(MESSAGE_HISTORY_MAX_ENTRIES + 3):
            store.append_message_history(b, REPLY, f"b{i}")
        assert _texts(store.list_message_history(a, REPLY)) == ["owner A secret"]
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# REST surface                                                                 #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client_and_app(tmp_path):
    from fastapi.testclient import TestClient

    app, _key = authed_app(db_path=str(tmp_path / "server.sqlite3"))
    with TestClient(app) as client:
        login(client)
        yield client, app


def test_history_routes_are_fail_closed(tmp_path):
    """No session, no history — both verbs, like every other /api route."""
    from fastapi.testclient import TestClient

    app, _key = authed_app(db_path=str(tmp_path / "closed.sqlite3"))
    with TestClient(app) as client:
        assert client.get(f"/api/message-history/{REPLY}").status_code == 401
        assert (
            client.post(f"/api/message-history/{REPLY}", json={"text": "x"}).status_code
            == 401
        )


def test_get_and_post_round_trip(client_and_app):
    client, _app = client_and_app
    empty = client.get(f"/api/message-history/{REPLY}")
    assert empty.status_code == 200
    assert empty.json() == {"channel": REPLY, "entries": [], "count": 0}

    first = client.post(f"/api/message-history/{REPLY}", json={"text": "the answer"})
    assert first.status_code == 200
    assert first.json()["status"] == "appended"
    assert first.json()["appended"] is True
    first_id = first.json()["entry_id"]
    assert first_id is not None
    # A repeat and a blank are dropped rather than rejected — the browser fires
    # this after a successful send and has nothing to do with an error.
    repeat = client.post(f"/api/message-history/{REPLY}", json={"text": "the answer"})
    assert repeat.json()["status"] == "skipped"
    assert repeat.json()["appended"] is False
    # ...and the fold names the row it landed on, which is the whole point: the
    # browser learns that its second send IS entry `first_id`, not that some
    # unrelated entry happens to carry the same words.
    assert repeat.json()["entry_id"] == first_id
    blank = client.post(f"/api/message-history/{REPLY}", json={"text": "   "})
    assert blank.json()["status"] == "skipped"
    # Blank is not a message, so no row is named.
    assert blank.json()["entry_id"] is None
    follow = client.post(f"/api/message-history/{REPLY}", json={"text": "a follow-up"})
    follow_id = follow.json()["entry_id"]
    assert follow.json()["appended"] is True and follow_id != first_id

    body = client.get(f"/api/message-history/{REPLY}").json()
    assert [e["text"] for e in body["entries"]] == [
        "the answer",
        "a follow-up",
    ], "oldest first, newest last"
    # Every entry a client can see carries its id, and the ids are the ones the
    # appends reported.
    assert [e["id"] for e in body["entries"]] == [first_id, follow_id]
    assert body["count"] == 2
    # The New Task channel is a separate stack over the same session.
    assert client.get(f"/api/message-history/{NEW_TASK}").json()["entries"] == []


def test_a_non_adjacent_repeat_is_a_second_append_with_its_own_id(client_and_app):
    """Equal text is not the same append — only the adjacent one folds.

    This is the pair the browser must be able to tell apart: "A" then "A"
    collapses onto one id, while "A", "B", "A" is genuinely two entries with
    two ids. Without distinct ids on the wire the client cannot distinguish a
    fold from an older coincidence and has to guess, which is exactly how a
    delivered message gets swallowed.
    """
    client, _app = client_and_app
    a1 = client.post(f"/api/message-history/{NEW_TASK}", json={"text": "A"}).json()
    folded = client.post(f"/api/message-history/{NEW_TASK}", json={"text": "A"}).json()
    b = client.post(f"/api/message-history/{NEW_TASK}", json={"text": "B"}).json()
    a2 = client.post(f"/api/message-history/{NEW_TASK}", json={"text": "A"}).json()

    assert a1["appended"] is True
    assert folded == {**folded, "appended": False, "entry_id": a1["entry_id"]}
    assert a2["appended"] is True and a2["entry_id"] != a1["entry_id"]

    body = client.get(f"/api/message-history/{NEW_TASK}").json()
    assert [e["text"] for e in body["entries"]] == ["A", "B", "A"]
    assert [e["id"] for e in body["entries"]] == [
        a1["entry_id"],
        b["entry_id"],
        a2["entry_id"],
    ]


def test_unknown_channel_is_a_404_on_both_verbs(client_and_app):
    client, _app = client_and_app
    for resp in (
        client.get("/api/message-history/issue-title"),
        client.post("/api/message-history/issue-title", json={"text": "x"}),
        client.get("/api/message-history/"),
    ):
        assert resp.status_code == 404, resp.text


def test_one_owner_never_reads_anothers_history(tmp_path):
    """The owner comes from the session; there is no shape that names another."""
    from fastapi.testclient import TestClient

    app, _key = authed_app(db_path=str(tmp_path / "two-owners.sqlite3"))
    store = app.state.store
    store.create_local_user("local", "bob", crypto.hash_password("pw2"), display_name="bob")

    with TestClient(app) as admin_client, TestClient(app) as bob_client:
        login(admin_client)
        login(bob_client, "bob", "pw2")

        admin_client.post(f"/api/message-history/{REPLY}", json={"text": "admin words"})
        bob_client.post(f"/api/message-history/{REPLY}", json={"text": "bob words"})
        admin_client.post(f"/api/message-history/{NEW_TASK}", json={"text": "admin task"})

        admin_reply = admin_client.get(f"/api/message-history/{REPLY}").json()
        bob_reply = bob_client.get(f"/api/message-history/{REPLY}").json()
        assert [e["text"] for e in admin_reply["entries"]] == ["admin words"]
        assert [e["text"] for e in bob_reply["entries"]] == ["bob words"]
        # Being an admin buys nothing here: history is personal, like a daemon
        # key, so there is no cross-owner read even for the operator role.
        assert bob_client.get(f"/api/message-history/{NEW_TASK}").json()["entries"] == []


def test_api_read_is_capped_at_the_max(tmp_path):
    """A GET returns at most the newest MAX_ENTRIES, oldest first."""
    from fastapi.testclient import TestClient

    app, _key = authed_app(db_path=str(tmp_path / "capped.sqlite3"))
    store = app.state.store
    owner_id = app.state.test_owner_id
    # Seeded through the store rather than 500+ HTTP round trips — the route
    # under test here is the READ, and the store's own truncation is covered
    # by test_overflow_drops_the_oldest_entries.
    for i in range(MESSAGE_HISTORY_MAX_ENTRIES + 5):
        store.append_message_history(owner_id, NEW_TASK, f"task {i}")

    with TestClient(app) as client:
        login(client)
        body = client.get(f"/api/message-history/{NEW_TASK}").json()
    assert body["count"] == MESSAGE_HISTORY_MAX_ENTRIES
    texts = [e["text"] for e in body["entries"]]
    assert texts[0] == "task 5"
    assert texts[-1] == f"task {MESSAGE_HISTORY_MAX_ENTRIES + 4}"
    ids = [e["id"] for e in body["entries"]]
    assert len(set(ids)) == len(ids) and ids == sorted(ids)
