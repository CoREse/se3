"""Tests for the shared stable-machine-id source of truth."""

from __future__ import annotations

import pytest

import tianluo.core.machine_id as machine_id
from tianluo.core.machine_id import is_local_machine, stable_machine_id


@pytest.fixture(autouse=True)
def _clear_machine_id_cache():
    """Isolate the process-lifetime cache so monkeypatched ids don't leak."""
    machine_id._cached_machine_id = None
    yield
    machine_id._cached_machine_id = None


def test_stable_machine_id_format_matches_legacy_aggregator():
    """Format is <hostname>-<nodehex>, matching the old aggregator impl."""
    import socket
    import uuid

    expected = f"{socket.gethostname()}-{uuid.getnode():x}"
    assert stable_machine_id() == expected


def test_stable_machine_id_is_stable_within_process():
    """Repeated calls return the identical (cached) value."""
    first = stable_machine_id()
    second = stable_machine_id()
    assert first == second


def test_is_local_machine_truth_table():
    # None and empty are legacy records -> treated as local.
    assert is_local_machine(None) is True
    assert is_local_machine("") is True
    # The current machine's own id is local.
    assert is_local_machine(stable_machine_id()) is True
    # Any other machine string is remote.
    assert is_local_machine("some-other-host-deadbeef") is False


def test_is_local_machine_respects_patched_identity(monkeypatch):
    """Patching hostname/getnode changes what counts as local."""
    monkeypatch.setattr(machine_id.socket, "gethostname", lambda: "hostA")
    monkeypatch.setattr(machine_id.uuid, "getnode", lambda: 0xABCDEF)
    assert stable_machine_id() == "hostA-abcdef"
    assert is_local_machine("hostA-abcdef") is True
    assert is_local_machine("hostB-abcdef") is False


def test_aggregator_alias_matches_core():
    """aggregator._stable_machine_id remains a valid alias of the core impl."""
    from tianluo.daemon.aggregator import _stable_machine_id

    assert _stable_machine_id() == stable_machine_id()
