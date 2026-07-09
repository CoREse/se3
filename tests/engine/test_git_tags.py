from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.engine.git_tags import (
    VersionTagError,
    commit_subject,
    create_annotated_version_tag,
    should_tag_semver_bump,
    tag_name_for_version,
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# Test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "release subject", "-m", "body text")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _tag_body(repo: Path, tag_name: str) -> str:
    content = _git(repo, "cat-file", "-p", tag_name).stdout
    _, _, body = content.partition("\n\n")
    return body


def test_tag_name_for_version_prefixes_once() -> None:
    assert tag_name_for_version("11.16.0") == "v11.16.0"
    assert tag_name_for_version("v11.16.0") == "v11.16.0"


@pytest.mark.parametrize(
    ("bump_type", "expected"),
    [
        ("major", True),
        ("minor", True),
        ("patch", False),
        ("none", False),
        ("", False),
        (None, False),
        ("MAJOR", True),
        (" minor ", True),
    ],
)
def test_should_tag_semver_bump(bump_type: str | None, expected: bool) -> None:
    assert should_tag_semver_bump(bump_type) is expected


def test_commit_subject_reads_target_commit_first_line(tmp_path: Path) -> None:
    commit = _init_repo(tmp_path)

    assert commit_subject(tmp_path, commit) == "release subject"


def test_create_annotated_version_tag_uses_commit_subject(tmp_path: Path) -> None:
    commit = _init_repo(tmp_path)

    tag_name = create_annotated_version_tag(tmp_path, "11.16.0", commit)

    assert tag_name == "v11.16.0"
    assert _git(tmp_path, "cat-file", "-t", tag_name).stdout.strip() == "tag"
    assert _tag_body(tmp_path, tag_name) == "release subject\n"
    assert _git(tmp_path, "rev-list", "-n", "1", tag_name).stdout.strip() == commit


def test_existing_tag_raises_typed_error_with_tag_and_stderr(tmp_path: Path) -> None:
    commit = _init_repo(tmp_path)
    create_annotated_version_tag(tmp_path, "11.16.0", commit)

    with pytest.raises(VersionTagError) as exc_info:
        create_annotated_version_tag(tmp_path, "11.16.0", commit)

    message = str(exc_info.value)
    assert exc_info.value.tag_name == "v11.16.0"
    assert "v11.16.0" in message
    assert "already exists" in message
    assert "already exists" in exc_info.value.stderr


def test_git_tag_failure_raises_typed_error_with_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = _init_repo(tmp_path)
    real_run = subprocess.run

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if "tag" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "tag failure")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(VersionTagError) as exc_info:
        create_annotated_version_tag(tmp_path, "11.16.0", commit)

    assert exc_info.value.tag_name == "v11.16.0"
    assert "tag failure" in str(exc_info.value)
    assert exc_info.value.stderr == "tag failure"
