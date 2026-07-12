"""SE3 Migrate — first-class, registry-based version/format migration channel.

``se3 migrate`` is the reusable entry point for one-shot schema / format
upgrades. It is deliberately a **registry** (``MIGRATORS``) so that future
schema bumps reuse the same skeleton (``se3 migrate list`` / ``se3 migrate run
<id>``) instead of growing a fresh ad-hoc command each time.

The shipped first migrator — :data:`SPEC_TO_NEW_SYSTEM` (id
``spec-to-new-system``) — performs the one-shot cutover from the retired
``se3/specs/`` spec corpus to the new **code-index + charter + why-comment**
triad. It runs as an ordered, each-step-independently-fault-tolerant pipeline
(mirroring :mod:`se3.commands.salvage_cmd`), with **salvage first**: the
why/intent worth keeping is extracted from the spec corpus *before* anything is
deleted, and the whole run lands as one reviewable, ``git revert``-able working
tree change (the command itself never commits — the engine's commit step does).

Pipeline (see :func:`run_spec_to_new_system`):

1. **assemble charter** — combine the shrunk, altitude-gated ``base`` spec with
   the cross-file / no-single-owner why/intent scanned out of the non-base
   specs, and write ``se3/charter.md`` exactly **once** (no overwrite window —
   the body is assembled fully in memory, then a single write lands it).
2. **colocate why-comments** — the code-location-bound why/intent extracted from
   the non-base specs is inserted as ``WHY``-comments into the corresponding
   source files.
3. **code-index first build** — a full ``code_index.build_index`` over the whole
   tree, producing the authoritative ``se3/code-index.md``.
4. **delete specs** — the entire ``se3/specs/`` tree is removed, but **only
   after** the charter assembly and colocation both succeeded (so nothing is
   deleted until the salvage is confirmed). Recoverability rides on git.
5. **rewrite .gitignore** — whitelist ``!/se3/code-index.md`` and
   ``!/se3/charter.md``, drop any ``!/se3/specs/`` whitelist (idempotent).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table

from ..engine import charter as charter_mod
from ..i18n import t

logger = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Salvage data model (the LLM-driven extraction surface, injectable for tests)
# ---------------------------------------------------------------------------

@dataclass
class ColocatedWhy:
    """A why/intent fragment bound to one concrete source file location.

    ``file_path`` is project-relative; ``why`` is the prose reason (the *why*,
    not the *what* — code already says the what) to colocate as a comment.
    """

    file_path: str
    why: str


@dataclass
class SalvageInput:
    """Everything the salvager needs, handed to it by the migrator.

    The migrator does the deterministic file reads and passes the corpus in, so
    the salvager (LLM or fake) never does its own discovery — keeping it pure
    and testable.
    """

    project_root: Path
    base_spec_text: str
    non_base_specs: Dict[str, str]  # spec name -> spec.md text
    admission_standard: str
    charter_template: str
    project_name: str


@dataclass
class SalvageResult:
    """The salvaged product: the assembled charter body + colocations.

    ``charter_body`` is the **full** ``se3/charter.md`` content (written once);
    ``colocations`` are the code-bound why/intent fragments to insert as
    comments; ``notes`` carry advisory messages for the human reviewer.
    """

    charter_body: str
    colocations: List[ColocatedWhy] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


#: A salvager maps the loaded spec corpus to a :class:`SalvageResult`. The
#: default is LLM-backed (:func:`_make_llm_salvager`); tests inject a fake.
SpecSalvager = Callable[[SalvageInput], SalvageResult]


# ---------------------------------------------------------------------------
# Migrator registry
# ---------------------------------------------------------------------------

@dataclass
class Migrator:
    """One registered migration.

    ``run`` is called as ``run(project_root, **opts)`` and returns a
    :class:`MigrationReport`.
    """

    id: str
    #: i18n key of the human-readable description. Resolved through ``t()`` at
    #: render time (registration happens at import, before the UI language is
    #: bound); a plain literal still works — ``t()`` returns an unknown key
    #: verbatim.
    description: str
    run: Callable[..., "MigrationReport"]


#: The registry of available migrations, keyed by id. Future schema/format
#: upgrades register here and are immediately reachable via ``se3 migrate``.
MIGRATORS: Dict[str, Migrator] = {}


def register_migrator(migrator: Migrator) -> Migrator:
    """Register *migrator* (id must be unique). Returns it for chaining."""
    if migrator.id in MIGRATORS:
        raise ValueError(f"migrator id already registered: {migrator.id!r}")
    MIGRATORS[migrator.id] = migrator
    return migrator


def get_migrator(migrator_id: str) -> Optional[Migrator]:
    """Return the registered migrator for *migrator_id*, or ``None``."""
    return MIGRATORS.get(migrator_id)


def list_migrators() -> List[Migrator]:
    """Return all registered migrators, sorted by id."""
    return [MIGRATORS[k] for k in sorted(MIGRATORS)]


# ---------------------------------------------------------------------------
# Migration report (each step's outcome)
# ---------------------------------------------------------------------------

@dataclass
class StepOutcome:
    name: str
    status: str  # "OK" | "SKIP" | "FAIL"
    detail: str = ""


@dataclass
class MigrationReport:
    results: List[StepOutcome] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(StepOutcome(name=name, status=status, detail=detail))

    @property
    def ok(self) -> bool:
        return not any(r.status == "FAIL" for r in self.results)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


# ---------------------------------------------------------------------------
# Spec corpus loading (deterministic, tolerant)
# ---------------------------------------------------------------------------

def _load_spec_corpus(specs_dir: Path) -> tuple[str, Dict[str, str]]:
    """Load the base spec text and the ``{name: text}`` non-base spec map.

    Tolerant: an unreadable / missing spec is skipped (its text omitted) rather
    than aborting the load. Directories whose name starts with ``_`` or ``.``
    (changelog / backlog / hidden) are skipped — they are not capability specs.
    """
    base_text = ""
    non_base: Dict[str, str] = {}
    if not specs_dir.is_dir():
        return base_text, non_base
    for child in sorted(specs_dir.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith("_") or name.startswith("."):
            continue
        spec_file = child / "spec.md"
        try:
            text = spec_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if name == "base":
            base_text = text
        else:
            non_base[name] = text
    return base_text, non_base


# ---------------------------------------------------------------------------
# Default LLM salvager (graceful degradation — never crashes a migration)
# ---------------------------------------------------------------------------

def _project_name_from_base(base_text: str, fallback: str) -> str:
    """Best-effort project name from the base spec title (``# X — ...``)."""
    for line in base_text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            for sep in (" — ", " - ", ":"):
                if sep in title:
                    title = title.split(sep, 1)[0].strip()
                    break
            # Drop a trailing "Specification" / "Base Specification" word.
            for tail in (" Base Specification", " Specification", " Base Spec"):
                if title.endswith(tail):
                    title = title[: -len(tail)].strip()
            return title or fallback
    return fallback


def _make_llm_salvager(project_root: Path) -> SpecSalvager:
    """Construct the default LLM-backed salvager.

    Two passes: (A) assemble the charter from the base spec + a digest of the
    non-base corpus under the admission standard; (B) per non-base spec, extract
    the code-location-bound why/intent. Every LLM call is defensive — a failure
    degrades that pass (the charter falls back to a review-flagged body that
    preserves the base spec verbatim; colocation falls back to empty) so the
    migration is never aborted by a flaky call.
    """

    def _salvage(inp: SalvageInput) -> SalvageResult:
        from ..engine.llm_caller import LLMCaller

        notes: List[str] = []
        caller = LLMCaller(project_root=project_root, step_type="migrate")

        # --- Pass A: assemble the charter -------------------------------------
        non_base_digest = "\n\n".join(
            f"### spec: {name}\n{text[:4000]}"
            for name, text in inp.non_base_specs.items()
        )
        charter_prompt = (
            "You are migrating a project from a spec corpus to a single "
            "high-altitude `charter`. Assemble the FULL content of "
            "`se3/charter.md`.\n\n"
            "Rules:\n"
            "- Shrink the base spec to only charter-admissible content, per the "
            "admission standard below.\n"
            "- Fold in ONLY cross-file / no-single-owner architectural why/intent "
            "from the other specs; per-module locators and per-symbol detail are "
            "DROPPED (they live in code-index now).\n"
            "- Output the complete markdown document and nothing else.\n\n"
            f"## Admission standard\n{inp.admission_standard}\n\n"
            f"## Charter template (structure to follow)\n{inp.charter_template}\n\n"
            f"## Base spec (shrink + altitude-filter this)\n{inp.base_spec_text}\n\n"
            f"## Other specs (mine cross-file why/intent only)\n{non_base_digest}\n"
        )
        try:
            charter_body = caller.call(charter_prompt, json_mode="off")
            charter_body = _strip_code_fence(charter_body).strip()
            if not charter_body:
                raise ValueError("empty charter body")
        except Exception as exc:  # noqa: BLE001 — never abort the migration
            logger.warning("migrate: charter LLM assembly failed: %s", exc)
            notes.append(t("migrate.note.charter_llm_failed"))
            charter_body = _fallback_charter(inp)

        # --- Pass B: colocate code-bound why/intent ---------------------------
        colocations: List[ColocatedWhy] = []
        for name, text in inp.non_base_specs.items():
            colo_prompt = (
                "From the spec below, extract ONLY the why/intent (the reason a "
                "thing is the way it is — never the what, code already says the "
                "what) that is bound to a SPECIFIC source file. Respond with a "
                "JSON array of objects {\"file_path\": <project-relative path>, "
                "\"why\": <one or two sentences>}. Omit anything not tied to a "
                "concrete file. Empty array if none.\n\n"
                f"## spec: {name}\n{text[:8000]}"
            )
            try:
                raw = caller.call(colo_prompt, json_mode="two_phase")
                parsed = json.loads(_strip_code_fence(raw)) if isinstance(raw, str) else []
            except Exception as exc:  # noqa: BLE001
                logger.warning("migrate: colocation extract failed for %s: %s", name, exc)
                parsed = []
            for item in parsed if isinstance(parsed, list) else []:
                if not isinstance(item, dict):
                    continue
                fp = str(item.get("file_path", "")).strip()
                why = str(item.get("why", "")).strip()
                if fp and why:
                    colocations.append(ColocatedWhy(file_path=fp, why=why))

        return SalvageResult(
            charter_body=charter_body, colocations=colocations, notes=notes
        )

    return _salvage


def _strip_code_fence(text: str) -> str:
    """Strip a single wrapping ```` ``` ```` fence if the body is fenced."""
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines)
    return text


