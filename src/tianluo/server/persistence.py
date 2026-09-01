"""Embedded-sqlite persistence layer for the multi-tenant server.

This is the project's first persistence layer (the codebase was previously
pure-in-memory — see ``server/state.py``). It is the source of truth for the
facts that **cannot** be rebuilt by a daemon reconnecting:

- ``owners``            — the internal, stable ``owner_id`` primary key
- ``identity_bindings`` — ``(provider, external_id) -> owner_id`` (a single
  owner may carry many bindings; one external identity maps to one owner)
- ``local_credentials`` — argon2/bcrypt password hash for the built-in local
  auth provider, keyed by ``owner_id``
- ``daemon_keys``       — issued daemon-key **hashes** bound to an owner
- ``breakglass_tokens`` — one-time admin break-glass token **hashes**
- ``message_history``   — the prompt text an owner has actually sent from the
  web console, per input channel (the up/down-arrow recall list)

Machine / flow live state stays in memory (``ServerState``) and is rebuilt
from daemon reconnects; it is deliberately NOT persisted here.

Design notes:

- All SQL is encapsulated behind the :class:`Store` repository interface so a
  future Postgres backend is a drop-in replacement (swap the implementation,
  not the call sites). Callers never see SQL.
- ``owner_id`` is an opaque internal identifier decoupled from every provider
  authentication identifier, satisfying the forward-compatibility requirement:
  adding / switching an auth provider is just hanging another
  ``identity_binding`` off an existing ``owner_id`` — owner_id, the
  daemon→owner binding, and already-issued daemon keys are all unchanged.
- WAL journal mode + per-thread connections make the single-file DB safe under
  the server's concurrent (async + worker-thread) access.

Only stdlib ``sqlite3`` is used — no external dependency, matching the
single-file / zero-dependency deployment goal.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

# Current schema version, tracked via ``PRAGMA user_version``. Bump and append
# a migration to ``_MIGRATIONS`` to evolve the schema.
SCHEMA_VERSION = 2

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS owners (
    owner_id     TEXT PRIMARY KEY,
    display_name TEXT,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS identity_bindings (
    provider    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (provider, external_id),
    FOREIGN KEY (owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_identity_owner ON identity_bindings(owner_id);

CREATE TABLE IF NOT EXISTS local_credentials (
    owner_id      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    updated_at    REAL NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daemon_keys (
    key_id     TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    key_hash   TEXT NOT NULL UNIQUE,
    label      TEXT,
    created_at REAL NOT NULL,
    revoked_at REAL,
    FOREIGN KEY (owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_daemon_keys_owner ON daemon_keys(owner_id);

CREATE TABLE IF NOT EXISTS breakglass_tokens (
    token_id    TEXT PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    consumed_at REAL
);
"""

