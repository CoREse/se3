"""Daemon keepalive tuning: the client dials the server with a relaxed ping
tolerance so lossy/high-latency links (e.g. node007) do not trip a "keepalive
ping timeout" close every ~45s.

The library default ping_timeout=20 caused a single lost PONG to drop the
connection, and each drop truncated an in-flight full history reload. We pin
ping_interval=20/ping_timeout=60; this test locks those values in by capturing
the kwargs passed to ``websockets.connect``.
"""

from __future__ import annotations

import asyncio

import pytest

from se3.daemon.client import DaemonClient


class _AbortSession(Exception):
    """Sentinel raised from the fake connect CM to short-circuit ``_session``."""


class _FakeConnectCM:
    def __init__(self, kwargs: dict) -> None:
        self._kwargs = kwargs

    async def __aenter__(self):
        # We only care about the connect kwargs; abort before the session body
        # runs so no HELLO / receive loop machinery is required.
        raise _AbortSession

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeWebsockets:
    def __init__(self) -> None:
        self.connect_kwargs: dict = {}

    def connect(self, url, **kwargs):
        self.connect_kwargs = kwargs
        return _FakeConnectCM(kwargs)


def _client() -> DaemonClient:
    return DaemonClient(
        "ws://test.invalid",
        machine_id="m1",
        hostname="testhost",
        se3_version="0.0.0",
        snapshot_provider=lambda: {"machine_id": "m1", "flows": []},
    )


def test_connect_uses_relaxed_ping_params() -> None:
    client = _client()
    fake = _FakeWebsockets()

    async def _drive() -> None:
        with pytest.raises(_AbortSession):
            await client._session(asyncio.Event(), fake)

    asyncio.run(_drive())

    assert fake.connect_kwargs.get("ping_interval") == 20
    assert fake.connect_kwargs.get("ping_timeout") == 60
