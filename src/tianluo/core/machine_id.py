"""Stable machine identity shared across engine, commands, and daemon.

This is the single source of truth for "which physical machine am I" used by
the cross-machine single-writer guards: ``merge.lock`` holder records and
``tianluo/state/run.pid`` both stamp their owner with :func:`stable_machine_id`,
and every stale / double-spawn decision routes through :func:`is_local_machine`
to distinguish "held by a process I can see in my own process table" from
"held by a live process on another host that psutil can never observe".

It lives in ``core`` (not the daemon aggregation layer where the original
``_stable_machine_id`` lived) so engine/commands can share one implementation
rather than each re-deriving the id and drifting. It imports only the standard
library, honouring the core/server dependency-isolation constraint.
"""

from __future__ import annotations

import socket
import uuid
from typing import Optional

# Cached because the id must be stable for the life of the process (a holder
# record written at startup is compared against this same value much later),
# and both gethostname() and getnode() are cheap-but-not-free syscalls.
_cached_machine_id: Optional[str] = None


def stable_machine_id() -> str:
    """Return a process-stable machine id (hostname plus a short uuid tail).

    Format is ``<hostname>-<nodehex>`` — human-readable so it can be echoed
    back in "held by machine X" messages. ``uuid.getnode()`` disambiguates
    hosts that happen to share a hostname on the same shared filesystem.
    """
    global _cached_machine_id
    if _cached_machine_id is None:
        _cached_machine_id = f"{socket.gethostname()}-{uuid.getnode():x}"
    return _cached_machine_id


def is_local_machine(machine_id: Optional[str]) -> bool:
    """Return whether ``machine_id`` denotes the machine we are running on.

    WHY: A missing machine id (``None`` or empty) is treated as local. Holder
    records written before this field existed carry no machine id; forcing them
    down the cross-machine "held by another host" path would wedge every
    pre-upgrade lock / pid file as un-reclaimable. INVARIANT: absent machine id
    == local machine, preserving the pre-upgrade same-machine behaviour.
    """
    if not machine_id:
        return True
    return machine_id == stable_machine_id()
