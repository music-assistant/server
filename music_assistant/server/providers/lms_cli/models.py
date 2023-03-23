"""Models used for the JSON-RPC API."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from music_assistant.common.models.enums import MediaType, PlayerState, RepeatMode

if TYPE_CHECKING:
    from music_assistant.common.models.player import Player
    from music_assistant.common.models.player_queue import PlayerQueue
    from music_assistant.common.models.queue_item import QueueItem

# ruff: noqa: UP013

PLAYMODE_MAP = {
    PlayerState.IDLE: "stop",
    PlayerState.PLAYING: "play",
    PlayerState.OFF: "stop",
    PlayerState.PAUSED: "pause",
}

REPEATMODE_MAP = {RepeatMode.OFF: 0, RepeatMode.ONE: 1, RepeatMode.ALL: 2}


class CommandMessage(TypedDict):
    """Representation of Base JSON RPC Command Message."""

    # https://www.jsonrpc.org/specification

    id: int | str
    method: str
    params: list[str | int | list[str | int]]


class CommandResultMessage(CommandMessage):
    """Representation of JSON RPC Result Message."""

    result: Any


class ErrorDetails(TypedDict):
    """Representation of JSON RPC ErrorDetails."""

    code: int
    message: str


class CommandErrorMessage(CommandMessage, TypedDict):
    """Base Representation of JSON RPC Command Message."""

    id: int | str | None
    error: ErrorDetails


PlayerItem = TypedDict(
    "PlayerItem",
    {
        "playerindex": int,
        "playerid": str,
        "name": str,
        "modelname": str,
        "connected": int,
        "isplaying": int,
        "power": int,
        "model": str,
        "canpoweroff": int,
        "firmware": int,
        "isplayer": int,
        "displaytype": str,
        "uuid": str | None,
        "seq_no": int,
        "ip": str,
    },
)


def player_item_from_mass(playerindex: int, player: Player) -> PlayerItem:
    """Parse PlayerItem for the Json RPC interface from MA QueueItem."""
    return {
        "playerindex": playerindex,
        "playerid": player.player_id,
        "name": player.display_name,
        "modelname": player.device_info.model,
        "connected": int(player.available),
        "isplaying": 1 if player.state == PlayerState.PLAYING else 0,
        "power": int(player.powered),
        "model": "squeezelite",
        "canpoweroff": 1,
        "firmware": 0,
        "isplayer": 1,
        "displaytype": None,
        "uuid": None,
        "seq_no": 0,
        "ip": player.device_info.address,
    }


PlayersResponse = TypedDict(
    "PlayersResponse",
    {
        "count": int,
        "players_loop": list[PlayerItem],
    },
)


PlaylistItem = TypedDict(
    "PlaylistItem",
    {
        "playlist index": int,
        "id": str,
        "title": str,
        "artist": str,
        "remote": int,
        "remote_title": str,
        "artwork_url": str,
        "bitrate": str,
        "duration": str | int | None,
        "coverid": str,
    },
)


def playlist_item_from_mass(queue_item: QueueItem, index: int = 0) -> PlaylistItem:
    """Parse PlaylistItem for the Json RPC interface from MA QueueItem."""
    if queue_item.media_item and queue_item.media_type == MediaType.TRACK:
        artist = queue_item.media_item.artist.name if queue_item.media_item.artist else ""
        album = queue_item.media_item.album.name if queue_item.media_item.album else ""
        title = queue_item.media_item.name
    elif queue_item.streamdetails and queue_item.streamdetails.stream_title:
        if " - " in queue_item.streamdetails.stream_title:
            artist, title = queue_item.streamdetails.stream_title.split(" - ")
        else:
            artist = ""
            title = queue_item.streamdetails.stream_title
        album = queue_item.name
    else:
        artist = ""
        album = ""
        title = queue_item.name
    return {
        "playlist index": index,
        "id": queue_item.queue_item_id,
        "title": title,
        "artist": artist,
        "album": album,
        "genre": "",
        "remote": 0,
        "remote_title": queue_item.streamdetails.stream_title if queue_item.streamdetails else "",
        "artwork_url": queue_item.image_url or "",
        "bitrate": "",
        "duration": queue_item.duration or 0,
        "coverid": "-94099753136392",
    }


PlayerStatusResponse = TypedDict(
    "PlayerStatusResponse",
    {
        "time": int,
        "mode": str,
        "sync_slaves": str,
        "playlist_cur_index": int | None,
        "player_name": str,
        "sync_master": str,
        "player_connected": int,
        "power": int,
        "mixer volume": int,
        "playlist repeat": int,
        "playlist shuffle": int,
        "playlist mode": str,
        "player_ip": str,
        "remoteMeta": dict | None,
        "digital_volume_control": int,
        "playlist_timestamp": float,
        "current_title": str,
        "duration": int,
        "seq_no": int,
        "remote": int,
        "can_seek": int,
        "signalstrength": int,
        "rate": int,
        "playlist_tracks": int,
        "playlist_loop": list[PlaylistItem],
    },
)


def player_status_from_mass(
    player: Player, queue: PlayerQueue, queue_items: list[QueueItem]
) -> PlayerStatusResponse:
    """Parse PlayerStatusResponse for the Json RPC interface from MA info."""
    return {
        "time": queue.corrected_elapsed_time,
        "mode": PLAYMODE_MAP[queue.state],
        "sync_slaves": ",".join(player.group_childs),
        "playlist_cur_index": queue.current_index,
        "player_name": player.display_name,
        "sync_master": player.synced_to or "",
        "player_connected": int(player.available),
        "mixer volume": player.volume_level,
        "power": int(player.powered),
        "digital_volume_control": 1,
        "playlist_timestamp": 0,  # TODO !
        "current_title": queue.current_item.queue_item_id
        if queue.current_item
        else "Music Assistant",
        "duration": queue.current_item.duration if queue.current_item else 0,
        "playlist repeat": REPEATMODE_MAP[queue.repeat_mode],
        "playlist shuffle": int(queue.shuffle_enabled),
        "playlist mode": "off",
        "player_ip": player.device_info.address,
        "seq_no": 0,
        "remote": 0,
        "can_seek": 1,
        "signalstrength": 0,
        "rate": 1,
        "playlist_tracks": queue.items,
        "playlist_loop": [
            playlist_item_from_mass(item, queue.current_index + index)
            for index, item in enumerate(queue_items)
        ],
    }
