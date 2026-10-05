"""
Tests for the YandexQuasar HTTP client.

Pinned-down contract for the response-release behaviour: each
``self.session.get(...)`` is wrapped in ``async with`` so the
connector slot is returned to aiohttp's pool when the body has been
read.  A mock response with ``__aenter__``/``__aexit__`` validates that
the context-manager protocol is exercised on every endpoint.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant.providers.yandex_station.quasar import YandexQuasar


def _ctx_response(json_payload: dict[str, Any]) -> MagicMock:
    """
    Build a MagicMock matching the aiohttp ClientResponse contract.

    Behaves as both a coroutine return value and an async context
    manager — caller wires it as ``session.get = AsyncMock(return_value=resp)``.
    """
    resp = MagicMock(name="ClientResponse")
    resp.json = AsyncMock(return_value=json_payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


async def test_get_devices_releases_response() -> None:
    """get_devices reads JSON inside `async with`, releasing the slot."""
    session = MagicMock(name="YandexSession")
    resp = _ctx_response(
        {
            "status": "ok",
            "households": [
                {
                    "name": "Home",
                    "all": [{"id": "d1", "name": "Mini"}],
                }
            ],
        }
    )
    session.get = AsyncMock(return_value=resp)

    quasar = YandexQuasar(session)
    devices = await quasar.get_devices()

    assert devices == [{"id": "d1", "name": "Mini", "house_name": "Home"}]
    resp.__aenter__.assert_awaited_once()
    resp.__aexit__.assert_awaited_once()


async def test_load_device_config_releases_response() -> None:
    """load_device_config wraps GET in `async with` and merges quasar_info."""
    session = MagicMock(name="YandexSession")
    resp = _ctx_response(
        {"status": "ok", "quasar_info": {"device_id": "abc", "platform": "yandexmini"}}
    )
    session.get = AsyncMock(return_value=resp)

    quasar = YandexQuasar(session)
    device: dict[str, Any] = {"id": "cloud_id_1"}
    await quasar.load_device_config(device)

    assert device["quasar_info"] == {"device_id": "abc", "platform": "yandexmini"}
    resp.__aenter__.assert_awaited_once()
    resp.__aexit__.assert_awaited_once()


async def test_get_local_speakers_releases_response() -> None:
    """get_local_speakers wraps GET in `async with` and filters on networkInfo."""
    session = MagicMock(name="YandexSession")
    resp = _ctx_response(
        {
            "devices": [
                {
                    "id": "dev_a",
                    "name": "Kitchen",
                    "platform": "yandexmini",
                    "networkInfo": {
                        "ip_addresses": ["192.168.1.10"],
                        "external_port": 1961,
                    },
                    "glagol": {"security": {}},
                },
                {
                    "id": "dev_no_ip",
                    "name": "Offline",
                    "networkInfo": {},
                },
            ]
        }
    )
    session.get = AsyncMock(return_value=resp)

    quasar = YandexQuasar(session)
    result = await quasar.get_local_speakers()

    assert len(result) == 1
    assert result[0]["device_id"] == "dev_a"
    assert result[0]["host"] == "192.168.1.10"
    resp.__aenter__.assert_awaited_once()
    resp.__aexit__.assert_awaited_once()


async def test_get_local_speakers_returns_empty_on_session_failure() -> None:
    """If the GET itself raises (e.g. transport error), return [] (existing contract)."""
    session = MagicMock(name="YandexSession")
    session.get = AsyncMock(side_effect=RuntimeError("transport failed"))

    quasar = YandexQuasar(session)
    result = await quasar.get_local_speakers()

    assert result == []


async def test_devices_cache_is_per_instance() -> None:
    """
    ``devices`` cache must not leak across instances.

    Originally a class-level attribute (``YandexQuasar.devices = None``),
    which is fine for ``None`` but a recurring footgun pattern: any future
    move to a mutable default would silently share state across instances.
    Two independent instances must each see their own ``devices`` cache,
    and one populating its cache must not affect the other.
    """
    s1 = MagicMock(name="Session1")
    s1.get = AsyncMock(
        return_value=_ctx_response(
            {
                "status": "ok",
                "households": [{"name": "Home1", "all": [{"id": "d1"}]}],
            }
        )
    )
    s2 = MagicMock(name="Session2")
    s2.get = AsyncMock(return_value=_ctx_response({"status": "ok", "households": []}))

    q1 = YandexQuasar(s1)
    q2 = YandexQuasar(s2)

    # Initial-state and post-call assertions in opposite order so mypy's
    # type-narrowing on the ``is None`` branch doesn't poison the later
    # ``is not None`` widening (the ``await`` mutates the attribute,
    # which mypy can't see).  We start by populating q1, then check both
    # — the contract is "q1 populated, q2 still None".
    await q1.get_devices()

    devices_q1 = q1.devices
    assert devices_q1 is not None
    assert devices_q1[0]["id"] == "d1"
    assert q2.devices is None
