"""Tests for tianluo.engine.prompt_history module."""

from __future__ import annotations

from pathlib import Path

from prompt_toolkit.history import FileHistory

from tianluo.engine.prompt_history import (
    HISTORY_DIR,
    HISTORY_FILENAME,
    MAX_ENTRIES,
    _truncate_history,
    get_prompt_history,
)


def test_get_prompt_history_creates_directory_and_returns_file_history(tmp_path: Path):
    """get_prompt_history creates tianluo/history/ dir and returns FileHistory."""
    history = get_prompt_history(tmp_path)

    assert isinstance(history, FileHistory)
    history_dir = tmp_path / "tianluo" / HISTORY_DIR
    assert history_dir.is_dir()
    # FileHistory doesn't create the file until an entry is stored;
    # verify the directory is ready for it
    history_file = history_dir / HISTORY_FILENAME
    # Store an entry to confirm the path is writable
    history.store_string("test entry")
    assert history_file.exists()


def test_get_prompt_history_idempotent(tmp_path: Path):
    """Calling get_prompt_history multiple times works without error."""
    h1 = get_prompt_history(tmp_path)
    h2 = get_prompt_history(tmp_path)

    assert isinstance(h1, FileHistory)
    assert isinstance(h2, FileHistory)


def test_get_prompt_history_preserves_existing_entries(tmp_path: Path):
    """Entries written by one FileHistory are readable by a new one."""
    history_dir = tmp_path / "tianluo" / HISTORY_DIR
    history_dir.mkdir(parents=True)
    history_file = history_dir / HISTORY_FILENAME

    # Write entries in prompt_toolkit's format
    history_file.write_text(
        "+first entry\n\n+second entry\n\n", encoding="utf-8"
    )

    history = get_prompt_history(tmp_path)
    # FileHistory loads strings; load_history_strings returns newest first
    strings = list(history.load_history_strings())
    assert len(strings) == 2
    assert "first entry" in strings
    assert "second entry" in strings


def test_truncate_history_no_op_when_under_limit(tmp_path: Path):
    """_truncate_history does nothing when entries <= max_entries."""
    history_file = tmp_path / "prompt_history"
    content = "".join(f"+entry {i}\n\n" for i in range(10))
    history_file.write_text(content, encoding="utf-8")

    _truncate_history(history_file, max_entries=10)

    # All entries should remain
    result = history_file.read_text(encoding="utf-8")
    for i in range(10):
        assert f"+entry {i}" in result


def test_truncate_history_keeps_last_n_entries(tmp_path: Path):
    """_truncate_history keeps only the last max_entries entries."""
    history_file = tmp_path / "prompt_history"
    content = "".join(f"+entry {i}\n\n" for i in range(20))
    history_file.write_text(content, encoding="utf-8")

    _truncate_history(history_file, max_entries=5)

    result = history_file.read_text(encoding="utf-8")
    # First 15 should be gone
    for i in range(15):
        assert f"+entry {i}\n" not in result
    # Last 5 should remain
    for i in range(15, 20):
        assert f"+entry {i}" in result


def test_truncate_history_preserves_multiline_entries(tmp_path: Path):
    """_truncate_history preserves multiline entries correctly."""
    history_file = tmp_path / "prompt_history"
    # Two multiline entries, then one single-line
    content = (
        "+line1 of entry1\n+line2 of entry1\n\n"
        "+line1 of entry2\n+line2 of entry2\n+line3 of entry2\n\n"
        "+single line entry3\n\n"
    )
    history_file.write_text(content, encoding="utf-8")

    _truncate_history(history_file, max_entries=2)

    result = history_file.read_text(encoding="utf-8")
    # entry1 should be gone
    assert "+line1 of entry1" not in result
    # entry2 and entry3 should remain intact
    assert "+line1 of entry2" in result
    assert "+line2 of entry2" in result
    assert "+line3 of entry2" in result
    assert "+single line entry3" in result


def test_truncate_history_no_op_on_missing_file(tmp_path: Path):
    """_truncate_history does nothing when file doesn't exist."""
    missing = tmp_path / "nonexistent"
    _truncate_history(missing)  # Should not raise


def test_truncate_history_no_op_on_empty_file(tmp_path: Path):
    """_truncate_history does nothing on an empty file."""
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    _truncate_history(empty)
    assert empty.read_text() == ""