def _fallback_charter(inp: SalvageInput) -> str:
    """A degraded charter that loses no information: the rendered template plus
    the base spec verbatim under a loud review banner."""
    try:
        head = charter_mod.render_charter_template(
            project_name=inp.project_name,
            project_description="(migrated — review)",
            languages_and_frameworks="(migrated — review)",
            top_level_architecture="(migrated — review the salvaged base content below)",
            coding_conventions="(migrated — review)",
            key_constraints="(migrated — review)",
            workflow_conventions="(migrated — review)",
        )
    except Exception:  # noqa: BLE001
        head = f"# {inp.project_name} — Charter\n"
    banner = (
        "\n\n<!-- MIGRATION REVIEW: the LLM charter assembly was unavailable. "
        "The pre-migration base spec is preserved verbatim below so no content "
        "is lost. A human should shrink it to charter-admissible altitude and "
        "delete this banner. -->\n\n"
        "## Salvaged base spec (review and shrink)\n\n"
    )
    return head + banner + inp.base_spec_text


# ---------------------------------------------------------------------------
# Why-comment colocation (insert into the corresponding source file)
# ---------------------------------------------------------------------------

#: Marker prefix so a colocated comment is unmistakable and reviewable.
WHY_MARKER = "WHY (salvaged from spec during migration):"

