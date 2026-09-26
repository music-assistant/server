"""Test YouTube Music Provider."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import ytmusicapi
from aiohttp import ClientError, ServerDisconnectedError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import LoginFailed, SetupFailedError
from ytmusicapi.exceptions import YTMusicServerError

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.ytmusic import YoutubeMusicProvider


@pytest.fixture
def provider() -> YoutubeMusicProvider:
    """Return a YoutubeMusicProvider instance with mocked dependencies."""
    mass = AsyncMock()
    mass.http_session = MagicMock()
    manifest = MagicMock()
    manifest.domain = "ytmusic"
    config = MagicMock()
    config.get_value.return_value = "GLOBAL"
    prov = YoutubeMusicProvider(mass, manifest, config)
    prov._po_token_server_url = "http://localhost:4416"
    prov._headers = {}
    prov._yt_user = None
    prov.language = "en"
    return prov


def _ping_context_manager(
    *, response: MagicMock | None = None, exc: Exception | None = None
) -> MagicMock:
    """Build a fake async context manager mimicking aiohttp's session.get()."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response, side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_verify_po_token_url_success(provider: YoutubeMusicProvider) -> None:
    """A healthy PO Token server (HTTP 200) verifies successfully."""
    response = MagicMock()
    response.status = 200
    response.raise_for_status = MagicMock()
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_ping_context_manager(response=response)
    )
    assert await provider._verify_po_token_url() is True


@pytest.mark.parametrize(
    "exc",
    [
        # boot race: the POT container's port accepts TCP before the server is serving,
        # so the ping fails with ServerDisconnectedError (a ClientError, but NOT a
        # ClientConnectorError, which is all the provider used to catch).
        ServerDisconnectedError(),
        # connection refused / DNS failure etc. (the originally-handled family).
        ClientError("connection error"),
        # an explicit/implicit request timeout.
        TimeoutError(),
    ],
)
async def test_verify_po_token_url_transient_failure(
    provider: YoutubeMusicProvider, exc: Exception
) -> None:
    """Transient PO Token server errors return False instead of escaping uncaught."""
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_ping_context_manager(exc=exc)
    )
    assert await provider._verify_po_token_url() is False


