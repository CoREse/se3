"""Pre-implement test baseline capture, keying, and caching.

This module measures the set of *failing tests at flow start* — before the
``implement`` step modifies anything — so that the ``test`` and ``verify_spec``
steps can distinguish **inherited** failures (already red before this flow) from
**introduced** failures (this flow's regressions). Only introduced failures may
drive the fix loop; inherited ones are surfaced (留痕) but never looped.

Two responsibilities live here:

1. :func:`compute_baseline_key` + :func:`load_cached` / :func:`save_cache` — a
   deterministic cache keyed by the git HEAD sha plus a working-tree "dirty"
   hash, so parallel/resumed flows on the same commit reuse the measured
   baseline. A corrupt or missing cache file safely reads as a miss.
2. :class:`BaselineCapture` — launches the full test suite as a **background**
   subprocess (concurrently with the LLM-bound ``analyze → plan → confirm``
   steps), then resolves the failing test-id set via :func:`wait`. Capture
   failure (the subprocess could not run / produced no parseable output) is
   reported as the sentinel ``None`` so the caller can fall back to a synchronous
   re-measurement.

The module is intentionally separate from ``steps/test.py``: capture happens at
flow start (state-machine side, before ``implement``), while consumption happens
inside the ``test`` / ``verify_spec`` steps. Keeping it standalone avoids a
reverse import from the state machine into the test step. It reuses
``steps/test.py``'s ``_parse_test_ids`` / ``_detect_test_command`` so parsing and
the command stay identical to the real test step.

The cache lives at ``se3/state/test_baseline_cache.json`` — gitignored by the
``/se3/*`` rule and therefore never tracked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASELINE_CACHE_SCHEMA_VERSION = 1
_CACHE_REL_PATH = Path("se3") / "state" / "test_baseline_cache.json"

# Wall-clock bound (seconds) for a pre-implement baseline run when the project's
# ``test.timeout`` cannot be read. Mirrors the test step's own fallback timeout
# default so a hung baseline subprocess is killed rather than blocking the flow
# forever right before ``implement``.
DEFAULT_BASELINE_TIMEOUT = 1800.0

# Grace period (seconds) to wait for a killed baseline subprocess to actually
# exit before giving up on reaping it.
_KILL_GRACE_SECONDS = 5.0

# Bound the on-disk cache so a long-lived serial commit-per-flow pipeline (where
# almost every flow lands on a fresh commit → a fresh key) cannot grow the file
# without limit. Most-recently-saved keys are retained (insertion-order LRU).
MAX_CACHE_ENTRIES = 50


# ---------------------------------------------------------------------------
# Baseline key (git HEAD sha + working-tree dirty hash)
# ---------------------------------------------------------------------------

def compute_baseline_key(project_root: Path) -> str:
    """Return a deterministic key identifying the repo state at flow start.

    The key combines the current git ``HEAD`` sha with a content hash of the
    working-tree "dirty" state (staged + unstaged tracked changes, plus
    untracked non-ignored files). Either a HEAD change or any working-tree
    content change therefore yields a different key, so a stale baseline can
    never be reused across a content change.

    Overhead is controlled: rather than hashing every file in the tree, the
    dirty hash is derived from ``git diff HEAD`` (tracked changes) and the
    content of untracked non-ignored files only — typically a small set.

    Always returns a non-empty string; when the directory is not a git repo the
    HEAD component degrades to ``"no-head"`` and the key still hashes any
    untracked content, so callers never have to special-case the failure path.
    """
    head = _git_head_sha(project_root)
    dirty = _working_tree_dirty_hash(project_root)
    return f"{head}:{dirty}"


def _git_head_sha(project_root: Path) -> str:
    """Return the current git HEAD sha, or ``"no-head"`` when unavailable."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.debug("git rev-parse HEAD failed: %s", exc)
        return "no-head"
    return out.strip() or "no-head"


