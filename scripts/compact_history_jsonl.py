#!/usr/bin/env python3
"""Backfill compaction for oversized records in tianluo/history/*.jsonl.

Background
----------
A single ``luo run`` step can write one physical ``jsonl`` line of tens of MB
(one observed ``discovery`` record was 23.6 MB, holding 206 ``tool_result``
events buried among ~46 000 zero-render telemetry events). The daemon read path
now shrinks such a record *online* before putting it on the wire, but records
already on disk keep their original size and keep costing the reader a full
parse of every byte. This script applies the very same shrinking to stored
files, using the very same implementation
(:mod:`tianluo.daemon.record_budget`) so an online-degraded record and a
backfilled stored record come out the same shape and the frontend only ever has
to know one truncation marker.

INVARIANT: ``ordinal`` is the physical line number of a record inside its step
file. It is the key the WebUI reconciles bundles by (``step_id#ordinal``) and it
is already cached, server-side and client-side, for every record ever delivered.
This script therefore rewrites strictly 1:1 -- every line read produces exactly
one line written, in the same position, with its line terminator preserved. It
NEVER deletes a line, NEVER inserts one, and NEVER reorders them; a line that
cannot be compacted for any reason (under the fast-path gate, unparseable,
compaction failed, product not a single valid JSON line, or no size gain) is
written back byte-for-byte. Any drift here silently misaligns every historical
conversation in the directory, irreversibly.

The rewrite is guarded twice on top of that: the temporary file's physical line
count must equal the source's before the atomic ``os.replace``, and nothing is
written at all unless ``--apply`` is passed.

Usage:
    python scripts/compact_history_jsonl.py                    # dry-run (default)
    python scripts/compact_history_jsonl.py --apply            # rewrite in place
    python scripts/compact_history_jsonl.py --flow-id 20260706-013803_96453dd6
    python scripts/compact_history_jsonl.py --history-dir /path/to/tianluo/history
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, List, Optional, Tuple

_CHUNK_BYTES = 1024 * 1024
_TMP_PREFIX = ".compact-tmp-"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_record_budget() -> Any:
    """Load ``record_budget`` without importing the ``tianluo.daemon`` package.

    WHY: ``tianluo/daemon/__init__.py`` eagerly pulls in the supervisor, spawner
    and client (psutil, the disk cache, the outbound link). This script only
    needs the pure, I/O-free compaction functions, and it must run from a plain
    source checkout with no install and no PYTHONPATH fiddling -- loading the
    module file directly gives exactly that, and also guarantees the checkout's
    implementation is used rather than an older installed copy shadowing it.
    """
    module_path = _project_root() / "src" / "tianluo" / "daemon" / "record_budget.py"
    if module_path.is_file():
        spec = importlib.util.spec_from_file_location(
            "tianluo_record_budget_backfill", module_path
        )
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            # ``@dataclass`` resolves its own module through ``sys.modules``, so
            # a file-loaded module has to be registered before it is executed.
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    from tianluo.daemon import record_budget  # noqa: F401 (installed fallback)

    return record_budget


record_budget = _load_record_budget()


def _dumps(record: Any) -> str:
    """Serialise a compacted record the way the daemon->server link does.

    WHY: must stay identical to ``record_budget``'s own encoder
    (``ensure_ascii=False``, default separators) -- the stored line is what the
    reader later measures against the same byte budgets, so a different encoder
    here would make a backfilled record and an online-compacted one disagree on
    size (badly so on CJK-heavy records).
    """
    return json.dumps(record, ensure_ascii=False, default=str)


@dataclass
class LineOutcome:
    """What happened to one physical line."""

    oversized: bool = False
    compacted: bool = False
    failed: bool = False
    original_bytes: int = 0
    new_bytes: int = 0


@dataclass
class FileReport:
    path: Path
    total_lines: int = 0
    oversized_lines: int = 0
    compacted_lines: int = 0
    failed_lines: int = 0
    oversized_bytes: int = 0
    compacted_bytes: int = 0
    rewritten: bool = False
    error: Optional[str] = None

    @property
    def saved_bytes(self) -> int:
        return max(0, self.oversized_bytes - self.compacted_bytes)


@dataclass
class RunReport:
    files: List[FileReport] = field(default_factory=list)
    scanned_files: int = 0

    @property
    def failed(self) -> bool:
        return any(report.error for report in self.files)


def compact_line(line: bytes) -> Tuple[bytes, LineOutcome]:
    """Compact one physical line, or hand it back untouched.

    Returns ``(line_bytes, outcome)``. The returned bytes always carry the
    original line terminator (or none, for a file whose last line has no
    trailing newline) so the caller's write is positionally 1:1 with its read.
    Every failure mode -- unparseable JSON, a compaction error, a product that
    is not one valid JSON line, or a product that did not actually get smaller
    -- returns the input verbatim rather than a best effort.
    """
    if line.endswith(b"\n"):
        payload, terminator = line[:-1], b"\n"
    else:
        payload, terminator = line, b""

    raw_len = len(payload)
    if not record_budget.needs_compaction(raw_len):
        return line, LineOutcome(original_bytes=raw_len, new_bytes=raw_len)

    outcome = LineOutcome(oversized=True, original_bytes=raw_len, new_bytes=raw_len)
    try:
        message = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        outcome.failed = True
        return line, outcome

    try:
        compacted, stats = record_budget.compact_record(message, raw_len)
    except Exception:  # noqa: BLE001 -- a bad record must never abort the pass
        outcome.failed = True
        return line, outcome

    if not stats.compacted:
        return line, outcome

    try:
        text = _dumps(compacted)
    except (TypeError, ValueError):
        outcome.failed = True
        return line, outcome

    encoded = text.encode("utf-8")
    # A record occupies exactly one physical line; an embedded newline would
    # split it in two and shift every following ordinal.
    if b"\n" in encoded or b"\r" in encoded:
        outcome.failed = True
        return line, outcome
    if len(encoded) >= raw_len:
        return line, outcome
    try:
        json.loads(encoded)
    except ValueError:
        outcome.failed = True
        return line, outcome

    outcome.compacted = True
    outcome.new_bytes = len(encoded)
    return encoded + terminator, outcome


def count_physical_lines(path: Path) -> int:
    """Count physical lines the way the reader numbers ordinals.

    A trailing byte that is not a newline still terminates a line (the reader
    treats it as a partial tail, but it occupies an ordinal slot all the same).
    """
    total = 0
    last_byte = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_BYTES)
            if not chunk:
                break
            total += chunk.count(b"\n")
            last_byte = chunk[-1:]
    if last_byte and last_byte != b"\n":
        total += 1
    return total


def _has_oversized_line(path: Path) -> Tuple[bool, int]:
    """Cheap pre-scan: physical line count and whether any line is oversized.

    WHY: the overwhelming majority of history files hold nothing above the
    fast-path gate (measured: 0.95 % of records reach 64 KB). Deciding that from
    line lengths alone -- no JSON parse, no compaction -- keeps a pass over a
    278 MB directory a plain linear read, and keeps files that need nothing from
    being needlessly copied through a temp file.
    """
    total = 0
    oversized = False
    with open(path, "rb") as fh:
        for line in fh:
            total += 1
            payload_len = len(line) - 1 if line.endswith(b"\n") else len(line)
            if record_budget.needs_compaction(payload_len):
                oversized = True
    return oversized, total


def _stream(path: Path, writer: Optional[BinaryIO], report: FileReport) -> None:
    with open(path, "rb") as fh:
        for line in fh:
            new_line, outcome = compact_line(line)
            if writer is not None:
                writer.write(new_line)
            report.total_lines += 1
            if outcome.oversized:
                report.oversized_lines += 1
                report.oversized_bytes += outcome.original_bytes
                report.compacted_bytes += outcome.new_bytes
            if outcome.compacted:
                report.compacted_lines += 1
            if outcome.failed:
                report.failed_lines += 1


def _tmp_path(path: Path) -> Path:
    """Scratch path for *path*'s rewrite, named so nothing reads it as history.

    INVARIANT: the temp file must be invisible to every name-based history
    stream discovery -- the daemon reader's ``_iter_history_jsonl``
    (``src/tianluo/daemon/history.py``) globs ``*.jsonl`` *and*
    ``*.jsonl.from-*``, and so does :func:`_step_files` here. Appending a
    suffix to the source name is not enough: a sidecar's temp would still carry
    the ``.jsonl.from-`` substring, so a daemon running during the rewrite
    window would deliver the temp as an *extra* step stream (a phantom
    duplicate step in the WebUI, cached server-side under a step_id that stops
    existing the moment ``os.replace`` lands), and a temp orphaned by a killed
    run would be treated as a real target -- scanned, rewritten and counted --
    on the next pass.

    Percent-escaping every ``.`` leaves no ``.jsonl`` anywhere in the name while
    keeping the mapping injective, so two source files in one directory can
    never map onto the same temp, and deterministic, so a re-run reuses an
    orphan instead of accumulating a new one. It stays a sibling of the source
    because ``os.replace`` is only atomic within a single filesystem.
    """
    escaped = path.name.replace("%", "%25").replace(".", "%2E")
    return path.with_name(_TMP_PREFIX + escaped)


def process_file(path: Path, apply: bool) -> FileReport:
    """Scan (and with *apply*, rewrite) one history jsonl file."""
    report = FileReport(path=path)
    try:
        oversized, total_lines = _has_oversized_line(path)
    except OSError as exc:
        report.error = "read failed: %s" % exc
        return report

    if not oversized:
        report.total_lines = total_lines
        return report

    if not apply:
        try:
            _stream(path, None, report)
        except OSError as exc:
            report.error = "read failed: %s" % exc
        return report

    tmp_path = _tmp_path(path)
    try:
        with open(tmp_path, "wb") as writer:
            _stream(path, writer, report)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as exc:
        report.error = "rewrite failed: %s" % exc
        _unlink(tmp_path)
        return report

    # INVARIANT guard: a rewritten file must hold exactly as many physical lines
    # as the original, because ordinal == physical line number. Any mismatch
    # means the transform above lost, gained or split a line, which would
    # misalign every already-delivered ordinal in this flow -- so the rewrite is
    # abandoned and the original is left exactly as it was.
    try:
        source_lines = count_physical_lines(path)
        tmp_lines = count_physical_lines(tmp_path)
    except OSError as exc:
        report.error = "line-count verification failed: %s" % exc
        _unlink(tmp_path)
        return report

    if source_lines != tmp_lines or tmp_lines != report.total_lines:
        report.error = (
            "line-count mismatch (source=%d rewritten=%d processed=%d) -- "
            "original left untouched" % (source_lines, tmp_lines, report.total_lines)
        )
        _unlink(tmp_path)
        return report

    if report.compacted_lines == 0:
        # Nothing actually shrank: replacing the file would only churn mtime and
        # invalidate the reader's cached prefix state for no gain.
        _unlink(tmp_path)
        return report

    try:
        shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
    except OSError as exc:
        report.error = "replace failed: %s" % exc
        _unlink(tmp_path)
        return report

    report.rewritten = True
    return report


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _step_files(directory: Path) -> List[Path]:
    """Physical history streams inside one directory, in reader order.

    INVARIANT: this target set must match the daemon reader's
    ``_iter_history_jsonl`` (``src/tianluo/daemon/history.py``). The reader
    delivers a step's primary ``*.jsonl`` *and* its ``*.jsonl.from-<branch>``
    sidecars -- the shape ``luo merge``'s runtime sync writes when a --worktree
    flow's per-step history collides with the main project on merge-back -- as
    distinct streams. A plain ``*.jsonl`` glob misses the sidecars, so an
    oversized record parked in one would be invisible to the report and left on
    disk by ``--apply``, i.e. exactly the stored-size cost this backfill exists
    to remove. Sorting by name keeps a step's primary file ahead of its
    sidecars and orders steps by their ``NN_`` sequence prefix.
    """
    if not directory.is_dir():
        return []
    files = set(directory.glob("*.jsonl"))
    files.update(directory.glob("*.jsonl.from-*"))
    return sorted(files, key=lambda p: p.name)


def iter_targets(history_dir: Path, flow_id: Optional[str]) -> List[Path]:
    """History step files to consider, in a stable order."""
    if flow_id:
        return _step_files(history_dir / flow_id)
    if not history_dir.is_dir():
        return []
    targets: List[Path] = []
    for flow_dir in sorted(p for p in history_dir.iterdir() if p.is_dir()):
        targets.extend(_step_files(flow_dir))
    targets.extend(_step_files(history_dir))
    return targets


def _fmt_bytes(count: int) -> str:
    if count < 1024:
        return "%d B" % count
    value = count / 1024.0
    for unit in ("KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%d B" % count


def compact_history(
    history_dir: Path, apply: bool, flow_id: Optional[str] = None
) -> Tuple[int, RunReport]:
    run = RunReport()
    if not history_dir.is_dir():
        print("History directory not found: %s" % history_dir)
        return 1, run

    targets = iter_targets(history_dir, flow_id)
    action = "rewriting" if apply else "[DRY-RUN] would rewrite"
    for path in targets:
        report = process_file(path, apply)
        run.scanned_files += 1
        run.files.append(report)
        if report.error:
            print("  ERROR %s: %s" % (path, report.error))
            continue
        # An oversized line that turned out to fit the record budget changes
        # nothing, so it stays out of the per-file listing (the summary still
        # counts it) and the listing shows only files this pass would touch.
        if not (report.compacted_lines or report.failed_lines):
            continue
        print(
            "  %s %s: %d/%d lines over budget, %s -> %s (saves %s)%s"
            % (
                action,
                path,
                report.oversized_lines,
                report.total_lines,
                _fmt_bytes(report.oversized_bytes),
                _fmt_bytes(report.compacted_bytes),
                _fmt_bytes(report.saved_bytes),
                ", %d line(s) left as-is" % report.failed_lines
                if report.failed_lines
                else "",
            )
        )

    touched = [report for report in run.files if report.oversized_lines]
    total_lines = sum(report.total_lines for report in run.files)
    oversized_lines = sum(report.oversized_lines for report in run.files)
    compacted_lines = sum(report.compacted_lines for report in run.files)
    failed_lines = sum(report.failed_lines for report in run.files)
    saved = sum(report.saved_bytes for report in run.files)
    rewritten = sum(1 for report in run.files if report.rewritten)

    print("\n--- Compaction Report ---")
    print("History dir: %s" % history_dir)
    if flow_id:
        print("Flow filter: %s" % flow_id)
    print("Mode: %s" % ("APPLY (files rewritten)" if apply else "DRY-RUN (no writes)"))
    print("Files scanned:              %d" % run.scanned_files)
    print("Files with oversized lines: %d" % len(touched))
    print("Physical lines scanned:     %d" % total_lines)
    print("Lines over budget:          %d" % oversized_lines)
    print(
        "Lines %-22s%d" % (("compacted:" if apply else "compactable:"), compacted_lines)
    )
    print("Lines left as-is:           %d" % failed_lines)
    print("Bytes %-22s%s" % (("saved:" if apply else "savable:"), _fmt_bytes(saved)))
    print("Files rewritten:            %d" % rewritten)
    if not apply and oversized_lines:
        print("\nRe-run with --apply to rewrite these files in place.")
    if run.failed:
        print("\nOne or more files failed; originals were left untouched.")
        return 2, run
    return 0, run


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compact oversized records in tianluo history jsonl files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report only, write nothing (this is the default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually rewrite the files (1:1 per line, atomically replaced)",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="path to tianluo/history (default: <project_root>/tianluo/history)",
    )
    parser.add_argument(
        "--flow-id",
        type=str,
        default=None,
        help="restrict the pass to a single flow directory",
    )
    args = parser.parse_args(argv)

    # WHY: dry-run is the default and --dry-run always wins over --apply. This
    # script rewrites irreplaceable conversation history in place; the safe mode
    # has to be the one you get by accident, and an explicit "just tell me"
    # must never be overridden by an --apply left over in shell history.
    apply = bool(args.apply) and not args.dry_run

    if args.history_dir:
        history_dir = args.history_dir
    else:
        root = _project_root()
        history_dir = root / "tianluo" / "history"
        if not history_dir.is_dir() and (root / "se3" / "history").is_dir():
            history_dir = root / "se3" / "history"

    exit_code, _ = compact_history(history_dir.resolve(), apply, args.flow_id)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
