"""Tests for a Sonos session taken over by another app, and announcements played as clips."""

import asyncio
import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.const import AudioClipStatusChangedEvent, SessionErrorEvent
from aiosonos.const import EventType as SonosEventType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sonos import player as player_module
from music_assistant.providers.sonos.player import SonosPlayer

if TYPE_CHECKING:
    from aiosonos.api.models import AudioClipStatusEvent, SessionError

PLAYER = "RINCON_F0F6C1CC8D0401400"
CLIP_ID = "05edf9d0-591a-494a-8045-f5f01295a3ce"


def _player(cloud_queue_id: str | None = None) -> tuple[SonosPlayer, MagicMock]:
    """Create a connected SonosPlayer with a mocked client, optionally serving our queue."""
    player = SonosPlayer.__new__(SonosPlayer)
    mass = MagicMock()
    mass.streams.get_announcement_duration = AsyncMock(return_value=3)
    player.mass = mass
    player.logger = logging.getLogger("test.sonos.player")
    player._player_id = PLAYER
    player._attr_name = "Office"
    player._cache = {}
    player._config = MagicMock()
    player._config.name = None
    player.cloud_queue_id = cloud_queue_id
    player._evicted_cloud_queue = None
    client = MagicMock()
    player.client = client
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player, client


def _session_error(error_code: str) -> SessionErrorEvent:
    body = cast(
        "SessionError",
        {"_objectType": "sessionError", "errorCode": error_code, "sessionId": "session1"},
    )
    return SessionErrorEvent(SonosEventType.SESSION_ERROR, "group1", body)


def test_an_eviction_releases_our_queue_without_commands() -> None:
    """Another app took the speaker over: MA lets go of its queue and sends nothing."""
    player, client = _player(cloud_queue_id=PLAYER)

    player._on_session_error(_session_error("ERROR_SESSION_EVICTED"))

    assert player.cloud_queue_id is None
    assert player._evicted_cloud_queue is not None
    assert player._evicted_cloud_queue[0] == PLAYER
    cast("MagicMock", player.update_state).assert_called_once()
    assert client.mock_calls == []


def test_another_session_error_keeps_our_queue() -> None:
    """Only an eviction means the speaker is no longer ours."""
    player, _ = _player(cloud_queue_id=PLAYER)

    player._on_session_error(_session_error("ERROR_SESSION_JOIN_FAILED"))

    assert player.cloud_queue_id == PLAYER
    assert player._evicted_cloud_queue is None


def test_an_eviction_without_our_queue_changes_nothing() -> None:
    """A speaker that was not playing our queue has nothing to release."""
    player, _ = _player()

    player._on_session_error(_session_error("ERROR_SESSION_EVICTED"))

    assert player._evicted_cloud_queue is None
    cast("MagicMock", player.update_state).assert_not_called()


def _clip_status(status: str) -> AudioClipStatusChangedEvent:
    """Build the audioClipStatus event the speaker sends as the clip moves on."""
    clip = {
        "_objectType": "audioClip",
        "id": CLIP_ID,
        "name": "Announcement",
        "appId": "aiosonos",
        "priority": "LOW",
        "clipType": "CUSTOM",
        "status": status,
        "clipLEDBehavior": "WHITE_LED_QUICK_BREATHING",
    }
    return AudioClipStatusChangedEvent(
        SonosEventType.AUDIO_CLIP_STATUS,
        PLAYER,
        cast("AudioClipStatusEvent", {"_objectType": "audioClipStatus", "audioClips": [clip]}),
    )


def _speaker_with_clip_status(client: MagicMock) -> list[object]:
    """Let the mocked client keep clip statuses and deliver events to subscribers."""
    statuses: dict[str, str] = {}
    listeners: list[object] = []
    unsubscribe = MagicMock()

    def _subscribe(callback: object, *_args: object) -> MagicMock:
        listeners.append(callback)
        return unsubscribe

    def _deliver(event: AudioClipStatusChangedEvent) -> None:
        for clip in event.data["audioClips"]:
            statuses[clip["id"]] = clip["status"]
        for callback in listeners:
            callback(event)  # type: ignore[operator]

    client.subscribe.side_effect = _subscribe
    client.player.audio_clip_status.side_effect = statuses.get
    client.player.play_audio_clip = AsyncMock(return_value={"id": CLIP_ID, "status": "ACTIVE"})
    return [_deliver, unsubscribe]


def _announcement() -> PlayerMedia:
    return PlayerMedia(uri="http://192.168.1.10:8097/announcement.mp3")


@pytest.mark.asyncio
async def test_an_announcement_returns_when_the_speaker_reports_the_clip_done() -> None:
    """The wait ends on the speaker's DONE, not after a fixed sleep."""
    player, client = _player()
    deliver, unsubscribe = _speaker_with_clip_status(client)

    announcement = asyncio.ensure_future(player.play_announcement(_announcement(), 30))
    await asyncio.sleep(0.05)
    assert not announcement.done()
    deliver(_clip_status("DONE"))  # type: ignore[operator]
    await asyncio.wait_for(announcement, 1)

    client.player.play_audio_clip.assert_awaited_once_with(
        "http://192.168.1.10:8097/announcement.mp3", 30, name="Announcement"
    )
    cast("MagicMock", unsubscribe).assert_called_once()
    assert client.subscribe.call_args.args[1:] == (SonosEventType.AUDIO_CLIP_STATUS, PLAYER)


@pytest.mark.asyncio
async def test_an_announcement_without_clip_status_waits_its_duration_at_most() -> None:
    """A speaker that reports nothing still releases the announcement after its duration."""
    player, client = _player()
    _speaker_with_clip_status(client)
    cast("MagicMock", player.mass).streams.get_announcement_duration = AsyncMock(return_value=0.05)

    with patch.object(player_module, "ANNOUNCEMENT_STATUS_MARGIN", 0.05):
        await asyncio.wait_for(player.play_announcement(_announcement()), 1)


@pytest.mark.asyncio
async def test_a_clip_already_done_when_played_ends_the_wait_at_once() -> None:
    """A status that arrived before the clip id was known is not missed."""
    player, client = _player()
    _speaker_with_clip_status(client)
    client.player.audio_clip_status.side_effect = lambda _clip_id: "DONE"

    await asyncio.wait_for(player.play_announcement(_announcement()), 1)