async def test_init_unreachable_po_token_server_is_a_retried_setup_failure(
    provider: YoutubeMusicProvider,
) -> None:
    """
    An unreachable PO Token server at load is a setup failure the core retries, not a login one.

    The PO Token server is a separate add-on/container that routinely comes up after
    Music Assistant on a host reboot. A LoginFailed is never retried (it waits for the
    user to fix their credentials), which left the provider dead until a manual reload.
    """
    provider.mass.http_session.get = MagicMock(  # type: ignore[method-assign]
        return_value=_ping_context_manager(exc=ClientError("connection refused"))
    )
    with (
        patch.object(provider, "_install_packages", AsyncMock()),
        patch.object(provider, "get_setup_value", return_value=""),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    assert not isinstance(exc_info.value, LoginFailed)
    assert exc_info.value.translation_key == "po_token_server_unreachable"
    assert exc_info.value.translation_owner == "provider.ytmusic"


async def test_sync_library_unloads_on_invalid_session(provider: YoutubeMusicProvider) -> None:
    """A library sync that hits an invalid session unloads the provider for re-auth."""
    provider.available = True
    provider.unload_with_error = MagicMock()  # type: ignore[method-assign]
    err = LoginFailed("Your YouTube Music session is no longer valid.")
    with (
        patch.object(MusicProvider, "sync_library", AsyncMock(side_effect=err)),
        pytest.raises(LoginFailed),
    ):
        await provider.sync_library(MediaType.PLAYLIST)
    provider.unload_with_error.assert_called_once_with(err)


async def test_sync_library_keeps_other_errors_silent(provider: YoutubeMusicProvider) -> None:
    """Any other sync failure must not unload the provider."""
    provider.available = True
    provider.unload_with_error = MagicMock()  # type: ignore[method-assign]
    with (
        patch.object(MusicProvider, "sync_library", AsyncMock(side_effect=KeyError("boom"))),
        pytest.raises(KeyError),
    ):
        await provider.sync_library(MediaType.PLAYLIST)
    provider.unload_with_error.assert_not_called()


def test_parse_owned_playlist_is_editable_without_privacy(
    provider: YoutubeMusicProvider,
) -> None:
    """An owned playlist is editable even when the library response omits privacy."""
    playlist = provider._parse_playlist(
        {
            "id": "PL_owned",
            "title": "Owned playlist",
            "owned": True,
        }
    )

    assert playlist.is_editable is True


async def test_search_is_not_translated(provider: YoutubeMusicProvider) -> None:
    """A search must run in English, whatever language the server is set to."""
    # ytmusicapi matches the (translated) result shelf title against the English filter
    # name, so a filtered search silently returns nothing in most other languages.
    provider.language = "cs"
    mock_ytm = MagicMock()
    mock_ytm.search.return_value = []
    search = cast("Any", YoutubeMusicProvider.search).__wrapped__
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        await search(provider, "test", [MediaType.TRACK])

    assert mock_ytmusic.call_args.kwargs["language"] == "en"


async def test_album_versions_with_versions(provider: YoutubeMusicProvider) -> None:
    """get_album_versions method of the YTM provider should return other album versions if any exist."""
    album_with_versions = {
        "title": "All Stand Together",
        "other_versions": [
            {"browseId": "MPREb_LzqETWfppYZ", "title": "All Stand Together (Deluxe)"}
        ],
    }
    with patch(
        "music_assistant.providers.ytmusic.get_album", AsyncMock(return_value=album_with_versions)
    ):
        # call the undecorated function so the @use_cache wrapper stays out of the test
        get_album_versions = cast("Any", YoutubeMusicProvider.get_album_versions).__wrapped__
        albums = await get_album_versions(provider, "_")

    assert albums[0].item_id == "MPREb_LzqETWfppYZ"
    assert albums[0].name == "All Stand Together"
    assert albums[0].version == "Deluxe"


async def test_album_versions_without_versions(provider: YoutubeMusicProvider) -> None:
    """get_album_versions method of the YTM provider should return nothing if there are no other versions."""
    album_without_versions = {
        "title": "All Stand Together",
    }
    with patch(
        "music_assistant.providers.ytmusic.get_album",
        AsyncMock(return_value=album_without_versions),
    ):
        # call the undecorated function so the @use_cache wrapper stays out of the test
        get_album_versions = cast("Any", YoutubeMusicProvider.get_album_versions).__wrapped__
        albums = await get_album_versions(provider, "_")

    assert len(albums) == 0


def _get_artist_albums_unwrapped() -> Any:
    """Return get_artist_albums with its @use_cache decorator stripped."""
    return cast("Any", YoutubeMusicProvider.get_artist_albums).__wrapped__


async def test_get_artist_albums_paginates_when_browse_pair_present(
    provider: YoutubeMusicProvider,
) -> None:
    """A release section with browseId+params is fetched via the full paginated call."""
    provider._headers = {}
    provider._yt_user = "test-brand-user"
    provider.language = "en"
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "shows": {
            "results": [{"browseId": "MPREb_inline1", "title": "Inline Only Show"}],
            "browseId": "MPADUCtest",
            "params": "params-token",
        },
    }
    mock_ytm.get_artist_albums.return_value = [
        {"browseId": "MPREb_paginated1", "title": "Paginated Show 1"},
        {"browseId": "MPREb_paginated2", "title": "Paginated Show 2"},
    ]
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm) as mock_ytmusic:
        albums = await _get_artist_albums_unwrapped()(provider, "UCtest")

    assert [a.item_id for a in albums] == ["MPREb_paginated1", "MPREb_paginated2"]
    mock_ytm.get_artist_albums.assert_called_once_with(
        channelId="MPADUCtest", params="params-token", limit=None
    )
    assert mock_ytmusic.call_args.kwargs["user"] == "test-brand-user"


async def test_get_artist_albums_skips_pagination_without_browse_pair(
    provider: YoutubeMusicProvider,
) -> None:
    """A section missing browseId or params is not paginated - only its preview is used."""
    provider._headers = {}
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "singles": {"results": [{"browseId": "MPREb_single1", "title": "A Single"}]},
    }
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        albums = await _get_artist_albums_unwrapped()(provider, "UCtest")

    assert [a.item_id for a in albums] == ["MPREb_single1"]
    mock_ytm.get_artist_albums.assert_not_called()


async def test_get_artist_albums_preview_with_empty_artists_falls_back_to_page_artist(
    provider: YoutubeMusicProvider,
) -> None:
    """An albums preview item with an empty artists list gets the page artist."""
    provider._headers = {}
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "albums": {"results": [{"browseId": "MPREb_album1", "title": "An Album", "artists": []}]},
    }
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        albums = await _get_artist_albums_unwrapped()(provider, "UCtest")

    assert [a.item_id for a in albums] == ["MPREb_album1"]
    assert [(artist.item_id, artist.name) for artist in albums[0].artists] == [
        ("UCtest", "Test Artist")
    ]


@pytest.mark.parametrize(
    "error",
    [
        KeyError(
            "Unable to find 'musicCarouselShelfRenderer' using path "
            "['musicCarouselShelfRenderer', 'contents'] on {'gridRenderer': {}}"
        ),
        IndexError("list index out of range"),
        TypeError("'NoneType' object is not subscriptable"),
    ],
    ids=["key_error", "index_error", "type_error"],
)
async def test_get_artist_albums_falls_back_to_preview_on_parse_failure(
    provider: YoutubeMusicProvider, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """A section whose pagination fails to parse degrades to its inline preview and is logged."""
    provider._headers = {}
    provider._yt_user = None
    provider.language = "en"
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "shows": {
            "results": [
                {
                    "browseId": "MPREb_show1",
                    "title": "Folge 1",
                    "artists": [{"id": "UCtest", "name": "Test Artist"}],
                }
            ],
            "browseId": "MPADUCtest",
            "params": "params-token",
        },
    }
    mock_ytm.get_artist_albums.side_effect = error
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        albums = await _get_artist_albums_unwrapped()(provider, "UCtest")

    assert [a.item_id for a in albums] == ["MPREb_show1"]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert "shows" in caplog.records[0].message
    exc_info = caplog.records[0].exc_info
    assert exc_info is not None
    assert exc_info[1] is error


