"""Bose SoundTouch Favorites plugin for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from defusedxml import ElementTree as DefusedET
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, IdentifierType, MediaType
from music_assistant_models.errors import MusicAssistantError, SetupFailedError
from music_assistant_models.media_items import (
    Album,
    Artist,
    Audiobook,
    Genre,
    ItemMapping,
    Playlist,
    Podcast,
    Radio,
    SearchResults,
    Track,
)

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.models.player import Player

PRESET_IDS = range(1, 7)
SEARCH_RESULT_LIMIT = 25
SEARCH_TIMEOUT = 10
RECONNECT_DELAY = 10
INSTANCE_POSTFIX_TARGET_LIMIT = 5

BOSE_SUBPROTOCOLS = ("gabbo",)

SearchResultItem = (
    Artist | Album | Genre | Track | Playlist | Radio | Audiobook | Podcast | ItemMapping
)
SEARCHABLE_MEDIA_TYPES = (
    MediaType.ARTIST,
    MediaType.ALBUM,
    MediaType.TRACK,
    MediaType.PLAYLIST,
    MediaType.RADIO,
    MediaType.AUDIOBOOK,
    MediaType.PODCAST,
    MediaType.GENRE,
)
DEFAULT_MEDIA_TYPE = MediaType.PLAYLIST
MEDIA_TYPE_OPTIONS = [
    ConfigValueOption(title=media_type.value.replace("_", " ").title(), value=media_type.value)
    for media_type in SEARCHABLE_MEDIA_TYPES
]


@dataclass
class BosePlayerInfo:
    """Relevant Bose player information extracted from a MA player."""

    player_id: str
    name: str
    model: str | None
    manufacturer: str | None
    ip_address: str | None
    mac_address: str | None
    bose_uuid: str | None


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Set up the Bose SoundTouch Favorites provider."""
    return BoseSoundTouchFavoritesProvider(mass, manifest, config, supported_features=set())


def build_bose_player_info(player: Player) -> BosePlayerInfo | None:
    """Build useful Bose info from a Music Assistant player."""
    device_info = player.device_info
    manufacturer = device_info.manufacturer
    model = device_info.model

    if not manufacturer or "bose" not in manufacturer.lower():
        return None

    identifiers = device_info.identifiers

    ip_address = identifiers.get(IdentifierType.IP_ADDRESS)
    mac_address = identifiers.get(IdentifierType.MAC_ADDRESS)
    bose_uuid = identifiers.get(IdentifierType.UUID)

    return BosePlayerInfo(
        player_id=player.player_id,
        name=player.display_name,
        model=model,
        manufacturer=manufacturer,
        ip_address=ip_address,
        mac_address=mac_address,
        bose_uuid=bose_uuid,
    )


def build_bose_players(mass: MusicAssistant) -> list[BosePlayerInfo]:
    """Return all Bose players detected by Music Assistant."""
    bose_players: list[BosePlayerInfo] = []

    for player in mass.players.all_players():
        info = build_bose_player_info(player)
        if info:
            bose_players.append(info)

    return sorted(bose_players, key=lambda item: item.name.lower())


def _unknown_if_empty(value: str | None) -> str:
    """Return a readable fallback for missing config detail values."""
    return value if value else "unknown"


def _target_player_option_title(player: BosePlayerInfo) -> str:
    """Return the target player dropdown title."""
    manufacturer_model = " ".join(part for part in (player.manufacturer, player.model) if part)
    return (
        f"🔊 {player.name} / {_unknown_if_empty(manufacturer_model)} / "
        f"{_unknown_if_empty(player.ip_address)}"
    )


def _target_player_detail_entries(
    player: BosePlayerInfo,
    player_index: int,
) -> tuple[ConfigEntry, ...]:
    """Return read-only detail rows for the selected target player."""
    details = (
        ("target_player_ma_id", player.player_id),
        ("target_player_mac_address", player.mac_address),
        ("target_player_bose_uuid", player.bose_uuid),
    )
    key_prefix = f"target_player_detail_{player_index}_"

    return tuple(
        ConfigEntry(
            key=f"{key_prefix}{index}",
            type=ConfigEntryType.LABEL,
            translation_key=translation_key,
            translation_params=[player.name, _unknown_if_empty(value)],
            required=False,
            category="target_players",
            advanced=True,
        )
        for index, (translation_key, value) in enumerate(details, start=1)
    )


def _string_config_value(
    values: dict[str, ConfigValueType] | None,
    key: str,
    default: str = "",
) -> str:
    """Return a string config value."""
    if values and isinstance(value := values.get(key), str):
        return value
    return default


