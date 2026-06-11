"""Tests for SE3 IssueManager — YAML-based issue tracking."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from se3.engine.issue_manager import (
    KNOWN_TYPES,
    Issue,
    IssueManager,
    IssueStatus,
    _derive_slug_for_issue,
    _make_slug,
)


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
            source="human",
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
        assert restored.source == "human"
        assert restored.created_at == now
        assert restored.updated_at == now

    def test_from_dict_defaults(self):
        data = {"id": "002", "title": "Minimal", "description": "Some desc"}
        issue = Issue.from_dict(data)
        assert issue.id == "002"
        assert issue.description == "Some desc"
        assert issue.status == IssueStatus.OPEN
        assert issue.priority is None
        assert issue.type is None
        assert issue.source == "system"
        assert issue.tags == []

    def test_from_dict_missing_description_degrades(self):
        """YAML without a description loads with description="" (legacy compat)."""
        issue = Issue.from_dict({"id": "001", "title": "No desc"})
        assert issue.description == ""
        assert issue.display_title == "No desc"

    def test_from_dict_empty_description_degrades(self):
        """Empty description loads as "" — write paths enforce non-empty."""
        issue = Issue.from_dict({"id": "001", "description": ""})
        assert issue.description == ""

    def test_from_dict_whitespace_description_degrades(self):
        """Whitespace-only description loads as "" — write paths enforce non-empty."""
        issue = Issue.from_dict({"id": "001", "description": "   \n  "})
        assert issue.description == ""

    def test_from_dict_datetime_objects(self):
        """PyYAML may parse datetimes as datetime objects."""
        now = datetime.now()
        data = {
            "id": "003",
            "title": "Test",
            "description": "A description",
            "created_at": now,
            "updated_at": now,
        }
        issue = Issue.from_dict(data)
        assert issue.created_at == now

    def test_from_dict_missing_source_defaults_system(self):
        """Pre-source YAML files (no 'source' field) load as system."""
        data = {"id": "004", "title": "Legacy", "description": "A desc", "status": "open"}
        issue = Issue.from_dict(data)
        assert issue.source == "system"

    def test_from_dict_explicit_source(self):
        data = {"id": "005", "title": "Manual", "description": "A desc", "source": "human"}
        issue = Issue.from_dict(data)
        assert issue.source == "human"

    def test_to_dict_omits_none_optional_fields(self):
        """title/priority/type are omitted from dict when None."""
        issue = Issue(id="006", description="desc only")
        data = issue.to_dict()
        assert "title" not in data
        assert "priority" not in data
        assert "type" not in data
        assert data["source"] == "system"

    def test_to_dict_includes_set_optional_fields(self):
        issue = Issue(
            id="007",
            title="Has Title",
            description="d",
            priority="low",
            type="feature",
            source="human",
        )
        data = issue.to_dict()
        assert data["title"] == "Has Title"
        assert data["priority"] == "low"
        assert data["type"] == "feature"
        assert data["source"] == "human"

    def test_roundtrip_with_none_optional_fields(self):
        """Issues with None title/priority/type survive round-trip."""
        issue = Issue(id="008", description="body text", source="system")
        data = issue.to_dict()
        restored = Issue.from_dict(data)
        assert restored.title is None
        assert restored.priority is None
        assert restored.type is None
        assert restored.description == "body text"

    def test_from_dict_invalid_datetime_falls_back(self):
        data = {
            "id": "009",
            "title": "Bad date",
            "description": "A desc",
            "created_at": "not-a-date",
            "updated_at": "also-bad",
        }
        issue = Issue.from_dict(data)
        # Should not raise, falls back to now()
        assert isinstance(issue.created_at, datetime)
        assert isinstance(issue.updated_at, datetime)


class TestDisplayTitle:
    """Tests for Issue.display_title derived title."""

    def test_explicit_title(self):
        issue = Issue(id="001", title="My Title", description="ignored")
        assert issue.display_title == "My Title"

    def test_derived_from_description_first_line(self):
        issue = Issue(id="001", description="First line\nSecond line")
        assert issue.display_title == "First line"

    def test_derived_from_description_skips_blank_lines(self):
        issue = Issue(id="001", description="\n  \nActual content\nMore")
        assert issue.display_title == "Actual content"

    def test_fallback_to_untitled(self):
        issue = Issue(id="001", description="")
        assert issue.display_title == "untitled"

    def test_title_empty_string_treated_as_none(self):
        """An empty-string title should fall through to description."""
        issue = Issue(id="001", title="", description="From desc")
        assert issue.display_title == "From desc"


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


class TestDeriveSlugForIssue:
    """Tests for _derive_slug_for_issue helper."""

    def test_slug_from_explicit_title(self):
        issue = Issue(id="001", title="Fix Bug", description="d")
        assert _derive_slug_for_issue(issue) == "fix-bug"

    def test_slug_from_description(self):
        issue = Issue(id="001", description="Crash on startup\nMore details")
        assert _derive_slug_for_issue(issue) == "crash-on-startup"

    def test_slug_fallback_untitled(self):
        issue = Issue(id="001", description="")
        assert _derive_slug_for_issue(issue) == "untitled"


class TestIssueManagerCreate:
    def test_create_issue(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create(
            "There's a bug",
            title="Fix bug",
            priority="high",
            tags=["source:ai"],
            source="human",
        )

        assert issue.id == "001"
        assert issue.title == "Fix bug"
        assert issue.status == IssueStatus.OPEN
        assert issue.priority == "high"
        assert issue.tags == ["source:ai"]
        assert issue.source == "human"

        # YAML file should exist in open/
        files = list((tmp_path / "se3" / "issues" / "open").glob("*.yaml"))
        assert len(files) == 1
        assert files[0].name.startswith("001_")

    def test_create_description_only(self, tmp_path):
        """Create with only description — title/priority/type are None."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Some problem description")

        assert issue.id == "001"
        assert issue.title is None
        assert issue.priority is None
        assert issue.type is None
        assert issue.source == "system"
        assert issue.display_title == "Some problem description"

        # File slug derived from description
        files = list(mgr.open_dir.glob("001_*"))
        assert len(files) == 1
        assert "some-problem-description" in files[0].name

    def test_create_empty_description_rejected(self, tmp_path):
        mgr = IssueManager(tmp_path)
        with pytest.raises(ValueError, match="description must not be empty"):
            mgr.create("")
        with pytest.raises(ValueError, match="description must not be empty"):
            mgr.create("   \n  ")

    def test_create_empty_title_normalized_to_none(self, tmp_path):
        """Empty-string title is treated as None."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc text", title="   ")
        assert issue.title is None
        assert issue.display_title == "desc text"

    def test_create_increments_id(self, tmp_path):
        mgr = IssueManager(tmp_path)
        i1 = mgr.create("First issue")
        i2 = mgr.create("Second issue")
        i3 = mgr.create("Third issue")

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

        issue = mgr.create("New issue")
        assert issue.id == "006"

    def test_creates_directories(self, tmp_path):
        mgr = IssueManager(tmp_path)
        assert not mgr.open_dir.exists()
        mgr.create("Test")
        assert mgr.open_dir.exists()
        assert mgr.closed_dir.exists()

    def test_yaml_content_valid(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Check content", title="YAML Test", priority="low", tags=["test"])

        files = list(mgr.open_dir.glob("*.yaml"))
        data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert data["title"] == "YAML Test"
        assert data["priority"] == "low"
        assert data["tags"] == ["test"]
        assert data["status"] == "open"

    def test_create_with_source(self, tmp_path):
        mgr = IssueManager(tmp_path)
        human_issue = mgr.create("Human issue", source="human")
        system_issue = mgr.create("System issue")

        assert human_issue.source == "human"
        assert system_issue.source == "system"

        # Verify YAML files
        human_data = yaml.safe_load(
            (mgr.open_dir / f"{human_issue.id}_*.yaml").__class__(
                mgr.open_dir
            ).glob(f"{human_issue.id}_*.yaml").__next__().read_text(encoding="utf-8")
        )
        # Actually let me do this properly
        files = list(mgr.open_dir.glob(f"{human_issue.id}_*.yaml"))
        data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert data["source"] == "human"

        files = list(mgr.open_dir.glob(f"{system_issue.id}_*.yaml"))
        data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert data["source"] == "system"

    def test_yaml_omits_none_optional_fields(self, tmp_path):
        """When title/priority/type are None, they should not appear in YAML."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Description only")
        files = list(mgr.open_dir.glob(f"{issue.id}_*.yaml"))
        raw = files[0].read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        assert "title" not in data
        assert "priority" not in data
        assert "type" not in data


