"""Shared helpers for version tag creation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class VersionTagError(RuntimeError):
    """Raised when a version tag cannot be created."""

    def __init__(
        self,
        tag_name: str,
        message: str,
        *,
        commit: Optional[str] = None,
        stderr: str = "",
        returncode: Optional[int] = None,
    ) -> None:
        self.tag_name = tag_name
        self.commit = commit
        self.stderr = stderr
        self.returncode = returncode
        details = message
        if returncode is not None:
            details = f"{details} (exit {returncode})"
        if stderr:
            details = f"{details}: {stderr.strip()}"
        # Recovery from a tag failure is manual (`git tag -a <name> <commit>`),
        # so the message must name BOTH the tag and the commit it belongs on —
        # the version commit is already durable and nothing will retry it.
        target = f" on commit {commit}" if commit else ""
        super().__init__(f"failed to create version tag {tag_name}{target}: {details}")


def tag_name_for_version(version: str) -> str:
    """Return the canonical git tag name for a version string."""
    normalized = str(version).strip()
    if normalized.startswith("v"):
        return normalized
    return f"v{normalized}"


def should_tag_semver_bump(bump_type: Optional[str]) -> bool:
    """Return whether the default SemVer policy creates a tag for a bump."""
    return str(bump_type or "").strip().lower() in {"major", "minor"}


def semver_tag_decision(
    current_version: Optional[str],
    new_version: Optional[str],
) -> Optional[bool]:
    """The default-SemVer tag verdict implied by the two version numbers.

    ``suggested_version`` is the authoritative version decision; ``bump_type`` is
    free-form auxiliary text an LLM can garble in either direction (a real minor
    release labelled ``patch``, or a patch-only release labelled ``minor``).
    Whenever both versions parse as ``MAJOR.MINOR.…`` the comparison is the whole
    truth: major/minor advance ⇒ tag, patch-only advance ⇒ no tag. Returns ``None``
    when the versions are not comparable (a calendar scheme, an unreadable current
    version): the default-SemVer policy has no verdict to give there, and callers
    must not manufacture one from ``bump_type``.
    """
    current = _semver_major_minor(current_version)
    new = _semver_major_minor(new_version)
    if current is None or new is None:
        return None
    return new > current


def semver_advance_requires_tag(
    current_version: Optional[str],
    new_version: Optional[str],
) -> bool:
    """Whether current -> new advances the SemVer major or minor component.

    ``False`` for anything not parseable as ``MAJOR.MINOR.…``.
    """
    return semver_tag_decision(current_version, new_version) is True


def _semver_major_minor(version: Optional[str]) -> Optional[tuple[int, int]]:
    if not version:
        return None
    core = str(version).strip().lstrip("v").split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def commit_subject(
    project_root: Path,
    commit: str,
    *,
    timeout: int = 30,
    tag_name: Optional[str] = None,
) -> str:
    """Read the first line of the target commit message from git.

    ``%B`` (raw body) rather than ``%s``: git's ``%s`` is the subject *paragraph*,
    folding a wrapped first paragraph's newlines into spaces. The tag message must
    be exactly the first physical line of the commit message.
    """
    result = _run_git(
        project_root,
        "log",
        "-1",
        "--format=%B",
        commit,
        timeout=timeout,
        tag_name=tag_name,
        commit=commit,
    )
    first_line, _, _ = result.stdout.partition("\n")
    return first_line.strip()


def create_annotated_version_tag(
    project_root: Path,
    version: str,
    commit: str,
    *,
    timeout: int = 30,
) -> str:
    """Create an annotated version tag on a commit using that commit's subject."""
    tag_name = tag_name_for_version(version)
    subject = commit_subject(project_root, commit, timeout=timeout, tag_name=tag_name)
    _run_git(
        project_root,
        "tag",
        "-a",
        tag_name,
        "-m",
        subject,
        commit,
        timeout=timeout,
        tag_name=tag_name,
        commit=commit,
    )
    return tag_name


def _run_git(
    project_root: Path,
    *args: str,
    timeout: int,
    tag_name: Optional[str] = None,
    commit: Optional[str] = None,
) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(project_root), *args]
    fallback_tag = tag_name or _tag_name_from_args(args)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = _decode_output(exc.stderr)
        raise VersionTagError(
            fallback_tag,
            f"git command timed out after {timeout}s",
            commit=commit,
            stderr=stderr,
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise VersionTagError(fallback_tag, str(exc), commit=commit) from exc

    if result.returncode != 0:
        raise VersionTagError(
            fallback_tag,
            "git command failed",
            commit=commit,
            stderr=result.stderr,
            returncode=result.returncode,
        )
    return result


def _tag_name_from_args(args: tuple[str, ...]) -> str:
    if len(args) >= 4 and args[0] == "tag" and args[1] == "-a":
        return args[2]
    return "<unknown>"


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
