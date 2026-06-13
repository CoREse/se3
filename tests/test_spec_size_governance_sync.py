"""G8 — sync base migration / parallel-spec split / domain backfill governance.

Covers the deterministic (no-LLM) *mechanism* of the spec volume-governance
refactors that ``se3 sync`` performs, the respond-channel plumbing that gates
them, and the LLM-assisted *proposal* generation (with a mocked caller).

The real base-content migration is a one-time, respond-confirmed runtime data
operation a user runs once the capability is ready; these tests construct
fixtures to assert the capability's correctness, and assert that the
navigation / index layer that backs the refactors never invokes an LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.engine import sync_governance as g
from se3.engine import sync_interaction as si
from se3.engine.sync_engine import SyncEngine, _governance_prompt_injection
from se3.engine.sync_governance import BaseMigration, SplitProposal
from se3.engine.spec_index import load_or_build
from se3.engine.spec_validator import validate_spec_structure


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

BASE_SPEC = """<!-- spec-format: v1 -->
<!-- domain: project -->

# base Specification

## Purpose

Project baseline conventions, in one sentence.

## Requirements

### Requirement: Project Identity
SE3 framework identity in one line. See base::Daemon Modules for the daemon.

### Requirement: Daemon Modules
The daemon package submodule detail belongs to the daemon subsystem.

### Requirement: Coding Conventions
PEP 8 style throughout the project.
"""

DAEMON_SPEC = """<!-- spec-format: v1 -->

# daemon Specification

## Purpose

The resident control-plane daemon, in one sentence.

## Requirements

### Requirement: Daemon Lifecycle
start / stop / status.
"""

BIG_SPEC = """<!-- spec-format: v1 -->

# big Specification

## Purpose

A multi-topic spec, in one sentence.

## Requirements

### Requirement: Topic A One
A1 opening summary. May reference big::Topic B One.

### Requirement: Topic A Two
A2 opening summary.

### Requirement: Topic B One
B1 opening summary.

### Requirement: Topic B Two
B2 opening summary.
"""

OTHER_SPEC = """<!-- spec-format: v1 -->

# other Specification

## Purpose

An unrelated spec, in one sentence.

## Requirements

