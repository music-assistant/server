"""Tests for a Sonos session taken over by another app, and announcements played as clips."""

import asyncio
import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiosonos.const import AudioClipStatusChangedEvent, SessionErrorEvent
from aiosonos.const import EventType as SonosEventType
from aiosonos.exceptions import CommandTimeout, FailedCommand
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.sonos import player as player_module
from music_assistant.providers.sonos.player import SonosPlayer

if TYPE_CHECKING:
    from aiosonos.api.models import AudioClipStatusEvent, SessionError

PLAYER = "RINCON_F0F6C1CC8D0401400"
CLIP_ID = "05edf9d0-591a-494a-8045-f5f01295a3ce"


def _player(
    cloud_queue_id: str | None = None, session_id: str | None = "session1"
) -> tuple[SonosPlayer, MagicMock]:
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
    player._flow_queue_id = None
    player._evicted_cloud_queue = None
    player._queue_session_id = session_id if cloud_queue_id else None
    client = MagicMock()
    player.client = client
    player.update_state = MagicMock()  # type: ignore[misc, method-assign]
    return player, client


def _session_error(error_code: str, session_id: str = "session1") -> SessionErrorEvent:
    body = cast(
        "SessionError",
        {"_objectType": "sessionError", "errorCode": error_code, "sessionId": session_id},
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


def test_an_eviction_of_a_replaced_session_keeps_our_queue() -> None:
    """The session our queue replaced being evicted does not end the queue now playing."""
    player, _ = _player(cloud_queue_id=PLAYER, session_id="session2")

    player._on_session_error(_session_error("ERROR_SESSION_EVICTED", "session1"))

    assert player.cloud_queue_id == PLAYER
    assert player._evicted_cloud_queue is None


def _player_loading_our_queue(
    on_load: object,
) -> tuple[SonosPlayer, MagicMock, PlayerMedia]:
    """Create a player whose speaker runs `on_load` while it loads our queue."""
    player, client = _player(cloud_queue_id="old_queue")
    player._woken_from_sleep = False
    player._announcement_media = None
    player.cloud_queue_item_generation = 0
    player.cloud_queue_version = 1.0
    cast("MagicMock", player.mass).streams.base_url = "http://192.168.1.10:8097"
    client.player.is_passive = False
    # the speaker plays the old queue in the session it holds
    client.player.group.active_session_id = "session1"
    client.player.group.play_cloud_queue = AsyncMock(side_effect=on_load)
    media = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id="new_queue",
        queue_item_id="track0",
        queue_session_id="queue-session-2",
    )
    return player, client, media


@pytest.mark.asyncio
async def test_an_eviction_of_the_old_session_during_a_new_load_keeps_the_new_queue() -> None:
    """An eviction the listener gets only once the next play began is not the new queue's."""
    player: SonosPlayer

    async def _load(*_args: object, **_kwargs: object) -> None:
        # the speaker evicted the old session; its listener runs while this load is out
        player._on_session_error(_session_error("ERROR_SESSION_EVICTED", "session1"))
        client.player.group.active_session_id = "session2"

    player, client, media = _player_loading_our_queue(_load)
    with patch.object(SonosPlayer, "flow_mode", property(lambda _self: False)):
        await player.play_media(media)

    assert player.cloud_queue_id == "new_queue"
    assert player._evicted_cloud_queue is None

    # an eviction of the session the new queue went into still releases it
    player._on_session_error(_session_error("ERROR_SESSION_EVICTED", "session2"))
    assert player.cloud_queue_id is None


@pytest.mark.asyncio
async def test_a_session_evicted_while_our_queue_loads_releases_it() -> None:
    """The speaker that lost the new session during the load does not play our queue."""
    player: SonosPlayer

    async def _load(*_args: object, **_kwargs: object) -> None:
        client.player.group.active_session_id = None

    player, client, media = _player_loading_our_queue(_load)
    with patch.object(SonosPlayer, "flow_mode", property(lambda _self: False)):
        await player.play_media(media)

    assert player.cloud_queue_id is None
    assert player._evicted_cloud_queue is not None
    assert player._evicted_cloud_queue[0] == "new_queue"
    close_stale = cast("MagicMock", player.mass).streams.close_superseded_item_streams
    close_stale.assert_not_called()


