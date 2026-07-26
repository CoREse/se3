"""SE3 daemon — the resident control-plane process.

``luo daemon`` is the bridge between one-shot ``luo run`` flows and the central
server. It outlives any individual ``luo run`` process and is therefore the
only component able to keep aggregating local flow state and to offer a stable
outbound endpoint after the CLI has exited.

The daemon lives inside the ``se3`` core package (not a separate binary) because
it is tightly coupled to the core: it spawns ``luo run``, depends on the
``tianluo/state|logs|calls|issues`` directory layout, and consumes the unified
structured event-stream schema. It therefore versions in lockstep with the
core.

Sub-modules:

* :mod:`supervisor` — discovers and tracks local ``luo run`` processes.
* :mod:`spawner`    — spawns new ``luo run`` child processes on request.
* :mod:`aggregator` — polls on-disk flow artifacts into a status snapshot.
* :mod:`daemon`     — the resident process composing the three above.
"""

from __future__ import annotations

from .aggregator import (
    DaemonAggregator,
    FlowSnapshot,
    MachineStatus,
    PendingCall,
)
from .daemon import (
    Daemon,
    DaemonAlreadyRunning,
    DaemonConfig,
    daemon_status,
    start_daemon,
    stop_daemon,
)
from .client import DaemonClient
from .spawner import DaemonSpawner, SpawnedProcess
from .supervisor import DaemonSupervisor, FlowRecord
from . import protocol

__all__ = [
    "DaemonAggregator",
    "FlowSnapshot",
    "MachineStatus",
    "PendingCall",
    "Daemon",
    "DaemonAlreadyRunning",
    "DaemonConfig",
    "daemon_status",
    "start_daemon",
    "stop_daemon",
    "DaemonClient",
    "DaemonSpawner",
    "SpawnedProcess",
    "DaemonSupervisor",
    "FlowRecord",
    "protocol",
]
