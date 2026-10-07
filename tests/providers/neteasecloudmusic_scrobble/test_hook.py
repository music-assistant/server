"""Tests for the NetEase Cloud Music scrobbler's playback report hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError, LoginFailed
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider
from music_assistant.providers.neteasecloudmusic_scrobble import (
    QUEUE_PAGE_SIZE,
    SUPPORTED_FEATURES,
    NeteaseScrobbleHandler,
    NeteaseScrobbleProvider,
)

INSTANCE_A = "neteasecloudmusic--aaaa"
INSTANCE_B = "neteasecloudmusic--bbbb"


def _ncm_provider(instance_id: str = INSTANCE_A) -> Mock:
    """Build a loaded, available NetEase Cloud Music provider instance."""
    provider = Mock(spec=NeteaseCloudMusicProvider)
    provider.instance_id = instance_id
    provider.name = instance_id
    provider.available = True
    provider.cookie = "MUSIC_U=secret"
    provider.api_client = Mock(get=AsyncMock(return_value={"code": 200}))
    return provider


def _mass(*providers: Mock) -> Mock:
    """Build a mocked server exposing the given providers and a working in-memory cache."""
    mass = Mock()
    mass.config.get.return_value = {}
    mass.providers = list(providers)
    stored: dict[str, str] = {}

    async def cache_get(key: str, default: str | None = None, **_kwargs: object) -> str | None:
        """Return the previously cached value for the key, if any."""
        return stored.get(key, default)

    async def cache_set(key: str, data: str, **_kwargs: object) -> None:
        """Store the value for the key."""
        stored[key] = data

    mass.cache.get = AsyncMock(side_effect=cache_get)
    mass.cache.set = AsyncMock(side_effect=cache_set)
    mass.player_queues.items.return_value = []
    return mass


def _provider(mass: Mock) -> NeteaseScrobbleProvider:
    """Build a NetEase scrobble plugin instance with default (empty) config."""
    config = Mock()
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    config.instance_id = "neteasecloudmusic_scrobble--x"
    return NeteaseScrobbleProvider(
        mass, Mock(domain="neteasecloudmusic_scrobble"), config, SUPPORTED_FEATURES
    )


def _report(
    uri: str = "neteasecloudmusic://track/123",
    *,
    seconds_played: int = 30,
    fully_played: bool = False,
    is_playing: bool = True,
    player_id: str = "player-1",
) -> MediaItemPlaybackProgressReport:
    """Build a playback progress report for a track."""
    return MediaItemPlaybackProgressReport(
        uri=uri,
        media_type=MediaType.TRACK,
        name="track",
        duration=200,
        seconds_played=seconds_played,
        fully_played=fully_played,
        is_playing=is_playing,
        player_id=player_id,
    )


async def test_single_instance_is_selected_by_default() -> None:
    """With exactly one NetEase instance there is nothing to choose."""
    ncm = _ncm_provider()
    provider = _provider(_mass(ncm))

    await provider.handle_async_init()

    assert provider._ncm_provider is ncm
    assert ProviderFeature.SCROBBLE in provider.supported_features


async def test_two_instances_need_an_explicit_selection() -> None:
    """Without a pick, an ambiguous set of instances is not guessed."""
    provider = _provider(_mass(_ncm_provider(INSTANCE_A), _ncm_provider(INSTANCE_B)))

    await provider.handle_async_init()

    assert provider._ncm_provider is None


async def test_explicit_selection_wins() -> None:
    """The configured instance id picks the matching provider."""
    inst_a = _ncm_provider(INSTANCE_A)
    inst_b = _ncm_provider(INSTANCE_B)
    provider = _provider(_mass(inst_a, inst_b))
    provider.get_setup_value = Mock(return_value=INSTANCE_B)  # type: ignore[method-assign]

    await provider.handle_async_init()

    assert provider._ncm_provider is inst_b


async def test_unavailable_instance_is_not_used() -> None:
    """A not-yet-loaded instance cannot scrobble."""
    ncm = _ncm_provider()
    ncm.available = False
    provider = _provider(_mass(ncm))

    await provider.handle_async_init()

    assert provider._ncm_provider is None


def test_should_scrobble_only_after_fully_played() -> None:
    """Nothing is checked in until the track has been listened to the end."""
    handler = NeteaseScrobbleHandler(_handler_provider())

    assert handler.should_scrobble(_report(seconds_played=5)) is False
    assert handler.should_scrobble(_report(seconds_played=100)) is False
    assert handler.should_scrobble(_report(seconds_played=180, fully_played=True)) is True


def test_should_scrobble_dedups_a_single_play() -> None:
    """A single play is only checked in once."""
    handler = NeteaseScrobbleHandler(_handler_provider())

    assert handler.should_scrobble(_report(seconds_played=180, fully_played=True)) is True
    assert handler.should_scrobble(_report(seconds_played=180, fully_played=True)) is False


def test_should_scrobble_allows_a_replay() -> None:
    """Progress going backwards marks a new play, which may be checked in again."""
    handler = NeteaseScrobbleHandler(_handler_provider())

    assert handler.should_scrobble(_report(seconds_played=180, fully_played=True)) is True
    assert handler.should_scrobble(_report(seconds_played=180, fully_played=True)) is False
    # the track restarted (loop/replay)
    assert handler.should_scrobble(_report(seconds_played=5)) is False
    assert handler.should_scrobble(_report(seconds_played=180, fully_played=True)) is True


async def test_scrobble_resolves_the_album_and_submits() -> None:
    """A direct provider-track uri is checked in with its album id as source."""
    ncm = _ncm_provider()
    ncm.api_client = Mock(
        get=AsyncMock(
            side_effect=lambda path, **_kwargs: (
                {"songs": [{"al": {"id": 456}}]} if path == "/song/detail" else {"code": 200}
            )
        )
    )
    handler = NeteaseScrobbleHandler(_handler_provider(ncm))

    await handler._scrobble(_report(seconds_played=42))

    calls = {call.args[0]: call for call in ncm.api_client.get.await_args_list}
    assert calls["/song/detail"].kwargs["params"] == {"ids": "123"}
    # the cookie must ride along in the scrobble params too: some NCM api backends
    # only attribute the play to the account when it is passed as a query param
    assert calls["/scrobble"].kwargs["params"] == {
        "id": "123",
        "sourceid": "456",
        "time": 42,
        "cookie": "MUSIC_U=secret",
    }
    assert calls["/scrobble"].kwargs["cookie"] == "MUSIC_U=secret"


async def test_library_track_not_streamed_from_netease_is_skipped() -> None:
    """A library play that streamed from another provider is not checked in."""
    ncm = _ncm_provider()
    mass = _mass(ncm)
    streamdetails = Mock(provider="some_other_provider", item_id="42")
    mass.player_queues.items.return_value = [
        Mock(uri="library://track/1", streamdetails=streamdetails)
    ]
    handler = NeteaseScrobbleHandler(_handler_provider(ncm, mass=mass))

    await handler._scrobble(_report(uri="library://track/1", seconds_played=42))

    ncm.api_client.get.assert_not_awaited()


async def test_handler_is_created_once_a_provider_loads() -> None:
    """A provider that loads after the plugin still gets its plays checked in."""
    mass = _mass()
    provider = _provider(mass)
    await provider.handle_async_init()
    initially = provider._ncm_provider
    assert initially is None

    ncm = _ncm_provider()
    ncm.api_client = _album_detail_client()
    mass.providers = [ncm]

    await provider.on_media_item_played(_report(seconds_played=200, fully_played=True))

    assert provider._ncm_provider is ncm
    handler = provider._handler
    assert handler is not None
    assert "/scrobble" in {call.args[0] for call in ncm.api_client.get.await_args_list}


async def test_handler_follows_a_reloaded_provider_instance() -> None:
    """A (re)loaded provider instance replaces the handler's stale reference."""
    inst_a = _ncm_provider(INSTANCE_A)
    mass = _mass(inst_a)
    provider = _provider(mass)
    await provider.handle_async_init()
    await provider.on_media_item_played(_report(seconds_played=200, fully_played=True))
    assert provider._ncm_provider is inst_a

    inst_b = _ncm_provider(INSTANCE_B)
    mass.providers = [inst_b]

    await provider.on_media_item_played(_report(seconds_played=200, fully_played=True))

    assert provider._ncm_provider is inst_b
    assert provider._handler is not None
    assert provider._handler._ncm is inst_b
    # the check-in must go out through the new instance (fresh cookie)
    assert "/song/detail" in {call.args[0] for call in inst_b.api_client.get.await_args_list}


