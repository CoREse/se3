"""State persistence for the flow engine.

Handles JSON serialization/deserialization with atomic writes
to prevent state corruption during interruptions.
"""

from tianluo.runtime_paths import runtime_dir
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    FIX_HISTORY_MAX_ENTRIES,
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
)
from .schema import build_context_from_flow

logger = logging.getLogger(__name__)

# Legacy engine.json / archive snapshots written before issue #244 一期 inline
# every step's inputs/outputs, so a single file can reach tens of MB. The
# CLI-side ``list_all_flows`` only needs a handful of top-level keys, so a file
# above this guard is never fully parsed there — it is read head+tail for its
# identity fields instead (mirrors ``daemon.disk_json_cache.MAX_PARSE_BYTES``).
LIST_MAX_PARSE_BYTES = 5 * 1024 * 1024


# Hot/cold split format marker (issue #244 一期). A header written in the new
# layout carries this key at top level; its absence identifies a legacy
# (fully-inline) engine.json / snapshot, which every load path reads verbatim
# for backward compatibility (no in-place migration — see B3).
ENGINE_FORMAT_KEY = "engine_format"
ENGINE_FORMAT_HOTCOLD = "hotcold/1"


def _stringify_keys(obj: Any) -> Any:
    """Recursively coerce every dict key to ``str``.

    Only invoked on the slow path when ``sort_keys`` cannot order a dict's keys
    because it mixes types (e.g. both ``"1"`` and ``1``). Coercing to str makes
    the keys mutually comparable — and matches how ``json.dumps`` serializes
    non-string keys anyway — so hashing succeeds without raising.
    """
    if isinstance(obj, dict):
        return {str(k): _stringify_keys(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_stringify_keys(v) for v in obj]
    return obj


def _canonical_json(obj: Any) -> str:
    """Stable JSON encoding for content-hashing cold payloads.

    ``sort_keys`` makes the hash independent of dict ordering so an unchanged
    step is recognised as unchanged across saves (the whole point of the
    incremental write path). ``default=str`` mirrors the persistence writer so
    non-JSON-native values (e.g. Path) hash the same way they serialize.

    A step's inputs/outputs/context may hold a dict with *mixed* key types
    (both ``str`` and ``int``), on which ``sort_keys=True`` raises ``TypeError``
    while comparing unlike keys — which would make the whole flow unsavable. The
    common case stays on the fast path; only a genuinely mixed-key payload pays
    the recursive stringify fallback.
    """
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        return json.dumps(
            _stringify_keys(obj), sort_keys=True, ensure_ascii=False, default=str
        )


def _content_hash(obj: Any) -> str:
    """Content hash of a cold payload, used to skip rewriting unchanged files.

    WHY the encode is not the strict default: a POSIX pathname is bytes, so a
    Git-visible path byte that is not valid UTF-8 reaches a persisted payload
    (a review baseline's tracked/untracked file map is keyed by pathname) as a
    lone surrogate, which strict UTF-8 refuses. Raising here would make the
    whole flow unsavable over one oddly named file — and this is the FIRST
    strict encode a captured baseline meets, before it is ever written. The
    handler is total and injective over those code points, which is all a
    comparison hash needs: this value is only ever compared with another hash
    computed the same way, never with a hash of the bytes actually written
    (:meth:`PersistenceManager._atomic_write_json` may escape them instead).
    """
    return hashlib.sha256(
        _canonical_json(obj).encode("utf-8", "surrogatepass")
    ).hexdigest()


def _is_hotcold(data: Any) -> bool:
    """True when ``data`` is a new-format (header + cold-ref) engine payload."""
    return (
        isinstance(data, dict)
        and str(data.get(ENGINE_FORMAT_KEY, "")).startswith("hotcold/")
    )


class _LazyStepDict(dict):
    """``step_id -> Step`` map that hydrates a step's cold body on first access.

    A resume loads only the engine.json header; each step's heavy
    inputs/outputs/artifacts stay on disk until the engine actually fetches that
    step by id (``steps.get(sid)`` / ``steps[sid]``), so a many-step flow no
    longer re-materializes every cold file up front (issue #244 B4). Because the
    engine reads *other* steps' outputs pervasively (adjudicated/refined/plan/
    review lookups walk ``step_history`` and pull ``steps.get(sid).outputs``),
    hydration hangs off keyed access — every such fetch faults in exactly the one
    step it touches and no more.

    Deliberately, iteration (``values`` / ``items`` / ``for``) does NOT hydrate:
    the incremental save path re-emits an untouched step's recorded ``cold_ref``
    without reading its body, and status-only scans (e.g. ``get_progress``) stay
    cheap. Equality DOES hydrate all (so a header-loaded flow still compares
    equal to the same flow built in memory for round-trip tests).
    """

    def __init__(self, hydrator, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hydrator = hydrator

    def __getitem__(self, key):
        step = super().__getitem__(key)
        self._hydrator(step)
        return step

    def get(self, key, default=None):
        if key not in self:
            return default
        return self.__getitem__(key)

    def _hydrate_all(self) -> None:
        for step in super().values():
            self._hydrator(step)

    def __eq__(self, other) -> bool:
        self._hydrate_all()
        return dict(self) == other

    # dict subclasses that override __eq__ must restate unhashability explicitly.
    __hash__ = None


def _split_flow(
    flow: "FlowInstance",
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Split a ``FlowInstance`` into (header, cold_steps, context_payload).

    Delegates the structural hot/cold split to the model layer
    (:meth:`FlowInstance.to_header_dict` / :meth:`Step.cold_payload` /
    :meth:`State.cold_context`) and layers on only the persistence concerns:
    content hashing and cold-file naming. The header keeps KB-scale fields — flow
    identity, status, the per-step *status table*, and small State scalars — with
    each step's heavy payload replaced by a ``cold_ref`` carrying the payload's
    content hash. The shared ``State.context`` and ``fix_history`` are
    externalized together into a single per-flow context cold payload, referenced
    by hash.

    Lazy-load aware (issue #244 B4): a step loaded header-only for resume and
    never hydrated (``cold_loaded`` False) has its recorded ``cold_ref``
    re-emitted verbatim and is *excluded* from ``cold_steps`` — its unchanged
    body (already on disk in the same ``steps/<flow_id>/`` partition) is never
    read back into memory just to rewrite it.

    Returns:
        header: the header dict to write to engine.json / the snapshot file.
        cold_steps: step_id -> {"inputs", "outputs", "artifacts"} cold payloads,
            for hydrated steps only.
        context_payload: {"context", "fix_history"} shared cold payload.
    """
    # Put the format marker FIRST so it survives head-truncation. The marker,
    # like created_at/updated_at, otherwise serializes after the large ``state``
    # block, so a machine crash mid-write (salvaged via load_flow_tolerant) would
    # lose it — and a repaired header without the marker is mistaken for a legacy
    # inline flow, silently degrading every step's cold payload to empty instead
    # of resolving the intact cold files. Leading it keeps hot/cold recovery
    # working from just the header's head (issue #244 B3, salvage path).
    header = {ENGINE_FORMAT_KEY: ENGINE_FORMAT_HOTCOLD, **flow.to_header_dict()}
    state = header["state"]
    header_steps: Dict[str, Any] = state.get("steps", {}) or {}

    cold_steps: Dict[str, Dict[str, Any]] = {}
    # Iterate the raw step map (a lazy step dict does NOT hydrate on iteration),
    # so an untouched header-only step is recognised by ``cold_loaded`` and its
    # body is never faulted in here.
    for sid, step in flow.state.steps.items():
        entry = header_steps.get(sid)
        if entry is None:
            continue
        if step.cold_loaded:
            cold = step.cold_payload()
            cold_steps[sid] = cold
            entry["cold_ref"] = {"file": f"{sid}.json", "hash": _content_hash(cold)}
        else:
            entry["cold_ref"] = dict(step.cold_ref or {})

    if flow.state.cold_context_loaded:
        context_payload = flow.state.cold_context()
        state["context_ref"] = {
            "file": PersistenceManager.CONTEXT_COLD_FILENAME,
            "hash": _content_hash(context_payload),
        }
    else:
        # Header-only load whose externalized context was never materialized (or
        # whose cold read failed on a transient blip): re-emit the recorded
        # reference verbatim and hand ``_write_cold`` a ``None`` payload so it
        # skips the file — never rewriting an intact _context.json with empty
        # data. The context analogue of an unhydrated step's lazy re-emit
        # (issue #244 B3-i).
        context_payload = None
        state["context_ref"] = dict(flow.state.cold_context_ref or {})
    return header, cold_steps, context_payload


class PersistenceManager:
    """Manages persistence of flow engine state.

    Uses atomic writes (write to temp file, then rename) to ensure
    state file integrity even if interrupted mid-write.
    """

    STATE_FILENAME = "engine.json"
    CONTEXT_FILENAME = "context.json"
    BACKUP_EXTENSION = ".bak"
    RESUMABLE_DIRNAME = "resumable"
    # Hot/cold split (issue #244 一期): heavy per-step inputs/outputs and the
    # shared context are externalized to tianluo/state/steps/<flow_id>/. Partitioned
    # by flow_id so a resumable snapshot's cold files never collide with a later
    # flow that reuses the same auto-generated step ids.
    STEPS_DIRNAME = "steps"
    CONTEXT_COLD_FILENAME = "_context.json"
    # Dirty sentinel: tianluo/state/.dirty holds {"seq": N}, bumped after every
    # successful state persist (save_flow / snapshot save / snapshot clear /
    # archive). Consumed by the daemon's DaemonHistoryReader, whose fast tick
    # gates an idle root's deep scan on this one file's stat — see the
    # sentinel gate in tianluo.daemon.history. se3/state/ is excluded by the root
    # .gitignore's deny-by-default whitelist, so the sentinel is never
    # committed.
    DIRTY_SENTINEL_FILENAME = ".dirty"

    def __init__(self, project_root: Path):
        """Initialize with project root.

        Args:
            project_root: Root directory of the project
        """
        self.project_root = Path(project_root)
        self.state_dir = runtime_dir(self.project_root) / "state"
        self.state_file = self.state_dir / self.STATE_FILENAME
        self.context_file = self.state_dir / self.CONTEXT_FILENAME
        # Per-flow resumable snapshots: tianluo/state/resumable/<flow_id>.json.
        # Unlike the single-slot engine.json (overwritten by the next run) and
        # the archive/ dir (terminal/completed snapshots only), this directory
        # holds the full FlowInstance of every flow that has NOT yet completed
        # normally, so a paused/interrupted/recoverable-failed flow stays
        # resumable even after a later run overwrites engine.json.
        self.resumable_dir = self.state_dir / self.RESUMABLE_DIRNAME
        # Root of the per-flow cold-file partitions (steps/<flow_id>/). The
        # engine.json header and the resumable snapshot header for the same flow
        # both reference this one partition, so a snapshot adds only its KB-scale
        # header — never a second copy of the cold data.
        self.steps_dir = self.state_dir / self.STEPS_DIRNAME

    def ensure_directories(self) -> None:
        """Ensure state directories exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _cold_dir(self, flow_id: str) -> Path:
        """Path to a flow's cold-file partition (steps/<flow_id>/)."""
        return self.steps_dir / str(flow_id)

    def _resolve_cold_dir(
        self,
        flow_id: Any,
        state: Optional[Dict[str, Any]],
        steps_root: Optional[Path] = None,
    ) -> Path:
        """Cold partition dir for a header, honoring a recorded override.

        Normally ``steps/<flow_id>/``. An *archived* header may carry an explicit
        ``state.cold_partition`` — recorded by :meth:`clear_state` when a
        same-flow_id archive collision forced this flow's cold files into a
        suffixed ``steps/<flow_id>_<ts>`` dir — so the header's ``cold_ref``
        entries resolve against the files that actually belong to it rather than
        a prior archive's (issue #244 B5). The recorded value is a bare partition
        basename resolved against the steps root.

        *steps_root* overrides that root: a reader rooted at ``archive/`` passes
        ``archive/steps`` so an archived header's cold refs resolve to the cold
        files that were archived alongside it (:meth:`load_archived_flow_by_id`),
        rather than the live ``state/steps`` dir. Defaults to ``self.steps_dir``.
        """
        root = steps_root if steps_root is not None else self.steps_dir
        partition = state.get("cold_partition") if isinstance(state, dict) else None
        if partition:
            return root / str(partition)
        return root / str(flow_id)

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        """Atomically write ``data`` as pretty JSON (temp file + rename).

        The single write seam for the header and every cold file, so the
        incremental-write regression tests can patch/observe exactly which files
        a persist touched. A distinct ``<name>.tmp`` sibling (not ``with_suffix``)
        keeps two files that share a stem — e.g. ``engine.json`` cold refs — from
        colliding on the temp name.
        """
        content = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        temp_file = path.with_name(path.name + ".tmp")
        try:
            try:
                temp_file.write_text(content, encoding="utf-8")
            except UnicodeEncodeError:
                # WHY the ASCII-escaped retry rather than a surrogate-tolerant
                # error handler: the payload may carry a pathname whose bytes
                # are not valid UTF-8 (a review baseline's file map keys), which
                # arrives here as a lone surrogate. Writing it back as its raw
                # byte would make the file undecodable for every reader in this
                # module and in the daemon, all of which read strict UTF-8 and
                # degrade a decode failure to "no state"; raising instead would
                # abort the run entirely. ``ensure_ascii=True`` renders the
                # surrogate as a plain ``\uXXXX`` escape, so the file stays pure
                # ASCII, every existing reader keeps working unchanged, and
                # ``json.loads`` restores the exact same string on resume. Only
                # a payload that could not be written at all pays this, so
                # ordinary state files stay human-readable.
                temp_file.write_text(
                    json.dumps(data, indent=2, ensure_ascii=True, default=str),
                    encoding="utf-8",
                )
            temp_file.replace(path)
        except Exception:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise

    def _touch_dirty_sentinel(self) -> None:
        """Bump the ``tianluo/state/.dirty`` sentinel's sequence number.

        Called at the end of every successful state persist (``save_flow``,
        ``save_resumable_snapshot``, ``clear_resumable_snapshot``,
        ``clear_state``) so a single stat of this one file tells the daemon's
        fast tick whether ANY persisted state under this root moved since the
        last tick.

        WHY: the sentinel is a pure optimization signal, never a correctness
        dependency — its consumer (the daemon reader's sentinel gate) fails
        open to a full deep scan whenever the file is missing, stale-looking,
        or unreadable. That contract is what allows every failure here to be
        swallowed: a read-only state dir, a corrupt sentinel, or a full disk
        must never break the persistence primary path, and the worst outcome
        of a missed bump is the daemon spending a few extra stats. The content
        is a monotonically increasing ``{"seq": N}`` (not a bare mtime touch)
        so a same-mtime-resolution double persist still moves the observable
        ``(mtime_ns, size, seq)`` state via the atomic-rename inode swap.
        """
        sentinel = self.state_dir / self.DIRTY_SENTINEL_FILENAME
        try:
            seq = 0
            try:
                data = json.loads(sentinel.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    seq = int(data.get("seq") or 0)
            except (OSError, ValueError, TypeError):
                seq = 0
            self._atomic_write_json(sentinel, {"seq": seq + 1})
        except Exception:
            logger.debug(
                "Failed to touch dirty sentinel %s; persistence unaffected",
                sentinel,
                exc_info=True,
            )

    def _prior_cold_hashes(
        self, header_path: Path, flow_id: str
    ) -> Tuple[Dict[str, str], Optional[str]]:
        """Read the hashes recorded by the last header written to ``header_path``.

        Returns (per-step-hash, context-hash) for the *same* flow's previously
        persisted header, enabling the incremental write path to skip cold files
        whose content is unchanged. Any condition that makes reuse unsafe —
        missing file, unreadable/legacy-inline header, or a header describing a
        *different* flow (the single-slot engine.json now holds another run) —
        returns empty hashes so every cold file is (re)written.
        """
        if not header_path.exists():
            return {}, None
        try:
            data = json.loads(header_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, None
        if not _is_hotcold(data) or str(data.get("flow_id")) != str(flow_id):
            return {}, None
        # Degrade to the "rewrite everything" default ({}, None) on any structural
        # corruption rather than raising: a non-dict 'state' or 'steps' surviving
        # as valid JSON must not raise AttributeError out of save_flow (which would
        # stop the running flow from persisting ANY further progress).
        state = data.get("state") if isinstance(data, dict) else None
        if not isinstance(state, dict):
            return {}, None
        steps = state.get("steps")
        if not isinstance(steps, dict):
            steps = {}
        per_step: Dict[str, str] = {}
        for sid, entry in steps.items():
            cold_ref = entry.get("cold_ref") if isinstance(entry, dict) else None
            if isinstance(cold_ref, dict) and "hash" in cold_ref:
                per_step[sid] = cold_ref["hash"]
        context_ref = state.get("context_ref") or {}
        ctx_hash = context_ref.get("hash") if isinstance(context_ref, dict) else None
        return per_step, ctx_hash

    @staticmethod
    def _dirty_step_ids(
        header: Dict[str, Any],
        prior_step_hashes: Dict[str, str],
        cold_dir: Optional[Path] = None,
    ) -> Set[str]:
        """Step ids whose cold payload differs from the last persisted header.

        A step whose hash still matches the prior header but whose on-disk cold
        file has gone missing (externally deleted while engine.json survived) is
        also reported dirty: the payload is still held in memory this persist, so
        re-marking it lets ``_write_cold`` repopulate the file instead of leaving
        it permanently absent — every later save would otherwise keep skipping it
        on the matching hash and the step's inputs/outputs would silently degrade
        to empty on the next load (issue #244 B2/B3).
        """
        dirty: Set[str] = set()
        for sid, entry in header.get("state", {}).get("steps", {}).items():
            new_hash = entry.get("cold_ref", {}).get("hash")
            if prior_step_hashes.get(sid) != new_hash:
                dirty.add(sid)
            elif (
                new_hash is not None
                and cold_dir is not None
                and not (cold_dir / f"{sid}.json").exists()
            ):
                dirty.add(sid)
        return dirty

    def _write_cold(
        self,
        flow_id: str,
        header: Dict[str, Any],
        cold_steps: Dict[str, Dict[str, Any]],
        context_payload: Dict[str, Any],
        prior_step_hashes: Dict[str, str],
        prior_ctx_hash: Optional[str],
    ) -> None:
        """Write only the cold files whose content changed since the last persist.

        Write volume is proportional to what actually changed this step, not to
        the flow's step count (issue #244 B2). Completed/archived cold files are
        never rewritten because their hash keeps matching.
        """
        cold_dir = self._cold_dir(flow_id)
        # Pass cold_dir so a hash-matching step whose cold file was externally
        # deleted is re-flagged dirty and rewritten from the in-memory payload.
        dirty = self._dirty_step_ids(header, prior_step_hashes, cold_dir)
        new_ctx_ref = header["state"].get("context_ref") or {}
        new_ctx_hash = new_ctx_ref.get("hash")
        ctx_dirty = new_ctx_hash != prior_ctx_hash
        ctx_file_missing = (
            new_ctx_hash is not None
            and not (cold_dir / self.CONTEXT_COLD_FILENAME).exists()
        )
        # A ``None`` payload means the externalized context was never loaded this
        # session (header-only load whose cold read failed); its intact on-disk
        # file must be left untouched — never rewritten with empty data — exactly
        # like an unhydrated step's body (issue #244 B3-i). We also cannot
        # repopulate a missing file we never read, so ``ctx_file_missing`` self-
        # heals only on a later load that actually materializes the context.
        write_ctx = context_payload is not None and (ctx_dirty or ctx_file_missing)

        if not dirty and not write_ctx:
            return

        cold_dir.mkdir(parents=True, exist_ok=True)
        for sid in dirty:
            # A "dirty" step with no in-memory cold payload is a header-only
            # step (loaded lazily, never hydrated) whose prior header — from a
            # different flow occupying the single-slot engine.json — could not be
            # matched. Its unchanged body still sits on disk in this flow's own
            # steps/<flow_id>/ partition, so skip it rather than clobber it with
            # empty data (issue #244 B4).
            if sid in cold_steps:
                self._atomic_write_json(cold_dir / f"{sid}.json", cold_steps[sid])
        # Rewrite the shared-context cold file when its hash changed or when a
        # hash-matching file went missing on disk (same repopulation rationale
        # as the per-step dirty check above) — but only for a genuinely-loaded
        # payload; a never-loaded context re-emits its reference untouched.
        if write_ctx:
            self._atomic_write_json(
                cold_dir / self.CONTEXT_COLD_FILENAME, context_payload
            )

    def save_flow(self, flow: FlowInstance) -> Path:
        """Save flow instance to state file atomically.

        Args:
            flow: Flow instance to save

        Returns:
            Path to the saved state file
        """
        self.ensure_directories()

        # Update timestamp
        from datetime import datetime
        flow.updated_at = datetime.now()

        # Hot/cold split (issue #244 一期): engine.json holds only the KB-scale
        # header; per-step inputs/outputs and the shared context live in
        # steps/<flow_id>/. Compare against the header already on disk so only
        # the cold files that actually changed this step are rewritten — the
        # write volume then tracks the step's own output, not the flow's step
        # count. Cold files first, then the header (which references them), so a
        # crash between the two never leaves the header pointing at absent data.
        header, cold_steps, context_payload = _split_flow(flow)
        prior_step_hashes, prior_ctx_hash = self._prior_cold_hashes(
            self.state_file, flow.flow_id
        )
        # Capture whom the single-slot engine.json held before this write. When
        # a fresh run overwrites a *different* prior flow (typically a prior
        # COMPLETED flow whose resumable snapshot was already cleared on
        # completion), that prior flow's steps/<prior_id>/ partition loses its
        # last reference and must be reclaimed after the swap — otherwise it
        # leaks forever (self-check fix, issue #244 B5).
        prior_flow_id: Optional[str] = None
        if self.state_file.exists():
            prior_header = _read_snapshot_header(self.state_file) or {}
            prior_flow_id = prior_header.get("flow_id")
        self._write_cold(
            flow.flow_id,
            header,
            cold_steps,
            context_payload,
            prior_step_hashes,
            prior_ctx_hash,
        )
        self._atomic_write_json(self.state_file, header)
        if prior_flow_id and str(prior_flow_id) != str(flow.flow_id):
            self._prune_cold_partition_if_orphan(str(prior_flow_id))

        # Per-flow resumable snapshot bookkeeping. save_flow is the single
        # convergence point for every pause/interrupt/failure/step-advance
        # persist, so hooking here guarantees "snapshot written the moment a
        # flow pauses/is interrupted" with no need to scatter writes across
        # run.py's exception branches. A normally COMPLETED flow needs no
        # resume, so its snapshot is removed; any other status keeps a fresh
        # snapshot. Best-effort: a snapshot I/O failure must never break the
        # primary engine.json write.
        try:
            if flow.status == FlowStatus.COMPLETED:
                self.clear_resumable_snapshot(flow.flow_id)
            else:
                # Reuse the header save_flow just built and the cold files it
                # just wrote: the snapshot references the same steps/<flow_id>/
                # partition, so it costs only a KB-scale header write and never
                # duplicates the cold payloads.
                self.save_resumable_snapshot(flow, _header=header)
        except Exception:
            logger.debug(
                "Failed to update resumable snapshot for flow %s",
                getattr(flow, "flow_id", "?"),
                exc_info=True,
            )

        self._touch_dirty_sentinel()
        return self.state_file

    @staticmethod
    def _read_cold_json(
        path: Path, label: str, warnings: Optional[List[str]]
    ) -> Optional[Any]:
        """Read+parse a cold file; return None (and warn) if missing/corrupt.

        Tolerant by design (issue #244 B3): a missing or damaged cold file must
        degrade that step's payload to empty, never crash the whole flow load.
        """
        if not path.exists():
            msg = f"Cold file missing for {label}: {path.name}; using empty payload"
            logger.warning("Cold file missing (%s): %s", label, path)
            if warnings is not None:
                warnings.append(msg)
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg = f"Cold file unreadable for {label}: {exc}; using empty payload"
            logger.warning("Cold file unreadable (%s): %s: %s", label, path, exc)
            if warnings is not None:
                warnings.append(msg)
            return None

    def _reconstruct_full_dict(
        self,
        header: Dict[str, Any],
        warnings: Optional[List[str]] = None,
        steps_root: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Inline a new-format header's cold files back into a full flow dict.

        Legacy (fully-inline) payloads are returned unchanged, so the same load
        path serves both formats (B3). A new-format header has its per-step
        ``cold_ref`` and the shared ``context_ref`` resolved from
        steps/<flow_id>/ back into the inline shape ``FlowInstance.from_dict``
        expects; missing/corrupt cold files degrade to empty payloads.

        *steps_root* overrides the cold-file root (see :meth:`_resolve_cold_dir`)
        so an archived header reconstructs against ``archive/steps`` rather than
        the live state dir.
        """
        if not _is_hotcold(header):
            return header

        cold_dir = self._resolve_cold_dir(
            header.get("flow_id"), header.get("state"), steps_root=steps_root
        )
        full = {k: v for k, v in header.items() if k != ENGINE_FORMAT_KEY}
        state = dict(full.get("state", {}))

        # ``cold_partition`` is a persistence-layer routing marker (see
        # _resolve_cold_dir); it must not leak into the inlined FlowInstance dict.
        state.pop("cold_partition", None)
        context_ref = state.pop("context_ref", None)
        cold_ref_present = isinstance(context_ref, dict) and bool(context_ref.get("file"))
        payload = None
        if cold_ref_present:
            payload = self._read_cold_json(
                cold_dir / context_ref["file"], "context", warnings
            )
        # ``or {}`` / ``or []`` (not ``.get(k, default)``): a cold _context.json
        # that parses but holds explicit nulls ({"context": null}) must degrade to
        # empty, matching the lazy _build_lazy_flow path. Without this, a null
        # context reaches State.from_dict and crashes with TypeError (len(None)),
        # which load_flow's except (JSONDecodeError/KeyError/ValueError) does not
        # catch — the eager and lazy loaders would then diverge on identical input.
        if isinstance(payload, dict):
            state["context"] = payload.get("context") or {}
            state["fix_history"] = payload.get("fix_history") or []
            context_loaded = True
        else:
            state["context"] = {}
            state["fix_history"] = []
            # A referenced-but-unreadable context is NOT genuinely empty: mark it
            # unloaded (and preserve the reference) so a resume-then-save through
            # the eager path re-emits the reference instead of persisting {} over
            # the intact cold file (issue #244 B3-i). No reference at all means the
            # empty context is real and should persist normally.
            context_loaded = not cold_ref_present
        # Provenance markers consumed by State.from_dict; absent for legacy inline
        # payloads (which default to loaded).
        state["_cold_context_loaded"] = context_loaded
        state["_cold_context_ref"] = context_ref if isinstance(context_ref, dict) else None

        rebuilt_steps: Dict[str, Any] = {}
        # Guard steps/entry against structural corruption BEFORE calling .items():
        # a valid-JSON header whose 'steps' is a non-dict, or whose step entry is a
        # non-dict, must not raise an uncaught AttributeError out of this
        # reconstructor (that would violate load_flow_tolerant's never-raises
        # contract). A non-dict step entry is passed through untouched so the
        # downstream FlowInstance.from_dict raises a *caught* TypeError/ValueError.
        raw_steps = state.get("steps")
        if not isinstance(raw_steps, dict):
            raw_steps = {}
        for sid, entry in raw_steps.items():
            if not isinstance(entry, dict):
                rebuilt_steps[sid] = entry
                continue
            step = {k: v for k, v in entry.items() if k != "cold_ref"}
            cold_ref = entry.get("cold_ref")
            cold_ref_present = isinstance(cold_ref, dict) and bool(cold_ref.get("file"))
            cold = None
            if cold_ref_present:
                cold = self._read_cold_json(
                    cold_dir / cold_ref["file"], f"step {sid}", warnings
                )
            if isinstance(cold, dict):
                step["inputs"] = cold.get("inputs", {})
                step["outputs"] = cold.get("outputs", {})
                step["artifacts"] = cold.get("artifacts", [])
                step["_cold_loaded"] = True
            else:
                step["inputs"] = {}
                step["outputs"] = {}
                step["artifacts"] = []
                # A referenced-but-unreadable step body is NOT genuinely empty:
                # mark it not-loaded and preserve the reference (consumed by
                # Step.from_dict) so a subsequent save through the eager path
                # re-emits the cold_ref instead of persisting {} over the intact
                # on-disk cold file — the per-step analogue of the context guard
                # above (issue #244 B3-i). A step with no reference at all is
                # legacy/genuinely-empty and stays loaded so it persists normally.
                step["_cold_loaded"] = not cold_ref_present
                step["_cold_ref"] = cold_ref if isinstance(cold_ref, dict) else None
            rebuilt_steps[sid] = step
        state["steps"] = rebuilt_steps

        full["state"] = state
        return full

    def hydrate_step(self, flow: FlowInstance, step_id: str) -> Optional[Step]:
        """Load one step's externalized cold payload on demand (issue #244 B4).

        A header-only ``FlowInstance`` (loaded from just the engine.json header,
        its per-step inputs/outputs still empty) can pull a single step's cold
        data without materializing every other step — the lazy counterpart to
        :meth:`load_flow`'s eager reconstruct, so a resume that only needs a few
        steps never re-inlines the whole flow. The step is mutated in place and
        returned; a missing/corrupt cold file degrades it to empty IO (B3)
        rather than raising. Returns ``None`` when *step_id* is unknown.
        """
        # Fetch the raw stored step (dict.get, not the lazy-dict override) so a
        # header-only flow does not double-hydrate; this method IS the hydration.
        step = dict.get(flow.state.steps, step_id)
        if step is None:
            return None
        # No-op on an already-materialized step, matching _make_step_hydrator's
        # guard. Re-applying cold here would re-read the file and clobber the
        # in-memory body — and for an eagerly-loaded legacy flow (no cold file)
        # apply_cold(None) degrades real inputs/outputs to empty IO. The two
        # hydration entry points must share identical semantics so this public
        # on-demand API can never destroy loaded or dirty step data.
        if step.cold_loaded:
            return step
        fname = None
        if isinstance(step.cold_ref, dict):
            fname = step.cold_ref.get("file")
        cold = self._read_cold_json(
            self._cold_dir(flow.flow_id) / (fname or f"{step_id}.json"),
            f"step {step_id}",
            None,
        )
        # apply_cold degrades a missing/corrupt payload to empty IO and marks the
        # step loaded either way, so a broken cold file is not re-read every call.
        step.apply_cold(cold)
        return step

    def _make_step_hydrator(self, cold_dir: Path):
        """Return a callable that hydrates a header-only Step in place.

        Bound into a :class:`_LazyStepDict` by :meth:`_build_lazy_flow`, it faults
        in a single step's cold body on first keyed access (from ``cold_dir``, the
        partition already resolved by the caller) and is a no-op once the step is
        loaded (or was never header-only).
        """

        def _hydrate(step: Step) -> None:
            if step.cold_loaded:
                return
            fname = None
            if isinstance(step.cold_ref, dict):
                fname = step.cold_ref.get("file")
            cold = self._read_cold_json(
                cold_dir / (fname or f"{step.step_id}.json"),
                f"step {step.step_id}",
                None,
            )
            step.apply_cold(cold)

        return _hydrate

    def _build_lazy_flow(
        self, data: Dict[str, Any], flow_id: str
    ) -> Optional[FlowInstance]:
        """Build a header-only FlowInstance whose step bodies load on demand.

        The resume/by-id counterpart to :meth:`load_flow`'s eager
        :meth:`_reconstruct_full_dict`: it reads only the KB-scale header, wiring
        each step to a lazy hydrator (:class:`_LazyStepDict`) so a many-step flow
        no longer faults in every cold file before execution can continue
        (issue #244 B4). The shared ``context`` / ``fix_history`` cold payload IS
        loaded eagerly — it is a single per-flow file the engine reads broadly,
        and it is small relative to the per-step bodies that caused the blow-up.

        A legacy (fully-inline) payload has no cold files, so it is returned
        complete via :meth:`FlowInstance.from_dict`. Returns ``None`` if the
        header cannot be deserialized.
        """
        if not _is_hotcold(data):
            try:
                return FlowInstance.from_dict(data)
            except (KeyError, ValueError, TypeError):
                return None

        try:
            flow = FlowInstance.from_header_dict(data)
        except (KeyError, ValueError, TypeError):
            return None

        cold_dir = self._resolve_cold_dir(flow_id, data.get("state"))
        state = flow.state
        context_ref = (data.get("state") or {}).get("context_ref")
        # Remember the recorded reference so a *failed* context read can re-emit
        # it verbatim on the next save instead of clobbering the intact cold file.
        state.cold_context_ref = context_ref if isinstance(context_ref, dict) else None
        if isinstance(context_ref, dict) and context_ref.get("file"):
            payload = self._read_cold_json(
                cold_dir / context_ref["file"], "context", None
            )
            if isinstance(payload, dict):
                state.context = payload.get("context", {}) or {}
                # Mirror State.from_dict's retroactive sliding-window clamp so the
                # lazy resume path and the eager load_flow path return the same
                # state for one on-disk flow — otherwise an oversized externalized
                # list (written under a higher cap) would be deepcopied per
                # transition and re-persisted unclamped on every save.
                loaded_history = payload.get("fix_history", []) or []
                if len(loaded_history) > FIX_HISTORY_MAX_ENTRIES:
                    loaded_history = loaded_history[-FIX_HISTORY_MAX_ENTRIES:]
                state.fix_history = loaded_history
                # The body is now materialized: a subsequent persist may write it.
                state.cold_context_loaded = True
            # A failed read leaves cold_context_loaded False (set by
            # from_header_dict), so save re-emits cold_context_ref untouched.
        else:
            # No externalized context to defer — the empty context IS the real
            # value and must persist normally, so mark it loaded.
            state.cold_context_loaded = True
        # Keep context['fix_history'] consistent with the externalized list, as
        # State.from_dict does for the inline format.
        if "fix_history" in state.context:
            state.context["fix_history"] = state.fix_history

        lazy = _LazyStepDict(self._make_step_hydrator(cold_dir))
        lazy.update(state.steps)
        state.steps = lazy
        return flow

    def _peek_active_flow_id(self) -> Optional[str]:
        """Return the flow_id currently held by engine.json, header-only.

        A size-guarded head read (never a full parse of a giant legacy file), so
        the resume path can cheaply tell whether it recovered a flow from the
        live engine.json or from a resumable snapshot.
        """
        if not self.state_file.exists():
            return None
        header = _read_snapshot_header(self.state_file)
        if isinstance(header, dict):
            fid = header.get("flow_id")
            return str(fid) if fid is not None else None
        return None

    def save_resumable_snapshot(
        self, flow: FlowInstance, *, _header: Optional[Dict[str, Any]] = None
    ) -> Path:
        """Persist a per-flow resumable snapshot to resumable/<flow_id>.json.

        The snapshot uses the same header + cold-reference layout as engine.json
        and shares the same ``steps/<flow_id>/`` cold partition, so it is only a
        KB-scale header write — it no longer grows linearly with an in-flight
        flow's inputs/outputs (issue #244 B2). It is the durable, per-flow copy
        that survives a later ``luo run`` overwriting the single-slot
        engine.json, so an interrupted/paused/failed flow can still be located
        and resumed by flow_id.

        Args:
            flow: Flow instance to snapshot
            _header: internal fast-path — the header already built (and whose
                cold files were already written) by ``save_flow`` for this same
                persist. When omitted (a standalone call), the snapshot recomputes
                the split and writes any changed cold files itself.

        Returns:
            Path to the snapshot file.
        """
        self.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot_file = self.resumable_dir / f"{flow.flow_id}.json"

        if _header is None:
            header, cold_steps, context_payload = _split_flow(flow)
            prior_step_hashes, prior_ctx_hash = self._prior_cold_hashes(
                snapshot_file, flow.flow_id
            )
            self._write_cold(
                flow.flow_id,
                header,
                cold_steps,
                context_payload,
                prior_step_hashes,
                prior_ctx_hash,
            )
        else:
            header = _header

        self._atomic_write_json(snapshot_file, header)
        self._touch_dirty_sentinel()
        return snapshot_file

    def load_resumable_snapshot(self, flow_id: str) -> Optional[FlowInstance]:
        """Load the per-flow resumable snapshot for ``flow_id``.

        Loaded **header-only with lazy per-step hydration** (issue #244 B4),
        exactly like :meth:`load_flow_by_id`: only the KB-scale header and the
        shared context cold file are read up front, and each step's heavy
        inputs/outputs fault in on first keyed access. Selecting a paused flow
        with many large completed steps therefore no longer re-materializes every
        step cold file before resume/display — the whole point of the split
        format. A legacy fully-inline snapshot is returned complete.

        The snapshot's embedded ``flow_id`` MUST match the requested ``flow_id``;
        a snapshot whose payload describes a different flow (a stale, misnamed,
        or operator-created artifact) is rejected and treated as not found,
        rather than silently resuming the wrong flow as the live engine.json.

        Returns:
            The reconstructed FlowInstance, or None when no (readable, matching)
            snapshot exists. Corruption is tolerated by returning None rather
            than raising.
        """
        snapshot_file = self.resumable_dir / f"{flow_id}.json"
        if not snapshot_file.exists():
            return None
        data = self._read_flow_file(snapshot_file)
        if not isinstance(data, dict):
            return None
        flow = self._build_lazy_flow(data, flow_id)
        if flow is None:
            return None
        if flow.flow_id != flow_id:
            logger.warning(
                "Resumable snapshot %s contains mismatched flow_id %r (requested %r); "
                "treating as not found",
                snapshot_file,
                flow.flow_id,
                flow_id,
            )
            return None
        return flow

    def resumable_snapshot_exists(self, flow_id: str) -> bool:
        """Whether a resumable snapshot for ``flow_id`` is still on disk.

        WHY this is a published probe: retiring a flow's resumable snapshot and
        reclaiming its review baselines are two separate deletions, and the
        second is only safe once the first is CONFIRMED done — a surviving
        snapshot keeps the flow resumable, and a resumed SELF_CHECK round has
        nothing to diff against once its baselines are gone. Callers therefore
        gate the baseline reclaim on this answer rather than on having *called*
        :meth:`clear_resumable_snapshot`. An unreadable snapshot dir is
        answered "still there", which keeps the baselines.
        """
        try:
            return (self.resumable_dir / f"{flow_id}.json").exists()
        except OSError:  # pragma: no cover - defensive
            return True

    def clear_resumable_snapshot(self, flow_id: str) -> bool:
        """Remove the per-flow resumable snapshot for ``flow_id`` (best effort).

        The snapshot is a *reference* to the shared ``steps/<flow_id>/`` cold
        partition, so dropping it can leave that partition orphaned. The two
        real callers (``luo end-session`` / ``luo salvage``) do exactly this:
        they ``clear_state()`` — which, seeing the still-live snapshot, only
        *copies* the cold files into the archive and leaves the live partition
        in place (its snapshot_alive guard) — and then call this to drop the
        snapshot. Without pruning here, that live partition would be referenced
        by nothing yet never deleted (self-check fix, issue #244 B5). So after
        unlinking the snapshot, reclaim the partition if it is now unreferenced.

        INVARIANT: returns whether the snapshot is CONFIRMED absent afterwards.
        Best-effort deletion swallows the unlink failure (a permission or I/O
        error must never break the caller's terminal bookkeeping), but a
        swallowed failure leaves the flow resumable — so the outcome has to be
        reportable, or the callers that go on to reclaim the flow's review
        baselines would strip a still-resumable flow of the snapshots its next
        SELF_CHECK round needs. Verified by re-probing the path rather than by
        trusting the unlink call, so a concurrent re-write is seen too.
        """
        snapshot_file = self.resumable_dir / f"{flow_id}.json"
        try:
            snapshot_file.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not remove resumable snapshot %s; flow stays resumable",
                snapshot_file,
                exc_info=True,
            )
        self._prune_cold_partition_if_orphan(flow_id)
        self._touch_dirty_sentinel()
        return not self.resumable_snapshot_exists(flow_id)

    def _prune_cold_partition_if_orphan(self, flow_id: str) -> None:
        """Delete ``steps/<flow_id>/`` when nothing references it any more.

        A *live* cold partition has exactly two possible references: the
        single-slot engine.json (while it holds this flow) and a resumable
        snapshot (which shares the same partition rather than duplicating it).
        Archives always own a separate copy under ``archive/steps/``, never the
        live partition. So once engine.json holds a different flow AND no
        resumable snapshot survives, the partition is dead weight — nothing will
        ever read it again. Leaving it turns every end-session/salvage of a
        paused new-format flow (and every new run overwriting a prior COMPLETED
        flow's engine.json) into a permanent multi-MB leak under
        ``tianluo/state/steps/``. Best effort: a failed removal only leaves a
        harmless orphan, so log rather than raise.
        """
        if not flow_id:
            return
        partition = self._cold_dir(str(flow_id))
        if not partition.is_dir():
            return
        # Referenced by the live engine.json? (size-guarded header read)
        if self.state_file.exists():
            header = _read_snapshot_header(self.state_file) or {}
            if str(header.get("flow_id") or "") == str(flow_id):
                return
        # Referenced by a resumable snapshot sharing this same partition?
        if (self.resumable_dir / f"{flow_id}.json").exists():
            return
        import shutil

        try:
            shutil.rmtree(partition)
        except OSError:
            logger.warning(
                "Failed to prune orphaned cold partition %s for flow %s; "
                "harmless leftover",
                partition,
                flow_id,
                exc_info=True,
            )

    def list_resumable_snapshots(self) -> List[FlowInstance]:
        """List all per-flow resumable snapshots.

        Each snapshot is loaded **header-only with lazy per-step hydration**
        (issue #244 B4): the resume picker only reads header fields (flow_id /
        status / task_description / current_step_id), so enumerating N paused
        flows must not read and parse every step cold file of every one of them.
        A step's body faults in only if that step is later fetched by id.

        Returns:
            A list of reconstructed FlowInstance objects, one per readable
            snapshot file under resumable/. Corrupt/unreadable snapshots are
            skipped silently.
        """
        flows: List[FlowInstance] = []
        if not self.resumable_dir.is_dir():
            return flows
        for snapshot_file in sorted(self.resumable_dir.glob("*.json")):
            data = self._read_flow_file(snapshot_file)
            if not isinstance(data, dict):
                continue
            flow = self._build_lazy_flow(data, snapshot_file.stem)
            if flow is None:
                continue
            # Only surface a snapshot whose embedded flow_id matches its
            # filename (resumable/<flow_id>.json). A mismatched payload is a
            # stale/misnamed/operator-created artifact that the load/resume
            # path (load_resumable_snapshot) would reject, so advertising it
            # here would offer a resume entry that can never actually resume.
            if flow.flow_id != snapshot_file.stem:
                logger.warning(
                    "Resumable snapshot %s contains mismatched flow_id %r; "
                    "skipping (cannot be resumed by filename)",
                    snapshot_file,
                    flow.flow_id,
                )
                continue
            flows.append(flow)
        return flows

    def load_flow_by_id(self, flow_id: str) -> Optional[FlowInstance]:
        """Locate and load a flow by id, preferring the active engine.json.

        Resolution order:

        1. The active engine.json, when it currently holds ``flow_id``.
        2. Otherwise the per-flow resumable snapshot (resumable/<flow_id>.json),
           which survives a later run overwriting engine.json.

        A normally COMPLETED flow has no resumable snapshot (it is cleared on
        completion by :meth:`save_flow`), so it is never resurrected through the
        snapshot path; only the still-active engine.json can return it.

        The flow is loaded **header-only with lazy per-step hydration**
        (issue #244 B4): only the KB-scale header (plus the shared context cold
        file) is read up front, and each step's heavy inputs/outputs fault in on
        first keyed access (``state.steps.get(sid)``). Resuming a flow with many
        large completed steps therefore no longer re-materializes every cold file
        before execution — a legacy fully-inline flow is still returned complete.

        Returns:
            The matching FlowInstance, or None when neither source holds it.
        """
        # 1. Active engine.json — header read only (no eager cold reconstruct).
        if self.state_file.exists():
            data = self._read_flow_file(self.state_file)
            if isinstance(data, dict) and str(data.get("flow_id") or "") == str(flow_id):
                flow = self._build_lazy_flow(data, flow_id)
                if flow is not None:
                    return flow

        # 2. Per-flow resumable snapshot.
        snapshot_file = self.resumable_dir / f"{flow_id}.json"
        if not snapshot_file.exists():
            return None
        data = self._read_flow_file(snapshot_file)
        if not isinstance(data, dict):
            return None
        flow = self._build_lazy_flow(data, flow_id)
        if flow is None:
            return None
        if flow.flow_id != flow_id:
            # The snapshot's payload describes a different flow (a stale, misnamed
            # or operator-created artifact); reject rather than resume the wrong
            # flow — mirrors load_resumable_snapshot's identity guard.
            logger.warning(
                "Resumable snapshot %s contains mismatched flow_id %r (requested %r); "
                "treating as not found",
                snapshot_file,
                flow.flow_id,
                flow_id,
            )
            return None
        return flow

    def load_archived_flow_by_id(self, flow_id: str) -> Optional[FlowInstance]:
        """Load an archived flow by id — split-aware and size-guarded.

        Scans ``tianluo/state/archive/engine_*.json`` for the header whose
        ``flow_id`` matches. The read is size-guarded (:func:`_read_snapshot_header`):

        * A **new-format** archive header (KB-scale) is parsed whole, then its
          per-step ``cold_ref`` / ``context_ref`` are resolved against
          ``archive/steps/<partition>/`` — the cold files ``clear_state`` archived
          alongside it — so the flow retains its full step inputs/outputs
          (issue #244 B5). Missing/corrupt cold files degrade that step to empty.
        * A **giant legacy** archive is NOT fully parsed: only its top-level
          identity keys are scanned from a bounded head+tail window, so a
          ``luo history show`` / listing never stalls decoding a 100 MB snapshot.
          Such a degraded header lacks ``state``/timestamps, so it cannot be
          reconstructed into a full FlowInstance and returns ``None`` — the
          caller then falls back to history-only detail rather than freezing.

        Returns the matching FlowInstance, or ``None`` when no archive holds the
        flow (or a giant legacy archive could only be read degraded).
        """
        archive_dir = self.state_dir / "archive"
        if not archive_dir.is_dir():
            return None
        archive_steps = archive_dir / self.STEPS_DIRNAME
        # Newest-first: the same flow_id can hold several archives (archive ->
        # history restore -> resume -> complete -> re-archive), and
        # ``luo history show`` should present the most recent one. Iterating
        # oldest-first would surface a stale earlier snapshot, and a degraded
        # oversized-legacy older archive (whose size-guarded read yields only a
        # header from_dict can't reconstruct) would mask a fully loadable newer
        # split-format archive of the same flow. So walk newest-first and, on a
        # from_dict failure, continue to the next-older candidate instead of
        # giving up (issue #244 B5 / mixed old-new display).
        #
        # Order by file mtime, NOT lexical filename: two naming schemes coexist
        # in archive/ — ``clear_state`` writes engine_<YYYYMMDD_HHMMSS>[_n].json
        # while worktree cold-partition promotion (merge/cleanup.py) writes
        # engine_<flow-id-slug>.json. Descending-lexical is not descending-recency
        # across schemes ('-' sorts before '_', so a timestamp name always
        # outranks a slug name regardless of age), which would reconstruct a stale
        # older archive over a newer one. mtime is the true recency signal and
        # only costs a stat per candidate (self-check fix).
        def _mtime(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0

        for archive_file in sorted(
            archive_dir.glob("engine_*.json"), key=_mtime, reverse=True
        ):
            header = _read_snapshot_header(archive_file)
            if not isinstance(header, dict):
                continue
            if str(header.get("flow_id") or "") != str(flow_id):
                continue
            full = self._reconstruct_full_dict(header, steps_root=archive_steps)
            try:
                return FlowInstance.from_dict(full)
            except (KeyError, ValueError, TypeError):
                # A degraded (oversized-legacy) header lacks the fields
                # from_dict requires; skip it and try an older archive of the
                # same flow_id rather than fully parsing the giant file or
                # masking a loadable sibling.
                continue
        return None

    @staticmethod
    def _read_flow_file(path: Path) -> Optional[Dict[str, Any]]:
        """Parse a header/engine.json/snapshot file to a dict (or None)."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def load_flow(self) -> Optional[FlowInstance]:
        """Load flow instance from state file.

        Returns:
            FlowInstance if state file exists, None otherwise
        """
        if not self.state_file.exists():
            return None

        try:
            content = self.state_file.read_text(encoding="utf-8")
            data = json.loads(content)
            return FlowInstance.from_dict(self._reconstruct_full_dict(data))
        except (json.JSONDecodeError, KeyError, ValueError):
            # Try backup if main file is corrupted
            backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
            if backup_file.exists():
                content = backup_file.read_text(encoding="utf-8")
                data = json.loads(content)
                return FlowInstance.from_dict(self._reconstruct_full_dict(data))
            return None

    def load_flow_tolerant(self) -> Tuple[Optional[FlowInstance], List[str]]:
        """Load flow instance with maximum tolerance for corruption.

        Unlike load_flow(), this method:
        - Attempts to repair truncated JSON
        - Fills missing fields with defaults
        - Falls back to .bak file
        - Never raises exceptions

        Returns:
            Tuple of (FlowInstance or None, list of warning messages)
        """
        warnings: List[str] = []

        # Try main file first, then backup
        candidates = [self.state_file]
        backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
        if backup_file.exists():
            candidates.append(backup_file)

        for filepath in candidates:
            if not filepath.exists():
                continue

            try:
                content = filepath.read_text(encoding="utf-8")
            except Exception as e:
                warnings.append(f"Failed to read {filepath.name}: {e}")
                continue

            if not content.strip():
                warnings.append(f"{filepath.name} is empty")
                continue

            # Try normal parsing first. The JSON-repair path (below) operates on
            # the KB-scale *header*; the cold files it references are resolved
            # after repair and degrade independently to empty payloads (B3), so a
            # truncated header never blocks recovery of intact cold data.
            try:
                data = json.loads(content)
                flow = FlowInstance.from_dict(
                    self._reconstruct_full_dict(data, warnings)
                )
                if filepath != self.state_file:
                    warnings.append(f"Loaded from backup {filepath.name}")
                return flow, warnings
            except json.JSONDecodeError as e:
                warnings.append(f"JSON parse error in {filepath.name}: {e}")
                # Try to repair truncated JSON (the header; cold files are never repaired)
                repaired = self._try_repair_json(content)
                if repaired is not None:
                    try:
                        flow = self._tolerant_from_dict(
                            self._reconstruct_full_dict(repaired, warnings), warnings
                        )
                        warnings.append(f"Recovered from truncated JSON in {filepath.name}")
                        return flow, warnings
                    except Exception as e2:
                        warnings.append(f"Failed to deserialize repaired JSON: {e2}")
            except (KeyError, ValueError, TypeError, AttributeError) as e:
                # AttributeError is included so a structurally corrupt-but-valid
                # JSON header (e.g. a non-dict 'steps' reaching the legacy
                # State.from_dict path, which calls .items() on it) degrades here
                # instead of escaping load_flow_tolerant's never-raises contract.
                warnings.append(f"Deserialization error in {filepath.name}: {e}")
                # Try with tolerant deserialization
                try:
                    data = json.loads(content)
                    flow = self._tolerant_from_dict(
                        self._reconstruct_full_dict(data, warnings), warnings
                    )
                    return flow, warnings
                except Exception as e2:
                    warnings.append(f"Tolerant deserialization also failed: {e2}")

        if not any(f.exists() for f in candidates):
            warnings.append("No state file found")

        return None, warnings

    @staticmethod
    def _try_repair_json(content: str) -> Optional[dict]:
        """Try to repair truncated JSON by closing open brackets.

        Args:
            content: Potentially truncated JSON string

        Returns:
            Parsed dict if repair successful, None otherwise
        """
        # Count open/close brackets
        open_braces = content.count("{") - content.count("}")
        open_brackets = content.count("[") - content.count("]")

        if open_braces <= 0 and open_brackets <= 0:
            return None  # Not a truncation issue

        # Strip trailing incomplete values (partial strings, numbers, etc.)
        repaired = content.rstrip()
        # Remove trailing comma if present
        repaired = repaired.rstrip(",")

        # Close open brackets and braces
        repaired += "]" * max(0, open_brackets)
        repaired += "}" * max(0, open_braces)

        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            # Try more aggressive repair: strip last partial key-value pair
            # Find last complete value (ending with comma, }, or ])
            import re
            # Strip back to last clean boundary
            match = re.search(r'(.*[}\]",\d])\s*[^}\]]*$', content, re.DOTALL)
            if match:
                repaired = match.group(1)
                open_braces = repaired.count("{") - repaired.count("}")
                open_brackets = repaired.count("[") - repaired.count("]")
                repaired += "]" * max(0, open_brackets)
                repaired += "}" * max(0, open_braces)
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass

        return None

    @staticmethod
    def _tolerant_from_dict(data: dict, warnings: List[str]) -> FlowInstance:
        """Create FlowInstance from dict with tolerance for missing fields.

        Args:
            data: Possibly incomplete dict
            warnings: List to append warnings to

        Returns:
            FlowInstance with defaults for missing fields
        """
        from datetime import datetime

        # Ensure required fields exist with defaults
        if "flow_id" not in data:
            data["flow_id"] = f"recovered_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            warnings.append("Missing flow_id, generated recovery ID")

        if "task_description" not in data:
            data["task_description"] = "(unknown - recovered from corrupted state)"
            warnings.append("Missing task_description")

        if "status" not in data:
            data["status"] = FlowStatus.FAILED.value
            warnings.append("Missing status, defaulting to FAILED")

        # created_at/updated_at are required by FlowInstance.from_dict but, in
        # the hot/cold header, serialize *after* the (larger) state block — so a
        # truncated header commonly loses them. Backfill here so a repaired
        # header still yields a loadable flow rather than a KeyError.
        if "created_at" not in data:
            data["created_at"] = datetime.now().isoformat()
            warnings.append("Missing created_at, using now")
        if "updated_at" not in data:
            data["updated_at"] = data["created_at"]
            warnings.append("Missing updated_at, using created_at")

        if "state" not in data or not isinstance(data.get("state"), dict):
            data["state"] = {}
            warnings.append("Missing or invalid state, using empty state")

        # Ensure state has required fields
        state_data = data["state"]
        if "steps" not in state_data:
            state_data["steps"] = {}
            warnings.append("Missing steps in state")
        if "step_history" not in state_data:
            state_data["step_history"] = list(state_data.get("steps", {}).keys())
        if "selected_steps" not in state_data:
            state_data["selected_steps"] = []
        if "context" not in state_data:
            state_data["context"] = {}

        return FlowInstance.from_dict(data)

    def create_backup(self) -> Optional[Path]:
        """Create a backup of the current state file.

        Returns:
            Path to backup file if successful, None otherwise
        """
        if not self.state_file.exists():
            return None

        backup_file = self.state_file.with_suffix(self.BACKUP_EXTENSION)
        try:
            import shutil
            shutil.copy2(self.state_file, backup_file)
            return backup_file
        except Exception:
            return None

    def clear_state(self) -> None:
        """Archive the current flow's state instead of deleting it.

        Split-format archival (issue #244 B5) keeps the flow *whole*: the
        engine.json header AND the flow's entire cold-data directory
        (``steps/<flow_id>/`` — per-step inputs/outputs plus ``_context.json``)
        move together into ``tianluo/state/archive/`` so no artifact is lost. The
        archived header keeps its ``engine_format`` marker and cold files sit at
        ``archive/steps/<flow_id>/``, mirroring the live layout, so a full-
        fidelity reload against the archive dir still finds them; the history /
        archive *listing* only needs the header (``read_engine_header``). If a
        prior archive of the same flow_id already owns that dir, this archive's
        cold files go to a timestamp-suffixed partition and the archived header
        records it (``state.cold_partition``) so header and cold files stay
        reference-consistent — stamped into the header dict and written atomically
        to the archive path (never published unstamped). A legacy
        inline engine.json has no cold dir and archives as a single file, exactly
        as before.

        If a resumable snapshot for this flow still exists (a non-completed flow
        archived by ``luo end-session`` / ``luo salvage``), its cold refs point at
        this very live partition, so the cold files are *copied* into the archive
        and the live partition is left in place — the snapshot keeps resuming at
        full fidelity instead of silently degrading to empty payloads. Only when
        no snapshot survives is the partition moved (the cheaper path).
        """
        if not self.state_file.exists():
            return

        import shutil

        # Move to archive instead of deleting
        archive_dir = self.state_dir / "archive"
        archive_dir.mkdir(exist_ok=True)

        # Capture the flow_id before renaming so the cold dir can follow. Read
        # is size-guarded: a tens-of-MB legacy engine.json is not fully parsed.
        header = _read_snapshot_header(self.state_file) or {}
        flow_id = header.get("flow_id")

        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = archive_dir / f"engine_{timestamp}.json"
        # Second-granular timestamps collide when two flows in the same project
        # are archived within the same second. On POSIX the second Path.rename
        # would silently replace the first archived header, orphaning its cold
        # partition and dropping that flow from list_all_flows / history show. So
        # probe for a free name — every archived header (with its cold files)
        # must be preserved (issue #244 B5). Consumers glob ``engine_*.json`` and
        # read identity from header content, so a numeric suffix stays matched.
        if archived.exists():
            suffix = 1
            while True:
                candidate = archive_dir / f"engine_{timestamp}_{suffix}.json"
                if not candidate.exists():
                    archived = candidate
                    break
                suffix += 1

        # Archive the cold-data directory BEFORE the header, and never publish the
        # header until every cold file is in place. The header carries ``cold_ref``
        # entries; if it were archived first (the old order) and the cold
        # copy/move then failed — permissions, disk exhaustion after the header
        # write, a destination collision — the archive would advertise cold files
        # that never arrived, and load_archived_flow_by_id / ``luo history show`` /
        # context export would silently degrade every step and the context to
        # empty payloads. So archival now fails CLOSED: any failure before the
        # header moves re-raises with the live flow (engine.json + its
        # steps/<flow_id>/ partition) fully intact, and the callers (end-session /
        # salvage) report it as a failed archive rather than a lost flow.
        #
        # The cold files are COPIED, never renamed, so a failed header move can
        # leave the live partition untouched. The redundant live partition is
        # removed only AFTER the header is safely archived — turning copy+remove
        # into a fail-closed move (self-check fix, issue #244 B5).
        cold_src: Optional[Path] = None
        cold_dst: Optional[Path] = None
        collision = False
        # A resumable snapshot for this flow shares this same live cold partition
        # (issue #244 B2 — snapshot and engine.json reference one steps/<flow_id>/
        # to avoid duplicating cold payloads). Removing the live partition would
        # strand it: the flow is still advertised as resumable (history reads it
        # from resumable/*.json), yet every step body and the shared context would
        # resolve to a now-missing file and silently degrade to empty. So when
        # such a snapshot survives this archival, the live partition is LEFT in
        # place so the snapshot still resumes at full fidelity.
        snapshot_alive = False
        if flow_id:
            cold_src = self._cold_dir(str(flow_id))
            if cold_src.is_dir():
                archive_steps = archive_dir / self.STEPS_DIRNAME
                archive_steps.mkdir(exist_ok=True)
                cold_dst = archive_steps / str(flow_id)
                # A prior archive of the same flow_id already owns
                # archive/steps/<flow_id>; suffix this archive's cold dir to keep
                # both intact rather than clobbering. The archived header's
                # cold_ref entries otherwise resolve to steps/<flow_id>/ — the
                # *previous* archive's files — so the suffixed partition name is
                # recorded in the header below (after it lands), keeping header
                # and cold files reference-consistent (issue #244 B5). Probe for a
                # free suffixed name just like the header above: re-archiving the
                # same flow_id twice within one second collides on
                # steps/<flow_id>_<timestamp> too, and copytree would raise onto
                # the existing dir and abort the whole archive.
                if cold_dst.exists():
                    collision = True
                    candidate = archive_steps / f"{flow_id}_{timestamp}"
                    suffix = 1
                    while candidate.exists():
                        candidate = archive_steps / f"{flow_id}_{timestamp}_{suffix}"
                        suffix += 1
                    cold_dst = candidate
                snapshot_alive = (self.resumable_dir / f"{flow_id}.json").exists()
                try:
                    shutil.copytree(cold_src, cold_dst)
                except (OSError, shutil.Error):
                    # Fail closed: drop any partial copy and leave the live flow
                    # whole rather than move the header onto missing cold files.
                    if cold_dst.exists():
                        shutil.rmtree(cold_dst, ignore_errors=True)
                    raise

        # Header last: the archive is only *published* once its cold files exist.
        # On a same-flow_id collision the archived header MUST already carry its
        # suffixed cold_partition at the instant it becomes visible. The old order
        # — publish via rename, then stamp the partition into the published header
        # — left a crash window in which the newer archive's header was live but
        # unstamped, so its cold_ref entries resolved to steps/<flow_id>/, the
        # OLDER sibling archive's partition: `luo history show` would silently
        # render the FIRST run's data as the second's. So for the collision case
        # we stamp the partition into the header dict and write it atomically to
        # the archive path, then drop the live header (copy+remove = fail-closed
        # move) — mirroring merge/cleanup._promote_cold_partition (issue #244 B5).
        try:
            if collision and cold_dst is not None:
                data = self._read_flow_file(self.state_file)
                if isinstance(data, dict) and _is_hotcold(data):
                    state = data.get("state")
                    if isinstance(state, dict):
                        state["cold_partition"] = cold_dst.name
                    self._atomic_write_json(archived, data)
                    self.state_file.unlink()
                else:
                    # Unreadable/legacy header (a legacy inline flow has no cold
                    # partition to misresolve anyway): fall back to a plain rename.
                    self.state_file.rename(archived)
            else:
                self.state_file.rename(archived)
        except OSError:
            # The header could not be archived; drop the just-copied cold files
            # so no orphaned archive partition is left behind, and keep the live
            # flow intact (its cold_src was copied, never moved).
            if cold_dst is not None and cold_dst.exists():
                shutil.rmtree(cold_dst, ignore_errors=True)
            raise
        # Guard on cold_dst (a cold partition was actually found AND copied), not
        # cold_src (set for any readable flow_id): a legacy inline engine.json has
        # no steps/<flow_id>/ partition, so removing the never-existent live dir
        # would raise FileNotFoundError and emit a spurious warning on every
        # legacy archival. Legacy archival stays the silent single-file rename.
        if cold_dst is not None and not snapshot_alive:
            # The archive copy is complete, so the live partition is redundant
            # (copy+remove = fail-closed move). A failed removal only leaves a
            # harmless orphan dir — nothing references it once the header is gone
            # — so log, never raise: the archive is already whole.
            try:
                shutil.rmtree(cold_src)
            except OSError:
                logger.warning(
                    "Archived cold data for flow %s but failed to remove live "
                    "partition %s; harmless leftover",
                    flow_id,
                    cold_src,
                    exc_info=True,
                )
        # Archival removed the live engine.json — a state change the daemon's
        # sentinel gate must see, or an idle-gated root would keep advertising
        # the archived flow until the status-tick backstop.
        self._touch_dirty_sentinel()

    def list_active_flows(self) -> List[Dict[str, Any]]:
        """List all active (non-archived) flow states.

        Returns:
            List of flow metadata dictionaries
        """
        flows = []
        if self.state_file.exists():
            try:
                flow = self.load_flow()
                if flow:
                    completed, total = flow.get_progress()
                    flows.append({
                        "flow_id": flow.flow_id,
                        "status": flow.status.value,
                        "task_description": flow.task_description[:100] + "..." if len(flow.task_description) > 100 else flow.task_description,
                        "progress": f"{completed}/{total}",
                        "updated_at": flow.updated_at.isoformat(),
                    })
            except Exception:
                pass
        return flows

    def list_all_flows(self) -> List[Dict[str, Any]]:
        """List all flows from all data sources: active, archived, and history-only.

        Combines tianluo/state/engine.json, tianluo/state/archive/engine_*.json,
        and tianluo/history/{flow_id}/ directories. De-duplicates by flow_id
        and sorts by updated_at descending.

        Returns:
            List of flow metadata dicts with keys:
              flow_id, status, task_description, progress, updated_at, source
        """
        import re
        from datetime import datetime

        seen: set = set()
        flows: List[Dict[str, Any]] = []

        # 1. Active flow from engine.json
        if self.state_file.exists():
            try:
                flow = self.load_flow()
                if flow:
                    completed, total = flow.get_progress()
                    desc = flow.task_description
                    flows.append({
                        "flow_id": flow.flow_id,
                        "status": flow.status.value,
                        "task_description": desc[:100] + "..." if len(desc) > 100 else desc,
                        "progress": f"{completed}/{total}",
                        "updated_at": flow.updated_at.isoformat(),
                        "source": "active",
                    })
                    seen.add(flow.flow_id)
            except Exception:
                pass

        # 2. Archived flows from tianluo/state/archive/
        archive_dir = self.state_dir / "archive"
        if archive_dir.exists():
            for archive_file in archive_dir.glob("engine_*.json"):
                try:
                    # Size-guarded header read: a legacy multi-MB archived
                    # engine.json is scanned head+tail for its identity fields
                    # rather than fully parsed, so listing never stalls on a
                    # giant snapshot. A split-format archive is a KB header.
                    data = _read_snapshot_header(archive_file)
                    if data is None:
                        continue
                    flow_id = data.get("flow_id", "unknown")
                    if flow_id in seen:
                        continue
                    seen.add(flow_id)
                    desc = data.get("task_description", "No description")
                    updated = data.get("updated_at") or datetime.fromtimestamp(
                        archive_file.stat().st_mtime
                    ).isoformat()
                    flows.append({
                        "flow_id": flow_id,
                        "status": data.get("status", "unknown"),
                        "task_description": desc[:100] + "..." if len(desc) > 100 else desc,
                        "progress": "-",
                        "updated_at": updated,
                        "source": "archived",
                    })
                except Exception:
                    continue

        # 3. History-only flows from tianluo/history/{flow_id}/
        history_dir = runtime_dir(self.project_root) / "history"
        if history_dir.exists():
            for flow_dir in history_dir.iterdir():
                if not flow_dir.is_dir():
                    continue
                flow_id = flow_dir.name
                if flow_id in seen:
                    continue
                seen.add(flow_id)

                # updated_at = mtime of most recent file in the directory
                try:
                    latest_mtime = max(
                        (f.stat().st_mtime for f in flow_dir.iterdir() if f.is_file()),
                        default=0,
                    )
                    updated_at = datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else ""
                except Exception:
                    updated_at = ""

                task_description = self.extract_history_summary(flow_dir)
                flows.append({
                    "flow_id": flow_id,
                    "status": "history",
                    "task_description": task_description,
                    "progress": "-",
                    "updated_at": updated_at,
                    "source": "history",
                })

        # Sort by updated_at descending (empty strings sort to end)
        flows.sort(key=lambda f: f.get("updated_at", ""), reverse=True)
        return flows

    @staticmethod
    def extract_history_summary(flow_dir: "Path") -> str:
        """Extract a short task description from the first JSONL file in a history dir.

        Title extraction follows a three-tier priority, aligned with the web
        chat-history display (``splitUserPromptByMarker``) and the daemon's
        ``_extract_history_summary``:

        1. The user's literal input cut out by the ``USER_CONTENT`` markers
           (:func:`~tianluo.engine.prompt_markers.extract_user_content`);
        2. otherwise the embedded ``Task description: --- ... ---`` block (the
           first-step-not-discovery ``luo run "task"`` flow);
        3. otherwise the truncated raw content.

        The first jsonl line is frequently a ``step_started`` (or other) *event*
        record carrying no user content, with the real user prompt on a later
        line; the extractor scans forward — skipping event records — to the
        first record actually carrying user content (see
        :func:`~tianluo.engine.prompt_markers.first_user_content`), bounded so a
        large file is never fully read. The CLI clips the result to 100
        characters with an ellipsis.
        """
        import re

        from .prompt_markers import extract_user_content, first_user_content

        def _clip(text: str) -> str:
            return text[:100] + "..." if len(text) > 100 else text

        jsonl_files = sorted(flow_dir.glob("*.jsonl"))
        if not jsonl_files:
            return "(no history data)"
        try:
            # Stream the leading records (bounded) rather than loading the whole
            # file: ``first_user_content`` skips ``step_started`` / progress
            # events and stops at the first record carrying user content.
            with open(
                jsonl_files[0], "r", encoding="utf-8", errors="replace"
            ) as fh:
                content = first_user_content(fh)
            if content is None:
                return "(no state data)"
            # 1. Prefer the user's literal input delimited by USER_CONTENT markers.
            user_content = extract_user_content(content)
            if user_content is not None:
                return _clip(user_content)
            # 2. Extract embedded task description if present.
            match = re.search(
                r"Task description:\s*-+\s*(.*?)\s*-+",
                content,
                re.DOTALL,
            )
            if match:
                return _clip(match.group(1).strip())
            # 3. Fallback: truncated raw content.
            return _clip(content)
        except Exception:
            return "(no state data)"

    def save_context(self, context: Dict[str, Any]) -> Path:
        """Save AI context export for handoff/resumption.

        This is a separate file optimized for AI consumption,
        containing the essential context for resuming work.

        Args:
            context: Context dictionary to save

        Returns:
            Path to the saved context file
        """
        self.ensure_directories()

        json_content = json.dumps(context, indent=2, ensure_ascii=False, default=str)

        # Atomic write
        temp_file = self.context_file.with_suffix(".tmp")
        try:
            temp_file.write_text(json_content, encoding="utf-8")
            temp_file.replace(self.context_file)
        except Exception:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise

        return self.context_file

    def export_context_from_flow(self, flow: FlowInstance) -> Path:
        """Export AI context from a flow instance.

        Automatically builds the context.json from the flow state
        using the schema-defined transformation.

        Args:
            flow: Flow instance to export context from

        Returns:
            Path to the saved context file
        """
        # A flow from a lazy loader (load_flow_by_id / load_resumable_snapshot /
        # list_resumable_snapshots) carries header-only steps whose cold bodies
        # are NOT faulted in by iteration — _LazyStepDict deliberately skips
        # hydration on items()/values() so incremental saves and status scans
        # stay cheap. But a context export is a full-fidelity serialization:
        # flow.to_dict() iterates the step map, so without hydrating first we
        # would emit every step with empty inputs/outputs/artifacts and silently
        # write a hollow context.json. Hydrate all cold bodies up front so the
        # export carries each step's real IO regardless of which loader produced
        # the flow (a legacy/eager-loaded flow has a plain dict and no-ops here).
        hydrate_all = getattr(flow.state.steps, "_hydrate_all", None)
        if callable(hydrate_all):
            hydrate_all()
        context = build_context_from_flow(flow.to_dict())
        return self.save_context(context)

    def load_context(self) -> Optional[Dict[str, Any]]:
        """Load AI context export.

        Returns:
            Context dictionary if file exists, None otherwise
        """
        if not self.context_file.exists():
            return None

        try:
            content = self.context_file.read_text(encoding="utf-8")
            data = json.loads(content)
            # Handle both new format (direct content) and old format (nested under "content")
            if data.get("type") == "se3_context":
                return data
            return data.get("content", {})
        except (json.JSONDecodeError, KeyError):
            return None

    def export_progress_markdown(self, flow: FlowInstance) -> str:
        """Export flow progress to markdown for human readability.

        This creates a markdown representation similar to the old
        progress.md but derived from the JSON state.

        Args:
            flow: Flow instance to export

        Returns:
            Markdown content
        """
        lines = [
            f"# SE3 Session Progress",
            "",
            f"**Flow ID:** {flow.flow_id}",
            f"**Status:** {flow.status.value}",
            f"**Task:** {flow.task_description}",
            "",
            "## Steps",
            "",
        ]

        for step_id in flow.state.step_history:
            step = flow.state.steps.get(step_id)
            if not step:
                continue

            status_icon = {
                StepStatus.PENDING: "⬜",
                StepStatus.RUNNING: "🔄",
                StepStatus.COMPLETED: "✅",
                StepStatus.FAILED: "❌",
                StepStatus.RETRYING: "🔁",
                StepStatus.PAUSED: "⏸️",
            }.get(step.status, "⬜")

            lines.append(f"{status_icon} **{step.step_type.value}** ({step.status.value})")

            if step.error_message:
                lines.append(f"   - Error: {step.error_message}")

            if step.artifacts:
                lines.append(f"   - Artifacts: {', '.join(str(a) for a in step.artifacts)}")

        lines.extend([
            "",
            "## Context",
            "",
            "```json",
            json.dumps(flow.state.context, indent=2, default=str),
            "```",
        ])

        return "\n".join(lines)


# -- CLI-side size-guarded header reader -----------------------------------
#
# The engine layer deliberately does NOT import ``daemon.disk_json_cache`` (that
# would invert the engine→daemon layering). ``list_all_flows`` / ``clear_state``
# only ever need a handful of top-level identity keys, so this self-contained
# reader mirrors the daemon guard: an at/under-guard file is parsed whole; an
# oversized legacy engine.json / archive snapshot is scanned head+tail for its
# top-level ``indent=2`` keys instead of being fully decoded.
import re as _re

_HEADER_STR_KEYS = ("flow_id", "status", "task_description", "task_type", "updated_at")
_HEADER_BOOL_KEYS = ("is_worktree_mode",)
_HEADER_WINDOW = 128 * 1024


def _read_snapshot_header(path: Path) -> Optional[Dict[str, Any]]:
    """Read a snapshot's top-level header, guarding against oversized files."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= LIST_MAX_PARSE_BYTES:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEADER_WINDOW)
            if size > _HEADER_WINDOW * 2:
                fh.seek(size - _HEADER_WINDOW)
                tail = fh.read(_HEADER_WINDOW)
                separate = True
            else:
                tail = fh.read()
                separate = False
    except OSError:
        return None

    head_text = head.decode("utf-8", "replace")
    tail_text = tail.decode("utf-8", "replace")
    if separate:
        # The seeked tail begins mid-line at an arbitrary byte; its partial
        # first line is usually a deeply-indented step/context copy of a hot key
        # inside the giant ``state`` block. Fabricating a ``\n`` before it would
        # forge a top-level ``\n  "key"`` anchor from a nested key truncated to
        # two-space indent, misreading e.g. a nested ``is_worktree_mode`` as the
        # file's real value. Drop up to and including the first genuine newline
        # so only real line-starts can match (self-check fix).
        nl = tail_text.find("\n")
        tail_text = tail_text[nl:] if nl != -1 else ""
    text = head_text + tail_text
    result: Dict[str, Any] = {}
    for key in _HEADER_STR_KEYS:
        # Exclude a raw newline from the value class (mirrors the daemon twin in
        # disk_json_cache._degraded_header): a boundary-truncated top-level string
        # would otherwise produce a seam-spanning match capturing head-remainder +
        # tail garbage as the value. Barring raw newlines makes it a clean miss.
        m = _re.search(r'\n  "' + _re.escape(key) + r'":\s*"((?:[^"\\\n]|\\.)*)"', text)
        if m is not None:
            try:
                result[key] = json.loads('"' + m.group(1) + '"')
            except ValueError:
                result[key] = m.group(1)
    for key in _HEADER_BOOL_KEYS:
        m = _re.search(r'\n  "' + _re.escape(key) + r'":\s*(true|false)', text)
        if m is not None:
            result[key] = m.group(1) == "true"
    if "flow_id" not in result:
        return None
    return result