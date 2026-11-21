"""Unit tests for stream resolution and the Widevine license request of Apple Music."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.apple_music.streaming import (
    AppleMusicStreamingManager,
    LicenseRefusedError,
)

MODULE = "music_assistant.providers.apple_music.streaming"
KEY_SERVER = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
CTRP_URL = "https://aod-ssl.itunes.apple.com/itunes-assets/x/mzaf_A397010985.rphq.aac.wa.m3u8"
CBCP_URL = "https://aod-ssl.itunes.apple.com/itunes-assets/x/mzaf_A397010985.cphq.aac.wa.m3u8"
KEY_URI = "data:;base64,AAAAABep6CkAHStsI7gv/Q=="


def _manager(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> tuple[Any, MagicMock]:
    """Return a streaming manager whose license server gives the given answer."""
    provider: Any = MagicMock(music_app_token="app", music_user_token="user")
    response = MagicMock()
    response.json = AsyncMock(return_value=answer)
    provider.mass.http_session.post.return_value.__aenter__.return_value = response
    provider.api_client.throttler.acquire.return_value = AsyncMock()
    cdm = MagicMock()
    cdm.open.return_value = "session"
    cdm.get_license_challenge.return_value = b"challenge"
    cdm.get_keys.return_value = [
        MagicMock(type="SIGNING"),
        MagicMock(type="CONTENT", key=bytes.fromhex("aabb")),
    ]
    monkeypatch.setattr(f"{MODULE}.Cdm.from_device", MagicMock(return_value=cdm))
    monkeypatch.setattr(f"{MODULE}.Device", MagicMock())
    return AppleMusicStreamingManager(provider), cdm


async def test_a_license_gives_the_content_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The challenge goes out with the request fields, the content key comes back."""
    manager, cdm = _manager(monkeypatch, {"status": 0, "license": "granted"})

    key, answer = await manager.get_content_key(
        "https://key.example", MagicMock(), {"uri": "u"}, "song 1", headers={"x-extra": "1"}
    )

    assert (key, answer) == ("aabb", {"status": 0, "license": "granted"})
    post = manager.provider.mass.http_session.post
    assert post.call_args.args == ("https://key.example",)
    assert json.loads(post.call_args.kwargs["data"]) == {
        "challenge": "Y2hhbGxlbmdl",
        "key-system": "com.widevine.alpha",
        "uri": "u",
    }
    assert post.call_args.kwargs["headers"]["x-extra"] == "1"
    assert post.call_args.kwargs["headers"]["media-user-token"] == "user"
    cdm.parse_license.assert_called_once_with("session", "granted")
    cdm.close.assert_called_once_with("session")
    # the answer is read whatever content type the license server declares
    response = post.return_value.__aenter__.return_value
    assert response.json.await_args.kwargs["content_type"] is None


async def test_an_answer_without_a_license_closes_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal inside a successful answer is an error, and the CDM session is closed."""
    manager, cdm = _manager(monkeypatch, {"status": 1, "errorCode": 5})

    with pytest.raises(LicenseRefusedError, match=r"song 1 \(status 1, error 5\)") as raised:
        await manager.get_content_key("https://key.example", MagicMock(), {}, "song 1")

    assert (raised.value.status, raised.value.error_code) == (1, 5)
    cdm.close.assert_called_once_with("session")


@pytest.mark.parametrize(("check_status", "refused"), [(True, True), (False, False)])
async def test_a_non_zero_status_with_a_license_is_refused_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, check_status: bool, refused: bool
) -> None:
    """The live radio treats any non-zero status as a refusal, songs keep the old check."""
    manager, cdm = _manager(monkeypatch, {"status": 2, "license": "granted"})

    if refused:
        with pytest.raises(LicenseRefusedError):
            await manager.get_content_key(
                "https://key.example", MagicMock(), {}, "live radio", check_status=check_status
            )
        cdm.parse_license.assert_not_called()
    else:
        key, _ = await manager.get_content_key(
            "https://key.example", MagicMock(), {}, "song 1", check_status=check_status
        )
        assert key == "aabb"


async def test_an_empty_answer_is_an_error_naming_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty body reads as None, which is no license answer at all."""
    manager, cdm = _manager(monkeypatch, {})
    response = manager.provider.mass.http_session.post.return_value.__aenter__.return_value
    response.json = AsyncMock(return_value=None)

    with pytest.raises(MediaNotFoundError, match="license answer for song 1") as err:
        await manager.get_content_key("https://key.example", MagicMock(), {}, "song 1")

    assert not isinstance(err.value, LicenseRefusedError)
    cdm.close.assert_called_once_with("session")


