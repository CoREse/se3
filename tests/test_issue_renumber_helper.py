"""Unit tests for the shared renumber primitives (``issue_renumber``).

These primitives are the geometry both ``se3 merge`` channels (git three-way
merge of committed issues, and runtime-sync of uncommitted worktree issues)
reuse to renumber a colliding issue. The two easiest-to-get-wrong parts are
token-precise reference rewriting (must not clobber ``#1234`` / ``abc#123``,
must treat ``#14`` and ``#014`` as the same reference) and pushing ``.next_id``
to ``max(ID) + 1`` regardless of the counter's prior state — so those are
covered directly here rather than only through the two channels.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tianluo.engine.issue_manager import Issue, IssueManager, IssueStatus
from tianluo.engine.merge.issue_renumber import (
    advance_next_id_to_max,
    append_description_note,
    count_reference_tokens,
    find_issue_id_owner,
    format_ambiguous_reference_note,
    format_renumber_trace,
    live_reference_count,
    mask_issue_references,
    resolve_issue_numeric_id,
    rewrite_issue_references,
    rewrite_issue_references_bulk,
    rewrite_references_in_added_lines,
    strip_renumber_traces,
)


def _write_issue(
    mgr: IssueManager,
    issue_id: str,
    description: str,
    status: IssueStatus = IssueStatus.OPEN,
) -> Path:
    """Write a minimal issue YAML with an explicit ID into open/ or closed/."""
    mgr._ensure_dirs()
    issue = Issue(id=issue_id, description=description, status=status)
    target = mgr.closed_dir if status != IssueStatus.OPEN else mgr.open_dir
    path = target / f"{issue_id}_fixture.yaml"
    mgr._write_issue(path, issue)
    return path


def _read_next_id(project_root: Path) -> int:
    return int((project_root / "se3" / "issues" / ".next_id").read_text().strip())


# --------------------------------------------------------------------------
# rewrite_issue_references — token precision
# --------------------------------------------------------------------------

def test_rewrite_leaves_non_reference_hashnumbers_untouched(tmp_path: Path) -> None:
    """``#1234`` and ``abc#123`` are prose, not a ``#123`` reference."""
    mgr = IssueManager(tmp_path)
    # A ``#1234`` (superstring), an ``abc#123`` (leading alnum), and a genuine
    # standalone ``#123`` all in one body — only the last must change.
    p = _write_issue(mgr, "050", "see #1234 and abc#123 but fix #123 now")

    n = rewrite_issue_references(tmp_path, "123", "240")

    assert n == 1
    body = p.read_text()
    assert "#1234" in body          # superstring untouched
    assert "abc#123" in body        # leading-alnum token untouched
    assert "#240" in body           # genuine reference rewritten
    # The only bare "#123" left must be the one inside "#1234"; no standalone.
    assert " #123 " not in body and not body.endswith(" #123")


def test_rewrite_leaves_trailing_alnum_tokens_untouched(tmp_path: Path) -> None:
    """``#123abc`` is prose (trailing alnum), not a ``#123`` reference."""
    mgr = IssueManager(tmp_path)
    p = _write_issue(mgr, "051", "label #123abc but fix #123 now")

    n = rewrite_issue_references(tmp_path, "123", "240")

    assert n == 1
    body = p.read_text()
    assert "#123abc" in body        # trailing-alnum token untouched
    assert "#240" in body           # genuine reference rewritten
    assert "#240abc" not in body


def test_rewrite_treats_zero_padding_as_equivalent(tmp_path: Path) -> None:
    """``#14`` and ``#014`` denote the same issue and both get rewritten."""
    mgr = IssueManager(tmp_path)
    p = _write_issue(mgr, "060", "blocks #14 and also #014 and #0014")

    n = rewrite_issue_references(tmp_path, "014", "240")

    assert n == 3
    body = p.read_text()
    assert "#14" not in body.replace("#240", "")  # every variant became #240
    assert body.count("#240") == 3


