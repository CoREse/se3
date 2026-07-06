"""Tests for scripts/cleanup_project_roots.py (one-time registry cleanup).

Locks in the destructive script's decision boundary so a future change to
``PYTEST_RESIDUE_RE`` or ``cleanup`` can never silently:
  * start deleting legitimate project roots (the anchored-prefix matcher must
    only fire on ``/tmp/pytest-of-*`` / ``/var/tmp/pytest-of-*`` residue, never
    on a real path that merely contains a ``pytest-of-*`` segment),
  * fail to remove genuine pytest-tempdir residue,
  * write to the registry during ``--dry-run``, or
  * regress the ``--prune-missing`` existence-based pruning.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "cleanup_project_roots.py"
)
_spec = importlib.util.spec_from_file_location("cleanup_project_roots", _SCRIPT)
cleanup_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(cleanup_mod)


def _write_registry(path: Path, roots: list[str]) -> None:
    path.write_text(
        json.dumps({"project_roots": roots}, indent=2), encoding="utf-8"
    )


def _read_roots(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["project_roots"]


# A synthetic *non-residue* project root. NOTE: paths built from ``tmp_path``
# cannot serve as "real roots" here — the pytest tmpdir factory nests tmp_path
# under ``/tmp/pytest-of-<user>/``, so such a path is itself matched by
# ``PYTEST_RESIDUE_RE`` and would (correctly) be pruned as residue.
_REAL_ROOT = "/data/cre/workspace/real_project"
# The repo root is a genuinely-existing, non-residue directory — used where a
# path must survive ``--prune-missing`` (os.path.exists) checks.
_EXISTING_REAL_ROOT = str(Path(__file__).resolve().parents[1])


# --- the residue matcher (unit-level) ---------------------------------------

def test_pytest_residue_matches_tmp_and_var_tmp_prefixes():
    assert cleanup_mod._is_pytest_residue(
        "/tmp/pytest-of-cre/pytest-1212/test_x/repo"
    )
    assert cleanup_mod._is_pytest_residue(
        "/var/tmp/pytest-of-alice/pytest-3/test_y/repo"
    )


def test_pytest_residue_is_anchored_not_substring():
    # A real project root that merely CONTAINS a ``pytest-of-*`` component must
    # never be classified as residue -- the matcher is anchored to the OS
    # tempdir prefix, not a bare segment match anywhere in the path.
    assert not cleanup_mod._is_pytest_residue("/home/cre/projects/pytest-of-cre/demo")
    assert not cleanup_mod._is_pytest_residue("/data/cre/workspace/se3.0")
    assert not cleanup_mod._is_pytest_residue("/opt/tmp/pytest-of-cre/x")


# --- cleanup(): pytest-residue removal --------------------------------------

def test_cleanup_removes_pytest_residue_keeps_real_roots(tmp_path):
    reg = tmp_path / "project_roots.json"
    _write_registry(
        reg,
        [
            _REAL_ROOT,
            "/tmp/pytest-of-cre/pytest-1212/test_x/repo",
            "/var/tmp/pytest-of-cre/pytest-9/test_y/repo",
        ],
    )

    rc = cleanup_mod.cleanup(reg, dry_run=False, prune_missing=False)

    assert rc == 0
    assert _read_roots(reg) == [_REAL_ROOT]


def test_cleanup_preserves_non_pytest_missing_root_without_prune(tmp_path):
    # Without --prune-missing, a non-existent but non-residue path is kept.
    reg = tmp_path / "project_roots.json"
    missing = "/data/cre/workspace/gone"
    _write_registry(reg, [missing])

    cleanup_mod.cleanup(reg, dry_run=False, prune_missing=False)

    assert _read_roots(reg) == [missing]


# --- cleanup(): dry-run never writes ----------------------------------------

def test_dry_run_does_not_modify_registry(tmp_path):
    reg = tmp_path / "project_roots.json"
    roots = [
        _REAL_ROOT,
        "/tmp/pytest-of-cre/pytest-1/test_x/repo",
    ]
    _write_registry(reg, roots)
    before = reg.read_text(encoding="utf-8")

    rc = cleanup_mod.cleanup(reg, dry_run=True, prune_missing=False)

    assert rc == 0
    assert reg.read_text(encoding="utf-8") == before  # byte-for-byte unchanged


# --- cleanup(): --prune-missing ---------------------------------------------

def test_prune_missing_drops_vanished_roots_keeps_existing(tmp_path):
    reg = tmp_path / "project_roots.json"
    # A genuinely-existing, non-residue dir (the repo root) must survive the
    # existence check; a non-residue non-existent path must be pruned.
    _write_registry(reg, [_EXISTING_REAL_ROOT, "/data/cre/workspace/gone"])

    cleanup_mod.cleanup(reg, dry_run=False, prune_missing=True)

    assert _read_roots(reg) == [_EXISTING_REAL_ROOT]


def test_prune_missing_dry_run_does_not_write(tmp_path):
    reg = tmp_path / "project_roots.json"
    _write_registry(reg, [_EXISTING_REAL_ROOT, "/data/cre/workspace/gone"])
    before = reg.read_text(encoding="utf-8")

    cleanup_mod.cleanup(reg, dry_run=True, prune_missing=True)

    assert reg.read_text(encoding="utf-8") == before


# --- cleanup(): no-op / edge cases ------------------------------------------

def test_no_removals_leaves_file_untouched(tmp_path):
    reg = tmp_path / "project_roots.json"
    _write_registry(reg, [_REAL_ROOT])
    before = reg.read_text(encoding="utf-8")

    cleanup_mod.cleanup(reg, dry_run=False, prune_missing=False)

    # Nothing removed -> file is not rewritten (byte-for-byte identical).
    assert reg.read_text(encoding="utf-8") == before


def test_missing_registry_is_noop(tmp_path):
    reg = tmp_path / "does_not_exist.json"
    rc = cleanup_mod.cleanup(reg, dry_run=False, prune_missing=False)
    assert rc == 0
    assert not reg.exists()


def test_atomic_rewrite_leaves_no_tmp_file(tmp_path):
    reg = tmp_path / "project_roots.json"
    _write_registry(
        reg,
        [_REAL_ROOT, "/tmp/pytest-of-cre/pytest-1/test_x/repo"],
    )

    cleanup_mod.cleanup(reg, dry_run=False, prune_missing=False)

    # os.replace should have consumed the ".tmp" sidecar.
    assert not (tmp_path / "project_roots.json.tmp").exists()


# --- main(): --registry wiring ----------------------------------------------

def test_main_honours_registry_flag(tmp_path):
    reg = tmp_path / "project_roots.json"
    _write_registry(reg, [_REAL_ROOT, "/tmp/pytest-of-cre/pytest-2/test/repo"])

    rc = cleanup_mod.main(["--registry", str(reg)])

    assert rc == 0
    assert _read_roots(reg) == [_REAL_ROOT]
