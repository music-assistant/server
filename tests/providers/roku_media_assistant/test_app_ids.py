"""Tests for which Roku app the Roku provider plays in."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.roku_media_assistant.player import (
    MediaAssistantPlayer,
    parse_app_ids,
)

PLAYER_ID = "ROKU_TEST0001"
MEDIA_ASSISTANT = "782875"
OTHER_APP = "123456"


def _app(app_id: str | None, screensaver: bool = False) -> MagicMock:
    """Return an app as rokuecp reports it."""
    app = MagicMock()
    app.app_id = app_id
    app.screensaver = screensaver
    return app


def _make_player(
    setting: str,
    in_front: str | None = "562859",
    installed: tuple[str, ...] = (),
    screensaver: bool = False,
) -> MediaAssistantPlayer:
    """Create a player whose Roku has an app in front and some apps installed."""
    player = MediaAssistantPlayer.__new__(MediaAssistantPlayer)
    mass = MagicMock()
    mass.streams.resolve_stream_url = AsyncMock(return_value="http://192.0.2.10:8097/s.flac")
    provider = MagicMock()
    provider.mass = mass
    provider.config.get_value.return_value = setting
    player.mass = mass
    player._provider = provider
    player.logger = logging.getLogger("test.roku_media_assistant.player")
    player._player_id = PLAYER_ID
    player._cache = {}
    player._config = MagicMock()
    player._config.get_value.return_value = False
    player._config.name = None
    player._attr_name = "Roku"
    player._attr_supported_features = {PlayerFeature.PLAY_MEDIA, PlayerFeature.ENQUEUE}
    player._attr_powered = False
    player._attr_playback_state = PlaybackState.IDLE
    player._attr_current_media = None
    player._attr_elapsed_time = None
    player._attr_elapsed_time_last_updated = None
    player.queued = None
    device = MagicMock()
    device.app = _app(in_front, screensaver)
    player.roku = MagicMock()
    player.roku.update = AsyncMock(return_value=device)
    player.roku._get_apps = AsyncMock(return_value=[{"@id": app_id} for app_id in installed])
    player.roku.launch = AsyncMock()
    player.roku._get_media_state = AsyncMock(return_value={"@state": "play"})
    player.roku_input = AsyncMock()  # type: ignore[method-assign]
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player


def _media() -> PlayerMedia:
    """Return a queue item."""
    return PlayerMedia(
        uri="library://track/1", media_type=MediaType.TRACK, title="Song", duration=200
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("782875", ["782875"]),
        (None, []),
        (782875, ["782875"]),
        (True, []),
    ],
)
def test_parse_app_ids(value: object, expected: list[str]) -> None:
    """The setting's app ID as a string; a number counts, anything else gives none."""
    assert parse_app_ids(value) == expected


async def test_play_launches_nothing_without_an_app_id(caplog: pytest.LogCaptureFixture) -> None:
    """A setting with no app ID (not a string or a number) launches nothing; the log says why."""
    player = _make_player(MEDIA_ASSISTANT)
    player.provider.config.get_value.return_value = True  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR):
        await player.play_media(_media())

    player.roku.launch.assert_not_awaited()  # type: ignore[attr-defined]
    assert "No Roku app ID is configured" in caplog.text


async def test_play_goes_to_the_app_in_front() -> None:
    """The app in front gets the stream through /input."""
    player = _make_player("dev", in_front="dev")
    await player.play_media(_media())

    player.roku_input.assert_awaited_once()  # type: ignore[attr-defined]
    player.roku.launch.assert_not_awaited()  # type: ignore[attr-defined]


async def test_play_launches_the_app_without_asking_for_apps() -> None:
    """With the app not in front, it is launched, without fetching the Roku's app list."""
    player = _make_player(MEDIA_ASSISTANT)
    await player.play_media(_media())

    player.roku.update.assert_awaited_once_with()  # type: ignore[attr-defined]
    player.roku._get_apps.assert_not_awaited()  # type: ignore[attr-defined]
    player.roku.launch.assert_awaited_once()  # type: ignore[attr-defined]
    assert player.roku.launch.await_args.args[0] == MEDIA_ASSISTANT  # type: ignore[attr-defined]
    player.roku_input.assert_not_awaited()  # type: ignore[attr-defined]


async def test_play_under_screensaver_launches_again() -> None:
    """The app under the screensaver is launched rather than sent /input."""
    player = _make_player("dev", in_front="dev", screensaver=True)
    await player.play_media(_media())

    player.roku.launch.assert_awaited_once()  # type: ignore[attr-defined]
    assert player.roku.launch.await_args.args[0] == "dev"  # type: ignore[attr-defined]
    player.roku_input.assert_not_awaited()  # type: ignore[attr-defined]