async def test_get_artist_albums_propagates_server_error_instead_of_caching_truncated(
    provider: YoutubeMusicProvider,
) -> None:
    """A genuine ytmusicapi server error must propagate, not degrade to the preview."""
    provider._headers = {}
    provider._yt_user = None
    provider.language = "en"
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "albums": {
            "results": [{"browseId": "MPREb_1", "title": "A"}],
            "browseId": "MPADUCtest",
            "params": "params-token",
        },
    }
    mock_ytm.get_artist_albums.side_effect = YTMusicServerError("backend returned 500")
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm),
        pytest.raises(YTMusicServerError),
    ):
        await _get_artist_albums_unwrapped()(provider, "UCtest")


async def test_get_artist_albums_signed_out_still_raises(
    provider: YoutubeMusicProvider,
) -> None:
    """A genuinely signed-out session must still surface as LoginFailed, not be swallowed."""
    provider._headers = {}
    provider._yt_user = None
    provider.language = "en"
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "albums": {
            "results": [{"browseId": "MPREb_1", "title": "A"}],
            "browseId": "MPADUCtest",
            "params": "params-token",
        },
    }
    mock_ytm.get_artist_albums.side_effect = KeyError(
        "Unable to find 'twoColumnBrowseResultsRenderer' using path [] on "
        "{'singleColumnBrowseResultsRenderer': {'signInEndpoint': {'hack': True}}}"
    )
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm),
        pytest.raises(LoginFailed),
    ):
        await _get_artist_albums_unwrapped()(provider, "UCtest")


async def test_get_artist_albums_signed_out_raises_even_if_another_section_succeeds(
    provider: YoutubeMusicProvider,
) -> None:
    """LoginFailed from one section must still propagate when sections are gathered concurrently."""
    provider._headers = {}
    provider._yt_user = None
    provider.language = "en"
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "albums": {
            "results": [{"browseId": "MPREb_ok", "title": "Fine"}],
            "browseId": "MPADUCtest-albums",
            "params": "params-albums",
        },
        "singles": {
            "results": [{"browseId": "MPREb_single", "title": "A Single"}],
            "browseId": "MPADUCtest-singles",
            "params": "params-singles",
        },
    }

    def _get_artist_albums_side_effect(**kwargs: str) -> list[dict[str, Any]]:
        if kwargs["channelId"] == "MPADUCtest-albums":
            return [{"browseId": "MPREb_paginated_ok", "title": "Fine, paginated"}]
        raise KeyError(
            "Unable to find 'twoColumnBrowseResultsRenderer' using path [] on "
            "{'singleColumnBrowseResultsRenderer': {'signInEndpoint': {'hack': True}}}"
        )

    mock_ytm.get_artist_albums.side_effect = _get_artist_albums_side_effect
    with (
        patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm),
        pytest.raises(LoginFailed),
    ):
        await _get_artist_albums_unwrapped()(provider, "UCtest")


async def test_get_artist_albums_orders_by_section_and_dedupes_across_sections(
    provider: YoutubeMusicProvider,
) -> None:
    """Output order follows each section's own order (albums, then singles, then shows)."""
    provider._headers = {}
    provider._yt_user = None
    provider.language = "en"
    mock_ytm = MagicMock()
    mock_ytm.get_artist.return_value = {
        "channelId": "UCtest",
        "name": "Test Artist",
        "albums": {
            "results": [],
            "browseId": "MPADUCtest-albums",
            "params": "params-albums",
        },
        "singles": {
            "results": [],
            "browseId": "MPADUCtest-singles",
            "params": "params-singles",
        },
    }

    def _get_artist_albums_side_effect(**kwargs: str) -> list[dict[str, Any]]:
        if kwargs["channelId"] == "MPADUCtest-albums":
            return [
                {"browseId": "MPREb_a2", "title": "Album 2"},
                {"browseId": "MPREb_a1", "title": "Album 1"},
            ]
        return [
            {"browseId": "MPREb_a2", "title": "Duplicate of Album 2"},  # cross-section dupe
            {"browseId": "MPREb_s1", "title": "Single 1"},
        ]

    mock_ytm.get_artist_albums.side_effect = _get_artist_albums_side_effect
    with patch.object(ytmusicapi, "YTMusic", return_value=mock_ytm):
        albums = await _get_artist_albums_unwrapped()(provider, "UCtest")

    assert [a.item_id for a in albums] == ["MPREb_a2", "MPREb_a1", "MPREb_s1"]
