"""Tests for the cloud queue and stream urls of a speaker that leads a sync group."""

import json
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.models.player import Player
from music_assistant.models.player_protocol import PlayerProtocol
from music_assistant.providers.sonos.cloud_queue import SonosCloudQueue
from music_assistant.providers.sonos.player import SonosPlayer
from music_assistant.providers.sonos.provider import SonosPlayerProvider
from tests.providers.sonos.test_cloud_queue import (
    QUEUE_ID,
    _loads_into_a_session,
    _make_cloud_queue,
    _make_player,
    _make_queue_item,
)

BASE_URL = "http://ma:8097"
OWN_PLAYER_ID = "own_player"
GROUP_QUEUE_URL = f"{BASE_URL}/sonos_queue/{QUEUE_ID}/v2.3/"
OWN_QUEUE_URL = f"{BASE_URL}/sonos_queue/sonos_player/v2.3/"


def _player_of(protocol: PlayerProtocol) -> MagicMock:
    """Return a player whose audio the given protocol renders."""
    player = MagicMock(spec=Player)
    player.resolve_output_protocol.return_value = protocol
    return player


def _protocol_rendered_by(renderer: PlayerProtocol | None) -> MagicMock:
    """Return a protocol whose streams the given protocol renders, itself when None."""
    protocol = MagicMock(spec=PlayerProtocol)
    protocol.resolve_output_protocol.return_value = renderer or protocol
    return protocol


def _make_speaker() -> tuple[SonosPlayer, dict[str, MagicMock]]:
    """
    Create a speaker whose loads of the queue are read back as the speaker reads them.

    :return: The speaker, and the players by id the queues belong to.
    """
    speaker, _ = _make_player([_make_queue_item(f"track{i}") for i in range(3)])
    speaker._provider = MagicMock(protocols=[speaker])
    players: dict[str, MagicMock] = {OWN_PLAYER_ID: _player_of(speaker)}
    mass = cast("MagicMock", speaker.mass)
    mass.players.get_player = MagicMock(side_effect=players.get)
    mass.streams.base_url = BASE_URL
    mass.streams.resolve_stream_url.side_effect = lambda player_id, _media, queue_index=None: (
        f"{player_id}/{queue_index}"
    )
    client = MagicMock()
    client.player.is_passive = False

    async def _read_window(*_args: object, item_id: str, **_kwargs: object) -> None:
        await speaker.build_cloud_queue_window(item_id)

    _loads_into_a_session(client.player.group, _read_window)
    speaker.client = client
    return speaker, players


def _group(speaker: SonosPlayer) -> MagicMock:
    """Return the mocked group the speaker coordinates."""
    return cast("MagicMock", cast("MagicMock", speaker.client).player.group)


async def _load(speaker: SonosPlayer, media: PlayerMedia) -> tuple[str, list[str]]:
    """
    Play media on a speaker.

    :return: The url of the cloud queue it loaded, and the stream urls of the window it read.
    """
    group = _group(speaker)
    group.play_cloud_queue.reset_mock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SonosPlayer, "flow_mode", property(lambda _self: False))
        await speaker.play_media(media)
    cloud_queue_url = group.play_cloud_queue.await_args.args[0]
    window = await speaker.build_cloud_queue_window(None)
    return cloud_queue_url, [media.uri for media in window.items]


def _queue_media(queue_id: str = QUEUE_ID) -> PlayerMedia:
    """Return the media of the first item of a queue."""
    return PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id=queue_id,
        queue_item_id="track0",
        queue_session_id="session",
    )


async def _lead_the_group(speaker: SonosPlayer, players: dict[str, MagicMock]) -> None:
    """Let a speaker load the queue of a sync group it leads, which names the group."""
    players[QUEUE_ID] = _player_of(_protocol_rendered_by(speaker))

    cloud_queue_url, stream_urls = await _load(speaker, _queue_media())

    assert cloud_queue_url == GROUP_QUEUE_URL
    assert stream_urls == [f"{QUEUE_ID}/{i}" for i in range(3)]


async def test_the_leader_of_a_sync_group_loads_its_queue_under_the_group() -> None:
    """Test the cloud queue path and the stream urls of a sync group's queue name the group."""
    speaker, players = _make_speaker()

    await _lead_the_group(speaker, players)

    assert speaker.group_queue_id == QUEUE_ID


@pytest.mark.parametrize(
    "owner",
    ["own queue", "universal group", "sync group led over airplay", "unknown player"],
)
async def test_any_other_queue_is_loaded_under_the_speaker(owner: str) -> None:
    """Test a queue that is no sync group's this speaker renders names the speaker."""
    speaker, players = _make_speaker()
    await _lead_the_group(speaker, players)
    if owner == "own queue":
        # also what a group made by hand plays: the queue of the speaker that leads it
        players[QUEUE_ID] = _player_of(speaker)
    elif owner == "universal group":
        players[QUEUE_ID] = _player_of(_protocol_rendered_by(None))
    elif owner == "sync group led over airplay":
        players[QUEUE_ID] = _player_of(_protocol_rendered_by(MagicMock(spec=PlayerProtocol)))
    else:
        del players[QUEUE_ID]

    cloud_queue_url, stream_urls = await _load(speaker, _queue_media())

    assert cloud_queue_url == OWN_QUEUE_URL
    assert stream_urls == [f"sonos_player/{i}" for i in range(3)]
    assert speaker.group_queue_id is None