_COMMENT_PREFIX_BY_EXT = {
    ".py": "#", ".pyi": "#", ".sh": "#", ".bash": "#", ".rb": "#",
    ".yaml": "#", ".yml": "#", ".toml": "#", ".cfg": "#",
    ".js": "//", ".ts": "//", ".jsx": "//", ".tsx": "//",
    ".go": "//", ".c": "//", ".h": "//", ".cpp": "//", ".rs": "//",
    ".java": "//",
}


def _comment_prefix(path: Path) -> str:
    return _COMMENT_PREFIX_BY_EXT.get(path.suffix.lower(), "#")


def _render_why_comment(prefix: str, why: str) -> List[str]:
    lines = [f"{prefix} {WHY_MARKER}"]
    for ln in why.splitlines() or [why]:
        lines.append(f"{prefix} {ln}".rstrip())
    return lines


def _insert_why_comment(path: Path, why: str) -> None:
    """Insert a why-comment at a safe top-of-file location.

    A leading shebang (``#!``) is preserved on line 1; otherwise the comment is
    inserted at the very top. Comments do not count as Python statements, so a
    following module docstring stays a docstring and any ``from __future__``
    import stays valid.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    prefix = _comment_prefix(path)
    block = "\n".join(_render_why_comment(prefix, why)) + "\n"

    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    lines.insert(insert_at, block)
    path.write_text("".join(lines), encoding="utf-8")


def _apply_colocations(
    project_root: Path, colocations: List[ColocatedWhy]
) -> tuple[int, List[str]]:
    """Insert every colocation's why-comment; return (applied_count, skipped).

    A colocation whose target file does not exist (or is binary/unreadable) is
    skipped with a recorded reason rather than aborting the step.
    """
    applied = 0
    skipped: List[str] = []
    for colo in colocations:
        rel = colo.file_path.replace("\\", "/").lstrip("/")
        target = project_root / rel
        if not target.is_file():
            skipped.append(f"{rel}: {t('migrate.reason.file_not_found')}")
            continue
        try:
            _insert_why_comment(target, colo.why)
            applied += 1
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(f"{rel}: {exc}")
    return applied, skipped


# ---------------------------------------------------------------------------
# Charter single-write
# ---------------------------------------------------------------------------

def _write_charter_once(project_root: Path, charter_body: str) -> Path:
    """Write the fully-assembled charter body in a single write (no overwrite
    window): the body is assembled in memory first, then one ``write_text``."""
    path = charter_mod.charter_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = charter_body if charter_body.endswith("\n") else charter_body + "\n"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# .gitignore rewrite (idempotent)
# ---------------------------------------------------------------------------

_GITIGNORE_WHITELISTS = [
    "!/se3/code-index.md",
    "!/se3/charter.md",
    # Version-reconcile intent metadata: committed on the flow branch so the
    # merge-side reconcile step can read every merged-in branch's intent from
    # master. Must be tracked, unlike the rest of se3/ runtime content.
    "!/se3/version-intents/",
]
_GITIGNORE_REMOVE = "!/se3/specs/"

_GITIGNORE_ROOT_DENY_HEADER = [
    "# Repository root: ignore everything by default; whitelist tracked entries.",
    "# Stray root files (logs, scratch, caches) must not be auto-committed. To",
    "# track a new top-level entry add an explicit `!/<name>` (file) or",
    "# `!/<name>/` (dir) line below.",
]


def _tracked_toplevel_whitelists(project_root: Path) -> Optional[List[str]]:
    """Return ``!/<name>`` whitelist lines for every git-tracked top-level path.

    Directories get a trailing slash (``!/<name>/``), files do not. ``.gitignore``
    is always whitelisted first so the root default-deny rule never hides the
    very file that defines it.

    Returns ``None`` when ``git ls-files`` is unavailable or fails. The caller
    treats ``None`` as 'do not introduce ``/*``': a root default-deny rule is
    never added without the matching existence-protection, so existing projects
    can never silently lose tracking of a top-level path.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    dirs: set = set()
    files: set = set()
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        head, sep, _ = line.partition("/")
        if sep:
            dirs.add(head)
        else:
            files.add(head)

    # A real filesystem cannot have a top-level name be both a file and a dir;
    # if git ever reports both, prefer the directory form.
    entries = [f"!/{name}/" for name in sorted(dirs)]
    entries += [f"!/{name}" for name in sorted(files) if name not in dirs]

    # .gitignore must lead so it is tracked even on the (degenerate) chance it
    # is not yet in the index.
    ordered = ["!/.gitignore"]
    ordered += [e for e in entries if e != "!/.gitignore"]
    return ordered


