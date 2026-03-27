"""Tests for SE3 IssueManager — YAML-based issue tracking."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from se3.engine.issue_manager import Issue, IssueManager, IssueStatus, _make_slug


class TestIssueModel:
    """Tests for Issue dataclass serialization."""

    def test_to_dict_roundtrip(self):
        now = datetime.now()
        issue = Issue(
            id="001",
            title="Test issue",
            description="A description",
            status=IssueStatus.OPEN,
            priority="high",
            tags=["source:ai", "bug"],
            created_at=now,
            updated_at=now,
        )
        data = issue.to_dict()
        restored = Issue.from_dict(data)

        assert restored.id == "001"
        assert restored.title == "Test issue"
        assert restored.description == "A description"
        assert restored.status == IssueStatus.OPEN
        assert restored.priority == "high"
        assert restored.tags == ["source:ai", "bug"]
        assert restored.created_at == now
        assert restored.updated_at == now

    def test_from_dict_defaults(self):
        data = {"id": "002", "title": "Minimal"}
        issue = Issue.from_dict(data)
        assert issue.id == "002"
        assert issue.description == ""
        assert issue.status == IssueStatus.OPEN
        assert issue.priority == "medium"
        assert issue.tags == []

    def test_from_dict_datetime_objects(self):
        """PyYAML may parse datetimes as datetime objects."""
        now = datetime.now()
        data = {
            "id": "003",
            "title": "Test",
            "created_at": now,
            "updated_at": now,
        }
        issue = Issue.from_dict(data)
        assert issue.created_at == now


class TestSlugGeneration:
    def test_basic_slug(self):
        assert _make_slug("Fix login bug") == "fix-login-bug"

    def test_special_characters(self):
        assert _make_slug("Fix: the (broken) thing!") == "fix-the-broken-thing"

    def test_long_title_truncated(self):
        slug = _make_slug("A" * 50)
        assert len(slug) <= 30

    def test_empty_title(self):
        assert _make_slug("") == "untitled"

    def test_unicode_removed(self):
        slug = _make_slug("修复登录问题")
        # Non-ascii gets stripped, so it falls back to "untitled"
        assert slug == "untitled"


class TestIssueManagerCreate:
    def test_create_issue(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Fix bug", "There's a bug", priority="high", tags=["source:ai"])

        assert issue.id == "001"
        assert issue.title == "Fix bug"
        assert issue.status == IssueStatus.OPEN
        assert issue.priority == "high"
        assert issue.tags == ["source:ai"]

        # YAML file should exist in open/
        files = list((tmp_path / "se3" / "issues" / "open").glob("*.yaml"))
        assert len(files) == 1
        assert files[0].name.startswith("001_")

    def test_create_increments_id(self, tmp_path):
        mgr = IssueManager(tmp_path)
        i1 = mgr.create("First", "desc1")
        i2 = mgr.create("Second", "desc2")
        i3 = mgr.create("Third", "desc3")

        assert i1.id == "001"
        assert i2.id == "002"
        assert i3.id == "003"

    def test_id_spans_open_and_closed(self, tmp_path):
        """ID assignment scans both open/ and closed/ directories."""
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()

        # Manually place a file in closed/ with ID 005
        closed_file = mgr.closed_dir / "005_old-issue.yaml"
        closed_file.write_text(
            yaml.dump({"id": "005", "title": "Old", "status": "closed"}),
            encoding="utf-8",
        )

        issue = mgr.create("New issue", "desc")
        assert issue.id == "006"

    def test_creates_directories(self, tmp_path):
        mgr = IssueManager(tmp_path)
        assert not mgr.open_dir.exists()
        mgr.create("Test", "desc")
        assert mgr.open_dir.exists()
        assert mgr.closed_dir.exists()

    def test_yaml_content_valid(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("YAML Test", "Check content", priority="low", tags=["test"])

        files = list(mgr.open_dir.glob("*.yaml"))
        data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert data["title"] == "YAML Test"
        assert data["priority"] == "low"
        assert data["tags"] == ["test"]
        assert data["status"] == "open"


class TestIssueManagerLoad:
    def test_load_from_open(self, tmp_path):
        mgr = IssueManager(tmp_path)
        created = mgr.create("Loadable", "desc")
        loaded = mgr.load(created.id)
        assert loaded is not None
        assert loaded.id == created.id
        assert loaded.title == "Loadable"

    def test_load_from_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()

        closed_file = mgr.closed_dir / "010_closed-issue.yaml"
        issue = Issue(id="010", title="Closed One", description="d", status=IssueStatus.CLOSED)
        closed_file.write_text(
            yaml.dump(issue.to_dict(), allow_unicode=True), encoding="utf-8"
        )

        loaded = mgr.load("010")
        assert loaded is not None
        assert loaded.title == "Closed One"
        assert loaded.status == IssueStatus.CLOSED

    def test_load_nonexistent(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        assert mgr.load("999") is None

    def test_load_with_unpadded_id(self, tmp_path):
        mgr = IssueManager(tmp_path)
        created = mgr.create("Padded", "desc")
        assert created.id == "001"
        # Loading with "1" should also work
        loaded = mgr.load("1")
        assert loaded is not None
        assert loaded.id == "001"


class TestIssueManagerList:
    def test_list_empty(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        assert mgr.list_issues() == []

    def test_list_open_only(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Open 1", "d1")
        mgr.create("Open 2", "d2")

        # Move one to closed
        mgr.update_status("002", IssueStatus.WONT_FIX)

        issues = mgr.list_issues(include_closed=False)
        assert len(issues) == 1
        assert issues[0].id == "001"

    def test_list_include_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Open 1", "d1")
        mgr.create("Closed 1", "d2")
        mgr.update_status("002", IssueStatus.WONT_FIX)

        issues = mgr.list_issues(include_closed=True)
        assert len(issues) == 2

    def test_list_sorted_by_id(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("C", "d")
        mgr.create("A", "d")
        mgr.create("B", "d")

        issues = mgr.list_issues()
        ids = [i.id for i in issues]
        assert ids == ["001", "002", "003"]


class TestIssueManagerStatusTransitions:
    def test_open_to_in_progress(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")
        updated = mgr.update_status("001", IssueStatus.IN_PROGRESS)
        assert updated.status == IssueStatus.IN_PROGRESS
        # Should still be in open/
        assert (mgr.open_dir / list(mgr.open_dir.glob("001_*"))[0].name).exists()

    def test_in_progress_to_resolved_moves_to_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")
        mgr.update_status("001", IssueStatus.IN_PROGRESS)
        mgr.update_status("001", IssueStatus.RESOLVED)

        # File should be in closed/
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1
        assert len(list(mgr.open_dir.glob("001_*"))) == 0

    def test_wont_fix_moves_to_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")
        mgr.update_status("001", IssueStatus.WONT_FIX)

        assert len(list(mgr.closed_dir.glob("001_*"))) == 1

    def test_resolved_to_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")
        mgr.update_status("001", IssueStatus.IN_PROGRESS)
        mgr.update_status("001", IssueStatus.RESOLVED)
        mgr.update_status("001", IssueStatus.CLOSED)

        loaded = mgr.load("001")
        assert loaded.status == IssueStatus.CLOSED

    def test_closed_to_open_moves_back(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")
        mgr.update_status("001", IssueStatus.WONT_FIX)
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1

        mgr.update_status("001", IssueStatus.OPEN)
        assert len(list(mgr.open_dir.glob("001_*"))) == 1
        assert len(list(mgr.closed_dir.glob("001_*"))) == 0

    def test_invalid_transition_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")

        with pytest.raises(ValueError, match="Invalid status transition"):
            mgr.update_status("001", IssueStatus.RESOLVED)

    def test_update_nonexistent_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()

        with pytest.raises(ValueError, match="not found"):
            mgr.update_status("999", IssueStatus.IN_PROGRESS)

    def test_move_failure_only_warns(self, tmp_path):
        """File move failure should log warning, not crash."""
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")

        with patch("shutil.move", side_effect=OSError("Permission denied")):
            # Should not raise
            updated = mgr.update_status("001", IssueStatus.WONT_FIX)
            assert updated.status == IssueStatus.WONT_FIX


class TestIssueManagerReset:
    def test_reset_in_progress_to_open(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")
        mgr.update_status("001", IssueStatus.IN_PROGRESS)

        reset = mgr.reset_to_open("001")
        assert reset.status == IssueStatus.OPEN

    def test_reset_non_in_progress_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test", "d")

        with pytest.raises(ValueError, match="Can only reset in-progress"):
            mgr.reset_to_open("001")

    def test_reset_nonexistent_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()

        with pytest.raises(ValueError, match="not found"):
            mgr.reset_to_open("999")


class TestNextId:
    def test_empty_directories(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        assert mgr._next_id() == "001"

    def test_nonexistent_directories(self, tmp_path):
        mgr = IssueManager(tmp_path)
        # Directories don't exist yet
        assert mgr._next_id() == "001"

    def test_after_multiple_creates(self, tmp_path):
        mgr = IssueManager(tmp_path)
        for i in range(5):
            mgr.create(f"Issue {i}", "d")
        assert mgr._next_id() == "006"