async def test_queue_lookup_scans_beyond_the_first_page() -> None:
    """A queue item past the first page is still found and checked in."""
    ncm = _ncm_provider()
    ncm.api_client = _album_detail_client()
    mass = _mass(ncm)
    first_page = [
        Mock(uri=f"library://track/{i}", streamdetails=Mock(provider="other", item_id=str(i)))
        for i in range(QUEUE_PAGE_SIZE)
    ]
    second_page = [
        Mock(uri="library://track/999", streamdetails=Mock(provider=ncm.instance_id, item_id="123"))
    ]
    mass.player_queues.items.side_effect = [first_page, second_page, []]
    handler = NeteaseScrobbleHandler(_handler_provider(ncm, mass=mass))

    await handler._scrobble(_report(uri="library://track/999", seconds_played=42))

    pages = [call.kwargs["offset"] for call in mass.player_queues.items.call_args_list]
    assert pages == [0, QUEUE_PAGE_SIZE]
    scrobble_call = next(
        call for call in ncm.api_client.get.await_args_list if call.args[0] == "/scrobble"
    )
    assert scrobble_call.kwargs["params"]["id"] == "123"


async def test_provider_instance_uri_scheme_is_accepted() -> None:
    """A direct provider-track uri may use the instance id as its scheme."""
    ncm = _ncm_provider()
    ncm.api_client = _album_detail_client()
    handler = NeteaseScrobbleHandler(_handler_provider(ncm))

    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/123", seconds_played=42))

    assert "/scrobble" in {call.args[0] for call in ncm.api_client.get.await_args_list}