def _string_list_config_value(value: ConfigValueType, default: list[str]) -> list[str]:
    """Return a string list config value."""
    if not isinstance(value, list):
        return default

    string_values: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return default
        string_values.append(item)

    return string_values


def _target_player_ids_config_value(
    values: dict[str, ConfigValueType] | None,
    default: list[str],
) -> list[str]:
    """Return selected target player ids from config values."""
    if not values:
        return default

    return _string_list_config_value(values.get("target_players"), default)


def _media_type_config_value(
    values: dict[str, ConfigValueType] | None,
    key: str,
    default: MediaType = DEFAULT_MEDIA_TYPE,
) -> MediaType:
    """Return a searchable media type from config values."""
    media_type = MediaType(_string_config_value(values, key, default.value))
    return media_type if media_type in SEARCHABLE_MEDIA_TYPES else default


def _iter_search_result_items(
    search_result: SearchResults,
    media_type: MediaType,
) -> list[SearchResultItem]:
    """Extract media items from a typed MA search result."""
    match media_type:
        case MediaType.ARTIST:
            return list(search_result.artists)
        case MediaType.ALBUM:
            return list(search_result.albums)
        case MediaType.GENRE:
            return list(search_result.genres)
        case MediaType.TRACK:
            return list(search_result.tracks)
        case MediaType.PLAYLIST:
            return list(search_result.playlists)
        case MediaType.RADIO:
            return list(search_result.radio)
        case MediaType.AUDIOBOOK:
            return list(search_result.audiobooks)
        case MediaType.PODCAST:
            return list(search_result.podcasts)
        case _:
            return []


async def _search_media_items(
    mass: MusicAssistant,
    media_type: MediaType,
    query: str,
) -> list[SearchResultItem]:
    """Search MA media items for config result options."""
    if not query:
        return []

    if media_type not in SEARCHABLE_MEDIA_TYPES:
        return []

    try:
        search_result = await asyncio.wait_for(
            mass.music.search(
                search_query=query,
                media_types=[media_type],
                limit=SEARCH_RESULT_LIMIT,
                library_only=False,
            ),
            timeout=SEARCH_TIMEOUT,
        )
    except MusicAssistantError, TimeoutError:
        return []

    return _iter_search_result_items(search_result, media_type)


async def build_media_options(
    mass: MusicAssistant,
    media_type: MediaType,
    query: str,
) -> list[ConfigValueOption]:
    """Build dropdown options for a favorite media search."""
    options_by_value: dict[str, ConfigValueOption] = {}
    for item in await _search_media_items(mass, media_type, query):
        value = item.uri
        if not value or value in options_by_value:
            continue
        options_by_value[value] = ConfigValueOption(
            title=f"{item.name} ({item.media_type.value}, {item.provider})",
            value=value,
        )

    return sorted(options_by_value.values(), key=lambda option: (option.title or "").lower())


def _xml_local_name(tag: str) -> str:
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


