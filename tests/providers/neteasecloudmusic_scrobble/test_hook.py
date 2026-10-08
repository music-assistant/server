"""Tests for the NetEase Cloud Music scrobbler's playback report hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError
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


def _album_detail_client() -> Mock:
    """Build an NCM api client mock that resolves any track to album 456."""
    return Mock(
        get=AsyncMock(
            side_effect=lambda path, **_kwargs: (
                {"songs": [{"al": {"id": 456}}]} if path == "/song/detail" else {"code": 200}
            )
        )
    )


def _ncm_provider(instance_id: str = INSTANCE_A) -> Mock:
    """Build a loaded, available NetEase Cloud Music provider instance."""
    provider = Mock(spec=NeteaseCloudMusicProvider)
    provider.instance_id = instance_id
    provider.name = instance_id
    provider.available = True
    provider.cookie = "MUSIC_U=secret"
    provider.api_client = _album_detail_client()
    return provider


def _mass(*providers: Mock) -> Mock:
    """Build a mocked server exposing the given providers and a working in-memory cache."""
    mass = Mock()
    mass.config.get.return_value = {}
    mass.providers = list(providers)

    def _get_provider(instance_id: str, **_kwargs: object) -> Mock | None:
        """Return the provider with exactly this instance id, if any."""
        return next((prov for prov in providers if prov.instance_id == instance_id), None)

    mass.get_provider = Mock(side_effect=_get_provider)
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
    mass.webserver.auth.list_users = AsyncMock(return_value=[])
    mass.players.all_players.return_value = []
    return mass


def _plugin(mass: Mock) -> NeteaseScrobbleProvider:
    """Build a NetEase scrobble plugin instance with default (empty) config."""
    config = Mock()
    config.values = {}
    config.get_value.side_effect = lambda _key, default=None: default
    config.instance_id = "neteasecloudmusic_scrobble--x"
    return NeteaseScrobbleProvider(
        mass, Mock(domain="neteasecloudmusic_scrobble"), config, SUPPORTED_FEATURES
    )


def _handler_provider(mass: Mock) -> Mock:
    """Build a plugin stub that a handler can be constructed from."""
    plugin = Mock()
    plugin.mass = mass
    plugin.instance_id = "neteasecloudmusic_scrobble--x"
    plugin.domain = "neteasecloudmusic_scrobble"
    plugin.config = Mock()
    plugin.config.get_value.side_effect = lambda _key, default=None: default
    plugin.logger = Mock()
    return plugin


def _handler(mass: Mock) -> NeteaseScrobbleHandler:
    """Build a handler directly against a mocked server."""
    return NeteaseScrobbleHandler(_handler_provider(mass))


def _report(
    uri: str = f"{INSTANCE_A}://track/123",
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


async def test_plugin_has_no_source_selection_entry() -> None:
    """The plugin exposes only the shared scrobbler options, no source picker."""
    provider = _plugin(_mass(_ncm_provider()))

    entries = await provider.get_config_entries()

    assert {entry.key for entry in entries} == {
        "suffix_version",
        "scrobble_users",
        "scrobble_players",
    }
    assert ProviderFeature.SCROBBLE in provider.supported_features


async def test_handler_is_created_on_async_init() -> None:
    """The handler is built during async init, with no source to resolve first."""
    provider = _plugin(_mass(_ncm_provider()))

    await provider.handle_async_init()

    assert provider._handler is not None


def test_should_scrobble_only_after_fully_played() -> None:
    """Nothing is checked in until the track has been listened to the end."""
    handler = _handler(_mass())

    assert handler.should_scrobble(_report(seconds_played=5)) is False
    assert handler.should_scrobble(_report(seconds_played=100)) is False
    assert handler.should_scrobble(_report(seconds_played=180, fully_played=True)) is True


async def test_should_scrobble_dedups_after_a_successful_check_in() -> None:
    """A completed check-in suppresses further reports for the same play."""
    handler = _handler(_mass(_ncm_provider()))
    report = _report(uri=f"{INSTANCE_A}://track/123", seconds_played=180, fully_played=True)

    # not yet marked before the check-in actually happens
    assert handler.should_scrobble(report) is True
    await handler._scrobble(report)
    assert handler.should_scrobble(report) is False


async def test_should_scrobble_allows_a_replay() -> None:
    """Progress going backwards marks a new play, which may be checked in again."""
    handler = _handler(_mass(_ncm_provider()))
    uri = f"{INSTANCE_A}://track/123"

    await handler._scrobble(_report(uri=uri, seconds_played=180, fully_played=True))
    assert handler.should_scrobble(_report(uri=uri, seconds_played=180, fully_played=True)) is False
    # the track restarted (loop/replay)
    assert handler.should_scrobble(_report(uri=uri, seconds_played=5)) is False
    assert handler.should_scrobble(_report(uri=uri, seconds_played=180, fully_played=True)) is True


async def test_state_is_keyed_per_player() -> None:
    """The same track playing on two players is tracked independently."""
    handler = _handler(_mass(_ncm_provider()))
    uri = f"{INSTANCE_A}://track/123"

    await handler._scrobble(_report(uri=uri, seconds_played=180, fully_played=True, player_id="p1"))

    assert (
        handler.should_scrobble(
            _report(uri=uri, seconds_played=180, fully_played=True, player_id="p2")
        )
        is True
    )
    assert (
        handler.should_scrobble(
            _report(uri=uri, seconds_played=180, fully_played=True, player_id="p1")
        )
        is False
    )


async def test_transient_failure_does_not_mark_the_play() -> None:
    """A failed /scrobble leaves the play unmarked so a later report can retry it."""
    inst = _ncm_provider(INSTANCE_A)
    scrobble_calls = {"n": 0}

    async def _get(path: str, **_kwargs: object) -> dict[str, object]:
        if path == "/song/detail":
            return {"songs": [{"al": {"id": 456}}]}
        scrobble_calls["n"] += 1
        if scrobble_calls["n"] == 1:
            raise InvalidDataError("Netease API error code 400 for /scrobble")
        return {"code": 200}

    inst.api_client = Mock(get=AsyncMock(side_effect=_get))
    provider = _plugin(_mass(inst))
    await provider.handle_async_init()
    report = _report(uri=f"{INSTANCE_A}://track/123", seconds_played=200, fully_played=True)

    # the transient failure is swallowed by the helper and the play stays unmarked
    await provider.on_media_item_played(report)
    assert provider._handler is not None
    assert provider._handler._scrobbled_plays == set()

    # a later completion report retries and succeeds
    await provider.on_media_item_played(report)
    assert provider._handler._scrobbled_plays


async def test_direct_uri_falls_back_to_the_scheme_instance() -> None:
    """Without queue streamdetails a direct uri reports through its scheme instance."""
    inst_a = _ncm_provider(INSTANCE_A)
    inst_b = _ncm_provider(INSTANCE_B)
    handler = _handler(_mass(inst_a, inst_b))

    await handler._scrobble(_report(uri=f"{INSTANCE_B}://track/123", seconds_played=42))

    scrobble = next(
        call for call in inst_b.api_client.get.await_args_list if call.args[0] == "/scrobble"
    )
    assert scrobble.kwargs["params"] == {
        "id": "123",
        "sourceid": "456",
        "time": 42,
        "cookie": "MUSIC_U=secret",
    }
    assert scrobble.kwargs["cookie"] == "MUSIC_U=secret"
    # the other instance is not involved
    assert inst_a.api_client.get.await_args_list == []


async def test_direct_uri_prefers_the_streaming_instance() -> None:
    """A direct uri is reported to the instance that streamed it, not its scheme."""
    inst_a = _ncm_provider(INSTANCE_A)
    inst_b = _ncm_provider(INSTANCE_B)
    mass = _mass(inst_a, inst_b)
    # the item sits on A (its uri scheme) but MA served the stream from B (failover)
    mass.player_queues.items.return_value = [
        Mock(
            uri=f"{INSTANCE_A}://track/123",
            streamdetails=Mock(provider=INSTANCE_B, item_id="123"),
        )
    ]
    handler = _handler(mass)

    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/123", seconds_played=42))

    assert "/scrobble" in {call.args[0] for call in inst_b.api_client.get.await_args_list}
    assert inst_a.api_client.get.await_args_list == []


async def test_library_track_reports_through_the_streaming_instance() -> None:
    """A library play is checked in to whichever NetEase instance served the stream."""
    inst_a = _ncm_provider(INSTANCE_A)
    inst_b = _ncm_provider(INSTANCE_B)
    mass = _mass(inst_a, inst_b)
    mass.player_queues.items.return_value = [
        Mock(uri="library://track/1", streamdetails=Mock(provider=INSTANCE_B, item_id="123"))
    ]
    handler = _handler(mass)

    await handler._scrobble(_report(uri="library://track/1", seconds_played=42))

    assert "/scrobble" in {call.args[0] for call in inst_b.api_client.get.await_args_list}
    assert inst_a.api_client.get.await_args_list == []


async def test_library_track_not_streamed_from_netease_is_skipped() -> None:
    """A library play that streamed from another provider is not checked in."""
    inst = _ncm_provider(INSTANCE_A)
    mass = _mass(inst)
    mass.player_queues.items.return_value = [
        Mock(
            uri="library://track/1",
            streamdetails=Mock(provider="some_other_provider", item_id="42"),
        )
    ]
    handler = _handler(mass)

    await handler._scrobble(_report(uri="library://track/1", seconds_played=42))

    inst.api_client.get.assert_not_awaited()


async def test_queue_lookup_scans_beyond_the_first_page() -> None:
    """A queue item past the first page is still found and checked in."""
    inst = _ncm_provider(INSTANCE_A)
    mass = _mass(inst)
    first_page = [
        Mock(uri=f"library://track/{i}", streamdetails=Mock(provider="other", item_id=str(i)))
        for i in range(QUEUE_PAGE_SIZE)
    ]
    second_page = [
        Mock(uri="library://track/999", streamdetails=Mock(provider=INSTANCE_A, item_id="123"))
    ]
    mass.player_queues.items.side_effect = [first_page, second_page, []]
    handler = _handler(mass)

    await handler._scrobble(_report(uri="library://track/999", seconds_played=42))

    pages = [call.kwargs["offset"] for call in mass.player_queues.items.call_args_list]
    assert pages == [0, QUEUE_PAGE_SIZE]
    scrobble = next(
        call for call in inst.api_client.get.await_args_list if call.args[0] == "/scrobble"
    )
    assert scrobble.kwargs["params"]["id"] == "123"


async def test_album_source_id_is_cached() -> None:
    """The album id lookup for a track hits the api only once."""
    inst = _ncm_provider(INSTANCE_A)
    handler = _handler(_mass(inst))

    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/123", seconds_played=42))
    await handler._scrobble(_report(uri=f"{INSTANCE_A}://track/123", seconds_played=42))

    detail_calls = [c for c in inst.api_client.get.await_args_list if c.args[0] == "/song/detail"]
    assert len(detail_calls) == 1


async def test_expired_session_is_skipped_without_unloading_plugin() -> None:
    """A lapsed NetEase login only skips the check-in; the plugin stays loaded."""
    inst = _ncm_provider(INSTANCE_A)
    inst.api_client = Mock(
        get=AsyncMock(side_effect=InvalidDataError("Netease API error code 301 for /song/detail"))
    )
    mass = _mass(inst)
    provider = _plugin(mass)
    await provider.handle_async_init()

    await provider.on_media_item_played(
        _report(uri=f"{INSTANCE_A}://track/123", seconds_played=200, fully_played=True)
    )

    assert provider._handler is not None
    mass.call_later.assert_not_called()


async def test_other_api_errors_are_swallowed_by_the_helper() -> None:
    """A transient NetEase api failure is logged and swallowed, the plugin stays loaded."""
    inst = _ncm_provider(INSTANCE_A)
    inst.api_client = Mock(
        get=AsyncMock(side_effect=InvalidDataError("Netease API error code 400 for /scrobble"))
    )
    mass = _mass(inst)
    provider = _plugin(mass)
    await provider.handle_async_init()

    await provider.on_media_item_played(
        _report(uri=f"{INSTANCE_A}://track/123", seconds_played=200, fully_played=True)
    )

    assert provider._handler is not None
    mass.call_later.assert_not_called()