class TestIssueManagerLoad:
    def test_load_from_open(self, tmp_path):
        mgr = IssueManager(tmp_path)
        created = mgr.create("desc", title="Loadable")
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
        created = mgr.create("desc", title="Padded")
        assert created.id == "001"
        # Loading with "1" should also work
        loaded = mgr.load("1")
        assert loaded is not None
        assert loaded.id == "001"

    def test_load_legacy_yaml_without_source(self, tmp_path):
        """Legacy files without source field load as system."""
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        legacy_file = mgr.open_dir / "001_legacy.yaml"
        legacy_file.write_text(
            yaml.dump({"id": "001", "title": "Legacy", "description": "A desc", "status": "open"}),
            encoding="utf-8",
        )
        loaded = mgr.load("001")
        assert loaded is not None
        assert loaded.source == "system"

    def test_load_legacy_yaml_without_optional_fields(self, tmp_path):
        """Legacy files without priority/type load as None."""
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        legacy_file = mgr.open_dir / "002_old.yaml"
        legacy_file.write_text(
            yaml.dump({"id": "002", "title": "Old", "description": "d", "status": "open"}),
            encoding="utf-8",
        )
        loaded = mgr.load("002")
        assert loaded is not None
        assert loaded.priority is None
        assert loaded.type is None

    def test_load_legacy_yaml_without_description(self, tmp_path):
        """Legacy YAML with missing/empty description remains loadable and listable."""
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()

        # Missing description key entirely
        legacy1 = mgr.open_dir / "050_no_desc.yaml"
        legacy1.write_text(
            yaml.dump({"id": "050", "title": "No Desc", "status": "open"}),
            encoding="utf-8",
        )
        loaded = mgr.load("050")
        assert loaded is not None
        assert loaded.description == ""
        assert loaded.display_title == "No Desc"

        # Empty description value
        legacy2 = mgr.open_dir / "051_empty_desc.yaml"
        legacy2.write_text(
            yaml.dump({"id": "051", "title": "Empty Desc", "description": "", "status": "open"}),
            encoding="utf-8",
        )
        loaded = mgr.load("051")
        assert loaded is not None
        assert loaded.description == ""

        # Both issues should appear in list
        issues = mgr.list_issues()
        assert len(issues) == 2


