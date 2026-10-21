"""Tests for the Teufel Raumfeld player provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant.providers.raumfeld.provider import RaumfeldPlayerProvider

_LOCATION = "http://192.168.1.42:54730/renderer.xml"
_DEVICE_DESCRIPTION = (
    '<?xml version="1.0"?>'
    '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
    "<friendlyName>Speaker Bar</friendlyName>"
    "<modelName>Raumfeld One S</modelName>"
    "<serialNumber>04:a3:16:f3:b6:c4</serialNumber>"
    "<serviceList><service>"
    "<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
    "<controlURL>/AVTransport/ctrl</controlURL>"
    "</service></serviceList>"
    "</device></root>"
)


def _provider(description: str) -> tuple[RaumfeldPlayerProvider, MagicMock]:
    """Build a provider whose room renderer answers with the given device description."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    provider._room_serials = {}
    provider._room_models = {}
    provider._room_transport_paths = {}
    provider.host = MagicMock()
    provider.host.resolve = {
        "room_to_udn": {"Bar": "uuid:room"},
        "roomudn_to_rendudn": {"uuid:room": "uuid:renderer"},
        "udn_to_devloc": {"uuid:renderer": _LOCATION},
    }
    response = MagicMock()
    response.text = AsyncMock(return_value=description)
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    request.__aexit__ = AsyncMock(return_value=None)
    mass = MagicMock()
    mass.http_session.get.return_value = request
    provider.mass = mass
    provider.logger = MagicMock()
    return provider, mass


async def test_room_model_is_read_with_the_serial() -> None:
    """Reading a room's device description stores its model name along with the serial."""
    provider, mass = _provider(_DEVICE_DESCRIPTION)
    assert provider.room_model("Bar") is None

    assert await provider._ensure_room_serial("Bar") == "04:a3:16:f3:b6:c4"

    assert provider.room_model("Bar") == "Raumfeld One S"
    assert mass.http_session.get.call_args.args[0] == _LOCATION


async def test_room_without_a_model_name_has_no_model() -> None:
    """A description without a model name leaves the room's model unknown."""
    provider, _ = _provider(
        _DEVICE_DESCRIPTION.replace("<modelName>Raumfeld One S</modelName>", "")
    )

    assert await provider._ensure_room_serial("Bar") == "04:a3:16:f3:b6:c4"

    assert provider.room_model("Bar") is None


async def test_transport_url_follows_the_renderers_current_address() -> None:
    """A renderer that restarts on another port is reached there, though read only once."""
    provider, _ = _provider(_DEVICE_DESCRIPTION)
    await provider._ensure_room_serial("Bar")
    assert provider.room_transport_url("Bar") == "http://192.168.1.42:54730/AVTransport/ctrl"

    provider.host.resolve["udn_to_devloc"]["uuid:renderer"] = "http://192.168.1.42:41000/r.xml"

    assert provider.room_transport_url("Bar") == "http://192.168.1.42:41000/AVTransport/ctrl"


def _button_provider() -> tuple[RaumfeldPlayerProvider, MagicMock]:
    """Build a provider whose host has a media server, and return the button action."""
    provider = RaumfeldPlayerProvider.__new__(RaumfeldPlayerProvider)
    provider.host = MagicMock()
    provider.host.media_server_udn = "uuid:media-server"
    provider.host.resolve = {"udn_to_devloc": {"uuid:media-server": "http://host/ms.xml"}}
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider.resolve_room_renderer = MagicMock(  # type: ignore[method-assign]
        return_value=("renderer-uuid", "192.168.1.42")
    )
    action = MagicMock()
    return provider, action


async def test_station_buttons_are_read_with_their_titles() -> None:
    """Assigned buttons are listed with their item and title; empty ones are left out."""
    provider, action = _button_provider()
    assigned = {1: "0/RadioTime/Search/s-s25777", 2: "", 3: "0/My Music/Albums/42", 4: ""}
    action.async_call = AsyncMock(
        side_effect=lambda **kwargs: {"ObjectID": assigned[kwargs["Button"]]}
    )
    listings = {
        "0/RadioTime/Search/s-s25777": '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/'
        'DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item id="0/RadioTime/Search/'
        's-s25777"><dc:title>NPO Radio 2</dc:title></item></DIDL-Lite>',
        "0/My Music/Albums/42": None,
    }
    provider.host.async_browse_media_server = AsyncMock(
        side_effect=lambda object_id, _flag: listings[object_id]
    )
    with patch(
        "music_assistant.providers.raumfeld.provider.hassfeld_upnp.get_dlna_action",
        AsyncMock(return_value=action),
    ):
        buttons = await provider._read_station_buttons("Bar")
    assert buttons == [
        (1, "0/RadioTime/Search/s-s25777", "NPO Radio 2", False),
        (3, "0/My Music/Albums/42", "Button 3", False),
    ]
    assert action.async_call.await_args_list[0].kwargs == {
        "Renderer": "uuid:renderer-uuid",
        "Button": 1,
    }


async def test_a_room_whose_buttons_cannot_be_read_offers_none() -> None:
    """A media server that does not answer leaves the room without button sources."""
    provider, _ = _button_provider()
    with patch(
        "music_assistant.providers.raumfeld.provider.hassfeld_upnp.get_dlna_action",
        AsyncMock(side_effect=TimeoutError),
    ):
        assert await provider._read_station_buttons("Bar") == []