async def _build_preset_media_options(
    mass: MusicAssistant,
    media_type: MediaType,
    query: str,
    selected_media: str,
    refresh_results: bool,
) -> list[ConfigValueOption]:
    """Build the result dropdown for a favorite without losing current selection state."""
    media_options = await build_media_options(mass, media_type, query) if refresh_results else []
    if selected_media and selected_media not in {option.value for option in media_options}:
        media_options.append(ConfigValueOption(title=selected_media, value=selected_media))

    return media_options


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    bose_players = build_bose_players(mass)
    configurable_players = [player for player in bose_players if player.ip_address]

    if not configurable_players:
        return (
            ConfigEntry(
                key="no_bose_players_found",
                type=ConfigEntryType.LABEL,
                translation_key=(
                    "no_bose_players_found"
                    if not bose_players
                    else "no_configurable_bose_players_found"
                ),
                required=False,
            ),
        )

    player_options = [
        ConfigValueOption(
            title=_target_player_option_title(player),
            value=player.player_id,
        )
        for player in configurable_players
    ]
    configurable_player_ids = {player.player_id for player in configurable_players}
    selected_target_player_ids = [
        player_id
        for player_id in _target_player_ids_config_value(values, [])
        if player_id in configurable_player_ids
    ]
    selected_target_players = [
        player for player in configurable_players if player.player_id in selected_target_player_ids
    ]
    target_player_detail_entries = tuple(
        detail_entry
        for player_index, player in enumerate(selected_target_players, start=1)
        for detail_entry in _target_player_detail_entries(player, player_index)
    )
    preset_entries: list[ConfigEntry] = []

    for preset_id in PRESET_IDS:
        media_type_key = f"preset_{preset_id}_media_type"
        search_key = f"preset_{preset_id}_search"
        selected_key = f"preset_{preset_id}_selected_media"
        media_key = f"preset_{preset_id}_media"
        search_action = f"preset_{preset_id}_search_media"
        copy_action = f"preset_{preset_id}_copy_media"

        media_type = _media_type_config_value(values, media_type_key)
        query = _string_config_value(values, search_key).strip()
        selected_media = _string_config_value(values, selected_key)
        media_value = _string_config_value(values, media_key)

        if action == copy_action and selected_media:
            media_value = selected_media

        media_options = await _build_preset_media_options(
            mass=mass,
            media_type=media_type,
            query=query,
            selected_media=selected_media,
            refresh_results=action in (search_action, copy_action),
        )

        show_media_selection = bool(media_options)

        preset_entries.append(
            ConfigEntry(
                key=f"preset_{preset_id}_header",
                type=ConfigEntryType.DIVIDER,
                translation_key="preset_header",
                translation_params=[str(preset_id)],
                required=False,
                category="favorites",
            )
        )
        preset_entries.extend(
            (
                ConfigEntry(
                    key=media_type_key,
                    type=ConfigEntryType.STRING,
                    translation_key="preset_media_type",
                    translation_params=[str(preset_id)],
                    required=False,
                    default_value=media_type.value,
                    value=media_type.value,
                    options=MEDIA_TYPE_OPTIONS,
                    category="favorites",
                ),
                ConfigEntry(
                    key=search_key,
                    type=ConfigEntryType.STRING,
                    translation_key="preset_search",
                    translation_params=[str(preset_id)],
                    required=False,
                    default_value=query,
                    value=query,
                    category="favorites",
                ),
                ConfigEntry(
                    key=f"preset_{preset_id}_do_search",
                    type=ConfigEntryType.ACTION,
                    translation_key="preset_search_action",
                    translation_params=[str(preset_id)],
                    action=search_action,
                    category="favorites",
                ),
            )
        )

        if show_media_selection:
            preset_entries.extend(
                (
                    ConfigEntry(
                        key=selected_key,
                        type=ConfigEntryType.STRING,
                        translation_key="preset_result_selection",
                        translation_params=[str(preset_id)],
                        required=False,
                        default_value=selected_media,
                        value=selected_media,
                        options=media_options,
                        category="favorites",
                    ),
                    ConfigEntry(
                        key=f"preset_{preset_id}_copy_media",
                        type=ConfigEntryType.ACTION,
                        translation_key="preset_select_action",
                        translation_params=[str(preset_id)],
                        action=copy_action,
                        category="favorites",
                    ),
                )
            )
        preset_entries.append(
            ConfigEntry(
                key=media_key,
                type=ConfigEntryType.STRING,
                translation_key="preset_media",
                translation_params=[str(preset_id)],
                required=False,
                default_value=media_value,
                value=media_value,
                category="favorites",
            )
        )

    return (
        ConfigEntry(
            key="target_players",
            type=ConfigEntryType.STRING,
            required=True,
            default_value=[],
            value=selected_target_player_ids,
            options=player_options,
            multi_value=True,
            category="target_players",
        ),
        *target_player_detail_entries,
        *preset_entries,
    )


def extract_preset_id(message: str) -> int | None:
    """Extract Bose SoundTouch favorite id from a WebSocket XML message."""
    try:
        root = DefusedET.fromstring(message)
    except DefusedET.ParseError:
        return None

    preset_id = next(
        (
            element.attrib.get("id")
            for element in root.iter()
            if _xml_local_name(element.tag) == "preset"
        ),
        None,
    )

    try:
        return int(preset_id) if preset_id else None
    except TypeError, ValueError:
        return None


