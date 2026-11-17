"""Tests for the UPnP calls of the Teufel Raumfeld provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_assistant.providers.raumfeld import upnp

_SERVER = "http://192.168.1.41:47366/MediaServer.xml"


def _device(action: MagicMock) -> MagicMock:
    """Build a device whose every service offers the given action."""
    device = MagicMock()
    device.service.return_value.action.return_value = action
    return device


def _factory(action: MagicMock) -> MagicMock:
    """Patch the UPnP factory to build a device that offers the given action."""
    factory = MagicMock()
    factory.return_value.async_create_device = AsyncMock(return_value=_device(action))
    return factory


async def test_browsing_returns_the_didl_as_the_server_sent_it() -> None:
    """The result is the raw DIDL-Lite, not a value parsed from it."""
    action = MagicMock()
    action.async_call = AsyncMock(return_value={"Result": "parsed"})
    action.argument.return_value.raw_upnp_value = "<DIDL-Lite/>"
    with patch.object(upnp, "UpnpFactory", _factory(action)):
        didl = await upnp.browse(MagicMock(), _SERVER, "0/My Music/Albums/42", children=False)
    assert didl == "<DIDL-Lite/>"
    assert action.async_call.await_args.kwargs["BrowseFlag"] == "BrowseMetadata"


async def test_browsing_without_a_result_returns_none() -> None:
    """A server that answers without a result gives nothing to parse."""
    action = MagicMock()
    action.async_call = AsyncMock(return_value={})
    with patch.object(upnp, "UpnpFactory", _factory(action)):
        assert await upnp.browse(MagicMock(), _SERVER, "0/Line In", children=True) is None


@pytest.mark.parametrize(
    ("object_id", "children", "app_headers"),
    [("0/RadioTime", True, True), ("0/RadioTime", False, False), ("0/Line In", True, False)],
)
async def test_tunein_and_tidal_are_listed_as_to_the_raumfeld_app(
    object_id: str, children: bool, app_headers: bool
) -> None:
    """The host lists TuneIn and Tidal only to its own app, so the request says it is one."""
    action = MagicMock()
    action.async_call = AsyncMock(return_value={})
    with (
        patch.object(upnp, "UpnpFactory", _factory(action)),
        patch.object(upnp, "AiohttpSessionRequester") as requester,
    ):
        await upnp.browse(MagicMock(), _SERVER, object_id, children=children)
    headers = requester.call_args.kwargs["http_headers"]
    assert (headers is not None) == app_headers


@pytest.mark.parametrize(
    ("reply", "version"), [({"SoftwareVersion": "2.21.0"}, "2.21.0"), ({}, None)]
)
async def test_the_software_version_is_read_from_the_setup_service(
    reply: dict[str, str], version: str | None
) -> None:
    """A speaker that gives no version reads as unknown."""
    action = MagicMock()
    action.async_call = AsyncMock(return_value=reply)
    with patch.object(upnp, "UpnpFactory", _factory(action)):
        assert await upnp.get_software_version(MagicMock(), _SERVER) == version