async def test_an_announcement_is_loaded_under_the_speaker() -> None:
    """Test an announcement names the speaker, while the group's queue stays served."""
    speaker, players = _make_speaker()
    await _lead_the_group(speaker, players)
    cast("MagicMock", speaker.mass).streams.get_announcement_duration = AsyncMock(return_value=3)
    group = _group(speaker)
    group.play_cloud_queue = AsyncMock()

    await speaker.play_media(
        PlayerMedia(uri="http://announcement", media_type=MediaType.ANNOUNCEMENT)
    )

    assert group.play_cloud_queue.await_args.args[0] == OWN_QUEUE_URL
    assert speaker.group_queue_id == QUEUE_ID


async def test_a_stop_forgets_the_group() -> None:
    """Test a stopped speaker no longer serves the group's queue."""
    speaker, players = _make_speaker()
    await _lead_the_group(speaker, players)
    _group(speaker).stop = AsyncMock()
    speaker._player = MagicMock(spec=Player)
    speaker.update_state = MagicMock()  # type: ignore[misc, method-assign]

    await speaker.stop()

    assert speaker.group_queue_id is None


def _make_household(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SonosCloudQueue, SonosPlayer, SonosPlayer]:
    """
    Create the cloud queue of two speakers, the second of which leads the group's queue.

    :return: The cloud queue, the other speaker and the leader.
    """
    other, _ = _make_player([_make_queue_item("other0")])
    other._protocol_id = "sonos_other"
    other.cloud_queue_id = "other_queue"
    other.cloud_queue_version = 2.0
    leader, _ = _make_player([_make_queue_item(f"track{i}") for i in range(3)])
    leader.group_queue_id = QUEUE_ID
    leader.cloud_queue_version = 7.0
    speakers = [other, leader]
    monkeypatch.setattr(SonosPlayerProvider, "protocols", property(lambda _self: speakers))
    return _make_cloud_queue(), other, leader


def _request(path_id: str, endpoint: str, body: object = None) -> MagicMock:
    """Return a request of a speaker to an endpoint of the cloud queue under the given id."""
    request = MagicMock()
    request.path = f"/sonos_queue/{path_id}/v2.3/{endpoint}"
    request.query = {}
    request.json = AsyncMock(return_value=body)
    return request


@pytest.mark.parametrize("endpoint", ["itemWindow", "version", "context"])
async def test_the_group_path_is_answered_by_the_speaker_with_its_queue(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """Test each endpoint under the group's path answers from the speaker that loaded it."""
    cloud_queue, _, _ = _make_household(monkeypatch)

    response = await cloud_queue.handle_request(_request(QUEUE_ID, endpoint))

    assert response.status == 200
    body = json.loads(response.text or "{}")
    assert body["queueVersion"] == "7.0"
    if endpoint == "itemWindow":
        assert [item["id"] for item in body["items"]] == [f"track{i}@0" for i in range(3)]
    if endpoint == "context":
        assert body["container"]["id"]["objectId"] == f"mass:{QUEUE_ID}"


async def test_a_position_report_under_the_group_path_reaches_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test the speaker that loaded the group's queue takes the position reported under it."""
    cloud_queue, other, leader = _make_household(monkeypatch)
    for speaker in (other, leader):
        speaker._attr_current_media = PlayerMedia(uri="track0", queue_item_id="track0")
        speaker.update_elapsed_time = MagicMock()  # type: ignore[method-assign]
    report = {"items": [{"type": "update", "id": "track0@0", "positionMillis": 5000}]}

    response = await cloud_queue.handle_request(_request(QUEUE_ID, "timePlayed", report))

    assert response.status == 204
    cast("MagicMock", leader.update_elapsed_time).assert_called_once_with(5.0)
    cast("MagicMock", other.update_elapsed_time).assert_not_called()


async def test_a_group_path_is_answered_by_the_speaker_that_loaded_its_queue_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test a new leader takes the group's queue over from one that could not be stopped."""
    lost, _ = _make_speaker()
    lost.group_queue_id = QUEUE_ID
    lost.cloud_queue_version = 2.0
    leader, players = _make_speaker()
    leader._protocol_id = "sonos_leader"
    speakers = [lost, leader]
    leader._provider = MagicMock(protocols=speakers)
    monkeypatch.setattr(SonosPlayerProvider, "protocols", property(lambda _self: speakers))

    await _lead_the_group(leader, players)

    assert [speaker.group_queue_id for speaker in speakers] == [None, QUEUE_ID]
    response = await _make_cloud_queue().handle_request(_request(QUEUE_ID, "version"))
    assert json.loads(response.text or "{}")["queueVersion"] == str(leader.cloud_queue_version)
    assert leader.cloud_queue_version != lost.cloud_queue_version


async def test_a_group_path_no_speaker_serves_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test the group's path is refused once no speaker has its queue, the speakers' are not."""
    cloud_queue, other, leader = _make_household(monkeypatch)
    assert (await cloud_queue.handle_request(_request(QUEUE_ID, "version"))).status == 200

    leader.group_queue_id = None

    assert (await cloud_queue.handle_request(_request(QUEUE_ID, "version"))).status == 501
    for speaker in (other, leader):
        response = await cloud_queue.handle_request(_request(speaker.protocol_id, "version"))
        assert response.status == 200
