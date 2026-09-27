"""Tests for Apple Music stream resolution."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.apple_music.streaming import AppleMusicStreamingManager

KEY_SERVER = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
CTRP_URL = "https://aod-ssl.itunes.apple.com/itunes-assets/x/mzaf_A397010985.rphq.aac.wa.m3u8"
CBCP_URL = "https://aod-ssl.itunes.apple.com/itunes-assets/x/mzaf_A397010985.cphq.aac.wa.m3u8"
KEY_URI = "data:;base64,AAAAABep6CkAHStsI7gv/Q=="


def _manager() -> AppleMusicStreamingManager:
    provider = MagicMock()
    provider.instance_id = "apple_music--test"
    provider.logger = MagicMock()
    manager = AppleMusicStreamingManager(provider)
    manager._decrypt_client_id = b"client-id"
    manager._decrypt_private_key = b"private-key"
    return manager


def _metadata(*, key_server: bool, song_id: str = "397010985") -> dict[str, Any]:
    """Build a webPlayback reply. cbcp256 comes first, as Apple really returns it."""
    return {
        "songId": song_id,
        "hls-key-server-url": KEY_SERVER if key_server else None,
        "assets": [
            {"flavor": "30:cbcp256", "URL": CBCP_URL},
            {"flavor": "28:ctrp256", "URL": CTRP_URL},
        ],
    }


async def test_library_track_with_key_server_uses_the_widevine_path() -> None:
    """A purchased library track is encrypted, so it must not be served as clear HTTP."""
    manager = _manager()
    manager._fetch_song_stream_metadata = AsyncMock(return_value=_metadata(key_server=True))
    manager._parse_stream_url_and_uri = AsyncMock(return_value=(CTRP_URL, KEY_URI))
    manager._get_decryption_key = AsyncMock(return_value="abc123")

    result = await manager.get_stream_details("i.AWPNG58CL3m51X")

    assert result.stream_type is StreamType.ENCRYPTED_HTTP
    assert result.decryption_key == "abc123"
    assert result.audio_format.content_type is ContentType.MP4
    # the cbcp256 asset at index 0 is FairPlay and has no usable key
    assert result.path == CTRP_URL


async def test_library_track_is_licensed_under_the_purchase_adam_id() -> None:
    """Apple rejects a library ID at the license server, so songId must be sent."""
    manager = _manager()
    manager._fetch_song_stream_metadata = AsyncMock(return_value=_metadata(key_server=True))
    manager._parse_stream_url_and_uri = AsyncMock(return_value=(CTRP_URL, KEY_URI))
    manager._get_decryption_key = AsyncMock(return_value="abc123")

    await manager.get_stream_details("i.AWPNG58CL3m51X")

    assert manager._get_decryption_key.await_args.args[3] == "i.AWPNG58CL3m51X"
    assert manager._get_decryption_key.await_args.args[4] == "397010985"


async def test_catalog_track_still_licenses_under_its_own_id() -> None:
    """For a catalog track songId is the catalog ID, so nothing changes."""
    manager = _manager()
    manager._fetch_song_stream_metadata = AsyncMock(
        return_value=_metadata(key_server=True, song_id="1193701326")
    )
    manager._parse_stream_url_and_uri = AsyncMock(return_value=(CTRP_URL, KEY_URI))
    manager._get_decryption_key = AsyncMock(return_value="abc123")

    await manager.get_stream_details("1193701326")

    assert manager._get_decryption_key.await_args.args[4] == "1193701326"


async def test_library_track_without_key_server_is_served_clear() -> None:
    """Uploads and iTunes Match carry no key server and play without decryption."""
    manager = _manager()
    manager._fetch_song_stream_metadata = AsyncMock(return_value=_metadata(key_server=False))

    result = await manager.get_stream_details("i.uploadedsong")

    assert result.stream_type is StreamType.HTTP
    assert result.decryption_key is None
    assert result.path == CBCP_URL


async def test_missing_cdm_files_raise_for_an_encrypted_library_track() -> None:
    """Without a CDM an encrypted library track must fail loudly, not stream unplayable."""
    manager = _manager()
    manager._decrypt_client_id = None
    manager._fetch_song_stream_metadata = AsyncMock(return_value=_metadata(key_server=True))

    with pytest.raises(MediaNotFoundError):
        await manager.get_stream_details("i.AWPNG58CL3m51X")