def test_rewrite_counts_across_open_and_closed(tmp_path: Path) -> None:
    """A rewrite spans both open/ and closed/ and returns the total count."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "070", "refs #93 and #093", status=IssueStatus.OPEN)
    _write_issue(mgr, "071", "closed but mentions #93", status=IssueStatus.RESOLVED)

    n = rewrite_issue_references(tmp_path, "093", "300")

    assert n == 3  # two in open (#93, #093) + one in closed (#93)


def test_rewrite_confined_to_issues_dir(tmp_path: Path) -> None:
    """References outside se3/issues/ are never touched."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "080", "fix #55")
    stray = tmp_path / "se3" / "logs"
    stray.mkdir(parents=True)
    (stray / "note.txt").write_text("unrelated #55 mention")

    rewrite_issue_references(tmp_path, "055", "240")

    assert (stray / "note.txt").read_text() == "unrelated #55 mention"


def test_rewrite_new_id_is_zero_padded(tmp_path: Path) -> None:
    """The replacement token is zero-padded to at least three digits."""
    mgr = IssueManager(tmp_path)
    p = _write_issue(mgr, "090", "depends on #55")

    rewrite_issue_references(tmp_path, "55", "7")

    assert "#007" in p.read_text()


# --------------------------------------------------------------------------
# rewrite_issue_references_bulk — simultaneous multi-pair rewrite
# --------------------------------------------------------------------------

def test_bulk_rewrite_does_not_chain_through_overlapping_pairs(
    tmp_path: Path,
) -> None:
    """A pair's new ID equal to another pair's old ID must not chain.

    With the map {005→010, 010→011}, sequential single-pair passes would
    turn an original ``#005`` into ``#010`` and then wrongly onward into
    ``#011``. The bulk pass resolves each token against the original text.
    """
    mgr = IssueManager(tmp_path)
    p = _write_issue(mgr, "020", "after #005 and before #010")

    n = rewrite_issue_references_bulk(
        tmp_path, {"005": "010", "010": "011"},
    )

    assert n == 2
    body = p.read_text()
    assert "#010" in body  # original #005 → #010, and STAYS there
    assert "#011" in body  # original #010 → #011
    assert "#005" not in body


def test_bulk_rewrite_ignores_identity_pairs_and_scopes(tmp_path: Path) -> None:
    """Identity pairs are no-ops and out-of-scope files stay untouched."""
    mgr = IssueManager(tmp_path)
    in_scope = _write_issue(mgr, "030", "refs #004")
    out_scope = _write_issue(mgr, "031", "also refs #004")

    n = rewrite_issue_references_bulk(
        tmp_path,
        {"004": "009", "007": "007"},
        scope_files=[in_scope],
    )

    assert n == 1
    assert "#009" in in_scope.read_text()
    assert "#004" in out_scope.read_text()


# --------------------------------------------------------------------------
# mask_issue_references — signature canonicalization
# --------------------------------------------------------------------------

def test_mask_replaces_only_standalone_reference_tokens() -> None:
    masked = mask_issue_references("fix #001, see #14; but abc#123 stays")
    assert masked == "fix #REF, see #REF; but abc#123 stays"


def test_mask_makes_rewritten_reference_sign_equal() -> None:
    """The pre- and post-renumber renderings of a body mask identically."""
    assert mask_issue_references("see #001") == mask_issue_references("see #004")


# --------------------------------------------------------------------------
# advance_next_id_to_max — counter recovery
# --------------------------------------------------------------------------

def test_advance_from_missing_counter(tmp_path: Path) -> None:
    """A missing .next_id is created at max(ID)+1."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "005", "a")
    _write_issue(mgr, "012", "b", status=IssueStatus.RESOLVED)
    counter = tmp_path / "se3" / "issues" / ".next_id"
    assert not counter.exists()

    result = advance_next_id_to_max(tmp_path)

    assert result == 13
    assert _read_next_id(tmp_path) == 13


def test_advance_from_lagging_counter(tmp_path: Path) -> None:
    """A counter that lags the true max is pushed forward to max+1."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "005", "a")
    _write_issue(mgr, "040", "b")
    counter = tmp_path / "se3" / "issues" / ".next_id"
    counter.write_text("6")  # stale: behind the real max of 40

    result = advance_next_id_to_max(tmp_path)

    assert result == 41
    assert _read_next_id(tmp_path) == 41


def test_advance_from_garbage_counter(tmp_path: Path) -> None:
    """A corrupt counter value is discarded and recomputed from the files."""
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "022", "a")
    counter = tmp_path / "se3" / "issues" / ".next_id"
    counter.write_text("not-a-number")

    result = advance_next_id_to_max(tmp_path)

    assert result == 23
    assert _read_next_id(tmp_path) == 23


