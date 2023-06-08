"""Models used for the JSON-RPC API."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from music_assistant.common.models.enums import MediaType, PlayerState, RepeatMode
from music_assistant.common.models.media_items import MediaItemType

if TYPE_CHECKING:
    from music_assistant.common.models.player import Player
    from music_assistant.common.models.player_queue import PlayerQueue
    from music_assistant.common.models.queue_item import QueueItem
    from music_assistant.server import MusicAssistant

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
    params: tuple[str, list[str | int]]


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


class CometDResponse(TypedDict):
    """CometD Response Message."""

    channel: str
    id: str
    data: dict[str, Any]


class SlimSubscribeData(CometDResponse):
    """CometD SlimSubscribe Data."""

    response: str  # e.g. '/slim/serverstatus',  the channel all messages should be sent back on
    request: tuple[str, list[str | int]]  # [ '', [ 'serverstatus', 0, 50, 'subscribe:60' ]
    priority: int  # # optional priority value, is passed-through with the response


class SlimSubscribeMessage(CometDResponse):
    """CometD SlimSubscribe Message."""

    channel: str
    id: str
    data: SlimSubscribeData


PlayerItem = TypedDict(
    "PlayerItem",
    {
        "playerindex": str,
        "playerid": str,
        "name": str,
        "modelname": str,
        "connected": int,
        "isplaying": int,
        "power": int,
        "model": str,
        "canpoweroff": int,
        "firmware": str,
        "isplayer": int,
        "displaytype": str,
        "uuid": str | None,
        "seq_no": str,
        "ip": str,
    },
)


def player_item_from_mass(playerindex: int, player: Player) -> PlayerItem:
    """Parse PlayerItem for the Json RPC interface from MA QueueItem."""
    return {
        "playerindex": str(playerindex),
        "playerid": player.player_id,
        "name": player.display_name,
        "modelname": player.device_info.model,
        "connected": int(player.available),
        "isplaying": 1 if player.state == PlayerState.PLAYING else 0,
        "power": int(player.powered),
        "model": player.provider,
        "canpoweroff": 1,
        "firmware": "unknown",
        "isplayer": 1,
        "displaytype": "none",
        "uuid": player.extra_data.get("uuid"),
        "seq_no": str(player.extra_data.get("seq_no", 0)),
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
        "params": dict,
    },
)


def playlist_item_from_mass(
    mass: MusicAssistant, queue_item: QueueItem, index: int = 0, is_cur_index: bool = False
) -> PlaylistItem:
    """Parse PlaylistItem for the Json RPC interface from MA QueueItem."""
    if queue_item.media_item:
        # media item
        media_details = get_media_details_from_mass(mass, queue_item.media_item)
    else:
        # fallback/generic queue item
        media_details = {
            "text": queue_item.name,
            "style": "itemplay",
            "trackType": "radio",
            "icon": mass.metadata.get_image_url(queue_item.image, 512) if queue_item.image else "",
            "params": {
                "playlist_index": index,
                "item_id": queue_item.queue_item_id,
                "uri": queue_item.uri,
            },
        }
    if (
        is_cur_index
        and queue_item.streamdetails
        and queue_item.streamdetails.stream_title
        and " - " in queue_item.streamdetails.stream_title
    ):
        # radio with remote stream title present
        # artist and title parsed from stream title
        artist, track = queue_item.streamdetails.stream_title.split(" - ")
        media_details["artist"] = artist
        media_details["track"] = track
        media_details["album"] = queue_item.name
        media_details["text"] = f"{track}\n{artist} - {queue_item.name}"
    # remove default item actions
    media_details.pop("actions")
    media_details["params"]["playlist_index"] = index
    return media_details


class SlimMediaItem(TypedDict):
    """Representation of MediaItem details."""

    style: str
    track: str
    album: str
    trackType: str  # noqa: N815
    icon: str
    artist: str
    text: str
    params: dict
    type: str
    actions: dict


def get_media_details_from_mass(mass: MusicAssistant, media_item: MediaItemType) -> SlimMediaItem:
    """Get media item details formatted to display on Squeezebox hardware."""
    if media_item.media_type == MediaType.TRACK:
        # track with all metadata
        artist = media_item.artists[0].name if media_item.artists else ""
        album = media_item.album.name if media_item.album else ""
        title = media_item.name
        text = f"{title}\n{artist} - {album}" if album else f"{title}\n{artist}"
    elif media_item.media_type == MediaType.ALBUM:
        # album with all metadata
        artist = media_item.artists[0].name if media_item.artists else ""
        title = media_item.name
        text = f"{title}\n{artist}" if artist else f"{title}\nalbum"
    elif media_item and media_item.metadata.description:
        # (radio) item with description field
        album = media_item.metadata.description
        artist = ""
        title = media_item.name
        text = f"{media_item.metadata.description}\n{media_item.name}"
    else:
        title = media_item.name
        artist = ""
        album = media_item.media_type.value
        text = f"{title}\n{album}"
    image_url = mass.metadata.get_image_url(media_item.image, 512) if media_item.image else ""
    if media_item.media_type in (MediaType.TRACK, MediaType.RADIO):
        go_action = {
            "cmd": ["playlistcontrol"],
            "itemsParams": "commonParams",
            "params": {"uri": media_item.uri, "cmd": "play"},
            "player": 0,
            "nextWindow": "nowPlaying",
        }
    else:
        go_action = {
            "params": {
                "uri": media_item.uri,
                "mode": media_item.media_type.value,
            },
            "itemsParams": "commonParams",
            "player": 0,
            "cmd": ["browselibrary", "items"],
        }
    details = SlimMediaItem(
        track=title,
        album=album,
        trackType="radio",
        icon=image_url,
        artist=artist,
        text=text,
        params={"item_id": media_item.item_id, "uri": media_item.uri},
        type=media_item.media_type.value,
        actions={
            "go": go_action,
            "add": {
                "player": 0,
                "itemsParams": "commonParams",
                "params": {"uri": media_item.uri, "cmd": "add"},
                "cmd": ["playlistcontrol"],
                "nextWindow": "refresh",
            },
            "more": {
                "player": 0,
                "itemsParams": "commonParams",
                "params": {"uri": media_item.uri, "cmd": "add"},
                "cmd": ["playlistcontrol"],
                "nextWindow": "refresh",
            },
            "play": {
                "cmd": ["playlistcontrol"],
                "itemsParams": "commonParams",
                "params": {
                    "uri": media_item.uri,
                    "cmd": "load" if media_item.media_type == MediaType.PLAYLIST else "play",
                },
                "player": 0,
                "nextWindow": "nowPlaying",
            },
            "play-hold": {
                "cmd": ["playlistcontrol"],
                "itemsParams": "commonParams",
                "params": {"uri": media_item.uri, "cmd": "load"},
                "player": 0,
                "nextWindow": "nowPlaying",
            },
            "add-hold": {
                "itemsParams": "commonParams",
                "params": {"uri": media_item.uri, "cmd": "insert"},
                "player": 0,
                "cmd": ["playlistcontrol"],
                "nextWindow": "refresh",
            },
        },
    )
    if media_item.media_type in (MediaType.TRACK, MediaType.RADIO):
        details["style"] = "itemplay"
        details["nextWindow"] = "nowPlaying"
    return details


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
        "item_loop": list[PlaylistItem],
        "uuid": str,
    },
)


def player_status_from_mass(
    mass: MusicAssistant,
    player: Player,
    queue: PlayerQueue,
    queue_items: list[QueueItem],
    offset: int | str,
    presets: list[tuple[int, SlimMediaItem]],
) -> PlayerStatusResponse:
    """Parse PlayerStatusResponse for the Json RPC interface from MA info."""
    if queue.current_item:
        cur_item = playlist_item_from_mass(mass, queue.current_item, queue.current_index, True)
        remote_meta = {
            **cur_item,
            "id": cur_item["params"]["item_id"],
            "title": cur_item["text"],
            "artwork_url": cur_item["icon"],
            "coverid": cur_item["params"]["item_id"],
            "remote": 1,
        }
    else:
        remote_meta = None
    # handle preset data
    preset_data: list[dict] = []
    preset_loop: list[int] = []
    for _, media_item in presets:
        preset_data.append(
            {
                "URL": media_item["params"]["uri"],
                "text": media_item["track"],
                "type": "audio",
            }
        )
        preset_loop.append(1)
    while len(preset_loop) < 10:
        preset_data.append({})
        preset_loop.append(0)
    return {
        "alarm_next": 0,
        "playlist repeat": REPEATMODE_MAP[queue.repeat_mode],
        "signalstrength": 0,
        "remoteMeta": remote_meta,
        "rate": 1,
        "player_name": player.display_name,
        "preset_loop": preset_loop,
        "mode": PLAYMODE_MAP[queue.state],
        "playlist_cur_index": queue.current_index,
        "playlist shuffle": int(queue.shuffle_enabled),
        "time": queue.elapsed_time,
        "alarm_version": 2,
        "mixer volume": player.volume_level,
        "player_connected": int(player.available),
        "sync_slaves": ",".join(player.group_childs),
        "playlist_tracks": queue.items,
        # "count": queue.items,
        # some players have trouble grabbing a very large list so limit it for now
        "count": len(queue_items),
        "base": {"actions": {}},
        "seq_no": player.extra_data.get("seq_no", 0),
        "player_ip": player.device_info.address,
        "alarm_state": "none",
        "duration": queue.current_item.duration if queue.current_item else 0,
        "alarm_snooze_seconds": 540,
        "digital_volume_control": 1,
        "power": int(player.powered),
        "playlist_timestamp": queue.elapsed_time_last_updated,
        "offset": offset,
        "can_seek": 1,
        "alarm_timeout_seconds": 3600,
        "current_title": None,
        "remote": 1,
        "preset_data": preset_data,
        "playlist mode": "off",
        "item_loop": [
            playlist_item_from_mass(
                mass,
                item,
                queue.current_index + index,
                queue.current_index == (queue.current_index + index),
            )
            for index, item in enumerate(queue_items)
        ],
    }


ServerStatusResponse = TypedDict(
    "ServerStatusMessage",
    {
        "ip": str,
        "httpport": str,
        "version": str,
        "uuid": str,
        "info total genres": int,
        "sn player count": int,
        "lastscan": str,
        "info total duration": int,
        "info total albums": int,
        "info total songs": int,
        "info total artists": int,
        "players_loop": list[PlayerItem],
        "player count": int,
        "other player count": int,
        "other_players_loop": list[PlayerItem],
    },
)
