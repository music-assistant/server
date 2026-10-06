"""Tests for the NetEase Cloud Music scrobbler's playback report hook."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.playback_progress_report import MediaItemPlaybackProgressReport

from music_assistant.providers.neteasecloudmusic import NeteaseCloudMusicProvider
from music_assistant.providers.neteasecloudmusic_scrobble import (
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
    """Build a mocked server exposing the given providers and empty cache."""
    mass = Mock()
    mass.config.get.return_value = {}
    mass.providers = list(providers)
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
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


def test_should_scrobble_waits_for_the_play_threshold() -> None:
    """A play is only checked in once it is finished or past the minimum duration."""
    handler = NeteaseScrobbleHandler(_handler_provider())

    assert handler.should_scrobble(_report(seconds_played=5)) is False
    assert handler.should_scrobble(_report(seconds_played=29)) is False
    assert handler.should_scrobble(_report(seconds_played=30)) is True


def test_should_scrobble_fully_played_short_track() -> None:
    """A fully played track counts even under the minimum duration."""
    handler = NeteaseScrobbleHandler(_handler_provider())

    assert handler.should_scrobble(_report(seconds_played=8, fully_played=True)) is True


def test_should_scrobble_dedups_a_single_continuous_play() -> None:
    """Periodic reports of one continuous play are only checked in once."""
    handler = NeteaseScrobbleHandler(_handler_provider())

    assert handler.should_scrobble(_report(seconds_played=30)) is True
    assert handler.should_scrobble(_report(seconds_played=60)) is False
    assert handler.should_scrobble(_report(seconds_played=90)) is False


def test_should_scrobble_allows_a_replay() -> None:
    """Progress going backwards marks a new play, which may be checked in again."""
    handler = NeteaseScrobbleHandler(_handler_provider())

    assert handler.should_scrobble(_report(seconds_played=30)) is True
    assert handler.should_scrobble(_report(seconds_played=90)) is False
    # the track restarted (loop/replay)
    assert handler.should_scrobble(_report(seconds_played=5)) is False
    assert handler.should_scrobble(_report(seconds_played=30)) is True


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
    assert calls["/scrobble"].kwargs["params"] == {
        "id": "123",
        "sourceid": "456",
        "time": 42,
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


def _handler_provider(ncm: Mock | None = None, *, mass: Mock | None = None) -> Mock:
    """Build a resolved plugin stub that a handler can be constructed from."""
    ncm = ncm or _ncm_provider()
    plugin = Mock()
    plugin.mass = mass or _mass(ncm)
    plugin.instance_id = "neteasecloudmusic_scrobble--x"
    plugin.config = Mock()
    plugin.config.get_value.side_effect = lambda _key, default=None: default
    plugin.logger = Mock()
    plugin._ncm_provider = ncm
    return plugin
