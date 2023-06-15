"""Models used for the JSON-RPC API."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

from music_assistant.common.models.enums import MediaType, PlayerState, RepeatMode
from music_assistant.common.models.media_items import MediaItemType

if TYPE_CHECKING:
    from music_assistant.common.models.player import Player
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
    if (
        is_cur_index
        and queue_item.streamdetails
        and queue_item.streamdetails.stream_title
        and " - " in queue_item.streamdetails.stream_title
    ):
        # radio with remote stream title present
        # artist and title parsed from stream title
        artist, title = queue_item.streamdetails.stream_title.split(" - ")
        album = queue_item.name
    elif queue_item.media_item and queue_item.media_item.media_type == MediaType.TRACK:
        # track with all metadata
        artist = queue_item.media_item.artists[0].name if queue_item.media_item.artists else ""
        album = queue_item.media_item.album.name if queue_item.media_item.album else ""
        title = queue_item.media_item.name
    elif queue_item.media_item and queue_item.media_item.metadata.description:
        # (radio) item with description field
        album = queue_item.media_item.metadata.description
        artist = ""
        title = queue_item.media_item.name
    else:
        title = queue_item.name
        artist = ""
        album = queue_item.media_type.value
    return {
        "playlist index": index,
        "id": "-187651250107376",
        "title": title,
        "artist": artist,
        "album": album,
        "remote": 1,
        "artwork_url": mass.metadata.get_image_url(queue_item.image, 512)
        if queue_item.image
        else "",
        "coverid": "-187651250107376",
        "duration": queue_item.duration,
        "bitrate": "",
    }


MenuItemParams = TypedDict(
    "MediaItemParams",
    {
        "track_id": str | int,
        "playlist_index": int,
    },
)


class SlimMenuItem(TypedDict):
    """Representation of MediaItem details."""

    style: str
    track: str
    album: str
    trackType: str  # noqa: N815
    icon: str
    artist: str
    text: str
    params: MenuItemParams
    type: str
    actions: dict  # optional


def menu_item_from_queue_item(
    mass: MusicAssistant, queue_item: QueueItem, index: int = 0, is_cur_index: bool = False
) -> SlimMenuItem:
    """Parse SlimMenuItem from MA QueueItem."""
    if queue_item.media_item:
        # media item
        media_details = menu_item_from_media_item(mass, queue_item.media_item)
        media_details["params"]["playlist_index"] = index
    else:
        # fallback/generic queue item
        media_details = SlimMenuItem(
            style="itemplay",
            track=queue_item.name,
            album="",
            trackType="radio",
            icon=mass.metadata.get_image_url(queue_item.image, 512) if queue_item.image else "",
            artist="",
            text=queue_item.name,
            params={
                "playlist_index": index,
                "item_id": queue_item.queue_item_id,
                "uri": queue_item.uri,
            },
            type=queue_item.media_type,
        )
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
    return media_details


def menu_item_from_media_item(
    mass: MusicAssistant, media_item: MediaItemType, include_actions: bool = False
) -> PlaylistItem:
    """Parse (menu) MediaItem from MA MediaItem."""
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
    details = SlimMenuItem(
        track=title,
        album=album,
        trackType="radio",
        icon=image_url,
        artist=artist,
        text=text,
        params={
            "track_id": media_item.item_id,
            "item_id": media_item.item_id,
            "uri": media_item.uri,
        },
        type=media_item.media_type.value,
    )
    # optionally include actions
    if include_actions:
        details["actions"] = {
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
        }
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
        "uuid": str,
        "playlist_tracks": int,
        "item_loop": list[PlaylistItem],
    },
)


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