@pytest.mark.asyncio
async def test_a_new_load_goes_into_the_session_already_playing() -> None:
    """A skip or seek reloads into the held session: a new one would stop the speaker first."""
    player: SonosPlayer
    session_during_load: list[object] = []

    async def _load(*_args: object, **_kwargs: object) -> None:
        session_during_load.append(client.player.group.active_session_id)

    player, client, media = _player_loading_our_queue(_load)
    with patch.object(SonosPlayer, "flow_mode", property(lambda _self: False)):
        await player.play_media(media)

    assert session_during_load == ["session1"]
    assert player._queue_session_id == "session1"
    assert player.cloud_queue_id == "new_queue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("playing_before", "recorded"),
    [("old_queue", "old_queue"), (None, "new_queue")],
    ids=["previous_queue", "nothing_before"],
)
async def test_an_eviction_in_the_load_reply_keeps_the_queue_a_move_will_carry(
    playing_before: str | None, recorded: str
) -> None:
    """A load refused as evicted counts like the event; the speakers carry what played before."""
    player, client, media = _player_loading_our_queue(None)
    player.cloud_queue_id = playing_before
    client.player.group.play_cloud_queue = AsyncMock(
        side_effect=FailedCommand("ERROR_SESSION_EVICTED")
    )

    with (
        patch.object(SonosPlayer, "flow_mode", property(lambda _self: False)),
        pytest.raises(FailedCommand),
    ):
        await player.play_media(media)

    assert player.cloud_queue_id is None
    assert player._evicted_cloud_queue is not None
    assert player._evicted_cloud_queue[0] == recorded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_after", "queue_after"),
    [("session1", "new_queue"), (None, None)],
    ids=["load_unanswered", "create_unanswered"],
)
async def test_an_unanswered_load_keeps_the_queue_only_with_a_session(
    session_after: str | None, queue_after: str | None
) -> None:
    """An unanswered load may have reached the speaker; an unanswered createSession did not."""
    player: SonosPlayer

    async def _load(*_args: object, **_kwargs: object) -> None:
        client.player.group.active_session_id = session_after
        raise CommandTimeout("playbackSession:loadCloudQueue", 10)

    player, client, media = _player_loading_our_queue(_load)
    with (
        patch.object(SonosPlayer, "flow_mode", property(lambda _self: False)),
        pytest.raises(CommandTimeout),
    ):
        await player.play_media(media)

    assert player.cloud_queue_id == queue_after
    assert player._queue_session_id == session_after
    assert player._evicted_cloud_queue is None


def _player_streaming_our_queue(
    on_load: object,
) -> tuple[SonosPlayer, MagicMock, PlayerMedia]:
    """Create a player in flow mode whose speaker runs `on_load` while it loads the stream."""
    player, client, media = _player_loading_our_queue(None)
    client.player.group.play_stream_url = AsyncMock(side_effect=on_load)
    player._provider = MagicMock()
    player._provider.mass = player.mass
    cast("MagicMock", player.mass).streams.resolve_stream_url = AsyncMock(
        return_value="http://192.168.1.10:8097/flow/abc.flac"
    )
    return player, client, media


@pytest.mark.asyncio
async def test_a_flow_mode_queue_is_ours_until_its_session_is_evicted() -> None:
    """A queue played as one stream is remembered and released like a cloud queue."""
    player: SonosPlayer

    async def _load(*_args: object, **_kwargs: object) -> None:
        client.player.group.active_session_id = "stream-session"

    player, client, media = _player_streaming_our_queue(_load)
    with patch.object(SonosPlayer, "flow_mode", property(lambda _self: True)):
        await player.play_media(media)

    assert player.cloud_queue_id is None
    assert player._flow_queue_id == "new_queue"
    assert player._queue_session_id == "stream-session"

    player._on_session_error(_session_error("ERROR_SESSION_EVICTED", "stream-session"))

    assert player._playing_queue_id() is None
    assert player._evicted_cloud_queue is not None
    assert player._evicted_cloud_queue[0] == "new_queue"