class BoseSoundTouchFavoritesProvider(PluginProvider):
    """Listen to Bose SoundTouch favorite button events."""

    _listener_tasks: dict[str, asyncio.Task[None]] | None = None
    _stop_event: asyncio.Event | None = None

    @property
    def instance_name_postfix(self) -> str | None:
        """Return the target player names to identify multi-instance configs."""
        target_player_ids = _string_list_config_value(
            self.config.get_value("target_players"),
            [],
        )
        if not target_player_ids:
            return None

        target_player_names = [
            player.display_name
            if (player := self.mass.players.get_player(player_id))
            else player_id
            for player_id in target_player_ids
        ]
        if len(target_player_names) <= INSTANCE_POSTFIX_TARGET_LIMIT:
            return ", ".join(target_player_names)

        remaining_count = len(target_player_names) - INSTANCE_POSTFIX_TARGET_LIMIT
        remaining_label = "other" if remaining_count == 1 else "others"
        visible_names = target_player_names[:INSTANCE_POSTFIX_TARGET_LIMIT]
        return f"{', '.join(visible_names)} + {remaining_count} {remaining_label}"

    async def loaded_in_mass(self) -> None:
        """Start listening after Music Assistant loads the provider."""
        target_player_ids = _string_list_config_value(
            self.config.get_value("target_players"),
            [],
        )
        if not target_player_ids:
            raise SetupFailedError("No Bose SoundTouch speaker has been configured")

        bose_players: list[BosePlayerInfo] = []
        for target_player_id in target_player_ids:
            player = self.mass.players.get_player(target_player_id)
            if player is None:
                raise SetupFailedError(
                    f"Configured Bose SoundTouch speaker no longer exists: {target_player_id}"
                )

            bose_player = build_bose_player_info(player)

            if not bose_player:
                raise SetupFailedError(f"Selected player is not a Bose player: {target_player_id}")

            if not bose_player.ip_address:
                raise SetupFailedError(f"Unable to find IP address for player {target_player_id}")

            bose_players.append(bose_player)

        self._stop_event = asyncio.Event()
        self._listener_tasks = {}
        for bose_player in bose_players:
            self.logger.info(
                (
                    "Bose SoundTouch Favorites target loaded: name=%s player_id=%s "
                    "ip=%s model=%s uuid=%s mac=%s"
                ),
                bose_player.name,
                bose_player.player_id,
                bose_player.ip_address,
                bose_player.model,
                bose_player.bose_uuid,
                bose_player.mac_address,
            )

            self._listener_tasks[bose_player.player_id] = asyncio.create_task(
                self._listen_to_bose(bose_player),
                name=f"bose_soundtouch_favorites_listener_{bose_player.player_id}",
            )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider and stop the Bose listener."""
        self.logger.info("Bose SoundTouch Favorites unloading. Removed=%s", is_removed)

        if self._stop_event:
            self._stop_event.set()

        if self._listener_tasks:
            for listener_task in self._listener_tasks.values():
                listener_task.cancel()
            for listener_task in self._listener_tasks.values():
                with contextlib.suppress(asyncio.CancelledError):
                    await listener_task
            self._listener_tasks.clear()
            self._listener_tasks = None

        self._stop_event = None

    async def _listen_to_bose(self, bose_player: BosePlayerInfo) -> None:
        """Connect to Bose WebSocket and handle physical favorite button presses."""
        speaker_ip = bose_player.ip_address
        if not speaker_ip:
            return

        uri = f"ws://{speaker_ip}:8080"

        while self._stop_event and not self._stop_event.is_set():
            try:
                self.logger.info("[%s] Connecting to Bose WebSocket: %s", bose_player.name, uri)

                async with self.mass.http_session.ws_connect(
                    uri,
                    protocols=BOSE_SUBPROTOCOLS,
                ) as ws:
                    self.logger.info("[%s] Connected to Bose WebSocket: %s", bose_player.name, uri)

                    async for msg in ws:
                        if self._stop_event and self._stop_event.is_set():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            message = msg.data
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            message = msg.data.decode()
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            break
                        else:
                            continue

                        preset_id = extract_preset_id(message)

                        if preset_id is not None:
                            await self._handle_preset(bose_player, preset_id)

            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, TimeoutError, UnicodeDecodeError) as err:
                self.logger.warning(
                    "[%s] Bose WebSocket error: %s. Reconnecting in %s seconds...",
                    bose_player.name,
                    err,
                    RECONNECT_DELAY,
                )
                if self._stop_event:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=RECONNECT_DELAY,
                        )

    async def _handle_preset(self, bose_player: BosePlayerInfo, preset_id: int) -> None:
        """Handle a Bose physical favorite press."""
        if preset_id not in PRESET_IDS:
            self.logger.debug("Ignoring unsupported Bose SoundTouch favorite id: %s", preset_id)
            return

        player_id = bose_player.player_id
        media_key = f"preset_{preset_id}_media"
        media_id = str(self.config.get_value(media_key) or "")
        media_type = str(self.config.get_value(f"preset_{preset_id}_media_type") or "playlist")

        if not media_id:
            self.logger.warning(
                "[%s] Bose SoundTouch favorite_%s detected, but no media configured for %s",
                bose_player.name,
                preset_id,
                media_key,
            )
            return

        self.logger.info(
            "[%s] Bose SoundTouch favorite_%s detected. Playing %s (%s) on player %s",
            bose_player.name,
            preset_id,
            media_id,
            media_type,
            player_id,
        )

        try:
            await self.mass.player_queues.play_media(queue_id=player_id, media=media_id)
        except MusicAssistantError:
            self.logger.exception(
                "Unable to play configured media for Bose SoundTouch favorite_%s",
                preset_id,
            )