def test_advance_with_no_issues(tmp_path: Path) -> None:
    """With an empty store the counter starts at 1 (max of 0, +1)."""
    result = advance_next_id_to_max(tmp_path)
    assert result == 1
    assert _read_next_id(tmp_path) == 1


def test_advance_counts_parsed_id_not_just_filename(tmp_path: Path) -> None:
    """A parsed ``id`` field that outruns its filename prefix still counts.

    ``005_mismatch.yaml`` carrying ``id: '100'`` owns the number 100; a max
    computed from filenames alone would write 6 and a later allocation could
    mint 100 again, reintroducing a duplicate parsed ID.
    """
    mgr = IssueManager(tmp_path)
    mgr._ensure_dirs()
    issue = Issue(id="100", description="parsed id outruns the filename")
    mgr._write_issue(mgr.open_dir / "005_mismatch.yaml", issue)

    result = advance_next_id_to_max(tmp_path)

    assert result == 101
    assert _read_next_id(tmp_path) == 101


def test_find_issue_id_owner_prefers_parsed_id_over_filename(
    tmp_path: Path,
) -> None:
    """Ownership follows the parsed ``id``, not the filename prefix.

    ``010_main.yaml`` carrying ``id: '005'`` owns the number 5 (not 10); a
    filename-prefix-only lookup for ``005`` would miss it and wrongly declare
    the number unowned.
    """
    mgr = IssueManager(tmp_path)
    mgr._ensure_dirs()
    issue = Issue(id="005", description="parsed id 5 under a 010 filename")
    path = mgr.open_dir / "010_main.yaml"
    mgr._write_issue(path, issue)

    assert resolve_issue_numeric_id(path) == 5
    assert find_issue_id_owner(tmp_path, "005") == path
    # The filename prefix (010) is NOT the parsed identity, so no owner of 10.
    assert find_issue_id_owner(tmp_path, "010") is None
    # A number nothing owns yields None.
    assert find_issue_id_owner(tmp_path, "007") is None


def test_find_issue_id_owner_excludes_given_files(tmp_path: Path) -> None:
    """The adopted file (now carrying the new ID) is excluded from ownership.

    Falls back to the filename prefix when a body has no parseable ``id``.
    """
    mgr = IssueManager(tmp_path)
    only = _write_issue(mgr, "004", "sole owner of 4")

    assert find_issue_id_owner(tmp_path, "004") == only
    assert find_issue_id_owner(tmp_path, "004", exclude_files=[only]) is None


def test_advance_never_lowers_ahead_counter(tmp_path: Path) -> None:
    """A counter AHEAD of the scanned max is left as-is, never lowered.

    An ahead value may be a peer allocator's LIVE reservation: ``_next_id``
    reserves ``#N`` by writing ``N+1`` to the counter *before* the
    ``N_*.yaml`` file exists on disk. Pulling the counter back to the
    on-disk max+1 would let a third allocator re-mint that reserved number,
    creating two files with the same numeric ID. Skipping numbers is
    harmless; reusing one is a hard-guarantee violation, so the advance is
    monotonic.
    """
    mgr = IssueManager(tmp_path)
    _write_issue(mgr, "010", "a")
    counter = tmp_path / "se3" / "issues" / ".next_id"
    counter.write_text("100")  # ahead: possibly a live reservation, keep it

    result = advance_next_id_to_max(tmp_path)

    assert result == 100
    assert _read_next_id(tmp_path) == 100


# --------------------------------------------------------------------------
# rewrite_references_in_added_lines — merge-modified pre-existing files
# --------------------------------------------------------------------------

def test_added_lines_rewrite_only_touches_new_lines(tmp_path: Path) -> None:
    """Only lines absent from the baseline follow the renumber."""
    f = tmp_path / "issue.yaml"
    baseline = "Ref holder\n\nLegacy pointer #005.\n"
    f.write_text(baseline + "Blocked by #005 until it lands.\n", encoding="utf-8")

    n = rewrite_references_in_added_lines(f, baseline, "005", "011")

    assert n == 1
    body = f.read_text(encoding="utf-8")
    # The pre-existing reference still names the issue that kept #005.
    assert "Legacy pointer #005." in body
    # The merge-added reference follows the renumbered issue.
    assert "Blocked by #011 until it lands." in body