def _rewrite_gitignore(project_root: Path) -> List[str]:
    """Add the code-index/charter whitelists, drop the specs whitelist, and put
    the repository root into default-deny (``/*``) form with existence-protected
    top-level whitelists.

    Idempotent: re-running makes no further change. Returns a human-readable
    list of the changes made (empty when already migrated). When ``/se3/*`` is
    present the whitelists are inserted right after it so the negations take
    effect; otherwise they are appended.

    The root ``/*`` default-deny is introduced only once and only after
    enumerating every currently-tracked top-level path (via ``git ls-files``)
    and emitting an explicit ``!/<name>`` for each — so an existing project can
    never silently lose tracking. If that enumeration is unavailable the rule is
    skipped entirely (tolerant degrade).
    """
    path = project_root / ".gitignore"
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError):
        original = ""

    lines = original.splitlines()
    stripped = {ln.strip() for ln in lines}
    changes: List[str] = []

    # Remove the retired specs whitelist.
    if _GITIGNORE_REMOVE in stripped:
        lines = [ln for ln in lines if ln.strip() != _GITIGNORE_REMOVE]
        changes.append(t("migrate.gitignore.removed", pattern=_GITIGNORE_REMOVE))
        stripped.discard(_GITIGNORE_REMOVE)

    # Find the `/se3/*` anchor to insert new whitelists right after it.
    missing = [w for w in _GITIGNORE_WHITELISTS if w not in stripped]
    if missing:
        anchor = next((i for i, ln in enumerate(lines) if ln.strip() == "/se3/*"), None)
        if anchor is not None:
            for offset, w in enumerate(missing):
                lines.insert(anchor + 1 + offset, w)
        else:
            lines.extend(missing)
        changes.extend(t("migrate.gitignore.added", pattern=w) for w in missing)

    # Introduce root default-deny (`/*`) with existence-protection. Skipped when
    # `/*` already present (idempotent) or when the tracked-path enumeration is
    # unavailable (tolerant degrade — never add `/*` without its whitelist).
    if "/*" not in stripped:
        protected = _tracked_toplevel_whitelists(project_root)
        if protected is not None:
            block = [*_GITIGNORE_ROOT_DENY_HEADER, "/*", *protected, ""]
            lines = block + lines
            stripped.add("/*")
            stripped.update(protected)
            changes.append(t("migrate.gitignore.root_deny"))

    if changes:
        new_text = "\n".join(lines)
        if original.endswith("\n") or not original:
            new_text += "\n"
        path.write_text(new_text, encoding="utf-8")
    return changes