def _file_content_hash(path: Path) -> Optional[str]:
    """Return SHA-256 of *path* contents, or None if it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _working_tree_dirty_hash(project_root: Path) -> str:
    """Hash the working-tree's dirty state (tracked diff + untracked content).

    Captures:
    - ``git diff HEAD`` (raw bytes): all staged + unstaged modifications to
      tracked files, which is content-sensitive (a single byte change flips it).
    - untracked, non-ignored files: each rel-path plus its content hash, sorted
      for determinism.

    The ``se3/state/test_baseline_cache.json`` file this module writes is
    gitignored and therefore never appears in either source, so writing the
    cache cannot perturb the key (no self-feedback).
    """
    root = Path(project_root).resolve()
    hasher = hashlib.sha256()

    # Tracked changes (staged + unstaged) relative to HEAD.
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        ).stdout or b""
        hasher.update(b"diff\0")
        hasher.update(diff)
    except (FileNotFoundError, OSError) as exc:
        logger.debug("git diff HEAD failed: %s", exc)
        hasher.update(b"diff-unavailable\0")

    # Untracked, non-ignored files (names + content).
    for rel_path in _git_untracked_files(root):
        hasher.update(b"u\0")
        hasher.update(rel_path.encode("utf-8", "surrogatepass"))
        ch = _file_content_hash(root / rel_path)
        hasher.update(b"\0")
        hasher.update((ch or "missing").encode("ascii"))
        hasher.update(b"\n")

    return hasher.hexdigest()


def _git_untracked_files(root: Path) -> List[str]:
    """Return sorted relative paths of untracked, non-ignored files."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=str(root),
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.debug("git ls-files --others failed: %s", exc)
        return []
    return sorted(p for p in out.split("\0") if p)


# ---------------------------------------------------------------------------
# Cache read / write (atomic, corruption-tolerant)
# ---------------------------------------------------------------------------

def cache_path(project_root: Path) -> Path:
    """Return the path to the baseline cache file for *project_root*."""
    return Path(project_root) / _CACHE_REL_PATH