class TestIssueManagerList:
    def test_list_empty(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        assert mgr.list_issues() == []

    def test_list_open_only(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("d1", title="Open 1")
        mgr.create("d2", title="Open 2")

        # Move one to closed
        mgr.update_status("002", IssueStatus.WONT_FIX)

        issues = mgr.list_issues(include_closed=False)
        assert len(issues) == 1
        assert issues[0].id == "001"

    def test_list_include_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("d1", title="Open 1")
        mgr.create("d2", title="Closed 1")
        mgr.update_status("002", IssueStatus.WONT_FIX)

        issues = mgr.list_issues(include_closed=True)
        assert len(issues) == 2

    def test_list_sorted_by_id(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("d")
        mgr.create("d")
        mgr.create("d")

        issues = mgr.list_issues()
        ids = [i.id for i in issues]
        assert ids == ["001", "002", "003"]

    def test_list_source_filter(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Human issue", source="human")
        mgr.create("System issue", source="system")
        mgr.create("Another human", source="human")

        human = mgr.list_issues(source_filter="human")
        assert len(human) == 2
        assert all(i.source == "human" for i in human)

        system = mgr.list_issues(source_filter="system")
        assert len(system) == 1
        assert system[0].source == "system"

        all_issues = mgr.list_issues()
        assert len(all_issues) == 3

    def test_list_combined_filters(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Bug from human", source="human", type="bug")
        mgr.create("Feature from human", source="human", type="feature")
        mgr.create("Bug from system", source="system", type="bug")

        bugs = mgr.list_issues(type_filter="bug", source_filter="human")
        assert len(bugs) == 1
        assert bugs[0].source == "human"
        assert bugs[0].type == "bug"


class TestIssueManagerStatusTransitions:
    def test_open_to_in_progress(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")
        updated = mgr.update_status("001", IssueStatus.IN_PROGRESS)
        assert updated.status == IssueStatus.IN_PROGRESS
        # Should still be in open/
        assert (mgr.open_dir / list(mgr.open_dir.glob("001_*"))[0].name).exists()

    def test_in_progress_to_resolved_moves_to_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")
        mgr.update_status("001", IssueStatus.IN_PROGRESS)
        mgr.update_status("001", IssueStatus.RESOLVED)

        # File should be in closed/
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1
        assert len(list(mgr.open_dir.glob("001_*"))) == 0

    def test_wont_fix_moves_to_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")
        mgr.update_status("001", IssueStatus.WONT_FIX)

        assert len(list(mgr.closed_dir.glob("001_*"))) == 1

    def test_resolved_to_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")
        mgr.update_status("001", IssueStatus.IN_PROGRESS)
        mgr.update_status("001", IssueStatus.RESOLVED)
        mgr.update_status("001", IssueStatus.CLOSED)

        loaded = mgr.load("001")
        assert loaded.status == IssueStatus.CLOSED

    def test_closed_to_open_moves_back(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")
        mgr.update_status("001", IssueStatus.WONT_FIX)
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1

        mgr.update_status("001", IssueStatus.OPEN)
        assert len(list(mgr.open_dir.glob("001_*"))) == 1
        assert len(list(mgr.closed_dir.glob("001_*"))) == 0

    def test_invalid_transition_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")

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
        mgr.create("Test")

        with patch("shutil.move", side_effect=OSError("Permission denied")):
            # Should not raise
            updated = mgr.update_status("001", IssueStatus.WONT_FIX)
            assert updated.status == IssueStatus.WONT_FIX


class TestIssueManagerReset:
    def test_reset_in_progress_to_open(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")
        mgr.update_status("001", IssueStatus.IN_PROGRESS)

        reset = mgr.reset_to_open("001")
        assert reset.status == IssueStatus.OPEN

    def test_reset_non_in_progress_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test")

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
            mgr.create(f"Issue {i}")
        assert mgr._next_id() == "006"


class TestIssueType:
    """Tests for Issue type field and filtering."""

    def test_issue_type_default_is_none(self):
        issue = Issue(id="001", title="Test", description="d")
        assert issue.type is None

    def test_issue_type_custom(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc", title="Feature X", type="feature")
        assert issue.type == "feature"
        loaded = mgr.load(issue.id)
        assert loaded.type == "feature"

    def test_issue_load_legacy_yaml(self, tmp_path):
        """Loading a YAML file without type field defaults to None."""
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        legacy_file = mgr.open_dir / "001_legacy.yaml"
        legacy_file.write_text(
            yaml.dump({"id": "001", "title": "Legacy", "description": "A desc", "status": "open"}),
            encoding="utf-8",
        )
        loaded = mgr.load("001")
        assert loaded is not None
        assert loaded.type is None

    def test_list_issues_type_filter(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("d", title="Bug 1", type="bug")
        mgr.create("d", title="Feature 1", type="feature")
        mgr.create("d", title="Bug 2", type="bug")
        mgr.create("d", title="Idea 1", type="idea")

        bugs = mgr.list_issues(type_filter="bug")
        assert len(bugs) == 2
        assert all(i.type == "bug" for i in bugs)

        features = mgr.list_issues(type_filter="feature")
        assert len(features) == 1
        assert features[0].type == "feature"

        all_issues = mgr.list_issues()
        assert len(all_issues) == 4

    def test_known_types_constant(self):
        assert len(KNOWN_TYPES) == 5
        assert "bug" in KNOWN_TYPES
        assert "feature" in KNOWN_TYPES
        assert "enhancement" in KNOWN_TYPES
        assert "idea" in KNOWN_TYPES
        assert "task" in KNOWN_TYPES

    def test_issue_yaml_roundtrip_with_type(self, tmp_path):
        mgr = IssueManager(tmp_path)
        created = mgr.create("desc", title="Typed Issue", type="enhancement")
        loaded = mgr.load(created.id)
        assert loaded.type == "enhancement"
        # Check YAML file has type field
        files = list(mgr.open_dir.glob(f"{created.id}_*"))
        data = yaml.safe_load(files[0].read_text(encoding="utf-8"))
        assert data["type"] == "enhancement"

    def test_to_dict_includes_type_when_set(self):
        issue = Issue(id="001", title="Test", description="d", type="feature")
        d = issue.to_dict()
        assert d["type"] == "feature"

    def test_to_dict_omits_type_when_none(self):
        issue = Issue(id="001", title="Test", description="d")
        d = issue.to_dict()
        assert "type" not in d

    def test_from_dict_missing_type_defaults_none(self):
        data = {"id": "001", "title": "Test", "description": "A desc"}
        issue = Issue.from_dict(data)
        assert issue.type is None


class TestCloseIssueOSError:
    """Tests that close_issue re-raises OSError on file move failure."""

    def test_close_issue_raises_oserror_on_move_failure(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("desc", title="Closable")

        with patch("shutil.move", side_effect=OSError("Permission denied")):
            with pytest.raises(OSError, match="Permission denied"):
                mgr.close_issue("001", reason="test close")

    def test_close_issue_normal_flow_unaffected(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("desc", title="Closable")

        closed = mgr.close_issue("001", reason="done")
        assert closed.status in (IssueStatus.CLOSED, IssueStatus.RESOLVED)
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1
        assert len(list(mgr.open_dir.glob("001_*"))) == 0


class TestUpdateFields:
    """Tests for IssueManager.update_fields method."""

    def test_update_title(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Original desc", title="Old Title")
        updated = mgr.update_fields(issue.id, title="New Title")

        assert updated.title == "New Title"
        assert updated.display_title == "New Title"

        # File should be renamed
        files = list(mgr.open_dir.glob(f"{issue.id}_*"))
        assert len(files) == 1
        assert "new-title" in files[0].name

    def test_update_description(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Original desc", title="Title")
        updated = mgr.update_fields(issue.id, description="Updated desc")

        assert updated.description == "Updated desc"

    def test_update_priority(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc")
        assert issue.priority is None

        updated = mgr.update_fields(issue.id, priority="high")
        assert updated.priority == "high"

    def test_update_type(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc")
        assert issue.type is None

        updated = mgr.update_fields(issue.id, type="feature")
        assert updated.type == "feature"

    def test_clear_optional_field_with_empty_string(self, tmp_path):
        """Passing empty string clears the field to None."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc", title="Has Title", priority="high", type="bug")

        updated = mgr.update_fields(issue.id, title="", priority="", type="")
        assert updated.title is None
        assert updated.priority is None
        assert updated.type is None

    def test_update_nonexistent_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        with pytest.raises(ValueError, match="not found"):
            mgr.update_fields("999", title="New")

    def test_update_empty_description_rejected(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Original")
        with pytest.raises(ValueError, match="description must not be empty"):
            mgr.update_fields(issue.id, description="")

    def test_rename_removes_stale_file(self, tmp_path):
        """When slug changes, old file is removed and no duplicate lingers."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Original desc", title="Alpha")

        old_files = list(mgr.open_dir.glob(f"{issue.id}_*"))
        assert len(old_files) == 1
        old_name = old_files[0].name

        updated = mgr.update_fields(issue.id, title="Beta")
        new_files = list(mgr.open_dir.glob(f"{issue.id}_*"))
        assert len(new_files) == 1
        assert new_files[0].name != old_name
        assert "beta" in new_files[0].name

    def test_update_preserves_unchanged_fields(self, tmp_path):
        """Only explicitly passed fields change; others are preserved."""
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc", title="Title", priority="low", type="bug")

        updated = mgr.update_fields(issue.id, title="New Title")
        assert updated.title == "New Title"
        assert updated.priority == "low"
        assert updated.type == "bug"

    def test_update_tags(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc", tags=["old-tag"])

        updated = mgr.update_fields(issue.id, tags=["new-tag", "another"])
        assert updated.tags == ["new-tag", "another"]


class TestReopenIssue:
    """Tests for IssueManager.reopen_issue method."""

    def test_reopen_resolved(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc")
        mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)
        mgr.update_status(issue.id, IssueStatus.RESOLVED)

        reopened = mgr.reopen_issue(issue.id)
        assert reopened.status == IssueStatus.OPEN
        assert len(list(mgr.open_dir.glob(f"{issue.id}_*"))) == 1
        assert len(list(mgr.closed_dir.glob(f"{issue.id}_*"))) == 0

    def test_reopen_wont_fix(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc")
        mgr.update_status(issue.id, IssueStatus.WONT_FIX)

        reopened = mgr.reopen_issue(issue.id)
        assert reopened.status == IssueStatus.OPEN

    def test_reopen_closed(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc")
        mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)
        mgr.update_status(issue.id, IssueStatus.RESOLVED)
        mgr.update_status(issue.id, IssueStatus.CLOSED)

        reopened = mgr.reopen_issue(issue.id)
        assert reopened.status == IssueStatus.OPEN

    def test_reopen_already_open_is_idempotent(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc")

        reopened = mgr.reopen_issue(issue.id)
        assert reopened.status == IssueStatus.OPEN

    def test_reopen_in_progress_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("desc")
        mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)

        with pytest.raises(ValueError, match="Cannot reopen"):
            mgr.reopen_issue(issue.id)

    def test_reopen_nonexistent_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()
        with pytest.raises(ValueError, match="not found"):
            mgr.reopen_issue("999")
