"""Tests for DLNA devices that announce a Content-Length larger than the body they send."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientPayloadError
from aiohttp.http_exceptions import ContentLengthError
from async_upnp_client.exceptions import UpnpCommunicationError

from music_assistant.providers.dlna.player import DLNAPlayer
from tests.common import MockProvider


def _content_length_off_by_one_error() -> UpnpCommunicationError:
    """Build the exact exception chain the Busch-Jaeger radio triggers."""
    try:
        try:
            raise ContentLengthError(
                "Not enough data to satisfy content length header (received 593 of 594 bytes)."
            )
        except ContentLengthError as content_err:
            raise ClientPayloadError(
                f"Response payload is not completed: {content_err!r}"
            ) from content_err
    except ClientPayloadError as payload_err:
        try:
            raise UpnpCommunicationError(repr(payload_err)) from payload_err
        except UpnpCommunicationError as comm_err:
            return comm_err


@pytest.mark.asyncio
async def test_poll_survives_content_length_off_by_one() -> None:
    """A firmware Content-Length quirk on GetMediaInfo must not mark the device unavailable."""
    provider = MockProvider("dlna", instance_id="dlna_test")
    provider.mass.streams.base_url = "http://192.168.1.2:8097"

    device = MagicMock()
    device.profile_device.available = True
    device.name = "Busch-Jaeger 8216 U"
    device.async_update = AsyncMock(side_effect=_content_length_off_by_one_error())

    player = DLNAPlayer(
        provider,  # type: ignore[arg-type]
        "uuid:busch-jaeger-player",
        "http://192.168.1.10/description.xml",
        device=device,
    )
    player.force_poll = True

    await player.poll()

    device.async_update.assert_awaited_once()
    assert player.device is device  # not disconnected
