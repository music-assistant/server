"""The tools the builtin MCP server offers to a language model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, PlayerType, QueueOption, RepeatMode
from music_assistant_models.media_items import Playlist

from .registry import ToolError, mcp_tool

if TYPE_CHECKING:
    from music_assistant_models.media_items import ItemMapping, MediaItemType, SearchResults
    from music_assistant_models.player import PlayerMedia
    from music_assistant_models.player_queue import PlayerQueue
    from music_assistant_models.queue_item import QueueItem

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

INSTRUCTIONS = (
    "Music Assistant manages a music library and plays music, radio, podcasts and audiobooks "
    "on the speakers (players) in the home. Use these tools to search for media, start "
    "playback, control players (pause, skip, volume, grouping), inspect the play queue and "
    "manage favorites and playlists. Players and media can be referred to by name; a uri "
    "returned by an earlier tool call is the most precise way to refer to an item."
)

SearchMediaType = Literal[
    "track", "album", "artist", "playlist", "radio", "podcast", "audiobook", "any"
]

# the most rows a tool returns per list, whatever limit the model asks for
MAX_LIMIT = 50

# the order in which results of a search over all media types are considered
_ANY_MEDIA_TYPES = (
    MediaType.TRACK,
    MediaType.ARTIST,
    MediaType.ALBUM,
    MediaType.PLAYLIST,
    MediaType.RADIO,
    MediaType.PODCAST,
    MediaType.AUDIOBOOK,
)


@mcp_tool("search_music", Scope.LIBRARY_READ)
async def search_music(
    mass: MusicAssistant, query: str, media_type: SearchMediaType = "any", limit: int = 10
) -> dict[str, Any]:
    """
    Search the library and all music sources for music, radio, podcasts and audiobooks.

    :param query: What to search for, such as a title, an artist or both.
    :param media_type: Only return this type of media.
    :param limit: The maximum number of results per media type, at most 50.
    """
    results = await mass.music.search(query, _media_types(media_type), limit=_clamp_limit(limit))
    return {"items": [_compact_item(item) for item in _flatten_results(results)]}


@mcp_tool("get_players", Scope.PLAYERS_READ)
async def get_players(mass: MusicAssistant) -> dict[str, Any]:
    """List the players (speakers) and their current state."""
    return {"players": [_compact_player(mass, player) for player in _players(mass)]}


@mcp_tool("play", Scope.QUEUES_CONTROL)
async def play(
    mass: MusicAssistant,
    player: str,
    media: str,
    media_type: SearchMediaType = "any",
    mode: Literal["replace", "next", "add", "replace_next"] = "replace",
    radio_mode: bool = False,
) -> dict[str, Any]:
    """
    Play media on a player, or add it to the player's queue.

    :param player: The name or id of the player.
    :param media: The uri of the item, or its name to search for.
    :param media_type: The type of media to look for when media is a name.
    :param mode: Replace the queue and play now, play next, add to the end of the queue,
        or replace the upcoming items.
    :param radio_mode: Play an endless mix of similar music based on the item.
    """
    target = _resolve_player(mass, player)
    item = await _resolve_media(mass, media, media_type)
    to_play = f"radio_playlist://playlist/{item.uri}" if radio_mode else item
    await mass.player_queues.play_media(target.player_id, to_play, QueueOption(mode))
    return {"player": target.state.name, "playing": _compact_item(item)}


@mcp_tool("control_player", (Scope.PLAYERS_CONTROL, Scope.QUEUES_CONTROL))
async def control_player(
    mass: MusicAssistant,
    player: str,
    command: Literal[
        "play",
        "pause",
        "play_pause",
        "stop",
        "next",
        "previous",
        "power_on",
        "power_off",
        "clear_queue",
        "seek",
    ],
    value: int | None = None,
) -> dict[str, Any]:
    """
    Send a playback or power command to a player.

    :param player: The name or id of the player.
    :param command: The command to send.
    :param value: The position in seconds to seek to, for the seek command.
    """
    target = _resolve_player(mass, player)
    player_id = target.player_id
    players = mass.players
    match command:
        case "seek":
            if value is None:
                raise ToolError("The seek command needs a value in seconds")
            await players.cmd_seek(player_id, value)
        case "power_on" | "power_off":
            await players.cmd_power(player_id, command == "power_on")
        case "clear_queue":
            mass.player_queues.clear(_active_queue(mass, target).queue_id)
        case _:
            await {
                "play": players.cmd_play,
                "pause": players.cmd_pause,
                "play_pause": players.cmd_play_pause,
                "stop": players.cmd_stop,
                "next": players.cmd_next_track,
                "previous": players.cmd_previous_track,
            }[command](player_id)
    return _compact_player(mass, target)


@mcp_tool("set_volume", Scope.PLAYERS_CONTROL)
async def set_volume(
    mass: MusicAssistant, player: str, level: int | None = None, muted: bool | None = None
) -> dict[str, Any]:
    """
    Set the volume of a player and/or mute or unmute it.

    :param player: The name or id of the player.
    :param level: The volume level, from 0 to 100.
    :param muted: True to mute the player, False to unmute it.
    """
    target = _resolve_player(mass, player)
    if level is None and muted is None:
        raise ToolError("Pass a level, muted or both")
    if level is not None:
        if not 0 <= level <= 100:
            raise ToolError("The volume level must be between 0 and 100")
        await mass.players.cmd_volume_set(target.player_id, level)
    if muted is not None:
        await mass.players.cmd_volume_mute(target.player_id, muted)
    return _compact_player(mass, target)


@mcp_tool("set_playback_options", (Scope.PLAYERS_CONTROL, Scope.QUEUES_CONTROL))
async def set_playback_options(
    mass: MusicAssistant,
    player: str,
    shuffle: bool | None = None,
    repeat: Literal["off", "one", "all"] | None = None,
) -> dict[str, Any]:
    """
    Turn shuffle on or off and set the repeat mode of a player's queue.

    :param player: The name or id of the player.
    :param shuffle: True to shuffle the queue, False to play it in order.
    :param repeat: Repeat nothing, the current item or the whole queue.
    """
    target = _resolve_player(mass, player)
    if shuffle is None and repeat is None:
        raise ToolError("Pass shuffle, repeat or both")
    if shuffle is not None:
        await mass.players.cmd_shuffle(target.player_id, shuffle)
    if repeat is not None:
        await mass.players.cmd_repeat(target.player_id, RepeatMode(repeat))
    return {"player": target.state.name, "shuffle": shuffle, "repeat": repeat}


@mcp_tool("get_queue", Scope.QUEUES_READ)
async def get_queue(mass: MusicAssistant, player: str, limit: int = 20) -> dict[str, Any]:
    """
    Show what a player is playing now and what comes next in its queue.

    :param player: The name or id of the player.
    :param limit: The maximum number of upcoming items to return, at most 50.
    """
    target = _resolve_player(mass, player)
    queue = _active_queue(mass, target)
    offset = queue.current_index + 1 if queue.current_index is not None else 0
    upcoming = mass.player_queues.get_queue_items(queue.queue_id, _clamp_limit(limit), offset)
    return {
        "player": target.state.name,
        "queue": queue.display_name,
        "state": queue.state.value,
        "shuffle": queue.shuffle_enabled,
        "repeat": queue.repeat_mode.value,
        "current_item": _compact_queue_item(queue.current_item) if queue.current_item else None,
        "next_items": [_compact_queue_item(item) for item in upcoming],
    }


@mcp_tool("get_recently_played", Scope.LIBRARY_READ)
async def get_recently_played(mass: MusicAssistant, limit: int = 20) -> dict[str, Any]:
    """
    List the items that were played most recently.

    :param limit: The maximum number of items to return, at most 50.
    """
    items = await mass.music.recently_played(_clamp_limit(limit))
    return {"items": [_compact_item(item) for item in items]}


@mcp_tool("get_favorites", Scope.LIBRARY_READ)
async def get_favorites(
    mass: MusicAssistant, media_type: SearchMediaType = "any", limit: int = 50
) -> dict[str, Any]:
    """
    List the favorite items in the library.

    :param media_type: Only return this type of media.
    :param limit: The maximum number of items per media type, at most 50.
    """
    favorites: list[dict[str, Any]] = []
    for item_type in _media_types(media_type):
        controller = mass.music.get_controller(item_type)
        items = await controller.library_items(favorite=True, limit=_clamp_limit(limit))
        favorites.extend(_compact_item(item) for item in items)
    return {"items": favorites}


@mcp_tool("add_favorite", Scope.LIBRARY_WRITE)
async def add_favorite(mass: MusicAssistant, uri: str) -> dict[str, Any]:
    """
    Add an item to the favorites.

    :param uri: The uri of the item.
    """
    await mass.music.set_item_favorite(uri, True)
    return {"favorite": True, "uri": uri}


@mcp_tool("remove_favorite", Scope.LIBRARY_WRITE)
async def remove_favorite(mass: MusicAssistant, uri: str) -> dict[str, Any]:
    """
    Remove an item from the favorites.

    :param uri: The uri of the item.
    """
    await mass.music.set_item_favorite(uri, None)
    return {"favorite": False, "uri": uri}


@mcp_tool("get_playlists", Scope.LIBRARY_READ)
async def get_playlists(mass: MusicAssistant) -> dict[str, Any]:
    """List the playlists in the library, with whether items can be added to them."""
    playlists = await mass.music.playlists.library_items(limit=500)
    return {
        "playlists": [
            {"uri": playlist.uri, "name": playlist.name, "editable": playlist.is_editable}
            for playlist in playlists
            if isinstance(playlist, Playlist)
        ]
    }


@mcp_tool("add_to_playlist", Scope.LIBRARY_WRITE)
async def add_to_playlist(mass: MusicAssistant, playlist: str, uris: list[str]) -> dict[str, Any]:
    """
    Add items to a playlist in the library.

    :param playlist: The uri or name of the playlist.
    :param uris: The uris of the items to add.
    """
    target = await _resolve_playlist(mass, playlist)
    await mass.music.playlists.add_playlist_tracks(target.item_id, uris)
    # the items are added by a background task, which also expands albums and skips duplicates
    return {"playlist": target.name, "requested": len(uris), "status": "adding in the background"}


@mcp_tool("group_players", Scope.PLAYERS_CONTROL)
async def group_players(mass: MusicAssistant, player: str, members: list[str]) -> dict[str, Any]:
    """
    Group players so they play the same audio in sync, with the first player as the leader.

    :param player: The name or id of the player that leads the group.
    :param members: The names or ids of the players to add to the group.
    """
    target = _resolve_player(mass, player)
    member_ids = [_resolve_player(mass, member).player_id for member in members]
    mass.players.check_grouping_access(target.player_id, player_ids_to_add=member_ids)
    await mass.players.cmd_set_members(target.player_id, player_ids_to_add=member_ids)
    return _compact_player(mass, target)


@mcp_tool("ungroup_player", Scope.PLAYERS_CONTROL)
async def ungroup_player(mass: MusicAssistant, player: str) -> dict[str, Any]:
    """
    Remove a player from the group it is part of.

    :param player: The name or id of the player.
    """
    target = _resolve_player(mass, player)
    await mass.players.cmd_ungroup(target.player_id)
    return _compact_player(mass, target)


def _players(mass: MusicAssistant) -> list[Player]:
    """Return the players the current user may use."""
    return [player for player in mass.players.all_players() if not player.state.hide_in_ui]


def _resolve_player(mass: MusicAssistant, player: str) -> Player:
    """Return the player the current user may use with the given id or name."""
    players = _players(mass)
    for candidate in players:
        if candidate.player_id == player:
            return candidate
    name = player.strip().lower()
    for candidate in players:
        if candidate.state.name.strip().lower() == name:
            return candidate
    available = ", ".join(candidate.state.name for candidate in players) or "none"
    raise ToolError(f"Unknown player {player!r}. Available players: {available}")


def _active_queue(mass: MusicAssistant, player: Player) -> PlayerQueue:
    """Return the queue that plays on the player, such as the queue of its group leader."""
    if queue := mass.player_queues.get_player_active_queue(player.player_id):
        return queue
    raise ToolError(f"{player.state.name} has no queue")


async def _resolve_media(
    mass: MusicAssistant, media: str, media_type: SearchMediaType
) -> MediaItemType | ItemMapping:
    """Return the media item with the given uri, or the best search hit for the given name."""
    if "://" in media:
        return await mass.music.get_item_by_uri(media)  # type: ignore[return-value]
    results = _flatten_results(await mass.music.search(media, _media_types(media_type), limit=5))
    if not results:
        raise ToolError(f"Nothing found for {media!r}")
    # an exact name match beats the first hit of a type that comes earlier in the order
    name = media.strip().lower()
    return next((item for item in results if item.name.strip().lower() == name), results[0])


async def _resolve_playlist(mass: MusicAssistant, playlist: str) -> Playlist | ItemMapping:
    """Return the library playlist with the given uri or name."""
    if "://" in playlist:
        item = await mass.music.get_item_by_uri(playlist)
        if item.media_type != MediaType.PLAYLIST or item.provider != "library":
            raise ToolError(f"{playlist!r} is not a playlist in the library")
        return item  # type: ignore[return-value]
    name = playlist.strip().lower()
    for item in await mass.music.playlists.library_items(search=playlist, limit=500):
        if item.name.strip().lower() == name:
            return item
    raise ToolError(f"No playlist named {playlist!r} in the library")


def _clamp_limit(limit: int) -> int:
    """Return the limit a model asked for, bounded to 1 up to MAX_LIMIT."""
    return max(1, min(limit, MAX_LIMIT))


def _media_types(media_type: SearchMediaType) -> list[MediaType]:
    """Return the media types to search for the given media type argument."""
    return list(_ANY_MEDIA_TYPES) if media_type == "any" else [MediaType(media_type)]


def _flatten_results(results: SearchResults) -> list[MediaItemType | ItemMapping]:
    """Return the items of search results, in the order of _ANY_MEDIA_TYPES."""
    return [
        *results.tracks,
        *results.artists,
        *results.albums,
        *results.playlists,
        *results.radio,
        *results.podcasts,
        *results.audiobooks,
    ]


def _compact_item(item: MediaItemType | ItemMapping) -> dict[str, Any]:
    """Return the fields of a media item that a model needs."""
    album = getattr(item, "album", None)
    return {
        "uri": item.uri,
        "name": item.name,
        "media_type": item.media_type.value,
        "artists": [artist.name for artist in getattr(item, "artists", None) or []],
        "album": album.name if album else None,
        "duration": getattr(item, "duration", None) or None,
    }


def _compact_media(media: PlayerMedia) -> dict[str, Any]:
    """Return the fields of the media loaded on a player that a model needs."""
    return {
        "uri": media.uri,
        "name": media.title,
        "media_type": media.media_type.value,
        "artists": [media.artist] if media.artist else [],
        "album": media.album,
        "duration": media.duration or None,
    }


def _compact_queue_item(queue_item: QueueItem) -> dict[str, Any]:
    """Return the fields of a queue item that a model needs."""
    if queue_item.media_item:
        return _compact_item(queue_item.media_item)
    return {
        "uri": None,
        "name": queue_item.name,
        "media_type": None,
        "artists": [],
        "album": None,
        "duration": queue_item.duration,
    }


def _compact_player(mass: MusicAssistant, player: Player) -> dict[str, Any]:
    """Return the fields of a player that a model needs."""
    state = player.state
    members = [
        member.state.name if (member := mass.players.get_player(member_id)) else member_id
        for member_id in state.group_members
    ]
    return {
        "player_id": state.player_id,
        "name": state.name,
        "state": state.playback_state.value,
        "available": state.available,
        "powered": state.powered,
        "volume": state.volume_level,
        "muted": state.volume_muted,
        "is_group": state.type == PlayerType.GROUP,
        "group_members": members,
        "now_playing": _compact_media(state.current_media) if state.current_media else None,
    }