# ---------------------------------------------------------------------------
# First migrator: spec -> code-index + charter + why-comments
# ---------------------------------------------------------------------------

def run_spec_to_new_system(
    project_root: Path,
    *,
    salvager: Optional[SpecSalvager] = None,
    summarizer=None,
    delete_specs: bool = True,
) -> MigrationReport:
    """Execute the one-shot spec -> new-system migration (each step tolerant).

    Ordered pipeline; deletion of ``se3/specs/`` happens ONLY after the charter
    assembly and colocation have both succeeded (salvage confirmed). The command
    never commits — all changes land in the working tree as one reviewable,
    ``git revert``-able change.

    Args:
        project_root: Project root.
        salvager: Injectable salvage function (default: LLM-backed).
        summarizer: Injectable code-index summariser (default: LLM-backed).
        delete_specs: When False, the specs tree is kept (dry-run-ish).
    """
    from ..engine import code_index

    project_root = Path(project_root)
    report = MigrationReport()
    specs_dir = project_root / "se3" / "specs"

    base_text, non_base = _load_spec_corpus(specs_dir)

    # --- Step 1: assemble + single-write the charter ----------------------
    charter_ok = False
    salvage_result: Optional[SalvageResult] = None
    try:
        sal = salvager or _make_llm_salvager(project_root)
        salvage_result = sal(
            SalvageInput(
                project_root=project_root,
                base_spec_text=base_text,
                non_base_specs=non_base,
                admission_standard=charter_mod.CHARTER_ADMISSION_STANDARD,
                charter_template=charter_mod.load_charter_template(),
                project_name=_project_name_from_base(base_text, project_root.name),
            )
        )
        _write_charter_once(project_root, salvage_result.charter_body)
        charter_ok = True
        report.notes.extend(salvage_result.notes)
        report.add(
            t("migrate.step.charter"), "OK", t("migrate.detail.charter_written")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("migrate: charter assembly failed: %s", exc)
        report.add(t("migrate.step.charter"), "FAIL", str(exc)[:80])

    # --- Step 2: colocate code-bound why-comments -------------------------
    colocate_ok = False
    try:
        if salvage_result is None:
            report.add(
                t("migrate.step.colocate"), "SKIP", t("migrate.detail.no_salvage")
            )
        else:
            applied, skipped = _apply_colocations(
                project_root, salvage_result.colocations
            )
            # Deletion of the spec corpus is gated on the code-bound why/intent
            # having actually been salvaged into source files. A skipped
            # colocation (e.g. its target source file could not be resolved)
            # means that intent never landed anywhere, so it is NOT safe to
            # delete the spec that carries it. Only an all-applied result — or a
            # genuinely empty colocation set (nothing code-bound to salvage) —
            # confirms the salvage; any skip keeps colocate_ok False so specs
            # are preserved.
            colocate_ok = not skipped
            detail = t("migrate.detail.colocated", count=applied)
            if skipped:
                detail += t("migrate.detail.colocate_skipped", count=len(skipped))
                report.notes.extend(
                    t("migrate.note.colocation_skipped", item=s) for s in skipped
                )
            report.add(t("migrate.step.colocate"), "OK", detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("migrate: colocation failed: %s", exc)
        report.add(t("migrate.step.colocate"), "FAIL", str(exc)[:80])

    # --- Step 3: code-index first build -----------------------------------
    try:
        code_index.build_index(project_root, summarizer=summarizer, force=True)
        report.add(
            t("migrate.step.code_index"), "OK",
            t("migrate.detail.code_index_written"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("migrate: code-index build failed: %s", exc)
        report.add(t("migrate.step.code_index"), "FAIL", str(exc)[:80])

    # --- Step 4: delete specs (ONLY after salvage confirmed) --------------
    try:
        if not specs_dir.exists():
            report.add(
                t("migrate.step.delete_specs"), "SKIP",
                t("migrate.detail.no_specs_dir"),
            )
        elif not delete_specs:
            report.add(
                t("migrate.step.delete_specs"), "SKIP",
                t("migrate.detail.delete_disabled"),
            )
        elif not (charter_ok and colocate_ok):
            report.add(
                t("migrate.step.delete_specs"), "SKIP",
                t("migrate.detail.salvage_incomplete"),
            )
        else:
            shutil.rmtree(specs_dir)
            report.add(
                t("migrate.step.delete_specs"), "OK",
                t("migrate.detail.specs_removed"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("migrate: delete specs failed: %s", exc)
        report.add(t("migrate.step.delete_specs"), "FAIL", str(exc)[:80])

    # --- Step 5: rewrite .gitignore ---------------------------------------
    try:
        changes = _rewrite_gitignore(project_root)
        if changes:
            report.add(t("migrate.step.gitignore"), "OK", "; ".join(changes))
        else:
            report.add(
                t("migrate.step.gitignore"), "OK",
                t("migrate.detail.already_migrated"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("migrate: gitignore rewrite failed: %s", exc)
        report.add(t("migrate.step.gitignore"), "FAIL", str(exc)[:80])

    return report


# Register the first migrator.
SPEC_TO_NEW_SYSTEM = register_migrator(
    Migrator(
        id="spec-to-new-system",
        description="migrate.migrator.spec_to_new_system.desc",
        run=run_spec_to_new_system,
    )
)


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------

def _display_report(report: MigrationReport, migrator_id: str) -> None:
    table = Table(title=t("migrate.report.title", migrator_id=migrator_id))
    table.add_column(t("migrate.report.col_step"), style="cyan")
    table.add_column(t("migrate.report.col_status"), style="bold")
    table.add_column(t("migrate.report.col_detail"))
    styles = {
        "OK": t("migrate.report.status_ok"),
        "SKIP": t("migrate.report.status_skip"),
        "FAIL": t("migrate.report.status_fail"),
    }
    for r in report.results:
        table.add_row(r.name, styles.get(r.status, r.status), r.detail)
    console.print()
    console.print(table)
    if report.notes:
        console.print()
        console.print(t("migrate.report.notes_header"))
        for note in report.notes:
            console.print(t("migrate.report.note_line", note=note))
    console.print()


# ---------------------------------------------------------------------------
# Typer sub-app
# ---------------------------------------------------------------------------

migrate_app = typer.Typer(
    name="migrate",
    help=t("cli.help.migrate.app"),
)


@migrate_app.command(name="list", help=t("cli.help.migrate.list.desc"))
def list_command() -> None:
    """List the available migrations."""
    migrators = list_migrators()
    if not migrators:
        console.print(t("migrate.list.none"))
        raise typer.Exit(0)
    table = Table(title=t("migrate.list.title"))
    table.add_column(t("migrate.list.col_id"), style="cyan")
    table.add_column(t("migrate.list.col_description"))
    for m in migrators:
        table.add_row(m.id, t(m.description))
    console.print(table)
    raise typer.Exit(0)


@migrate_app.command(name="run", help=t("cli.help.migrate.run.desc"))
def run_command(
    migrator_id: str = typer.Argument(..., help=t("cli.help.migrate.run.migrator_id")),
    project_root: Optional[str] = typer.Option(
        None, "--project-root", "-p", help=t("cli.help.migrate.run.project_root")
    ),
    no_delete_specs: bool = typer.Option(
        False, "--no-delete-specs",
        help=t("cli.help.migrate.run.no_delete_specs"),
    ),
) -> None:
    """Run the migration identified by *migrator_id*."""
    # Resolve the root (and with it the UI language) before the first t() render,
    # so even the unknown-migrator error speaks the target project's language.
    if project_root:
        from ..i18n import bind_project_root

        root = Path(project_root)
        # get_project_root() binds the UI language itself; an explicit
        # --project-root bypasses it, so bind here too.
        bind_project_root(root)
    else:
        from .run import get_project_root

        root = get_project_root()

    migrator = get_migrator(migrator_id)
    if migrator is None:
        ids = ", ".join(m.id for m in list_migrators()) or t("migrate.run.no_migrators")
        console.print(
            t("migrate.run.unknown", migrator_id=migrator_id, ids=ids)
        )
        raise typer.Exit(1)

    report = migrator.run(root, delete_specs=not no_delete_specs)
    _display_report(report, migrator.id)
    raise typer.Exit(report.exit_code)