async def test_a_license_without_a_content_key_names_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A license that holds no content key is an error naming the item, not a refusal."""
    manager, cdm = _manager(monkeypatch, {"status": 0, "license": "granted"})
    cdm.get_keys.return_value = [MagicMock(type="SIGNING")]

    with pytest.raises(MediaNotFoundError, match="Unable to get decryption key for song 1") as err:
        await manager.get_content_key("https://key.example", MagicMock(), {}, "song 1")

    assert not isinstance(err.value, LicenseRefusedError)


def _stream_manager(metadata: dict[str, Any]) -> tuple[AppleMusicStreamingManager, AsyncMock]:
    """Return a streaming manager serving the given webPlayback reply, with its key request."""
    provider: Any = MagicMock(instance_id="apple_music--test")
    provider.mass.cache.get = AsyncMock(return_value=None)
    manager = AppleMusicStreamingManager(provider)
    manager._decrypt_client_id = b"client-id"
    manager._decrypt_private_key = b"private-key"
    manager._fetch_song_stream_metadata = AsyncMock(return_value=metadata)  # type: ignore[method-assign]
    manager._parse_stream_url_and_uri = AsyncMock(return_value=(CTRP_URL, KEY_URI))  # type: ignore[method-assign]
    get_content_key = AsyncMock(return_value=("abc123", {}))
    manager.get_content_key = get_content_key  # type: ignore[method-assign]
    return manager, get_content_key


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
    manager, _ = _stream_manager(_metadata(key_server=True))

    result = await manager.get_stream_details("i.AWPNG58CL3m51X")

    assert result.stream_type is StreamType.ENCRYPTED_HTTP
    assert result.decryption_key == "abc123"
    assert result.audio_format.content_type is ContentType.MP4
    # the cbcp256 asset at index 0 is FairPlay and has no usable key
    assert result.path == CTRP_URL


async def test_library_track_is_licensed_under_the_purchase_adam_id() -> None:
    """Apple rejects a library ID at the license server, so songId must be sent."""
    manager, get_content_key = _stream_manager(_metadata(key_server=True))

    await manager.get_stream_details("i.AWPNG58CL3m51X")

    assert get_content_key.await_args is not None
    assert get_content_key.await_args.args[2]["adamId"] == "397010985"


async def test_catalog_track_still_licenses_under_its_own_id() -> None:
    """For a catalog track songId is the catalog ID, so nothing changes."""
    manager, get_content_key = _stream_manager(_metadata(key_server=True, song_id="1193701326"))

    await manager.get_stream_details("1193701326")

    assert get_content_key.await_args is not None
    assert get_content_key.await_args.args[2]["adamId"] == "1193701326"


async def test_library_track_without_key_server_is_served_clear() -> None:
    """Uploads and iTunes Match carry no key server and play without decryption."""
    manager, get_content_key = _stream_manager(_metadata(key_server=False))

    result = await manager.get_stream_details("i.uploadedsong")

    assert result.stream_type is StreamType.HTTP
    assert result.decryption_key is None
    assert result.path == CBCP_URL
    get_content_key.assert_not_called()


async def test_missing_cdm_files_raise_for_an_encrypted_library_track() -> None:
    """Without a CDM an encrypted library track must fail loudly, not stream unplayable."""
    manager, _ = _stream_manager(_metadata(key_server=True))
    manager._decrypt_client_id = None

    with pytest.raises(MediaNotFoundError):
        await manager.get_stream_details("i.AWPNG58CL3m51X")