@pytest.mark.asyncio
async def test_a_flow_mode_load_evicted_in_its_reply_keeps_the_queue_a_move_will_carry() -> None:
    """The flow path settles a refused load the same way as the cloud-queue path."""
    player, _, media = _player_streaming_our_queue(FailedCommand("ERROR_SESSION_EVICTED"))

    with (
        patch.object(SonosPlayer, "flow_mode", property(lambda _self: True)),
        pytest.raises(FailedCommand),
    ):
        await player.play_media(media)

    assert player._flow_queue_id is None
    assert player._evicted_cloud_queue is not None
    assert player._evicted_cloud_queue[0] == "old_queue"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_after", "flow_after"),
    [("stream-session", "new_queue"), ("session1", None)],
    ids=["new_session_unanswered", "create_unanswered"],
)
async def test_an_unanswered_stream_keeps_the_queue_only_in_a_session_of_its_own(
    session_after: str, flow_after: str | None
) -> None:
    """A stream always gets a new session; the cloud queue's session left held is not its own."""
    player: SonosPlayer

    async def _load(*_args: object, **_kwargs: object) -> None:
        client.player.group.active_session_id = session_after
        raise CommandTimeout("playbackSession:createSession", 10)

    player, client, media = _player_streaming_our_queue(_load)
    with (
        patch.object(SonosPlayer, "flow_mode", property(lambda _self: True)),
        pytest.raises(CommandTimeout),
    ):
        await player.play_media(media)

    assert player._flow_queue_id == flow_after
    assert player._queue_session_id == (session_after if flow_after else None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_after", "queue_after"),
    [("session1", "new_queue"), (None, None)],
    ids=["session_held", "no_session"],
)
async def test_a_cancelled_load_is_settled_like_an_unanswered_one(
    session_after: str | None, queue_after: str | None
) -> None:
    """A load cut off by a cancel may have reached the speaker; it is kept only with a session."""
    player: SonosPlayer

    async def _load(*_args: object, **_kwargs: object) -> None:
        client.player.group.active_session_id = session_after
        raise asyncio.CancelledError

    player, client, media = _player_loading_our_queue(_load)
    with (
        patch.object(SonosPlayer, "flow_mode", property(lambda _self: False)),
        pytest.raises(asyncio.CancelledError),
    ):
        await player.play_media(media)

    assert player.cloud_queue_id == queue_after
    assert player._queue_session_id == session_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_after", "flow_after"),
    [("stream-session", "new_queue"), ("session1", None)],
    ids=["new_session_held", "replaced_session_held"],
)
async def test_a_cancelled_stream_load_keeps_the_queue_only_in_a_session_of_its_own(
    session_after: str, flow_after: str | None
) -> None:
    """A stream cut off by a cancel is kept only when the speaker made its session already."""
    player: SonosPlayer

    async def _load(*_args: object, **_kwargs: object) -> None:
        client.player.group.active_session_id = session_after
        raise asyncio.CancelledError

    player, client, media = _player_streaming_our_queue(_load)
    with (
        patch.object(SonosPlayer, "flow_mode", property(lambda _self: True)),
        pytest.raises(asyncio.CancelledError),
    ):
        await player.play_media(media)

    assert player._flow_queue_id == flow_after
    assert player._queue_session_id == (session_after if flow_after else None)


