"""SE3 Init command - Initialize a new SE3 project."""

from tianluo.runtime_paths import UPLOADS_DIR_NAME, runtime_dir, runtime_dir_name
import fnmatch
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

import typer

from ..engine import charter as charter_mod
from ..i18n import t

# Note: This module exports the init function directly to be registered by cli.py
# Not using app.command() here because cli.py registers it directly

# Directory holding the project-init template files (base_spec.md,
# versions_md.md, …). Resolved relative to this module so it works
# regardless of the current working directory or install location.
TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _render_template(template_name: str, **values: str) -> str:
    """Load a template file from ``TEMPLATES_DIR`` and substitute placeholders.

    Each ``{key}`` token in the template is replaced with the corresponding
    ``values[key]``. A literal ``.replace`` (rather than ``str.format``) is
    used so that any incidental braces in the template body — e.g. inside a
    fenced code block — are left untouched and never raise ``KeyError``.
    """
    content = (TEMPLATES_DIR / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{" + key + "}", value)
    return content

DEFAULT_SE3_YAML = """# tianluo Project Configuration
# https://github.com/CoREse/tianluo
#
# For local-only overrides, create tianluo.local.yaml in the project root.
# When present, it fully replaces this file at load time and is
# gitignored by default so personal tweaks never get committed.

project_name: {project_name}

# Version management settings
version:
  enabled: true

# Confirmation steps (optional)
# Per-step dict: list a step here to insert a CONFIRM after it.
# Steps NOT listed are not confirmed (there is no global toggle).
# `adjudicate` is 免确认 by default — a ruling (even one that rewrites the
# task description) auto-passes with no门. Opt in to human review as below
# if an unattended run must not silently rewrite the task description.
# confirmation:
#   steps:
#     plan: {{reviewer: human}}
#     design: {{reviewer: reviewer_bot, max_iterations: 3}}
#     adjudicate: {{reviewer: human}}

# Agent registry (optional) — referenced by name from llm_caller / confirmation.
# agents:
#   primary: {{type: claude-code, cmd: claude, priority: 10}}

# LLM caller chain (optional)
# llm_caller:
#   defaults: [primary]

# Language configuration (optional) — two independent settings, merged
# project-over-global with ~/.se3/config.yaml.
# language:
#   # Unified human language: drives BOTH the CLI's fixed UI copy AND the LLM
#   # human-facing step outputs (summarize / discovery / confirmed steps).
#   # (The central WebUI console's interface language is a per-user browser /
#   #  localStorage preference and does NOT follow this project setting.)
#   # CLI resolution precedence: SE3_LANG env > this key > ~/.se3/config.yaml >
#   # system locale (LANG/LC_ALL) > en-US. e.g. "zh-CN", "en-US".
#   language: en-US
#   # Knowledge-asset language: the writing language of charter.md and the
#   # code-index. e.g. "zh-CN", "en-US". null = no restriction (LLM decides).
#   spec_language: null
"""

# Default .gitignore template for SE3 projects.
#
# The root block is default-deny: `/*` ignores everything at the repo root,
# and only the explicitly un-ignored top-level entries below are tracked.
# The real committer is an agent running `git add -A`, so any stray file
# dropped at the root (test logs, scratch output, caches, .venv) would
# otherwise be staged silently. Flipping the root layer from a runtime-
# signature blacklist to default-deny strictly covers the unsigned root
# junk the blacklist misses. To track a NEW top-level entry, add an
# explicit `!/<name>` (files) or `!/<name>/` (dirs) line in the root block.
DEFAULT_GITIGNORE_TEMPLATE = """# =====================================================================
# Repository root: ignore everything by default.
# Only the project files/dirs explicitly un-ignored below are tracked.
# To track a new top-level entry, add an explicit `!/<name>` (files) or
# `!/<name>/` (dirs) line here.
# =====================================================================
/*
!/.gitignore
!/.claude/
!/LICENSE
!/NOTICE
!/README.md
!/README.zh.md
!/VERSIONS.md
!/progress.md
!/pyproject.toml
!/tianluo.yaml
!/docs/
!/scripts/
!/tianluo/
!/src/
!/tests/

# --- Global ignore patterns (apply at any depth, e.g. inside src/, tests/) ---
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
MANIFEST

# Virtual environments
venv/
ENV/
env/
.venv

# IDEs
.vscode/
.idea/
*.swp
*.swo
*~
.DS_Store

# tianluo: ignore runtime content, whitelist committed artifacts.
# `!/tianluo/` above un-ignores the tianluo/ directory so git descends into it;
# `/tianluo/*` then re-applies default-deny one level down, tracking only the
# committable artifacts whitelisted below.
/tianluo/*
!/tianluo/code-index.md
!/tianluo/charter.md
!/tianluo/issues/
!/tianluo/scripts/
!/tianluo/version-rules.md
# Version-reconcile intent metadata: written by a worktree session's commit
# step and committed on the flow branch so the merge-side reconcile step can
# read every merged-in branch's intent from master. Must be tracked, unlike
# the rest of tianluo/ runtime content that /tianluo/* ignores.
!/tianluo/version-intents/
# Web-UI attachments: files the operator pastes/drops into a prompt land here
# on the project's machine. Redundant under `/tianluo/*`, but kept explicit
# because these are runtime artifacts of unbounded size that carry whatever
# was dropped into a prompt (screenshots, logs, customer data) — the intent
# must survive someone later widening the whitelist above.
/tianluo/uploads/

# tianluo: local-only config overrides (never committed). Redundant under the
# root default-deny, but kept explicit so the intent survives manual edits
# that whitelist tianluo.local.yaml's siblings.
tianluo.local.yaml
"""


def is_git_repository(path: Path) -> bool:
    """Check if the given path is already inside a git repository.

    Args:
        path: Directory to check

    Returns:
        True if the path is inside a git repository, False otherwise
    """
    # Check for .git directory in the path or any parent directory
    current = path.resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return True
        current = current.parent
    return False


def init_repository(path: Path) -> tuple[bool, str]:
    """Initialize a new git repository at the given path.

    Args:
        path: Directory where git repository should be initialized

    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        result = subprocess.run(
            ["git", "init"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=True,
        )
        return True, result.stdout.strip() if result.stdout else "Git repository initialized"
    except subprocess.CalledProcessError as e:
        return False, f"Failed to initialize git repository: {e.stderr.strip() if e.stderr else str(e)}"
    except FileNotFoundError:
        return False, "Git is not installed or not in PATH"


LOCAL_CONFIG_PATTERN = "tianluo.local.yaml"
# Sentinel used to distinguish narrow negation patterns (that specifically
# un-ignore ``tianluo.local.yaml``) from broad ones (like ``!*.yaml``). A
# negation is "narrow" only if it matches LOCAL_CONFIG_PATTERN while NOT
# also matching the committed ``tianluo.yaml`` — i.e. the user was targeting
# the ``.local.yaml`` name specifically, not ``.yaml`` in general.
_PROJECT_CONFIG_PATTERN = "tianluo.yaml"


class _EnsuredPattern(NamedTuple):
    """One gitignore rule ``luo init`` guarantees an existing file carries.

    *target* is the project-relative posix path probed against the existing
    rules to decide whether the file is already covered; *broad_probe* is a
    sibling path used to tell a narrow ``!<this exact thing>`` negation apart
    from a wide rule (``!*``) that merely happens to cover *target* too.
    """

    pattern: str
    target: str
    broad_probe: str
    comment: str


def _ensured_patterns(project_root: Path) -> Tuple[_EnsuredPattern, ...]:
    """Return the rules an existing ``.gitignore`` must end up carrying.

    The uploads rule is spelled with the project's *actual* runtime directory
    name rather than a hard-coded ``tianluo/``: during the 12.x transition a
    legacy project still lands uploads in ``se3/uploads/``, and an ignore rule
    naming the wrong directory would leave the real one tracked.
    """
    runtime_name = runtime_dir_name(project_root)
    uploads_rel = f"{runtime_name}/{UPLOADS_DIR_NAME}"
    return (
        _EnsuredPattern(
            pattern=LOCAL_CONFIG_PATTERN,
            target=LOCAL_CONFIG_PATTERN,
            broad_probe=_PROJECT_CONFIG_PATTERN,
            comment="# tianluo: local-only config overrides (never committed)",
        ),
        _EnsuredPattern(
            pattern=f"{uploads_rel}/",
            target=uploads_rel,
            # A negation covering a committed runtime artifact as well (e.g.
            # ``!/tianluo/*``) is a wide whitelist, not a deliberate decision
            # to track uploaded attachments.
            broad_probe=f"{runtime_name}/charter.md",
            comment=(
                "# tianluo: web-UI attachments land here (runtime output of\n"
                "# unbounded size that may carry anything dropped into a\n"
                "# prompt) — never committed."
            ),
        ),
    )


class GitignoreResult(NamedTuple):
    """Outcome of :func:`create_gitignore`.

    *patterns* names the rules the status is *about* — the ones appended, or
    the ones whose negation blocked the write — so the CLI can report exactly
    what happened instead of naming every rule init knows about.
    """

    status: str
    message: str
    patterns: List[str]


def _normalize_gitignore_pattern(pattern: str) -> str:
    """Strip anchor / recursive-glob / directory markers for fnmatch.

    - Leading ``/``: gitignore root anchor, not part of the filename.
    - Leading ``**/``: git's recursive-glob semantics — matches the file
      at any depth. ``fnmatchcase`` does not model ``**``, so without
      stripping we would miss patterns like ``**/tianluo.local.yaml`` and
      append a redundant rule.
    - Trailing ``/``: directory-only marker. A directory-only pattern
      does not strictly ignore a regular file, but the user has already
      spelled the name out — treat it as intent to ignore and avoid
      appending a duplicate line.
    """
    if pattern.startswith("/"):
        pattern = pattern[1:]
    if pattern.startswith("**/"):
        pattern = pattern[3:]
    if pattern.endswith("/"):
        pattern = pattern[:-1]
    return pattern


def _gitignore_covers(content: str, target: str) -> bool:
    """Return True when .gitignore already ignores *target*.

    Matches literal lines (``tianluo.local.yaml`` / ``/tianluo.local.yaml`` /
    ``**/tianluo.local.yaml``) as well as glob patterns that already cover
    the path (e.g. ``*.local.yaml``, ``tianluo/*``). Without this the user
    would get a redundant append block on every ``luo init`` even though the
    path is already ignored by an existing broader pattern. Negation patterns
    (``!...``) are skipped — they weaken ignore rules rather than add them.
    """
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        pattern = _normalize_gitignore_pattern(line)
        if not pattern:
            continue
        if fnmatch.fnmatchcase(target, pattern):
            return True
        # Git treats a slash-free pattern as matching at *any* depth, so a
        # bare ``uploads/`` really does ignore ``tianluo/uploads``. Comparing
        # only against the full path would miss that and append a redundant
        # rule beside one that already does the job.
        if "/" not in pattern and any(
            fnmatch.fnmatchcase(part, pattern) for part in target.split("/")
        ):
            return True
    return False


def _gitignore_narrowly_negates(content: str, target: str, broad_probe: str) -> bool:
    """Return True when .gitignore *narrowly* un-ignores *target*.

    Git's ``!pattern`` syntax re-includes a previously-ignored path. If
    the user has explicitly written ``!tianluo.local.yaml`` (perhaps because
    a broad pattern like ``*.yaml`` was ignoring it and they wanted the
    file tracked), silently appending ``tianluo.local.yaml`` afterwards
    creates two conflicting rules that fight by last-line-wins order —
    the user could end up with the file tracked or ignored depending on
    unrelated edits, without any warning.

    "Narrow" means the negation matches *target* but NOT *broad_probe* — a
    sibling path a wide rule would sweep up too. ``!*.yaml``, ``!se3.*`` or
    ``!*`` happen to cover our path, but the user was not explicitly
    un-ignoring it — they just have a general rule. In that case appending
    our ignore rule is the right thing to do, and the warning would mislead.

    Unlike :func:`_gitignore_covers` this deliberately does *not* fall back
    to matching a slash-free pattern against individual path components: the
    generated template itself carries ``!/tianluo/`` (git must be allowed to
    descend into the runtime dir before ``/tianluo/*`` re-ignores its
    contents), and reading that component-wise would misjudge it as
    un-ignoring ``tianluo/uploads`` and refuse to ever touch the file.
    """
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line.startswith("!"):
            continue
        body = line[1:].lstrip()
        if not body or body.startswith("#"):
            continue
        pattern = _normalize_gitignore_pattern(body)
        if not pattern:
            continue
        matches_target = fnmatch.fnmatchcase(target, pattern)
        matches_probe = fnmatch.fnmatchcase(broad_probe, pattern)
        if matches_target and not matches_probe:
            return True
    return False


def create_gitignore(path: Path, force: bool = False) -> GitignoreResult:
    """Ensure ``.gitignore`` carries every rule in :func:`_ensured_patterns`.

    Today that is ``tianluo.local.yaml`` (user-owned config that must never be
    committed) and ``<runtime dir>/uploads/`` (web-UI attachments: runtime
    output whose size is unbounded and whose content is whatever the operator
    dropped into a prompt, so it is default-untracked).

    Five outcomes are returned via ``status``:

    - ``"created"`` — file did not exist (or ``force=True``); template was
      written from scratch (it already carries every ensured rule).
    - ``"appended"`` — file existed missing one or more ensured rules; the
      missing blocks were appended (idempotent: re-running is a no-op). Even
      without ``--force`` this happens, because the rules must be present.
    - ``"negated"`` — file existed and contained an explicit negation
      (e.g. ``!tianluo.local.yaml``) that would fight the matching append.
      We leave the file untouched — *including* the rules that could have
      been appended cleanly, since a half-applied write would leave the
      operator reasoning about a file we mutated while warning them we did
      not — and surface a warning rather than create two conflicting rules
      that silently resolve by last-line-wins.
    - ``"unchanged"`` — file existed and already covered every ensured rule.
    - ``"error"`` — an I/O error prevented reading or writing the file.
      Distinct from ``"unchanged"`` so callers can surface the real
      failure instead of showing a misleading "already exists" message.

    Args:
        path: Directory where ``.gitignore`` lives.
        force: When True, overwrite any existing file with the full template.

    Returns:
        A :class:`GitignoreResult` of ``(status, message, patterns)``.
    """
    gitignore_path = path / ".gitignore"

    if not gitignore_path.exists() or force:
        try:
            gitignore_path.write_text(DEFAULT_GITIGNORE_TEMPLATE, encoding="utf-8")
            return GitignoreResult("created", ".gitignore created", [])
        except Exception as e:
            # The message is echoed to the user (init.warning_line), so it is
            # UI copy and renders through i18n; only the OS error text is raw.
            return GitignoreResult(
                "error", t("init.gitignore_error_create", error=str(e)), []
            )

    try:
        existing = gitignore_path.read_text(encoding="utf-8")
    except Exception as e:
        return GitignoreResult("error", t("init.gitignore_error_read", error=str(e)), [])

    ensured = _ensured_patterns(path)

    # Negation check runs BEFORE the coverage check on purpose: a file can
    # contain both a broad ignore (e.g. ``*.yaml``) AND an explicit
    # ``!tianluo.local.yaml`` negation. Semantically the negation wins — git
    # keeps the file tracked — so returning ``"unchanged"`` because the broad
    # pattern also matches would make us silently accept a state where the
    # file is NOT ignored and the operator never gets warned. Surface the
    # negation warning first.
    negated = [
        p
        for p in ensured
        if _gitignore_narrowly_negates(existing, p.target, p.broad_probe)
    ]
    if negated:
        # User explicitly un-ignored one of our paths. Appending a plain
        # ignore line now would create two conflicting rules where
        # later-line-wins determines the outcome — exactly the kind of
        # silent foot-gun we want to avoid. Do not modify the file; the
        # caller will surface the warning to the operator.
        names = ", ".join(p.pattern for p in negated)
        return GitignoreResult(
            "negated",
            f".gitignore contains an explicit negation of {names} "
            f"(``!…``); refusing to append a conflicting rule",
            [p.pattern for p in negated],
        )

    missing = [p for p in ensured if not _gitignore_covers(existing, p.target)]
    if not missing:
        return GitignoreResult(
            "unchanged", ".gitignore already exists (use --force to overwrite)", []
        )

    # Append the missing blocks with exactly one blank line of separation,
    # regardless of whether the existing file ends with a trailing newline:
    #   "xyz\n"  + separator + block → "xyz\n\n# tianluo…"
    #   "xyz"    + separator + block → "xyz\n\n# tianluo…"
    # Both end up with one blank line between the previous content and
    # the comment header.
    if not existing:
        separator = ""
    elif existing.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    block = "\n".join(f"{p.comment}\n{p.pattern}\n" for p in missing)
    # Single write_text call replaces the read+append pair, so the
    # on-disk transition from "existing" to "existing + block" is one
    # syscall rather than two — and all missing rules land together, so a
    # crash cannot leave one of them behind. A concurrent writer slipping in
    # between the earlier read and an append can no longer produce a
    # duplicated pattern line — the worst case now is a last-writer-wins
    # clobber, which is the normal semantics of any non-locking file writer.
    new_content = existing + separator + block
    try:
        gitignore_path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        return GitignoreResult(
            "error", t("init.gitignore_error_append", error=str(e)), []
        )
    appended = [p.pattern for p in missing]
    return GitignoreResult(
        "appended",
        f"appended {', '.join(appended)} to existing .gitignore",
        appended,
    )


def _get_charter_template(project_name: str) -> str:
    """Generate the project charter content from the ``charter.md`` template.

    The charter (``tianluo/charter.md``) is the new-knowledge-system replacement
    for the retired base spec: it is injected — in full — into every `luo run`
    step and doubles as the conventions channel for sandboxed LLM
    sub-processes. ``luo init`` scaffolds it (rather than the retired
    ``tianluo/specs/base/spec.md``) so a fresh, non-migrated project bootstraps the
    code-index + charter + why-comment triad with a committable charter from
    the start.

    The fill-in placeholders are seeded with the same please-fill-this-in
    prompts the old base-spec skeleton used, so a fresh project still gets a
    guided skeleton, plus the one convention that is worth stating before any
    code exists (``charter.DEFAULT_PARALLEL_SAFE_TESTS_CONVENTION``); the
    rendering is delegated to
    ``charter.render_charter_template`` so it stays consistent with the
    `luo migrate` charter assembly and the packaged template stays the single
    source of truth.
    """
    return charter_mod.render_charter_template(
        project_name=project_name,
        project_description="（请填写项目简述）",
        languages_and_frameworks="（请填写语言和框架）",
        top_level_architecture="（请填写顶层架构：主要子系统及其边界）",
        coding_conventions=(
            "（请填写代码规范）\n"
            # Seeded alongside the fill-in prompt, not in place of it: a fresh
            # project should start out with parallel-safe tests rather than
            # retrofit them once the suite is already order-sensitive.
            "- " + charter_mod.DEFAULT_PARALLEL_SAFE_TESTS_CONVENTION
        ),
        key_constraints="（请填写关键约束）",
        workflow_conventions=(
            '使用 `luo run "task description"` 启动开发流程\n'
            "- 运行测试后才可标记功能完成\n"
            "- 主分支保持可运行状态"
        ),
    )


def _get_versions_md_template(project_name: str) -> str:
    """Generate the initial ``VERSIONS.md`` content from its template.

    Reads ``src/tianluo/templates/versions_md.md`` and fills the
    ``{project_name}`` / ``{date}`` placeholders. The rendered file starts
    with a ``# Version History`` title and a ``## 0.1.0 - <date>`` entry,
    matching the changelog shape the documentation-updater pipeline inserts
    into on subsequent commits.
    """
    return _render_template(
        "versions_md.md",
        project_name=project_name,
        date=datetime.now().strftime("%Y-%m-%d"),
    )


def run_init(project_root: Path, project_name: str, force: bool = False) -> dict:
    """Core init logic, separated for testability.

    Args:
        project_root: Root directory of the project
        project_name: Name of the project
        force: Whether to overwrite existing files

    Returns:
        dict with "created" (list of relative paths), "skipped" (list of messages),
        and git/gitignore status flags
    """
    root = Path(project_root).resolve()
    created = []
    skipped = []

    # Create se3 runtime directory. The retired spec corpus (tianluo/specs/) is no
    # longer scaffolded — the new knowledge system stores project conventions
    # in tianluo/charter.md (committable via the gitignore whitelist) and the
    # auto-maintained tianluo/code-index.md.
    se3_dir = runtime_dir(root)
    se3_dir.mkdir(exist_ok=True)

    # Create tianluo.yaml (never touch tianluo.local.yaml — it is user-owned and
    # takes precedence at load time).
    se3_yaml = root / "tianluo.yaml"
    if not se3_yaml.exists() or force:
        se3_yaml.write_text(
            DEFAULT_SE3_YAML.format(project_name=project_name), encoding="utf-8"
        )
        created.append(str(se3_yaml.relative_to(root)))
    else:
        skipped.append(t("init.file_exists", path=str(se3_yaml.relative_to(root))))

    # Detect (but do not modify) an existing tianluo.local.yaml so the operator
    # knows it will shadow the just-generated tianluo.yaml at load time.
    local_yaml = root / "tianluo.local.yaml"
    # Use is_file() (not exists()) so the warning fires only for a real
    # file that will actually shadow tianluo.yaml at load time — matches the
    # check in get_project_config_path(). A directory or dangling symlink
    # at this path would not shadow, so we shouldn't warn about it.
    local_overrides_yaml = local_yaml.is_file()

    # Create the project charter (tianluo/charter.md). This is the single
    # project-convention artifact the new knowledge system scaffolds; it is
    # whitelisted by the generated .gitignore (!/tianluo/charter.md) so it is
    # committable, and get_charter_injection injects it in full into every
    # step. Writing a base spec into the now-gitignored tianluo/specs/ tree would
    # leave a fresh project with no committable, no injectable conventions.
    charter_file = charter_mod.charter_path(root)
    if not charter_file.exists() or force:
        charter_file.write_text(_get_charter_template(project_name), encoding="utf-8")
        created.append(str(charter_file.relative_to(root)))
    else:
        skipped.append(t("init.file_exists", path=str(charter_file.relative_to(root))))

    # Create initial VERSIONS.md from template (skip if it already exists,
    # unless --force). Tracked via dedicated flags rather than the
    # created/skipped lists — mirroring how .gitignore is handled — so the
    # changelog state is reported distinctly and existing skipped-count
    # expectations are preserved.
    versions_md = root / "VERSIONS.md"
    versions_md_created = False
    versions_md_already_existed = False
    if not versions_md.exists() or force:
        versions_md.write_text(_get_versions_md_template(project_name), encoding="utf-8")
        versions_md_created = True
    else:
        versions_md_already_existed = True

    # Initialize git repository if not already in one
    git_initialized = False
    git_already_existed = False
    git_message = ""

    if is_git_repository(root):
        git_already_existed = True
        git_message = "Already inside a git repository"
    else:
        success, git_message = init_repository(root)
        if success:
            git_initialized = True

    # Create or update .gitignore. Five outcomes are surfaced: created
    # (brand-new file), appended (existing file gained a missing ensured
    # pattern), negated (file explicitly un-ignores one of them so we
    # refused to append a conflicting rule), unchanged (existing file
    # already covered them all), and error (an I/O error prevented the
    # read or write). The error status is kept distinct from unchanged so
    # the UI does not mislabel a real I/O failure as "already exists".
    gitignore_status, gitignore_message, gitignore_patterns = create_gitignore(
        root, force=force
    )
    gitignore_created = gitignore_status == "created"
    gitignore_appended = gitignore_status == "appended"
    gitignore_negated = gitignore_status == "negated"
    gitignore_already_existed = gitignore_status == "unchanged"
    gitignore_error = gitignore_status == "error"

    return {
        "created": created,
        "skipped": skipped,
        "versions_md_created": versions_md_created,
        "versions_md_already_existed": versions_md_already_existed,
        "git_initialized": git_initialized,
        "git_already_existed": git_already_existed,
        "git_message": git_message,
        "gitignore_created": gitignore_created,
        "gitignore_appended": gitignore_appended,
        "gitignore_negated": gitignore_negated,
        "gitignore_already_existed": gitignore_already_existed,
        "gitignore_error": gitignore_error,
        "gitignore_message": gitignore_message,
        # The rules the status is about — the CLI names exactly those rather
        # than every pattern init knows how to ensure.
        "gitignore_patterns": list(gitignore_patterns),
        "local_overrides_yaml": local_overrides_yaml,
    }


def init_cmd(
    project_root: str = typer.Option(".", "--project-root", "-p", help=t("cli.help.common.project_root")),
    name: Optional[str] = typer.Option(None, "--name", "-n", help=t("cli.help.init.name")),
    force: bool = typer.Option(False, "--force", "-f", help=t("cli.help.init.force")),
):
    """Initialize a new SE3 project.

    Creates the standard SE3 directory structure:
    - tianluo.yaml - Project configuration
    - tianluo/ - SE3 runtime directory
    - tianluo/charter.md - Project charter (injected into every flow step)
    """
    root = Path(project_root).resolve()

    # Detect project name if not provided
    if not name:
        name = root.name or "my-project"

    result = run_init(root, name, force)

    for path in result["created"]:
        typer.echo(t("init.created", path=path))
    for msg in result["skipped"]:
        typer.echo(t("init.warning_line", msg=msg))

    # Display VERSIONS.md status (tracked separately from created/skipped)
    if result.get("versions_md_created"):
        typer.echo(t("init.versions_created"))
    elif result.get("versions_md_already_existed"):
        typer.echo(t("init.versions_exists"))

    # Display git initialization status
    if result.get("git_initialized"):
        typer.echo(t("init.git_initialized"))
    elif result.get("git_already_existed"):
        typer.echo(t("init.git_exists"))

    # Display .gitignore creation status
    patterns = ", ".join(result.get("gitignore_patterns") or [LOCAL_CONFIG_PATTERN])
    if result.get("gitignore_created"):
        typer.echo(t("init.gitignore_created"))
    elif result.get("gitignore_appended"):
        typer.echo(t("init.gitignore_appended", pattern=patterns))
    elif result.get("gitignore_negated"):
        typer.echo(t("init.gitignore_negated", pattern=patterns))
    elif result.get("gitignore_error"):
        typer.echo(t("init.warning_line", msg=result.get("gitignore_message")))
    elif result.get("gitignore_already_existed"):
        typer.echo(t("init.gitignore_exists"))

    # Warn when an existing tianluo.local.yaml will shadow the generated tianluo.yaml
    if result.get("local_overrides_yaml"):
        typer.echo(t("init.local_overrides"))

    typer.echo(t("init.success", name=name))
    typer.echo(t("init.next_steps"))
    typer.echo(t("init.next_step_1"))
    typer.echo(t("init.next_step_2"))
    typer.echo(t("init.next_step_3"))