async def test_album_source_id_is_cached() -> None:
    """The album id lookup for a track hits the api only once."""
    ncm = _ncm_provider()
    ncm.api_client = _album_detail_client()
    handler = NeteaseScrobbleHandler(_handler_provider(ncm))

    await handler._scrobble(_report(seconds_played=42))
    await handler._scrobble(_report(seconds_played=42))

    detail_calls = [c for c in ncm.api_client.get.await_args_list if c.args[0] == "/song/detail"]
    assert len(detail_calls) == 1


async def test_expired_session_unloads_the_plugin() -> None:
    """A NetEase api 'not logged in' answer unloads the plugin for re-authentication."""
    ncm = _ncm_provider()
    ncm.api_client = Mock(
        get=AsyncMock(side_effect=InvalidDataError("Netease API error code 301 for /scrobble"))
    )
    mass = _mass(ncm)
    provider = _provider(mass)
    await provider.handle_async_init()

    await provider.on_media_item_played(_report(seconds_played=200, fully_played=True))

    assert provider._handler is None
    mass.call_later.assert_called_once()
    unload_error = mass.call_later.call_args.args[3]
    assert isinstance(unload_error, LoginFailed)


async def test_other_api_errors_do_not_unload_the_plugin() -> None:
    """A transient NetEase api failure is only logged, the plugin stays loaded."""
    ncm = _ncm_provider()
    ncm.api_client = Mock(
        get=AsyncMock(side_effect=InvalidDataError("Netease API error code 400 for /scrobble"))
    )
    mass = _mass(ncm)
    provider = _provider(mass)
    await provider.handle_async_init()

    await provider.on_media_item_played(_report(seconds_played=200, fully_played=True))

    assert provider._handler is not None
    mass.call_later.assert_not_called()


def _album_detail_client() -> Mock:
    """Build an NCM api client mock that resolves track 123 to album 456."""
    return Mock(
        get=AsyncMock(
            side_effect=lambda path, **_kwargs: (
                {"songs": [{"al": {"id": 456}}]} if path == "/song/detail" else {"code": 200}
            )
        )
    )


def _handler_provider(ncm: Mock | None = None, *, mass: Mock | None = None) -> Mock:
    """Build a resolved plugin stub that a handler can be constructed from."""
    ncm = ncm or _ncm_provider()
    plugin = Mock()
    plugin.mass = mass or _mass(ncm)
    plugin.instance_id = "neteasecloudmusic_scrobble--x"
    plugin.domain = "neteasecloudmusic_scrobble"
    plugin.config = Mock()
    plugin.config.get_value.side_effect = lambda _key, default=None: default
    plugin.logger = Mock()
    plugin._ncm_provider = ncm
    return plugin