@pytest.mark.asyncio
async def test_a_new_play_ends_the_grace_of_an_evicted_queue() -> None:
    """After a new play, a move no longer hands over the queue released before it."""
    player, _, media = _player_streaming_our_queue(None)
    player._evicted_cloud_queue = cast("tuple[str, float] | None", ("old_queue", 0.0))
    media.source_id = None
    media.queue_item_id = None

    with patch.object(SonosPlayer, "flow_mode", property(lambda _self: False)):
        await player.play_media(media)

    assert player._evicted_cloud_queue is None
    assert player._moved_queue_id() is None


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
@pytest.mark.parametrize("status", ["DONE", "DISMISSED", "INTERRUPTED", "ERROR"])
async def test_a_clip_already_done_when_played_ends_the_wait_at_once(status: str) -> None:
    """A status that arrived before the clip id was known is not missed, for every end state."""
    player, client = _player()
    _speaker_with_clip_status(client)
    client.player.audio_clip_status.side_effect = lambda _clip_id: status

    await asyncio.wait_for(player.play_announcement(_announcement()), 1)


@pytest.mark.asyncio
async def test_a_clip_done_before_its_duration_is_known_ends_the_wait() -> None:
    """A clip reported done while its duration is still being looked up does not wait for that."""
    player, client = _player()
    deliver, _ = _speaker_with_clip_status(client)
    lookup_cancelled = asyncio.Event()

    async def _slow_lookup(_media: PlayerMedia) -> float | None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            lookup_cancelled.set()
            raise
        return None

    cast("MagicMock", player.mass).streams.get_announcement_duration = _slow_lookup

    announcement = asyncio.ensure_future(player.play_announcement(_announcement()))
    await asyncio.sleep(0.05)
    assert not announcement.done()
    deliver(_clip_status("DONE"))  # type: ignore[operator]
    await asyncio.wait_for(announcement, 1)

    assert lookup_cancelled.is_set()


@pytest.mark.asyncio
async def test_a_done_status_already_replaced_by_the_next_one_still_ends_the_wait() -> None:
    """The DONE the listener is sent ends the wait though the speaker has moved on since."""
    player, client = _player()
    deliver, _ = _speaker_with_clip_status(client)
    # the speaker no longer lists the clip by the time the listener runs
    client.player.audio_clip_status.side_effect = lambda _clip_id: None

    announcement = asyncio.ensure_future(player.play_announcement(_announcement()))
    await asyncio.sleep(0.05)
    deliver(_clip_status("DONE"))  # type: ignore[operator]
    await asyncio.wait_for(announcement, 1)


@pytest.mark.asyncio
async def test_a_done_status_sent_before_the_clip_was_accepted_ends_the_wait() -> None:
    """A DONE that reaches the listener before the clip's id is known is kept for it."""
    player, client = _player()
    deliver, _ = _speaker_with_clip_status(client)
    client.player.audio_clip_status.side_effect = lambda _clip_id: None

    async def _play_audio_clip(*_args: object, **_kwargs: object) -> dict[str, str]:
        deliver(_clip_status("DONE"))  # type: ignore[operator]
        return {"id": CLIP_ID, "status": "ACTIVE"}

    client.player.play_audio_clip = AsyncMock(side_effect=_play_audio_clip)

    await asyncio.wait_for(player.play_announcement(_announcement()), 1)


@pytest.mark.asyncio
async def test_a_lookup_failing_after_the_clip_ended_is_only_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The clip ended first, so a failed duration lookup holds nothing up and is not an error."""
    player, client = _player()
    deliver, _ = _speaker_with_clip_status(client)
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def _failing_lookup(_media: PlayerMedia) -> float | None:
        lookup_started.set()
        await release_lookup.wait()
        raise RuntimeError("no duration")

    cast("MagicMock", player.mass).streams.get_announcement_duration = _failing_lookup

    with caplog.at_level(logging.DEBUG):
        announcement = asyncio.ensure_future(player.play_announcement(_announcement()))
        await lookup_started.wait()
        # the lookup fails in the same turn the clip is reported done
        release_lookup.set()
        deliver(_clip_status("DONE"))  # type: ignore[operator]
        await asyncio.wait_for(announcement, 1)

    assert "Could not look up the announcement duration" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