async def test_poll_counts_the_app_in_front_as_powered() -> None:
    """The player is on while the app is in front, and off otherwise."""
    player = _make_player("dev", in_front="dev")
    await player.poll()
    assert player._attr_powered is True

    player.roku.update.return_value.app = _app(OTHER_APP)  # type: ignore[attr-defined]
    await player.poll()
    assert player._attr_powered is False


async def test_enqueue_goes_to_the_app_in_front() -> None:
    """The next item is enqueued in the app in front."""
    player = _make_player("dev", in_front="dev")
    await player.enqueue_next_media(_media())

    player.roku_input.assert_awaited_once()  # type: ignore[attr-defined]
    assert player.queued is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("782875,dev", ["782875", "dev"]),
        (" 782875 , , dev ", ["782875", "dev"]),
        ("", []),
    ],
)
def test_parse_app_ids_splits_a_list(value: str, expected: list[str]) -> None:
    """A comma-separated setting gives its IDs in order; blanks and spaces are ignored."""
    assert parse_app_ids(value) == expected


async def test_play_goes_to_any_listed_app_in_front() -> None:
    """A listed app in front gets the stream, even if it is not the first listed."""
    player = _make_player(f"{MEDIA_ASSISTANT},dev", in_front="dev")
    await player.play_media(_media())

    player.roku_input.assert_awaited_once()  # type: ignore[attr-defined]
    player.roku.launch.assert_not_awaited()  # type: ignore[attr-defined]


async def test_play_launches_first_installed_listed_app() -> None:
    """With no listed app in front, the first listed one installed is launched."""
    player = _make_player(f"{MEDIA_ASSISTANT},{OTHER_APP},dev", installed=("12", "dev", OTHER_APP))
    await player.play_media(_media())

    player.roku.update.assert_awaited_once_with()  # type: ignore[attr-defined]
    player.roku._get_apps.assert_awaited_once()  # type: ignore[attr-defined]
    player.roku.launch.assert_awaited_once()  # type: ignore[attr-defined]
    assert player.roku.launch.await_args.args[0] == OTHER_APP  # type: ignore[attr-defined]
    player.roku_input.assert_not_awaited()  # type: ignore[attr-defined]


async def test_play_launches_nothing_when_no_listed_app_is_installed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Roku ignores /launch of an app it lacks, so nothing is sent; the log says why."""
    player = _make_player(f"{MEDIA_ASSISTANT},dev", installed=("12",))
    with caplog.at_level(logging.ERROR):
        await player.play_media(_media())

    player.roku.launch.assert_not_awaited()  # type: ignore[attr-defined]
    assert "None of the Roku apps 782875, dev is installed" in caplog.text


async def test_play_with_a_cleared_setting_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """A cleared setting launches nothing, and the log names the setting, not an app."""
    player = _make_player("")
    with caplog.at_level(logging.ERROR):
        await player.play_media(_media())

    player.roku.launch.assert_not_awaited()  # type: ignore[attr-defined]
    assert "No Roku app ID is configured" in caplog.text


async def test_play_under_screensaver_launches_the_listed_app_again() -> None:
    """A listed app under the screensaver is launched, if installed, rather than sent /input."""
    player = _make_player(f"{MEDIA_ASSISTANT},dev", in_front="dev", installed=("dev",))
    player.roku.update.return_value.app.screensaver = True  # type: ignore[attr-defined]
    await player.play_media(_media())

    player.roku.launch.assert_awaited_once()  # type: ignore[attr-defined]
    assert player.roku.launch.await_args.args[0] == "dev"  # type: ignore[attr-defined]


async def test_poll_counts_any_listed_app_in_front_as_powered() -> None:
    """The player is on while any listed app is in front, and off otherwise."""
    player = _make_player(f"{MEDIA_ASSISTANT},dev", in_front="dev")
    await player.poll()
    assert player._attr_powered is True

    player.roku.update.return_value.app = _app(OTHER_APP)  # type: ignore[attr-defined]
    await player.poll()
    assert player._attr_powered is False


async def test_enqueue_goes_to_any_listed_app_in_front() -> None:
    """The next item is enqueued in whichever listed app is in front."""
    player = _make_player(f"{MEDIA_ASSISTANT},dev", in_front="dev")
    await player.enqueue_next_media(_media())

    player.roku_input.assert_awaited_once()  # type: ignore[attr-defined]
    assert player.queued is not None