def _read_cache_entries(path: Path) -> "dict":
    """Best-effort read of the cache's ``entries`` map.

    Returns an empty dict on any problem (missing file, corrupt JSON,
    unexpected shape, schema mismatch), so a damaged cache reads as a clean miss
    rather than raising.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read baseline cache at %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("schema_version") != BASELINE_CACHE_SCHEMA_VERSION:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def load_cached(project_root: Path, key: str) -> Optional[Set[str]]:
    """Return the cached set of failing test ids for *key*, or None on miss.

    A miss is any of: no cache file, corrupt JSON, schema mismatch, or the key
    not present. The returned value is a (possibly empty) ``set[str]`` on a hit
    — an empty set means "this commit was measured and had zero failures",
    which is distinct from a ``None`` miss.
    """
    entries = _read_cache_entries(cache_path(project_root))
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return None
    failures = entry.get("failures")
    if not isinstance(failures, list):
        return None
    return {str(x) for x in failures}


def save_cache(project_root: Path, key: str, failures: Set[str]) -> Path:
    """Atomically persist the baseline *failures* set under *key*.

    Existing entries for other keys are preserved. The cache is bounded to
    ``MAX_CACHE_ENTRIES`` most-recently-saved keys (insertion-order LRU). Uses a
    tempfile + ``os.replace`` so a mid-write crash never leaves a half-written
    file behind.
    """
    path = cache_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    entries = dict(_read_cache_entries(path))
    # Move the key to the end (most-recent) on re-save.
    entries.pop(key, None)
    entries[key] = {"failures": sorted({str(x) for x in failures})}

    # Trim oldest entries beyond the cap.
    while len(entries) > MAX_CACHE_ENTRIES:
        oldest_key = next(iter(entries))
        entries.pop(oldest_key)

    payload = json.dumps(
        {"schema_version": BASELINE_CACHE_SCHEMA_VERSION, "entries": entries},
        indent=2,
        ensure_ascii=False,
    )

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".test_baseline_cache.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.debug("Wrote baseline cache to %s (key=%s, %d failures)", path, key, len(failures))
    return path


# ---------------------------------------------------------------------------
# Timeout resolution
# ---------------------------------------------------------------------------

def resolve_baseline_timeout(project_root: Path) -> float:
    """Return the wall-clock bound (seconds) for a pre-implement baseline run.

    Reuses ``test.timeout`` from ``se3.yaml`` (the suite fallback timeout,
    default 1800s) so the baseline run is bounded by the same ceiling the real
    test step applies to a full suite run. On expiry the baseline subprocess is
    killed (see :meth:`BaselineCapture.wait_or_kill`) instead of letting a hung
    test block the flow forever before ``implement``.

    Any failure to read the config degrades to :data:`DEFAULT_BASELINE_TIMEOUT`.
    """
    try:
        from ..config import TestConfig

        timeout = float(TestConfig.load(project_root).timeout)
        if timeout > 0:
            return timeout
    except Exception as exc:  # noqa: BLE001 — never let config errors block the flow
        logger.debug("Could not resolve baseline timeout from config: %s", exc)
    return DEFAULT_BASELINE_TIMEOUT


# ---------------------------------------------------------------------------
# Background capture
# ---------------------------------------------------------------------------

class BaselineCapture:
    """Run the test suite in the background and resolve the failing-id set.

    Usage::

        capture = BaselineCapture(project_root).launch()
        # ... do the LLM-bound analyze/plan/confirm steps concurrently ...
        failures = capture.wait()          # blocks until the suite finishes
        if failures is None:               # capture failed → fall back
            ...

    ``wait`` returns a ``set[str]`` of failing test ids on success (empty when
    every test passed), or the sentinel ``None`` when the capture itself failed
    (the subprocess could not be launched, or produced no parseable per-test
    output while exiting non-zero — typically a collection/infra error). The
    sentinel lets the caller fall back to a synchronous re-measurement instead of
    treating an unmeasured baseline as "no failures" (which would revive the
    infinite fix loop).
    """

    def __init__(self, project_root: Path, command: Optional[List[str]] = None) -> None:
        self.project_root = Path(project_root)
        self._command_override = command
        self._command_used: Optional[List[str]] = None
        self._process: Optional[subprocess.Popen] = None
        self._stdout_path: Optional[Path] = None
        self._stdout_file = None
        self._launch_error: Optional[BaseException] = None
        self._done = False
        self._result: Optional[Set[str]] = None
        self._timed_out = False

    # -- lifecycle ---------------------------------------------------------

    def launch(self) -> "BaselineCapture":
        """Start the background test subprocess. Idempotent and never raises.

        Sets ``SE3_TEST_RUNNING=1`` in the child environment so that any test
        which itself invokes the se3 test handler is short-circuited rather than
        recursing into another full suite run. A launch failure is recorded and
        surfaced later as the ``None`` sentinel from :meth:`wait`.
        """
        if self._process is not None or self._launch_error is not None:
            return self  # already launched
        try:
            command = self._command_override or self._resolve_command()
            env = dict(os.environ)
            env["SE3_TEST_RUNNING"] = "1"

            fd, tmp = tempfile.mkstemp(prefix="se3_baseline_", suffix=".out")
            os.close(fd)
            self._stdout_path = Path(tmp)
            self._stdout_file = open(tmp, "w", encoding="utf-8")

            self._process = subprocess.Popen(
                command,
                stdout=self._stdout_file,
                stderr=subprocess.STDOUT,
                cwd=str(self.project_root),
                env=env,
                text=True,
            )
            self._command_used = command
            logger.info("Baseline test capture launched: %s", " ".join(command))
        except Exception as exc:  # noqa: BLE001 — capture failure must not crash the flow
            self._launch_error = exc
            self._close_stdout_file()
            logger.warning("Failed to launch baseline test capture: %s", exc)
        return self

    def is_ready(self) -> bool:
        """Return True when :meth:`wait` will resolve without blocking.

        True when the subprocess has finished, when launch failed, or when no
        capture was ever launched (nothing to wait for). False only while a
        launched subprocess is still running.
        """
        if self._done:
            return True
        if self._launch_error is not None:
            return True
        if self._process is None:
            return True
        return self._process.poll() is not None

    def wait(self, timeout: Optional[float] = None) -> Optional[Set[str]]:
        """Block until the capture finishes, then return the failing-id set.

        Returns a ``set[str]`` on success (empty when no tests failed) or
        ``None`` when the capture failed (the upper layer should fall back to a
        synchronous re-measurement).

        With ``timeout`` set, raises :class:`subprocess.TimeoutExpired` if the
        subprocess has not finished in time, leaving the capture resumable
        (a later ``wait`` call will resolve it). With ``timeout=None`` it blocks
        until the subprocess exits. Repeated calls are idempotent — the resolved
        result is cached.
        """
        if self._done:
            return self._result
        if self._launch_error is not None or self._process is None:
            self._done = True
            self._result = None
            return None

        # Propagates subprocess.TimeoutExpired to the caller when timeout hits;
        # the capture stays unresolved so it can be waited on again.
        self._process.wait(timeout=timeout)

        self._close_stdout_file()
        self._result = self._parse_result()
        self._done = True
        return self._result

    @property
    def timed_out(self) -> bool:
        """True when the capture was killed because it exceeded its time bound.

        Distinguishes a hung-suite timeout (where re-running synchronously would
        just hang again) from an ordinary infra failure (a transient collection
        error that a synchronous retry might recover). The caller uses this to
        skip the synchronous fallback after a timeout and go straight to an empty
        baseline.
        """
        return self._timed_out

    def wait_or_kill(self, timeout: Optional[float] = None) -> Optional[Set[str]]:
        """Wait up to *timeout* seconds, killing the subprocess on expiry.

        Unlike :meth:`wait` (which raises :class:`subprocess.TimeoutExpired` and
        leaves the capture resumable), this *bounds* the pre-implement baseline
        run: when the subprocess does not finish within *timeout*, it is killed
        and the capture resolves to the ``None`` sentinel (and :attr:`timed_out`
        becomes True). This guarantees a hung test (deadlock, infinite loop,
        stuck IO) cannot block the flow forever before ``implement`` — the caller
        falls through to its synchronous / empty-baseline fallback instead.

        With ``timeout=None`` it behaves exactly like :meth:`wait` (unbounded).
        """
        if self._done:
            return self._result
        if timeout is None:
            return self.wait()
        try:
            return self.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning(
                "Baseline capture exceeded %.0fs; killing the subprocess and "
                "signalling synchronous fallback",
                timeout,
            )
            self._timed_out = True
            self.kill()
            return None

    def kill(self) -> None:
        """Terminate the background subprocess and resolve to the ``None`` sentinel.

        Best-effort: kill a still-running subprocess, reap it within a short
        grace period, drop the temp output file, and mark the capture as a failed
        measurement so :meth:`wait` / :meth:`wait_or_kill` return ``None``.
        Idempotent and never raises — safe to call whether or not the subprocess
        is still alive.
        """
        if self._done:
            return
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                try:
                    proc.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.debug("Baseline subprocess did not exit after kill")
            except Exception as exc:  # noqa: BLE001 — teardown must not crash the flow
                logger.debug("Baseline capture kill failed: %s", exc)
        self._cleanup_stdout_file()
        self._done = True
        self._result = None

    # -- internals ---------------------------------------------------------

    def _resolve_command(self) -> List[str]:
        """Resolve the test command, matching steps/test.py's detection.

        Uses ``test.command`` from se3.yaml when configured, else auto-detects
        via the test step's ``_detect_test_command``. Ensures a pytest command
        carries a per-test verbose flag so ``_parse_test_ids`` can read
        ``file::test PASSED/FAILED`` lines (the auto-detected command already
        includes ``-v``).
        """
        from ..config import TestConfig
        from .steps.test import _detect_test_command, _is_pytest_command, _VERBOSE_PYTEST_FLAGS

        config = TestConfig.load(self.project_root)
        if config.command:
            command = shlex.split(config.command)
        else:
            command = _detect_test_command(self.project_root)

        if _is_pytest_command(command) and not any(
            flag in command for flag in _VERBOSE_PYTEST_FLAGS
        ):
            command = [*command, "-v"]
        return command

    def _parse_result(self) -> Optional[Set[str]]:
        """Parse the captured output into a failing-id set, or None on failure."""
        from .steps.test import _parse_test_ids

        output = ""
        if self._stdout_path is not None:
            try:
                output = self._stdout_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("Could not read baseline capture output: %s", exc)
        self._cleanup_stdout_file()

        parsed = _parse_test_ids(output)
        failed = {tid for tid, passed in parsed if not passed}
        returncode = self._process.returncode if self._process else None

        if returncode == 0:
            # Suite ran to completion with success — failing set is whatever was
            # parsed (normally empty).
            return failed
        if parsed:
            # Non-zero exit but we have per-test results: the failing set is
            # authoritative.
            return failed
        # Non-zero exit and nothing parseable → collection/infra error. Signal
        # fallback rather than pretending the baseline is empty.
        logger.warning(
            "Baseline capture produced no parseable test results (returncode=%s); "
            "signalling synchronous fallback",
            returncode,
        )
        return None

    def _close_stdout_file(self) -> None:
        if self._stdout_file is not None:
            try:
                self._stdout_file.close()
            except OSError:
                pass
            self._stdout_file = None

    def _cleanup_stdout_file(self) -> None:
        """Remove the temp output file once it has been read."""
        self._close_stdout_file()
        if self._stdout_path is not None:
            try:
                os.unlink(self._stdout_path)
            except OSError:
                pass
            self._stdout_path = None
