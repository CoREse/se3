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
        stderr: str = "",
        returncode: Optional[int] = None,
    ) -> None:
        self.tag_name = tag_name
        self.stderr = stderr
        self.returncode = returncode
        details = message
        if returncode is not None:
            details = f"{details} (exit {returncode})"
        if stderr:
            details = f"{details}: {stderr.strip()}"
        super().__init__(f"failed to create version tag {tag_name}: {details}")


def tag_name_for_version(version: str) -> str:
    """Return the canonical git tag name for a version string."""
    normalized = str(version).strip()
    if normalized.startswith("v"):
        return normalized
    return f"v{normalized}"


def should_tag_semver_bump(bump_type: Optional[str]) -> bool:
    """Return whether the default SemVer policy creates a tag for a bump."""
    return str(bump_type or "").strip().lower() in {"major", "minor"}


def commit_subject(
    project_root: Path,
    commit: str,
    *,
    timeout: int = 30,
    tag_name: Optional[str] = None,
) -> str:
    """Read the first line of the target commit message from git."""
    result = _run_git(
        project_root,
        "log",
        "-1",
        "--format=%s",
        commit,
        timeout=timeout,
        tag_name=tag_name,
    )
    return result.stdout.strip()


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
    )
    return tag_name


def _run_git(
    project_root: Path,
    *args: str,
    timeout: int,
    tag_name: Optional[str] = None,
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
            stderr=stderr,
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise VersionTagError(fallback_tag, str(exc)) from exc

    if result.returncode != 0:
        raise VersionTagError(
            fallback_tag,
            "git command failed",
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
