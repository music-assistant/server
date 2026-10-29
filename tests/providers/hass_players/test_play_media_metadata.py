"""Tests for the metadata Music Assistant hands to Home Assistant with a play command."""

from __future__ import annotations

from typing import Any

import pytest
from music_assistant_models.player import PlayerMedia

from music_assistant.constants import MASS_LOGO_ONLINE
from music_assistant.providers.hass_players.player import HomeAssistantPlayer

IMAGE_URL = "http://10.0.0.5:8097/imageproxy?path=cover.jpg&size=512&fmt=jpeg"


async def _play_metadata(
    player: HomeAssistantPlayer, hass_domain: str, media: PlayerMedia
) -> dict[str, Any]:
    """Play media on a player of the given HA integration and return the sent metadata."""
    player._extra_data = {"hass_domain": hass_domain}
    await player.play_media(media)
    service_data = player.hass.call_service.await_args.kwargs["service_data"]
    metadata: dict[str, Any] = service_data["extra"]["metadata"]
    return metadata


@pytest.mark.parametrize(
    ("hass_domain", "expect_album_art"),
    [("dlna_dmr", True), ("cast", False), ("some_other_integration", False)],
)
async def test_album_art_uri_only_sent_to_dlna(
    player: HomeAssistantPlayer, hass_domain: str, expect_album_art: bool
) -> None:
    """Only the HA DLNA integration gets the album art key it reads, the Cast keys stay."""
    media = PlayerMedia(uri="library://track/1", title="MA Track", image_url=IMAGE_URL)
    metadata = await _play_metadata(player, hass_domain, media)

    assert ("album_art_uri" in metadata) is expect_album_art
    if expect_album_art:
        assert metadata["album_art_uri"] == IMAGE_URL
    assert metadata["imageUrl"] == IMAGE_URL
    assert metadata["images"] == [{"url": IMAGE_URL}]


async def test_dlna_falls_back_to_logo_without_image(player: HomeAssistantPlayer) -> None:
    """Media without an image shows the Music Assistant logo on a DLNA renderer."""
    media = PlayerMedia(uri="library://track/1", title="MA Track")
    metadata = await _play_metadata(player, "dlna_dmr", media)

    assert metadata["album_art_uri"] == MASS_LOGO_ONLINE


@pytest.mark.parametrize("hass_domain", ["dlna_dmr", "cast"])
async def test_unset_metadata_is_left_out(player: HomeAssistantPlayer, hass_domain: str) -> None:
    """Unset fields are not sent, the HA DLNA integration would show them as the text None."""
    media = PlayerMedia(uri="library://track/1")
    metadata = await _play_metadata(player, hass_domain, media)

    for key in ("title", "artist", "album", "albumName", "imageUrl", "images", "duration"):
        assert key not in metadata
    assert None not in metadata.values()