# v2 — web-console message history, the up/down-arrow recall list.
#
# WHY the text an owner SENT is persisted server-side while the text they have
# not sent (the draft) stays in the browser: a sent message is a fact about the
# owner, not about the device it was typed on, so it must follow them across
# browsers and machines. ``owner_id`` cascades so deleting an owner takes their
# history with it, and the ``(owner_id, channel)`` index is the only access
# path — every read and write is scoped to exactly one owner's one channel.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS message_history (
    entry_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   TEXT NOT NULL,
    channel    TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES owners(owner_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_message_history_owner_channel
    ON message_history(owner_id, channel);
"""

#: The web console's history channels. Two inputs recall independently: the
#: docked reply box (shared by respond and interject — one textarea, one
#: conversation) and the New Task description. Keeping them apart is the point:
#: a task description surfacing while answering a running flow's question is
#: noise, not recall. The issue modal has no channel — it is not a prompt.
HISTORY_CHANNEL_FLOW_REPLY = "flow-reply"
HISTORY_CHANNEL_NEW_TASK = "new-task"
MESSAGE_HISTORY_CHANNELS = (HISTORY_CHANNEL_FLOW_REPLY, HISTORY_CHANNEL_NEW_TASK)

#: Per (owner, channel) cap, deliberately the same 500 as the CLI's
#: ``tianluo.engine.prompt_history.MAX_ENTRIES``. The two stores are separate
#: (the console never reads the CLI's file), but an operator moving between
#: them should not find one of them forgetting sooner than the other.
MESSAGE_HISTORY_MAX_ENTRIES = 500


# --------------------------------------------------------------------------- #
# Record dataclasses (returned by the repository; never expose raw Rows)      #
# --------------------------------------------------------------------------- #


@dataclass
class Owner:
    owner_id: str
    display_name: Optional[str]
    is_admin: bool
    created_at: float


@dataclass
class DaemonKey:
    key_id: str
    owner_id: str
    key_hash: str
    label: Optional[str]
    created_at: float
    revoked_at: Optional[float]

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass
class MessageHistoryEntry:
    """One recall-list entry, carrying the identity the browser merges on.

    WHY ``entry_id`` leaves the store at all: a browser holds its own list of
    what THIS session sent and folds it together with whatever a later read
    returns. Text cannot decide whether a remote row *is* one of those sends —
    the same words legitimately appear twice in one history — so the row's own
    server-assigned id is the only sound identity, and it has to travel with
    every entry the client ever sees.
    """

    entry_id: int
    text: str
    created_at: float


@dataclass
class MessageHistoryAppend:
    """Outcome of one append: did it create a row, and which row is it now?

    ``entry_id`` is the row this append *is*: the newly inserted one when
    ``appended``, and — when the adjacent-repeat rule folded it — the existing
    row it folded onto, which is exactly what lets the caller recognise its own
    append in a later read. ``None`` only for text that is not a message at all
    (blank), where there is no row to point at.
    """

    appended: bool
    entry_id: Optional[int]


class Store:
    """SQLite-backed repository for the server's persisted facts.

    One ``Store`` wraps one database file (or ``":memory:"``). Construct it
    once at server startup and share it; it is safe to use from multiple
    threads (each thread gets its own connection; ``:memory:`` uses a single
    lock-serialized connection since per-thread in-memory DBs would not share
    data).
    """

    def __init__(self, db_path: Union[str, Path] = ":memory:"):
        self._db_path = str(db_path)
        self._is_memory = self._db_path == ":memory:"
        self._lock = threading.Lock()
        self._tlocal = threading.local()
        # A single shared connection for the in-memory case (per-thread
        # in-memory connections would each get an isolated empty DB).
        self._shared_conn: Optional[sqlite3.Connection] = None

        if not self._is_memory:
            Path(self._db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._db_path = str(Path(self._db_path).expanduser())

        self._init_schema()

    # ----- connection management ------------------------------------------ #

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        # WAL gives concurrent readers a consistent view while a writer is
        # active; busy_timeout avoids spurious "database is locked" under
        # contention. foreign_keys must be enabled per-connection.
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._is_memory:
            if self._shared_conn is None:
                self._shared_conn = self._new_connection()
            return self._shared_conn
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._tlocal.conn = conn
        return conn

    @contextlib.contextmanager
    def _reading(self):
        """Yield a connection safe to READ on, serializing only when shared.

        WHY reads need this at all: :meth:`_conn` hands every thread its own
        connection for a file-backed store, but an in-memory store — the default
        for ``create_app()``, and so for every test and every bare deployment —
        must share ONE connection, because per-thread in-memory databases would
        each be a separate empty DB. The writes already serialize on
        :attr:`_lock`; the reads did not, and Starlette runs every sync
        dependency (``require_owner`` → :meth:`get_owner` on the request hot
        path) in its threadpool. Two concurrent requests therefore drove the
        same sqlite connection at once and raised
        ``sqlite3.InterfaceError: bad parameter or other API misuse``, surfacing
        as a 500 followed by 401s for that session.

        The lock is taken ONLY for the shared-connection case, so a file-backed
        deployment keeps its fully parallel reads.
        """
        if self._is_memory:
            with self._lock:
                yield self._conn()
        else:
            yield self._conn()

    def _read_one(self, sql: str, params: tuple = ()):
        """Run one read that returns at most one row, on a safe connection."""
        with self._reading() as conn:
            return conn.execute(sql, params).fetchone()

    def _read_all(self, sql: str, params: tuple = ()) -> list:
        """Run one read that returns many rows, on a safe connection."""
        with self._reading() as conn:
            return conn.execute(sql, params).fetchall()

    def close(self) -> None:
        """Close the connection for the current thread (and the shared one)."""
        if self._shared_conn is not None:
            try:
                self._shared_conn.close()
            finally:
                self._shared_conn = None
        conn = getattr(self._tlocal, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._tlocal.conn = None

    # ----- schema / migrations -------------------------------------------- #

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                return
            self._apply_migrations(conn, version)

    def _apply_migrations(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply migrations from ``from_version`` up to ``SCHEMA_VERSION``.

        Migration hook: each step is an ``(target_version, sql)`` pair applied
        in order. v1 lays down the full initial schema; future schema changes
        append additional steps here.

        INVARIANT: an already-published step is never edited in place. A
        deployed database has recorded that it ran v1 and will only ever run
        the steps *after* it, so a v1 edited today would reach a fresh install
        and no existing one — the two would silently diverge. Schema changes
        are only ever a new pair appended here plus a ``SCHEMA_VERSION`` bump.
        """
        migrations = [
            (1, _SCHEMA_V1),
            (2, _SCHEMA_V2),
        ]
        for target, sql in migrations:
            if from_version < target:
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version={target}")
                conn.commit()
                logger.debug("persistence: migrated schema to v%d", target)

    @staticmethod
    def _now() -> float:
        return time.time()

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    # ----- owners --------------------------------------------------------- #

    def create_owner(
        self,
        display_name: Optional[str] = None,
        *,
        is_admin: bool = False,
        owner_id: Optional[str] = None,
    ) -> str:
        """Create an owner and return its stable internal ``owner_id``."""
        oid = owner_id or self._new_id()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO owners (owner_id, display_name, is_admin, created_at) "
                "VALUES (?, ?, ?, ?)",
                (oid, display_name, 1 if is_admin else 0, self._now()),
            )
            conn.commit()
        return oid

    def get_owner(self, owner_id: str) -> Optional[Owner]:
        row = self._read_one(
            "SELECT owner_id, display_name, is_admin, created_at "
            "FROM owners WHERE owner_id = ?",
            (owner_id,),
        )
        if row is None:
            return None
        return Owner(
            owner_id=row["owner_id"],
            display_name=row["display_name"],
            is_admin=bool(row["is_admin"]),
            created_at=row["created_at"],
        )

    def list_owners(self) -> List[Owner]:
        rows = self._read_all(
            "SELECT owner_id, display_name, is_admin, created_at "
            "FROM owners ORDER BY created_at ASC"
        )
        return [
            Owner(
                owner_id=r["owner_id"],
                display_name=r["display_name"],
                is_admin=bool(r["is_admin"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def delete_owner(self, owner_id: str) -> bool:
        """Delete an owner and (via ON DELETE CASCADE) its bindings/creds/keys."""
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM owners WHERE owner_id = ?", (owner_id,))
            conn.commit()
            return cur.rowcount > 0

    def set_admin(self, owner_id: str, is_admin: bool) -> bool:
        """Update an owner's ``is_admin`` flag; return whether the owner existed.

        Backs the admin user-management "toggle admin" endpoint. Returns
        ``True`` when an owner row matched ``owner_id`` (the flag was written),
        ``False`` for an unknown ``owner_id`` (no row touched, no error).
        """
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "UPDATE owners SET is_admin = ? WHERE owner_id = ?",
                (1 if is_admin else 0, owner_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def _count_real_admins(
        self, conn: sqlite3.Connection, breakglass_owner_id: Optional[str]
    ) -> int:
        """Count admin owners excluding the break-glass subject (lock-held).

        Helper for the guarded mutations below: it runs on an already-open
        connection while ``self._lock`` is held, so the count and the
        subsequent write commit as one critical section. The break-glass
        escape-hatch owner (if any) is excluded — it is a fallback entrance,
        never counted as real-admin headroom.
        """
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM owners "
            "WHERE is_admin = 1 AND (? IS NULL OR owner_id != ?)",
            (breakglass_owner_id, breakglass_owner_id),
        ).fetchone()
        return int(row["c"])

    def delete_owner_guarded(
        self, owner_id: str, *, breakglass_owner_id: Optional[str] = None
    ) -> str:
        """Delete an owner, refusing atomically if it is the last real admin.

        Like :meth:`delete_owner`, but the last-real-admin invariant is checked
        and the row deleted inside the *same* held write lock, so two concurrent
        deletions of two distinct real admins cannot each observe a stale
        ``count > 1`` and both commit (which would leave zero real admins and
        lock management out). Returns one of:

        - ``"not_found"`` — no owner matched ``owner_id`` (nothing deleted)
        - ``"last_admin"`` — the target is the last real admin (refused)
        - ``"deleted"`` — the owner (and its cascade) was removed
        """
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT is_admin FROM owners WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                return "not_found"
            if row["is_admin"] and self._count_real_admins(conn, breakglass_owner_id) <= 1:
                return "last_admin"
            conn.execute("DELETE FROM owners WHERE owner_id = ?", (owner_id,))
            conn.commit()
            return "deleted"

    def set_admin_guarded(
        self,
        owner_id: str,
        is_admin: bool,
        *,
        breakglass_owner_id: Optional[str] = None,
    ) -> str:
        """Set an owner's admin flag, refusing a last-real-admin demotion atomically.

        Promotion (``is_admin=True``) is unguarded. A demotion is checked and
        applied inside the *same* held write lock, so two concurrent demotions
        of two distinct real admins cannot both pass a stale count and both
        commit. Returns one of:

        - ``"not_found"`` — no owner matched ``owner_id``
        - ``"last_admin"`` — demoting would remove the last real admin (refused)
        - ``"updated"`` — the flag was written
        """
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT is_admin FROM owners WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                return "not_found"
            if (
                not is_admin
                and row["is_admin"]
                and self._count_real_admins(conn, breakglass_owner_id) <= 1
            ):
                return "last_admin"
            conn.execute(
                "UPDATE owners SET is_admin = ? WHERE owner_id = ?",
                (1 if is_admin else 0, owner_id),
            )
            conn.commit()
            return "updated"

    def create_local_user(
        self,
        provider: str,
        external_id: str,
        password_hash: str,
        *,
        display_name: Optional[str] = None,
        is_admin: bool = False,
        owner_id: Optional[str] = None,
    ) -> str:
        """Atomically create an owner + identity binding + local password hash.

        This is the admin user-provisioning primitive (``POST /api/users``):
        the owner record, the ``(provider, external_id)`` binding, and the
        password-hash credential are inserted in a **single transaction**, so a
        duplicate username never leaves an orphan owner behind. ``password_hash``
        is the already-slow-hashed value (the caller hashes via
        :func:`tianluo.server.crypto.hash_password`); this layer never sees plaintext.

        Raises :class:`IdentityAlreadyBound` when ``(provider, external_id)``
        already maps to an owner — the whole insert is rolled back.
        """
        oid = owner_id or self._new_id()
        now = self._now()
        with self._lock:
            conn = self._conn()
            existing = conn.execute(
                "SELECT owner_id FROM identity_bindings "
                "WHERE provider = ? AND external_id = ?",
                (provider, external_id),
            ).fetchone()
            if existing is not None:
                raise IdentityAlreadyBound(
                    f"identity ({provider!r}, {external_id!r}) is already bound"
                )
            try:
                conn.execute(
                    "INSERT INTO owners (owner_id, display_name, is_admin, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (oid, display_name, 1 if is_admin else 0, now),
                )
                conn.execute(
                    "INSERT INTO identity_bindings "
                    "(provider, external_id, owner_id, created_at) VALUES (?, ?, ?, ?)",
                    (provider, external_id, oid, now),
                )
                conn.execute(
                    "INSERT INTO local_credentials (owner_id, password_hash, updated_at) "
                    "VALUES (?, ?, ?)",
                    (oid, password_hash, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return oid

    # ----- identity bindings ---------------------------------------------- #

    def link_identity(self, owner_id: str, provider: str, external_id: str) -> None:
        """Attach a ``(provider, external_id)`` binding to ``owner_id``.

        Enforces the ``UNIQUE(provider, external_id)`` constraint: one external
        identity maps to at most one owner. Re-linking the same identity to the
        same owner is idempotent; linking it to a *different* owner raises
        :class:`IdentityAlreadyBound` (the trust-gate / account-takeover guard
        lives one layer up, in ``identity.py``).
        """
        existing = self.resolve_owner_by_identity(provider, external_id)
        if existing is not None:
            if existing == owner_id:
                return
            raise IdentityAlreadyBound(
                f"identity ({provider!r}, {external_id!r}) is already bound to a "
                f"different owner"
            )
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO identity_bindings (provider, external_id, owner_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (provider, external_id, owner_id, self._now()),
            )
            conn.commit()

    def resolve_owner_by_identity(self, provider: str, external_id: str) -> Optional[str]:
        row = self._read_one(
            "SELECT owner_id FROM identity_bindings WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        )
        return row["owner_id"] if row is not None else None

    def list_identities(self, owner_id: str) -> List[tuple]:
        """Return ``[(provider, external_id), ...]`` bound to ``owner_id``."""
        rows = self._read_all(
            "SELECT provider, external_id FROM identity_bindings "
            "WHERE owner_id = ? ORDER BY created_at ASC",
            (owner_id,),
        )
        return [(r["provider"], r["external_id"]) for r in rows]

    # ----- local credentials (password hash) ------------------------------ #

    def set_password(self, owner_id: str, password_hash: str) -> None:
        """Upsert the local password hash for ``owner_id``.

        Only the already-hashed value is accepted; this layer never sees or
        stores a plaintext password (hashing is the caller's responsibility via
        :func:`tianluo.server.crypto.hash_password`).
        """
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO local_credentials (owner_id, password_hash, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(owner_id) DO UPDATE SET password_hash = excluded.password_hash, "
                "updated_at = excluded.updated_at",
                (owner_id, password_hash, self._now()),
            )
            conn.commit()

    def get_password_hash(self, owner_id: str) -> Optional[str]:
        row = self._read_one(
            "SELECT password_hash FROM local_credentials WHERE owner_id = ?",
            (owner_id,),
        )
        return row["password_hash"] if row is not None else None

    # ----- daemon keys ----------------------------------------------------- #

    def issue_daemon_key(
        self, owner_id: str, key_hash: str, label: Optional[str] = None
    ) -> str:
        """Persist a daemon-key *hash* bound to ``owner_id``; return the key_id.

        The plaintext key is generated and shown once by the caller
        (:func:`tianluo.server.crypto.generate_token`); only its hash reaches here.
        """
        key_id = self._new_id()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO daemon_keys (key_id, owner_id, key_hash, label, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key_id, owner_id, key_hash, label, self._now()),
            )
            conn.commit()
        return key_id

    def resolve_owner_by_daemon_key(self, key_hash: str) -> Optional[str]:
        """Return the owner a *non-revoked* daemon key hash belongs to."""
        row = self._read_one(
            "SELECT owner_id FROM daemon_keys WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        )
        return row["owner_id"] if row is not None else None

    def revoke_daemon_key(self, key_id: str) -> bool:
        """Mark a daemon key revoked (by key_id). Returns False if not found.

        Idempotent: revoking an already-revoked key keeps the original
        ``revoked_at`` and still reports success.
        """
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "UPDATE daemon_keys SET revoked_at = ? "
                "WHERE key_id = ? AND revoked_at IS NULL",
                (self._now(), key_id),
            )
            conn.commit()
            if cur.rowcount > 0:
                return True
            # Already revoked / present but no-op still counts as success;
            # only a genuinely missing key_id is a failure.
            exists = conn.execute(
                "SELECT 1 FROM daemon_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
            return exists is not None

    def list_daemon_keys(
        self, owner_id: str, *, include_revoked: bool = True
    ) -> List[DaemonKey]:
        sql = (
            "SELECT key_id, owner_id, key_hash, label, created_at, revoked_at "
            "FROM daemon_keys WHERE owner_id = ?"
        )
        if not include_revoked:
            sql += " AND revoked_at IS NULL"
        sql += " ORDER BY created_at ASC"
        rows = self._read_all(sql, (owner_id,))
        return [
            DaemonKey(
                key_id=r["key_id"],
                owner_id=r["owner_id"],
                key_hash=r["key_hash"],
                label=r["label"],
                created_at=r["created_at"],
                revoked_at=r["revoked_at"],
            )
            for r in rows
        ]

    # ----- message history (web-console prompt recall) ---------------------- #

    def append_message_history(
        self,
        owner_id: str,
        channel: str,
        text: str,
        *,
        max_entries: int = MESSAGE_HISTORY_MAX_ENTRIES,
    ) -> MessageHistoryAppend:
        """Append one *sent* prompt to ``(owner_id, channel)``; report where it landed.

        Three rules, all enforced here rather than at the call site so every
        entrance (REST today, anything later) obeys them:

        - blank / whitespace-only text is not a message and is dropped;
        - text identical to the newest entry is dropped — holding Enter on the
          same answer should not push the previous ones out of reach;
        - the newest ``max_entries`` survive, older rows are deleted.

        WHY the return value names a row rather than answering yes/no: the
        caller has to be able to recognise *this* append in a list it reads
        later, and a fold produces no new row to recognise. Reporting the row
        the fold landed on makes both outcomes point at the entry this append
        became, which is the only identity the browser is allowed to merge on.

        The read-back, the insert and the trim run inside one held write lock
        so two concurrent sends cannot both see the same "last entry" and both
        insert, nor race the trim into deleting a row that just arrived.
        """
        body = text if isinstance(text, str) else ""
        if not body.strip():
            return MessageHistoryAppend(appended=False, entry_id=None)
        cap = max(1, int(max_entries))
        with self._lock:
            conn = self._conn()
            last = conn.execute(
                "SELECT entry_id, text FROM message_history "
                "WHERE owner_id = ? AND channel = ? ORDER BY entry_id DESC LIMIT 1",
                (owner_id, channel),
            ).fetchone()
            if last is not None and last["text"] == body:
                return MessageHistoryAppend(
                    appended=False, entry_id=int(last["entry_id"])
                )
            cur = conn.execute(
                "INSERT INTO message_history (owner_id, channel, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (owner_id, channel, body, self._now()),
            )
            entry_id = int(cur.lastrowid)
            conn.execute(
                "DELETE FROM message_history "
                "WHERE owner_id = ? AND channel = ? AND entry_id NOT IN ("
                "    SELECT entry_id FROM message_history "
                "    WHERE owner_id = ? AND channel = ? "
                "    ORDER BY entry_id DESC LIMIT ?"
                ")",
                (owner_id, channel, owner_id, channel, cap),
            )
            conn.commit()
            return MessageHistoryAppend(appended=True, entry_id=entry_id)

    def list_message_history(
        self,
        owner_id: str,
        channel: str,
        *,
        limit: int = MESSAGE_HISTORY_MAX_ENTRIES,
    ) -> List[MessageHistoryEntry]:
        """Return ``(owner_id, channel)``'s entries oldest-first, newest last.

        Oldest-first because that is the order the arrow-key navigator walks
        backwards through; the SQL selects the *newest* ``limit`` rows (that is
        what a cap means) and hands them back re-ordered.
        """
        cap = max(0, int(limit))
        if cap == 0:
            return []
        rows = self._read_all(
            "SELECT entry_id, text, created_at FROM message_history "
            "WHERE owner_id = ? AND channel = ? ORDER BY entry_id DESC LIMIT ?",
            (owner_id, channel, cap),
        )
        return [
            MessageHistoryEntry(
                entry_id=int(r["entry_id"]),
                text=r["text"],
                created_at=float(r["created_at"]),
            )
            for r in reversed(rows)
        ]

    def count_message_history(self, owner_id: str, channel: str) -> int:
        """Row count for one ``(owner_id, channel)`` — the truncation probe."""
        row = self._read_one(
            "SELECT COUNT(*) AS c FROM message_history WHERE owner_id = ? AND channel = ?",
            (owner_id, channel),
        )
        return int(row["c"])

    # ----- break-glass tokens ---------------------------------------------- #

    def put_breakglass(
        self, token_hash: str, *, expires_at: Optional[float] = None
    ) -> str:
        """Persist a break-glass token *hash*; return its token_id.

        Break-glass is a single admin escape hatch — one-time / re-issuable,
        hashed at rest, never logged. The plaintext is printed once to the
        server console by the issuing CLI and never stored.
        """
        token_id = self._new_id()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO breakglass_tokens (token_id, token_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (token_id, token_hash, self._now(), expires_at),
            )
            conn.commit()
        return token_id

    def consume_breakglass(self, token_hash: str, *, now: Optional[float] = None) -> bool:
        """Atomically consume a break-glass token by hash.

        Returns ``True`` exactly once for a valid, unexpired, unconsumed token
        (marking it consumed in the same transaction); ``False`` for unknown,
        already-consumed, or expired tokens. The check-and-mark is serialized
        under the write lock so a token can never be consumed twice.
        """
        ts = self._now() if now is None else now
        with self._lock:
            conn = self._conn()
            row = conn.execute(
                "SELECT token_id, expires_at, consumed_at FROM breakglass_tokens "
                "WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return False
            if row["consumed_at"] is not None:
                return False
            if row["expires_at"] is not None and ts > row["expires_at"]:
                return False
            conn.execute(
                "UPDATE breakglass_tokens SET consumed_at = ? WHERE token_id = ?",
                (ts, row["token_id"]),
            )
            conn.commit()
            return True

    def purge_breakglass(self) -> int:
        """Delete all break-glass tokens (e.g. invalidate prior escape hatches).

        Returns the number of rows deleted. Useful when re-issuing: an admin
        can wipe outstanding tokens before minting a fresh one.
        """
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM breakglass_tokens")
            conn.commit()
            return cur.rowcount


class IdentityAlreadyBound(Exception):
    """Raised when linking an external identity already bound to another owner."""