### Requirement: X
Cross reference to big::Topic B One lives here.
"""


def _write_spec(root: Path, name: str, content: str) -> Path:
    p = root / "se3" / "specs" / name / "spec.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _make_project(root: Path, specs: dict) -> None:
    for name, content in specs.items():
        _write_spec(root, name, content)


# ---------------------------------------------------------------------------
# pure helpers: domain markers
# ---------------------------------------------------------------------------

class TestDomainHelpers:
    def test_domain_of_present_and_absent(self):
        assert g.domain_of(BASE_SPEC) == "project"
        assert g.domain_of(DAEMON_SPEC) is None
        assert g.has_domain_marker(BASE_SPEC) is True
        assert g.has_domain_marker(DAEMON_SPEC) is False

    def test_ensure_inserts_after_v1_marker(self):
        out = g.ensure_domain_marker(DAEMON_SPEC, "engine/daemon")
        lines = out.splitlines()
        assert lines[0] == "<!-- spec-format: v1 -->"
        assert lines[1] == "<!-- domain: engine/daemon -->"
        assert g.domain_of(out) == "engine/daemon"
        # The inserted marker must not break structural validation.
        assert validate_spec_structure(out, "daemon").passed

    def test_ensure_replaces_existing(self):
        out = g.ensure_domain_marker(BASE_SPEC, "renamed/path")
        assert g.domain_of(out) == "renamed/path"
        # Only one domain marker remains.
        assert out.count("<!-- domain:") == 1

    def test_ensure_empty_domain_is_noop(self):
        assert g.ensure_domain_marker(DAEMON_SPEC, "") == DAEMON_SPEC
        assert g.ensure_domain_marker(DAEMON_SPEC, "   ") == DAEMON_SPEC


# ---------------------------------------------------------------------------
# pure helpers: requirement cut / paste / refs
# ---------------------------------------------------------------------------

class TestRequirementTransforms:
    def test_split_out_requirements(self):
        remaining, blocks = g.split_out_requirements(BASE_SPEC, ["Daemon Modules"])
        assert set(blocks) == {"Daemon Modules"}
        assert blocks["Daemon Modules"].startswith("### Requirement: Daemon Modules")
        assert "daemon package submodule detail" in blocks["Daemon Modules"]
        # remaining lost that requirement but kept the others.
        assert "### Requirement: Daemon Modules" not in remaining
        assert "### Requirement: Project Identity" in remaining
        assert "### Requirement: Coding Conventions" in remaining
        assert validate_spec_structure(remaining, "base").passed

    def test_split_out_unknown_name_ignored(self):
        remaining, blocks = g.split_out_requirements(BASE_SPEC, ["Nope"])
        assert blocks == {}
        assert remaining.strip() == BASE_SPEC.strip()

    def test_append_requirements(self):
        block = "### Requirement: New One\nbody."
        out = g.append_requirements(DAEMON_SPEC, [block])
        assert "### Requirement: New One" in out
        assert validate_spec_structure(out, "daemon").passed

    def test_build_parallel_spec_is_valid(self):
        blocks = ["### Requirement: Topic B One\nB1.", "### Requirement: Topic B Two\nB2."]
        spec = g.build_parallel_spec("big-b", blocks, domain="engine/big", purpose="Topic B cluster.")
        assert validate_spec_structure(spec, "big-b").passed
        assert g.domain_of(spec) == "engine/big"
        assert "## Purpose" in spec and "Topic B cluster." in spec

    def test_rewrite_moved_refs(self):
        text = "see base::Daemon Modules and base::Project Identity."
        out = g.rewrite_moved_refs(text, {("base", "Daemon Modules"): ("daemon", "Daemon Modules")})
        assert "daemon::Daemon Modules" in out
        assert "base::Project Identity" in out  # untouched
        assert "base::Daemon Modules" not in out

    def test_rewrite_moved_refs_prefix_name_not_corrupted(self):
        # Edge case: one Requirement name is a literal prefix of another in the
        # same spec. Moving only the shorter ``Auth`` must NOT corrupt the
        # longer ``Auth Token`` reference (a blind substring replace would).
        text = "see base::Auth and also base::Auth Token here."
        out = g.rewrite_moved_refs(text, {("base", "Auth"): ("authsvc", "Auth")})
        # The standalone ``base::Auth`` was rewritten.
        assert "authsvc::Auth and" in out
        # The longer ``base::Auth Token`` is left intact.
        assert "base::Auth Token" in out
        # And it was NOT corrupted into the moved spec.
        assert "authsvc::Auth Token" not in out

    def test_rewrite_moved_refs_prefix_both_moved(self):
        # When both the prefix and the longer name are moved, each rewrites to
        # its own target independently (refs delimited so the greedy name span
        # stops cleanly at the punctuation boundary).
        text = "alpha base::Auth, beta base::Auth Token."
        out = g.rewrite_moved_refs(
            text,
            {
                ("base", "Auth"): ("authsvc", "Auth"),
                ("base", "Auth Token"): ("authsvc", "Auth Token"),
            },
        )
        assert "authsvc::Auth," in out
        assert "authsvc::Auth Token" in out
        assert "base::Auth" not in out

    def test_rewrite_moved_refs_lowercase_known_name_not_corrupted(self):
        # A distinct Requirement whose name extends the moved one with a
        # *lowercase* word (``Foo bar``) must be preserved when only ``Foo``
        # moves. The capitalization heuristic alone would miss this; the known
        # requirement-name set disambiguates it.
        text = "see base::Foo and base::Foo bar here."
        out = g.rewrite_moved_refs(
            text,
            {("base", "Foo"): ("target", "Foo")},
            known_reqs={"base": ["Foo", "Foo bar"]},
        )
        assert "target::Foo and" in out
        # The distinct lowercase-continuation reference is untouched.
        assert "base::Foo bar" in out
        assert "target::Foo bar" not in out

    def test_relink_intra_lowercase_known_name_not_corrupted(self):
        text = "See Requirement: Foo bar for detail; also Requirement: Foo here."
        # final_location only relocates ``Foo``; the known-name set carries the
        # distinct lowercase-extended ``Foo bar`` so it is not corrupted.
        out = g.relink_intra_spec_refs(
            text, "base", {"Foo": "daemon"}, known_reqs=["Foo", "Foo bar"]
        )
        assert "Requirement: Foo bar for detail" in out  # untouched
        assert "daemon::Foo here" in out

    def test_requirement_names_order(self):
        assert g.requirement_names(BASE_SPEC) == [
            "Project Identity", "Daemon Modules", "Coding Conventions",
        ]

    def test_relink_intra_spec_refs_source_points_at_moved(self):
        # Home = source spec; a `Requirement: Foo` pointing at a Requirement that
        # moved out is rewritten to the inter-spec form; one that stayed is kept.
        text = "See Requirement: Foo for detail; also Requirement: Bar here."
        final_location = {"Foo": "daemon", "Bar": "base"}
        out = g.relink_intra_spec_refs(text, "base", final_location)
        assert "daemon::Foo for detail" in out
        assert "Requirement: Bar here" in out  # stayed — intra form kept
        assert "Requirement: Foo" not in out

    def test_relink_intra_spec_refs_moved_block_points_back(self):
        # Home = target spec (a moved block); a `Requirement: Bar` pointing back
        # at a Requirement that stayed in the source is rewritten to source::Bar,
        # while a reference to the block's own (co-moved) name stays intra.
        block = (
            "### Requirement: Foo\n"
            "Depends on Requirement: Bar that stayed; see Requirement: Foo too."
        )
        final_location = {"Foo": "daemon", "Bar": "base"}
        out = g.relink_intra_spec_refs(block, "daemon", final_location)
        assert "base::Bar that stayed" in out
        assert "Requirement: Foo too" in out  # co-located — intra form kept
        # The boundary heading is never rewritten.
        assert out.startswith("### Requirement: Foo\n")

    def test_relink_intra_spec_refs_prefix_collision_guarded(self):
        # `Requirement: Foo` must not match the longer `Requirement: Foo Bar`.
        text = "See Requirement: Foo Bar here."
        out = g.relink_intra_spec_refs(text, "base", {"Foo": "daemon"})
        assert out == text  # unchanged


# ---------------------------------------------------------------------------
# validator tolerance for the header domain marker
# ---------------------------------------------------------------------------

def test_validator_accepts_domain_marker_before_title():
    # The canonical placement is the domain marker right after the v1 marker
    # and before the title; this must still validate.
    assert validate_spec_structure(BASE_SPEC, "base").passed


# ---------------------------------------------------------------------------
# SyncEngine: base migration
# ---------------------------------------------------------------------------

class TestBaseMigration:
    def test_migrate_moves_relinks_and_reindexes(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        res = eng.migrate_requirements([BaseMigration("Daemon Modules", "daemon")])

        assert res["specs_updated"] >= 1
        assert {"requirement_name": "Daemon Modules", "target_spec": "daemon"} in res["migrated"]

        new_base = _spec_text(tmp_path, "base")
        new_daemon = _spec_text(tmp_path, "daemon")
        assert "### Requirement: Daemon Modules" not in new_base
        assert "### Requirement: Daemon Modules" in new_daemon
        assert validate_spec_structure(new_base, "base").passed
        assert validate_spec_structure(new_daemon, "daemon").passed

        # base::Daemon Modules cross-ref (in Project Identity) was relinked.
        assert "daemon::Daemon Modules" in new_base
        assert "base::Daemon Modules" not in new_base

        # Logical address moved in the index.
        idx = load_or_build(tmp_path)
        assert idx.resolve_item_location("daemon", "Daemon Modules") is not None
        assert idx.resolve_item_location("base", "Daemon Modules") is None

    def test_migrate_aborts_atomically_on_validation_failure(self, tmp_path):
        # Moving EVERY requirement out of base would leave base with zero
        # Requirements (invalid). The migration must abort and write nothing.
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        res = eng.migrate_requirements([
            BaseMigration("Project Identity", "daemon"),
            BaseMigration("Daemon Modules", "daemon"),
            BaseMigration("Coding Conventions", "daemon"),
        ])
        assert res["specs_updated"] == 0
        assert "error" in res
        # base unchanged on disk.
        assert _spec_text(tmp_path, "base") == BASE_SPEC
        assert _spec_text(tmp_path, "daemon") == DAEMON_SPEC

    def test_migrate_relinks_intra_spec_refs(self, tmp_path):
        # base's Project Identity references the moved Requirement with the
        # documented intra-spec `Requirement: <name>` form, and the moved block
        # references a base Requirement that stays. Both must be relinked across
        # the relocation boundary so 1-hop expansion keeps resolving.
        base = (
            "<!-- spec-format: v1 -->\n<!-- domain: project -->\n\n"
            "# base Specification\n\n## Purpose\n\nbaseline, one line.\n\n"
            "## Requirements\n\n"
            "### Requirement: Project Identity\n"
            "Identity line. See Requirement: Daemon Modules for the daemon.\n\n"
            "### Requirement: Daemon Modules\n"
            "Daemon detail. Built on Requirement: Project Identity.\n\n"
            "### Requirement: Coding Conventions\nPEP 8.\n"
        )
        _make_project(tmp_path, {"base": base, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        res = eng.migrate_requirements([BaseMigration("Daemon Modules", "daemon")])
        assert res["specs_updated"] >= 1

        new_base = _spec_text(tmp_path, "base")
        new_daemon = _spec_text(tmp_path, "daemon")
        # Source spec: intra ref pointing at the moved Requirement is relinked.
        assert "daemon::Daemon Modules" in new_base
        assert "Requirement: Daemon Modules" not in new_base
        # Moved block now in daemon: intra ref pointing back at a stayed
        # Requirement is relinked to base::Project Identity.
        assert "base::Project Identity" in new_daemon
        assert "Requirement: Project Identity" not in new_daemon
        # The moved Requirement's own heading survives intact.
        assert "### Requirement: Daemon Modules" in new_daemon

    def test_migrate_skips_unknown_target(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        res = eng.migrate_requirements([BaseMigration("Daemon Modules", "nonexistent")])
        assert res["specs_updated"] == 0
        assert "Daemon Modules" in res["skipped"]

    def test_migrate_keeps_skipped_requirement_in_base(self, tmp_path):
        # A response with one valid migration and one whose target spec is
        # missing (e.g. deleted after the proposal was created). The valid move
        # proceeds, but the skipped migration's Requirement MUST stay intact in
        # base — it must NOT be silently dropped without a destination.
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        res = eng.migrate_requirements([
            BaseMigration("Daemon Modules", "daemon"),       # valid
            BaseMigration("Coding Conventions", "nonexistent"),  # stale target
        ])

        assert {"requirement_name": "Daemon Modules", "target_spec": "daemon"} in res["migrated"]
        assert "Coding Conventions" in res["skipped"]

        new_base = _spec_text(tmp_path, "base")
        new_daemon = _spec_text(tmp_path, "daemon")
        # The valid move happened.
        assert "### Requirement: Daemon Modules" not in new_base
        assert "### Requirement: Daemon Modules" in new_daemon
        # The skipped Requirement is preserved verbatim in base — not lost.
        assert "### Requirement: Coding Conventions" in new_base
        assert "PEP 8 style throughout the project." in new_base
        # Project Identity (never migrated) is also still present.
        assert "### Requirement: Project Identity" in new_base
        assert validate_spec_structure(new_base, "base").passed

    def test_base_exceeds_limit_config_driven(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC})
        # default 32KiB → not exceeded
        eng = SyncEngine(tmp_path)
        assert eng.base_exceeds_limit() is False
        # tiny configured limit → exceeded
        (tmp_path / "se3.yaml").write_text(
            "spec_governance:\n  base_max_bytes: 10\n", encoding="utf-8"
        )
        assert SyncEngine(tmp_path).base_exceeds_limit() is True


# ---------------------------------------------------------------------------
# SyncEngine: parallel spec split
# ---------------------------------------------------------------------------

class TestSpecSplit:
    def test_apply_split_creates_parallel_relinks_and_indexes(self, tmp_path):
        _make_project(tmp_path, {"big": BIG_SPEC, "other": OTHER_SPEC})
        eng = SyncEngine(tmp_path)
        out = eng.apply_split(SplitProposal(
            source_spec="big",
            new_spec="big-topic-b",
            requirement_names=["Topic B One", "Topic B Two"],
            domain="engine/big",
            purpose="Topic B cluster.",
        ))
        assert out["created"] is True
        assert out["new_spec"] == "big-topic-b"
        assert "other" in out["relinked_specs"]

        new_big = _spec_text(tmp_path, "big")
        new_split = _spec_text(tmp_path, "big-topic-b")
        new_other = _spec_text(tmp_path, "other")

        assert "### Requirement: Topic B One" not in new_big
        assert "### Requirement: Topic B One" in new_split
        assert "### Requirement: Topic B Two" in new_split
        assert g.domain_of(new_split) == "engine/big"
        assert validate_spec_structure(new_big, "big").passed
        assert validate_spec_structure(new_split, "big-topic-b").passed

        # Cross-spec ref in `other` relinked to the new logical address.
        assert "big-topic-b::Topic B One" in new_other
        assert "big::Topic B One" not in new_other

        idx = load_or_build(tmp_path)
        assert idx.resolve_item_location("big-topic-b", "Topic B One") is not None
        assert idx.resolve_item_location("big", "Topic B One") is None

    def test_split_rewrites_inter_spec_ref_between_co_moved_blocks(self, tmp_path):
        # A moved Requirement block that references a *sibling* which also moves
        # into the new spec, via the explicit inter-spec ``<source>::<req>``
        # address, must have that address rewritten to ``<new_spec>::<req>`` in
        # the new spec — otherwise the reference no longer resolves after split.
        big = (
            "<!-- spec-format: v1 -->\n\n"
            "# big Specification\n\n"
            "## Purpose\n\n"
            "A multi-topic spec, in one sentence.\n\n"
            "## Requirements\n\n"
            "### Requirement: Topic A One\n"
            "A1 opening summary.\n\n"
            "### Requirement: Topic B One\n"
            "B1 opening summary. See big::Topic B Two for the sibling rule.\n\n"
            "### Requirement: Topic B Two\n"
            "B2 opening summary.\n"
        )
        _make_project(tmp_path, {"big": big})
        eng = SyncEngine(tmp_path)
        out = eng.apply_split(SplitProposal(
            source_spec="big",
            new_spec="big-topic-b",
            requirement_names=["Topic B One", "Topic B Two"],
            domain="engine/big",
            purpose="Topic B cluster.",
        ))
        assert out["created"] is True

        new_split = _spec_text(tmp_path, "big-topic-b")
        # The inter-spec reference inside the moved block is relinked to the new
        # spec address; the stale source address is gone.
        assert "big-topic-b::Topic B Two" in new_split
        assert "big::Topic B Two" not in new_split

        # The reference resolves against the rebuilt index at its new address.
        idx = load_or_build(tmp_path)
        assert idx.resolve_item_location("big-topic-b", "Topic B Two") is not None
        assert idx.resolve_item_location("big", "Topic B Two") is None

    def test_split_refuses_existing_target(self, tmp_path):
        _make_project(tmp_path, {"big": BIG_SPEC, "other": OTHER_SPEC})
        eng = SyncEngine(tmp_path)
        out = eng.apply_split(SplitProposal(
            source_spec="big", new_spec="other",
            requirement_names=["Topic B One"],
        ))
        assert out["created"] is False
        assert "already exists" in out["error"]

    def test_split_refuses_unknown_source(self, tmp_path):
        _make_project(tmp_path, {"big": BIG_SPEC})
        eng = SyncEngine(tmp_path)
        out = eng.apply_split(SplitProposal(
            source_spec="nope", new_spec="x", requirement_names=["Y"],
        ))
        assert out["created"] is False


# ---------------------------------------------------------------------------
# SyncEngine: domain backfill (non-blocking)
# ---------------------------------------------------------------------------

class TestDomainBackfill:
    def test_backfill_adds_marker_for_listed_specs_only(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC, "big": BIG_SPEC})
        eng = SyncEngine(tmp_path)
        assert set(eng.specs_missing_domain()) == {"daemon", "big"}

        # Only backfill daemon; big is left without a domain (renders 未分类).
        updated = eng.backfill_domains({"daemon": "engine/daemon"})
        assert updated == ["daemon"]

        assert g.domain_of(_spec_text(tmp_path, "daemon")) == "engine/daemon"
        # big untouched and still missing — that is non-blocking.
        assert g.domain_of(_spec_text(tmp_path, "big")) is None
        assert "big" in SyncEngine(tmp_path).specs_missing_domain()

    def test_backfill_does_not_overwrite_existing_domain(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC})
        eng = SyncEngine(tmp_path)
        updated = eng.backfill_domains({"base": "something/else"})
        assert updated == []
        assert g.domain_of(_spec_text(tmp_path, "base")) == "project"

    def test_missing_domain_renders_unclassified_in_index(self, tmp_path):
        # A spec with no domain marker carries SpecMeta.domain=None so the
        # renderer groups it under UNCLASSIFIED_GROUP — never blocking.
        _make_project(tmp_path, {"daemon": DAEMON_SPEC})
        idx = load_or_build(tmp_path)
        meta = idx.spec_metas.get("daemon")
        assert meta is not None
        assert meta.domain is None


# ---------------------------------------------------------------------------
# respond channel: call files + se3 sync-respond
# ---------------------------------------------------------------------------

def _write_response(call_file: Path, decisions: dict) -> None:
    """Write a ``.response`` file: ``{item_id: 'approve'|'skip'}``."""
    call_data = json.loads(call_file.read_text(encoding="utf-8"))
    items = []
    for item in call_data["items"]:
        items.append({
            "id": item["id"],
            "item_id": item["item_id"],
            "decision": decisions.get(item["item_id"], "skip"),
        })
    Path(str(call_file) + ".response").write_text(
        json.dumps({"items": items}, indent=2), encoding="utf-8"
    )


class TestRespondChannel:
    def test_base_migration_respond_roundtrip(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        migs = [BaseMigration("Daemon Modules", "daemon")]
        call_file = si.write_base_migration_call(tmp_path, migs)
        assert call_file.exists()
        cdata = json.loads(call_file.read_text(encoding="utf-8"))
        assert cdata["type"] == "sync_base_migration"

        _write_response(call_file, {migs[0].item_id: "approve"})
        eng = SyncEngine(tmp_path)
        res = eng.process_call_response(call_file)
        assert res["specs_updated"] >= 1
        assert "### Requirement: Daemon Modules" in _spec_text(tmp_path, "daemon")

    def test_spec_split_respond_roundtrip(self, tmp_path):
        _make_project(tmp_path, {"big": BIG_SPEC, "other": OTHER_SPEC})
        props = [SplitProposal(
            source_spec="big", new_spec="big-topic-b",
            requirement_names=["Topic B One", "Topic B Two"],
            domain="engine/big", purpose="Topic B cluster.",
        )]
        call_file = si.write_spec_split_call(tmp_path, props)
        cdata = json.loads(call_file.read_text(encoding="utf-8"))
        assert cdata["type"] == "sync_spec_split"

        _write_response(call_file, {props[0].item_id: "approve"})
        eng = SyncEngine(tmp_path)
        res = eng.process_call_response(call_file)
        assert "big-topic-b" in res["specs_created"]
        assert (tmp_path / "se3" / "specs" / "big-topic-b" / "spec.md").exists()

    def test_skip_decision_applies_nothing(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        migs = [BaseMigration("Daemon Modules", "daemon")]
        call_file = si.write_base_migration_call(tmp_path, migs)
        _write_response(call_file, {migs[0].item_id: "skip"})
        res = SyncEngine(tmp_path).process_call_response(call_file)
        assert res["specs_updated"] == 0
        assert _spec_text(tmp_path, "base") == BASE_SPEC

    def test_missing_decision_defaults_to_skip(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        migs = [BaseMigration("Daemon Modules", "daemon")]
        call_file = si.write_base_migration_call(tmp_path, migs)
        # Empty response → default skip.
        Path(str(call_file) + ".response").write_text(
            json.dumps({"items": []}), encoding="utf-8"
        )
        res = SyncEngine(tmp_path).process_call_response(call_file)
        assert res["specs_updated"] == 0

    def test_unsupported_call_type_raises(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC})
        call_file = tmp_path / "se3" / "calls" / "legacy.json"
        call_file.parent.mkdir(parents=True, exist_ok=True)
        call_file.write_text(json.dumps({"type": "sync_pending_decisions"}), encoding="utf-8")
        Path(str(call_file) + ".response").write_text(json.dumps({"items": []}), encoding="utf-8")
        with pytest.raises(ValueError):
            SyncEngine(tmp_path).process_call_response(call_file)


# ---------------------------------------------------------------------------
# prompt injection
# ---------------------------------------------------------------------------

class TestPromptInjection:
    def test_base_injection_has_admission_discipline_split(self):
        inj = _governance_prompt_injection("base")
        assert "base Spec Admission Standard" in inj
        assert "Spec Writing Discipline" in inj
        assert "Spec Split Criteria" in inj

    def test_nonbase_injection_omits_admission(self):
        inj = _governance_prompt_injection("daemon")
        assert "base Spec Admission Standard" not in inj
        assert "Spec Writing Discipline" in inj
        assert "Spec Split Criteria" in inj


# ---------------------------------------------------------------------------
# LLM-assisted proposal generation (mocked caller)
# ---------------------------------------------------------------------------

class _MockCaller:
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls = 0
        self.step_id = ""
        self.step_type = ""

    def call(self, prompt: str = "", json_mode: str = "off", **kwargs) -> str:
        self.calls += 1
        return self.response


class TestProposalGeneration:
    def test_propose_base_migration_writes_call_file(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        caller = _MockCaller(json.dumps([
            {"requirement_name": "Daemon Modules", "target_spec": "daemon"}
        ]))
        call_file = eng.propose_base_migration(caller)
        assert call_file is not None and call_file.exists()
        cdata = json.loads(call_file.read_text(encoding="utf-8"))
        assert cdata["type"] == "sync_base_migration"
        assert cdata["items"][0]["requirement_name"] == "Daemon Modules"
        assert caller.calls == 1

    def test_propose_base_migration_drops_invalid_entries(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        # Unknown requirement + unknown target → nothing valid → None.
        caller = _MockCaller(json.dumps([
            {"requirement_name": "Ghost", "target_spec": "daemon"},
            {"requirement_name": "Daemon Modules", "target_spec": "ghost"},
        ]))
        assert eng.propose_base_migration(caller) is None

    def test_propose_spec_split_writes_call_file(self, tmp_path):
        _make_project(tmp_path, {"big": BIG_SPEC})
        eng = SyncEngine(tmp_path)
        caller = _MockCaller(json.dumps([{
            "new_spec": "big-topic-b",
            "requirement_names": ["Topic B One", "Topic B Two"],
            "domain": "engine/big",
            "purpose": "Topic B cluster.",
            "rationale": "sparse cross-cluster refs",
        }]))
        call_file = eng.propose_spec_split("big", caller)
        assert call_file is not None and call_file.exists()
        cdata = json.loads(call_file.read_text(encoding="utf-8"))
        assert cdata["type"] == "sync_spec_split"
        assert cdata["items"][0]["new_spec"] == "big-topic-b"

    def test_propose_spec_split_refuses_degenerate_full_move(self, tmp_path):
        _make_project(tmp_path, {"big": BIG_SPEC})
        eng = SyncEngine(tmp_path)
        # Moving ALL requirements is not a split — refused.
        caller = _MockCaller(json.dumps([{
            "new_spec": "big-2",
            "requirement_names": ["Topic A One", "Topic A Two", "Topic B One", "Topic B Two"],
        }]))
        assert eng.propose_spec_split("big", caller) is None

    def test_propose_cohesive_spec_returns_none(self, tmp_path):
        _make_project(tmp_path, {"big": BIG_SPEC})
        eng = SyncEngine(tmp_path)
        caller = _MockCaller("[]")  # LLM judges it cohesive
        assert eng.propose_spec_split("big", caller) is None


class TestDomainBackfillProposal:
    def test_propose_domain_backfill_assigns_and_persists(self, tmp_path):
        # base has a domain; daemon + big do not.
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC, "big": BIG_SPEC})
        eng = SyncEngine(tmp_path)
        assert set(eng.specs_missing_domain()) == {"daemon", "big"}

        caller = _MockCaller(json.dumps([
            {"spec_name": "daemon", "domain": "engine/daemon"},
            {"spec_name": "big", "domain": "engine/big"},
        ]))
        updated = eng.propose_domain_backfill(caller)
        assert caller.calls == 1
        assert set(updated) == {"daemon", "big"}
        # Markers are persisted to disk.
        assert g.domain_of(_spec_text(tmp_path, "daemon")) == "engine/daemon"
        assert g.domain_of(_spec_text(tmp_path, "big")) == "engine/big"
        # Nothing left unclassified.
        assert SyncEngine(tmp_path).specs_missing_domain() == []

    def test_propose_domain_backfill_noop_when_all_have_domain(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC})  # base already has a domain
        eng = SyncEngine(tmp_path)
        caller = _MockCaller("should-not-be-called")
        assert eng.propose_domain_backfill(caller) == []
        assert caller.calls == 0  # zero-LLM when nothing is missing

    def test_propose_domain_backfill_drops_invalid_entries(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        # Unknown spec + empty domain → nothing valid applied.
        caller = _MockCaller(json.dumps([
            {"spec_name": "ghost", "domain": "x/y"},
            {"spec_name": "daemon", "domain": "  "},
        ]))
        assert eng.propose_domain_backfill(caller) == []
        assert g.domain_of(_spec_text(tmp_path, "daemon")) is None

    def test_run_governance_wires_domain_backfill(self, tmp_path):
        _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC})
        eng = SyncEngine(tmp_path)
        caller = _MockCaller(json.dumps([
            {"spec_name": "daemon", "domain": "engine/daemon"},
        ]))
        result = eng.run_governance(caller)
        # The backfill ran (production wiring), and the reported missing-domain
        # backlog reflects the POST-backfill state.
        assert result["domains_backfilled"] == ["daemon"]
        assert "daemon" not in result["specs_missing_domain"]
        assert g.domain_of(_spec_text(tmp_path, "daemon")) == "engine/daemon"


# ---------------------------------------------------------------------------
# zero-LLM invariant for the navigation / refactor-application layer
# ---------------------------------------------------------------------------

def test_governance_application_and_index_make_no_llm_calls(tmp_path, monkeypatch):
    """Migration / split / backfill application + index build must never use an LLM.

    Any attempt to construct an ``LLMCaller`` during these deterministic
    operations fails the test, asserting the navigation layer is LLM-free.
    """
    import se3.engine.llm_caller as llm_caller_mod

    def _boom(*args, **kwargs):  # pragma: no cover - only runs on regression
        raise AssertionError("LLMCaller must not be constructed by governance ops")

    monkeypatch.setattr(llm_caller_mod.LLMCaller, "__init__", _boom)

    _make_project(tmp_path, {"base": BASE_SPEC, "daemon": DAEMON_SPEC, "big": BIG_SPEC, "other": OTHER_SPEC})
    eng = SyncEngine(tmp_path)

    # Index build / item resolution — pure navigation layer.
    idx = load_or_build(tmp_path)
    assert idx.resolve_item_location("base", "Project Identity") is not None

    # All three governance mechanisms run without constructing an LLMCaller.
    eng.migrate_requirements([BaseMigration("Daemon Modules", "daemon")])
    eng.backfill_domains({"daemon": "engine/daemon"})
    eng.apply_split(SplitProposal(
        source_spec="big", new_spec="big-b",
        requirement_names=["Topic B One", "Topic B Two"],
        domain="engine/big",
    ))
    # Reaching here means no LLMCaller was constructed.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _spec_text(root: Path, name: str) -> str:
    return (root / "se3" / "specs" / name / "spec.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Atomic write / rollback (issue: a mid-write failure must never leave a spec
# truncated; rollback must restore every committed file).
# ---------------------------------------------------------------------------

class TestAtomicWriteOrRestore:
    def test_temp_write_failure_leaves_destination_intact(self, tmp_path, monkeypatch):
        import builtins
        eng = SyncEngine(tmp_path)
        f = tmp_path / "b.md"
        f.write_text("ORIGINAL CONTENT", encoding="utf-8")

        real_open = builtins.open

        def boom(file, *a, **k):
            if str(file).endswith(".sync-tmp"):
                raise OSError("disk full")
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", boom)
        with pytest.raises(OSError):
            eng._atomic_write(f, "NEW CONTENT")
        # Destination is never half-written; no stray temp left behind.
        assert f.read_text(encoding="utf-8") == "ORIGINAL CONTENT"
        assert not (tmp_path / "b.md.sync-tmp").exists()

    def test_rollback_restores_committed_edit_when_later_write_fails(self, tmp_path, monkeypatch):
        eng = SyncEngine(tmp_path)
        f1 = tmp_path / "a.md"
        f1.write_text("ORIG-A", encoding="utf-8")
        f2 = tmp_path / "b.md"
        f2.write_text("ORIG-B", encoding="utf-8")

        real_atomic = SyncEngine._atomic_write
        state = {"n": 0}

        def flaky(path, content):
            state["n"] += 1
            if state["n"] == 2:
                raise OSError("disk full")  # destination stays intact
            real_atomic(path, content)

        monkeypatch.setattr(SyncEngine, "_atomic_write", staticmethod(flaky))
        err = eng._write_all_or_restore(
            edits=[(f1, "NEW-A", "ORIG-A"), (f2, "NEW-B", "ORIG-B")],
            creates=[],
        )
        assert err is not None  # failure reported
        # The committed first edit was rolled back; the failing one is untouched.
        assert f1.read_text(encoding="utf-8") == "ORIG-A"
        assert f2.read_text(encoding="utf-8") == "ORIG-B"