def test_added_lines_rewrite_is_token_precise(tmp_path: Path) -> None:
    """Added-line rewriting keeps the same #NNN token boundaries."""
    f = tmp_path / "issue.yaml"
    baseline = "original line\n"
    f.write_text(baseline + "see #1234 and abc#123 but fix #123 now\n", encoding="utf-8")

    n = rewrite_references_in_added_lines(f, baseline, "123", "240")

    assert n == 1
    body = f.read_text(encoding="utf-8")
    assert "#1234" in body
    assert "abc#123" in body
    assert "#240" in body


def test_added_lines_rewrite_no_hits_leaves_file_untouched(tmp_path: Path) -> None:
    """A file whose added lines carry no matching reference is not rewritten."""
    f = tmp_path / "issue.yaml"
    baseline = "points at #005\n"
    content = baseline + "an added line without references\n"
    f.write_text(content, encoding="utf-8")

    n = rewrite_references_in_added_lines(f, baseline, "005", "011")

    assert n == 0
    assert f.read_text(encoding="utf-8") == content


def test_added_lines_rewrite_survives_yaml_quote_style_shift(tmp_path: Path) -> None:
    """Appending a description line must not misclassify the previous line.

    PyYAML folds multi-line descriptions into quoted scalars, so appending a
    line physically moves the closing quote off the previously-last line. A
    physical-line comparison would see that untouched line as "added" and
    wrongly repoint its kept-side #005 — the comparison must run on the parsed
    string's logical lines instead.
    """
    def _dump(description: str) -> str:
        return yaml.dump(
            {"id": "010", "description": description, "status": "open"},
            default_flow_style=False, allow_unicode=True, sort_keys=False,
        )

    baseline = _dump("Ref holder\n\nLegacy pointer #005.")
    f = tmp_path / "010_refholder.yaml"
    f.write_text(
        _dump("Ref holder\n\nLegacy pointer #005.\nBlocked by #005 until it lands."),
        encoding="utf-8",
    )

    n = rewrite_references_in_added_lines(f, baseline, "005", "011")

    assert n == 1
    desc = yaml.safe_load(f.read_text(encoding="utf-8"))["description"]
    # Pre-existing logical line still names the issue that kept #005 ...
    assert "Legacy pointer #005." in desc
    # ... while the branch-added line follows the renumber.
    assert "Blocked by #011 until it lands." in desc


def test_added_duplicate_of_existing_line_still_follows_renumber(
    tmp_path: Path,
) -> None:
    """An added line identical to a pre-existing one is still merge-added.

    Baseline membership must be counted per occurrence, not per distinct
    text: when the branch appends a second copy of a line the baseline
    already held once, only ONE occurrence is pre-existing — the duplicate
    is new and its reference must follow the renumber.
    """
    def _dump(description: str) -> str:
        return yaml.dump(
            {"id": "010", "description": description, "status": "open"},
            default_flow_style=False, allow_unicode=True, sort_keys=False,
        )

    line = "Blocked by #005 until it lands."
    baseline = _dump(f"Ref holder\n\n{line}")
    f = tmp_path / "010_refholder.yaml"
    f.write_text(_dump(f"Ref holder\n\n{line}\n{line}"), encoding="utf-8")

    n = rewrite_references_in_added_lines(f, baseline, "005", "011")

    assert n == 1
    desc = yaml.safe_load(f.read_text(encoding="utf-8"))["description"]
    # Exactly one occurrence keeps the kept-side #005 (the baseline budget),
    # and the appended duplicate follows the renumber.
    assert desc.count("Blocked by #005 until it lands.") == 1
    assert desc.count("Blocked by #011 until it lands.") == 1


def test_added_duplicate_line_raw_fallback_still_follows_renumber(
    tmp_path: Path,
) -> None:
    """The non-YAML raw fallback also counts baseline lines per occurrence."""
    f = tmp_path / "notes.txt"
    baseline = "- item\n- see #005\n"
    # Not a YAML mapping -> physical-line fallback path.
    f.write_text(baseline + "- see #005\n", encoding="utf-8")

    n = rewrite_references_in_added_lines(f, baseline, "005", "011")

    assert n == 1
    body = f.read_text(encoding="utf-8")
    assert body.count("- see #005\n") == 1
    assert body.count("- see #011\n") == 1


# --------------------------------------------------------------------------
# format_renumber_trace
# --------------------------------------------------------------------------

def test_trace_contains_old_and_new_ids() -> None:
    trace = format_renumber_trace("014", "240")
    assert "#014" in trace
    assert "#240" in trace


def test_trace_normalizes_and_zero_pads() -> None:
    """Trace zero-pads both IDs regardless of input padding/type."""
    assert format_renumber_trace(14, 7) == "旧号 #014 → 新号 #007 (se3 merge)"


def test_trace_appended_preserves_display_title(tmp_path: Path) -> None:
    """Appending the trace at the tail leaves display_title/slug derivation intact."""
    desc = "Fix the merge bug\n\nmore details"
    trace = format_renumber_trace("014", "240")
    issue = Issue(id="240", description=f"{desc}\n\n{trace}")
    # display_title takes the first non-empty line — appending at the end keeps it.
    assert issue.display_title == "Fix the merge bug"
    assert trace in issue.description


# --------------------------------------------------------------------------
# ambiguity primitives — note formatting, stripping, live-reference detection
# --------------------------------------------------------------------------

def test_ambiguous_note_lists_all_candidates_zero_padded() -> None:
    note = format_ambiguous_reference_note(5, ["011", 12])
    assert note == "歧义引用 #005 → 候选 #011 / #012 (se3 merge)"


def test_strip_removes_traces_and_ambiguity_notes() -> None:
    """Both audit-line kinds are stripped, live text is kept verbatim.

    Idempotent re-runs depend on this: the adopted copy carries the audit
    lines its worktree source lacks, and only the stripped forms may be
    compared.
    """
    body = "\n".join([
        "Dependent issue",
        "",
        "Blocked by #005 until fixed",
        "",
        format_renumber_trace("007", "004"),
        format_ambiguous_reference_note("005", ["002", "003"]),
    ])
    stripped = strip_renumber_traces(body)
    assert "Blocked by #005 until fixed" in stripped
    assert "旧号" not in stripped
    assert "歧义引用" not in stripped


def test_count_reference_tokens_is_token_precise() -> None:
    text = "see #1234 and abc#123 but fix #123 plus #0123 now"
    # Superstring and leading-alnum forms are prose; zero-padding is absorbed.
    assert count_reference_tokens(text, "123") == 2
    assert count_reference_tokens(text, 1234) == 1
    assert count_reference_tokens("", "123") == 0


def test_live_reference_count_ignores_audit_lines(tmp_path: Path) -> None:
    """A trace's historical ``#005`` is a record, not a live reference."""
    mgr = IssueManager(tmp_path)
    live = _write_issue(
        mgr, "010",
        "Dependent issue\n\nBlocked by #005 until fixed\n\n"
        + format_renumber_trace("005", "004"),
    )
    audit_only = _write_issue(
        mgr, "011",
        "Renumbered issue\n\nno live refs\n\n"
        + format_renumber_trace("005", "011")
        + "\n"
        + format_ambiguous_reference_note("005", ["002", "003"]),
    )
    assert live_reference_count(live, "005") == 1
    assert live_reference_count(audit_only, "005") == 0
    assert live_reference_count(tmp_path / "absent.yaml", "005") == 0


def test_append_description_note_preserves_title_and_parses(
    tmp_path: Path,
) -> None:
    mgr = IssueManager(tmp_path)
    path = _write_issue(mgr, "010", "Dependent issue\n\nBlocked by #005")
    note = format_ambiguous_reference_note("005", ["002", "003"])
    append_description_note(path, note)

    issue = mgr._read_issue(path)
    assert issue is not None
    assert issue.display_title == "Dependent issue"
    assert issue.description.endswith(note)
    assert "Blocked by #005" in issue.description


def test_added_lines_dry_run_counts_without_writing(tmp_path: Path) -> None:
    """dry_run reports the would-be hits and leaves the file byte-identical."""
    mgr = IssueManager(tmp_path)
    baseline_desc = "Pre-existing issue\n\nkeep #005 as is"
    path = _write_issue(mgr, "020", baseline_desc)
    baseline_text = path.read_text(encoding="utf-8")

    issue = mgr._read_issue(path)
    issue.description += "\nBlocked by #005 too"
    mgr._write_issue(path, issue)
    before = path.read_text(encoding="utf-8")

    hits = rewrite_references_in_added_lines(
        path, baseline_text, "005", "005", dry_run=True,
    )
    assert hits == 1
    assert path.read_text(encoding="utf-8") == before
