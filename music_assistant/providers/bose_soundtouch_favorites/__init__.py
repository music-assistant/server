"""Bose SoundTouch Favorites plugin for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import websockets
from defusedxml import ElementTree as DefusedET
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType
from websockets.typing import Subprotocol

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

LOGGER = logging.getLogger("music_assistant.Bose SoundTouch Favorites")
PLUGIN_VERSION = "0.2.42"
PRESET_IDS = range(1, 7)
SEARCH_RESULT_LIMIT = 25
SEARCH_TIMEOUT = 10
RECONNECT_DELAY = 10
NO_SEARCH_RESULTS_VALUE = "__bose_soundtouch_favorites_no_results__"
CATEGORY_GENERIC = "generic"
CATEGORY_TARGET_PLAYER = "Target player"

BOSE_SUBPROTOCOLS = [Subprotocol("gabbo")]

MEDIA_TYPE_OPTIONS = [
    ConfigValueOption(title="Artist", value="artist"),
    ConfigValueOption(title="Album", value="album"),
    ConfigValueOption(title="Track", value="track"),
    ConfigValueOption(title="Playlist", value="playlist"),
    ConfigValueOption(title="Radio", value="radio"),
    ConfigValueOption(title="Audiobook", value="audiobook"),
    ConfigValueOption(title="Podcast", value="podcast"),
    ConfigValueOption(title="Podcast episode", value="podcast_episode"),
    ConfigValueOption(title="Folder", value="folder"),
    ConfigValueOption(title="Announcement", value="announcement"),
    ConfigValueOption(title="Flow stream", value="flow_stream"),
    ConfigValueOption(title="Plugin source", value="plugin_source"),
    ConfigValueOption(title="Sound effect", value="sound_effect"),
    ConfigValueOption(title="Genre", value="genre"),
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
    airplay_id: str | None
    dlna_id: str | None
    sendspin_id: str | None
    volume_level: int | None
    available: bool
    can_group: bool


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Set up the Bose SoundTouch Favorites provider."""
    return BoseSoundTouchFavoritesProvider(mass, manifest, config, supported_features=set())


def _identifier_value(identifiers: dict[Any, str], key: str) -> str | None:
    """Extract identifier value from MA IdentifierType dict."""
    for identifier_type, value in identifiers.items():
        if getattr(identifier_type, "value", None) == key:
            return value
        if key in str(identifier_type).lower():
            return value
    return None


def build_bose_player_info(player: Any) -> BosePlayerInfo | None:
    """Build useful Bose info from a Music Assistant player."""
    device_info = getattr(player, "device_info", None)
    if not device_info:
        return None

    manufacturer = getattr(device_info, "manufacturer", None)
    model = getattr(device_info, "model", None)

    if not manufacturer or "bose" not in manufacturer.lower():
        return None

    identifiers = getattr(device_info, "identifiers", {}) or {}

    ip_address = _identifier_value(identifiers, "ip_address")
    mac_address = _identifier_value(identifiers, "mac_address")
    bose_uuid = _identifier_value(identifiers, "uuid")
    airplay_id = _identifier_value(identifiers, "airplay_id")

    dlna_id = None
    sendspin_id = None

    for protocol in getattr(player, "output_protocols", []) or []:
        protocol_domain = getattr(protocol, "protocol_domain", None)
        protocol_id = getattr(protocol, "output_protocol_id", None)

        if protocol_domain == "dlna":
            dlna_id = protocol_id
        elif protocol_domain == "sendspin":
            sendspin_id = protocol_id

    return BosePlayerInfo(
        player_id=player.player_id,
        name=getattr(player, "display_name", None) or getattr(player, "name", player.player_id),
        model=model,
        manufacturer=manufacturer,
        ip_address=ip_address,
        mac_address=mac_address,
        bose_uuid=bose_uuid,
        airplay_id=airplay_id,
        dlna_id=dlna_id,
        sendspin_id=sendspin_id,
        volume_level=getattr(player, "volume_level", None),
        available=getattr(player, "available", False),
        can_group=bool(getattr(player, "can_group_with", None)),
    )


def build_bose_players(mass: MusicAssistant) -> list[BosePlayerInfo]:
    """Return all Bose players detected by Music Assistant."""
    bose_players: list[BosePlayerInfo] = []

    for player in mass.players.all_players():
        info = build_bose_player_info(player)
        if info:
            LOGGER.debug("Detected Bose player: %s", info)
            bose_players.append(info)

    return sorted(bose_players, key=lambda item: item.name.lower())


def _plugin_version_entry() -> ConfigEntry:
    """Return the read-only plugin version config entry."""
    return ConfigEntry(
        key="plugin_version",
        type=ConfigEntryType.LABEL,
        label=f"Plugin version {PLUGIN_VERSION}",
        required=False,
        description="Installed version of the Bose SoundTouch Favorites plugin.",
        category=CATEGORY_GENERIC,
        advanced=True,
    )


def _unknown_if_empty(value: Any) -> str:
    """Return a readable fallback for missing config detail values."""
    return str(value) if value not in (None, "") else "unknown"


def _target_player_option_title(player: BosePlayerInfo) -> str:
    """Return the target player dropdown title."""
    manufacturer_model = " ".join(part for part in (player.manufacturer, player.model) if part)
    return (
        f"🔊 {player.name} / {_unknown_if_empty(manufacturer_model)} / "
        f"{_unknown_if_empty(player.ip_address)}"
    )


def _target_player_detail_entries(player: BosePlayerInfo) -> tuple[ConfigEntry, ...]:
    """Return read-only detail rows for the selected target player."""
    details = (
        ("Music Assistant player Id", player.player_id),
        ("MAC address", player.mac_address),
        ("Bose UUID", player.bose_uuid),
    )

    return tuple(
        ConfigEntry(
            key=f"target_player_detail_{index}",
            type=ConfigEntryType.LABEL,
            label=f"{label}: {value if value not in (None, '') else 'unknown'}",
            required=False,
            category=CATEGORY_TARGET_PLAYER,
            advanced=True,
        )
        for index, (label, value) in enumerate(details, start=1)
    )


async def _resolve_maybe_awaitable(value: Any) -> Any:
    """Return awaited value if needed."""
    if hasattr(value, "__await__"):
        return await value
    return value


def _config_value(
    values: dict[str, ConfigValueType] | None,
    key: str,
    default: ConfigValueType = "",
) -> ConfigValueType:
    """Return an intermediate config value."""
    if values and key in values and values[key] is not None:
        return values[key]
    return default


def _media_type_value(media_type: str) -> Any:
    """Return a MA MediaType enum value when available."""
    try:
        return MediaType(media_type)
    except ValueError:
        return media_type


def _media_item_title(item: Any) -> str:
    """Build a readable config option title for a media item."""
    name = getattr(item, "name", None) or getattr(item, "sort_name", None)
    provider = getattr(item, "provider", None) or getattr(item, "provider_instance", None)
    media_type = getattr(getattr(item, "media_type", None), "value", None) or getattr(
        item, "media_type", None
    )

    if not provider:
        provider_mappings = getattr(item, "provider_mappings", None) or []
        for mapping in provider_mappings:
            provider = getattr(mapping, "provider_domain", None) or getattr(
                mapping, "provider_instance", None
            )
            if provider:
                break

    details = [str(part) for part in (media_type, provider) if part]
    if name and details:
        return f"{name} ({', '.join(details)})"
    if name:
        return str(name)
    return str(getattr(item, "uri", None) or getattr(item, "item_id", "Unknown radio"))


def _media_item_value(item: Any) -> str | None:
    """Return the best value to pass to MA play_media."""
    value = (
        getattr(item, "uri", None) or getattr(item, "item_id", None) or getattr(item, "name", None)
    )
    return str(value) if value else None


def _iter_search_result_items(search_result: Any, media_type: str) -> list[Any]:
    """Extract media items from a MA search result across known API shapes."""
    items = getattr(search_result, "items", None)
    if items is not None:
        return list(items)

    if isinstance(search_result, list | tuple):
        return list(search_result)

    if isinstance(search_result, dict):
        for key in (f"{media_type}s", media_type):
            if key in search_result:
                return list(search_result[key] or [])
        return []

    attr_names = (f"{media_type}s", media_type)
    if media_type == "radio":
        attr_names = ("radio", "radios")

    for attr_name in attr_names:
        items = getattr(search_result, attr_name, None)
        if items is not None:
            return list(items)

    return []


async def _search_media_items(
    mass: MusicAssistant,
    media_type: str,
    query: str,
) -> list[Any]:
    """Search MA media items for config result options."""
    if not query:
        return []

    music = getattr(mass, "music", None)
    if music is None:
        return []

    search = getattr(music, "search", None)
    if search is None:
        return []

    media_type_value = _media_type_value(media_type)
    kwargs_candidates = (
        {
            "search_query": query,
            "media_types": [media_type_value],
            "limit": SEARCH_RESULT_LIMIT,
        },
        {"query": query, "media_types": [media_type_value], "limit": SEARCH_RESULT_LIMIT},
    )

    for kwargs in kwargs_candidates:
        try:
            search_result = await asyncio.wait_for(
                _resolve_maybe_awaitable(search(**kwargs)),
                timeout=SEARCH_TIMEOUT,
            )
            return _iter_search_result_items(search_result, media_type)
        except TimeoutError:
            LOGGER.warning(
                "Timed out searching %s media for query %r after %s seconds",
                media_type,
                query,
                SEARCH_TIMEOUT,
            )
            return []
        except TypeError:
            continue
        except Exception as err:
            LOGGER.warning("Unable to search media items: %s", err)
            break

    for args in (
        (query, [media_type_value], SEARCH_RESULT_LIMIT),
        (query, [media_type_value]),
        (query,),
    ):
        try:
            search_result = await asyncio.wait_for(
                _resolve_maybe_awaitable(search(*args)),
                timeout=SEARCH_TIMEOUT,
            )
            return _iter_search_result_items(search_result, media_type)
        except TimeoutError:
            LOGGER.warning(
                "Timed out searching %s media for query %r after %s seconds",
                media_type,
                query,
                SEARCH_TIMEOUT,
            )
            return []
        except TypeError:
            continue
        except Exception as err:
            LOGGER.warning("Unable to search media items with positional args: %s", err)
            break

    return []


async def build_media_options(
    mass: MusicAssistant,
    media_type: str,
    query: str,
) -> list[ConfigValueOption]:
    """Build dropdown options for a favorite media search."""
    options_by_value: dict[str, ConfigValueOption] = {}
    for item in await _search_media_items(mass, media_type, query):
        value = _media_item_value(item)
        if not value or value in options_by_value:
            continue
        options_by_value[value] = ConfigValueOption(
            title=_media_item_title(item),
            value=value,
        )

    if query and not options_by_value:
        return [
            ConfigValueOption(
                title=f"No results found for {query}",
                value=NO_SEARCH_RESULTS_VALUE,
            )
        ]

    return sorted(options_by_value.values(), key=lambda option: option.title.lower())


def _is_real_media_selection(value: str) -> bool:
    """Return true if the selected config value points to a playable media item."""
    return bool(value and value != NO_SEARCH_RESULTS_VALUE)


def _xml_local_name(tag: str) -> str:
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1]


async def _build_preset_media_options(
    mass: MusicAssistant,
    media_type: str,
    query: str,
    selected_media: str,
    refresh_results: bool,
) -> list[ConfigValueOption]:
    """Build the result dropdown for a favorite without losing current selection state."""
    media_options = await build_media_options(mass, media_type, query) if refresh_results else []
    if _is_real_media_selection(selected_media) and selected_media not in {
        option.value for option in media_options
    }:
        media_options.append(ConfigValueOption(title=selected_media, value=selected_media))

    return media_options


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    bose_players = build_bose_players(mass)
    configurable_players = [player for player in bose_players if player.ip_address]

    if not configurable_players:
        description = (
            "No Bose SoundTouch speakers were detected by Music Assistant."
            if not bose_players
            else (
                "Bose speakers were detected, but none exposes an IP address "
                "required by SoundTouch."
            )
        )
        return (
            _plugin_version_entry(),
            ConfigEntry(
                key="no_bose_players_found",
                type=ConfigEntryType.LABEL,
                label="No configurable Bose SoundTouch speaker found",
                required=False,
                description=description,
                category=CATEGORY_GENERIC,
            ),
        )

    player_options = [
        ConfigValueOption(
            title=_target_player_option_title(player),
            value=player.player_id,
        )
        for player in configurable_players
    ]
    selected_target_player_id = str(_config_value(values, "target_player", player_options[0].value))
    selected_target_player = next(
        (
            player
            for player in configurable_players
            if player.player_id == selected_target_player_id
        ),
        configurable_players[0],
    )
    preset_entries: list[ConfigEntry] = []

    for preset_id in PRESET_IDS:
        preset_category = f"Button {preset_id}"
        media_type_key = f"preset_{preset_id}_media_type"
        search_key = f"preset_{preset_id}_search"
        selected_key = f"preset_{preset_id}_selected_media"
        media_key = f"preset_{preset_id}_media"
        search_action = f"preset_{preset_id}_search_media"
        copy_action = f"preset_{preset_id}_copy_media"

        media_type = str(_config_value(values, media_type_key, "playlist"))
        query = str(_config_value(values, search_key, "")).strip()
        selected_media = str(_config_value(values, selected_key, ""))
        media_value = str(_config_value(values, media_key, ""))

        if action == copy_action and _is_real_media_selection(selected_media):
            media_value = selected_media

        media_options = await _build_preset_media_options(
            mass=mass,
            media_type=media_type,
            query=query,
            selected_media=selected_media,
            refresh_results=action in (search_action, copy_action),
        )

        show_media_selection = any(
            _is_real_media_selection(str(option.value)) for option in media_options
        )

        preset_entries.extend(
            (
                ConfigEntry(
                    key=media_type_key,
                    type=ConfigEntryType.STRING,
                    label="Media type",
                    required=False,
                    default_value=media_type,
                    value=media_type,
                    options=MEDIA_TYPE_OPTIONS,
                    description=(
                        "Type of media used for this SoundTouch favorite search and playback."
                    ),
                    category=preset_category,
                ),
                ConfigEntry(
                    key=search_key,
                    type=ConfigEntryType.STRING,
                    label="Enter your search",
                    required=False,
                    default_value=query,
                    value=query,
                    description="Type a search term, then press Search.",
                    category=preset_category,
                ),
                ConfigEntry(
                    key=f"preset_{preset_id}_do_search",
                    type=ConfigEntryType.ACTION,
                    label="Search 🔎",
                    action=search_action,
                    category=preset_category,
                ),
            )
        )

        if show_media_selection:
            preset_entries.extend(
                (
                    ConfigEntry(
                        key=selected_key,
                        type=ConfigEntryType.STRING,
                        label="Result selection",
                        required=False,
                        default_value=selected_media,
                        value=selected_media,
                        options=media_options,
                        category=preset_category,
                    ),
                    ConfigEntry(
                        key=f"preset_{preset_id}_copy_media",
                        type=ConfigEntryType.ACTION,
                        label="Select ⭐",
                        action=copy_action,
                        category=preset_category,
                    ),
                )
            )
        elif media_options:
            preset_entries.append(
                ConfigEntry(
                    key=f"preset_{preset_id}_no_results",
                    type=ConfigEntryType.LABEL,
                    label=media_options[0].title,
                    required=False,
                    category=preset_category,
                )
            )

        preset_entries.append(
            ConfigEntry(
                key=media_key,
                type=ConfigEntryType.STRING,
                label=f"Favorite {preset_id} to play",
                required=False,
                default_value=media_value,
                value=media_value,
                description="URI copied from the selected result or manually entered.",
                category=preset_category,
            )
        )

    return (
        _plugin_version_entry(),
        ConfigEntry(
            key="target_player",
            type=ConfigEntryType.STRING,
            label="Target player",
            required=True,
            default_value=player_options[0].value,
            value=selected_target_player.player_id,
            options=player_options,
            description="Bose SoundTouch speaker detected by Music Assistant.",
            category=CATEGORY_TARGET_PLAYER,
            immediate_apply=bool(instance_id),
        ),
        *_target_player_detail_entries(selected_target_player),
        *preset_entries,
    )


def extract_preset_id(message: str) -> int | None:
    """Extract Bose SoundTouch favorite id from a WebSocket XML message."""
    try:
        root = DefusedET.fromstring(message)
    except DefusedET.ParseError:
        return None

    preset = next(
        (element for element in root.iter() if _xml_local_name(element.tag) == "preset"),
        None,
    )
    if preset is None:
        return None

    preset_id = preset.attrib.get("id")
    if not preset_id:
        return None

    try:
        return int(preset_id)
    except ValueError:
        return None


class BoseSoundTouchFavoritesProvider(PluginProvider):
    """Listen to Bose SoundTouch favorite button events."""

    _listener_task: asyncio.Task[None] | None = None
    _stop_event: asyncio.Event | None = None
    _bose_player: BosePlayerInfo | None = None

    @property
    def instance_name_postfix(self) -> str | None:
        """Return the target player name to identify multi-instance configs."""
        if self._bose_player:
            return self._bose_player.name

        target_player = self.config.get_value("target_player")
        if not target_player:
            return None

        try:
            player = self.mass.players.get_player(str(target_player))
        except Exception:
            return str(target_player)

        return (
            getattr(player, "display_name", None)
            or getattr(player, "name", None)
            or str(target_player)
        )

    async def loaded_in_mass(self) -> None:
        """Start listening after Music Assistant loads the provider."""
        target_player = self.config.get_value("target_player")
        if not target_player:
            raise RuntimeError("No Bose SoundTouch speaker has been configured")

        target_player_id = str(target_player)
        try:
            player = self.mass.players.get_player(target_player_id)
        except Exception as err:
            raise RuntimeError(
                f"Configured Bose SoundTouch speaker no longer exists: {target_player_id}"
            ) from err

        self._bose_player = build_bose_player_info(player)

        if not self._bose_player:
            raise RuntimeError(f"Selected player is not a Bose player: {target_player_id}")

        if not self._bose_player.ip_address:
            raise RuntimeError(f"Unable to find IP address for player {target_player_id}")

        self.logger.info(
            (
                "Bose SoundTouch Favorites %s loaded: name=%s player_id=%s "
                "ip=%s model=%s uuid=%s mac=%s"
            ),
            PLUGIN_VERSION,
            self._bose_player.name,
            self._bose_player.player_id,
            self._bose_player.ip_address,
            self._bose_player.model,
            self._bose_player.bose_uuid,
            self._bose_player.mac_address,
        )

        self._stop_event = asyncio.Event()
        self._listener_task = asyncio.create_task(
            self._listen_to_bose(self._bose_player.ip_address),
            name="bose_soundtouch_favorites_listener",
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider and stop the Bose listener."""
        self.logger.info("Bose SoundTouch Favorites unloading. Removed=%s", is_removed)

        if self._stop_event:
            self._stop_event.set()

        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
            self._listener_task = None

        self._stop_event = None
        self._bose_player = None

    async def _listen_to_bose(self, speaker_ip: str) -> None:
        """Connect to Bose WebSocket and handle physical favorite button presses."""
        uri = f"ws://{speaker_ip}:8080"

        while self._stop_event and not self._stop_event.is_set():
            try:
                self.logger.info("Connecting to Bose WebSocket: %s", uri)

                async with websockets.connect(uri, subprotocols=BOSE_SUBPROTOCOLS) as ws:
                    self.logger.info("Connected to Bose WebSocket: %s", uri)

                    while self._stop_event and not self._stop_event.is_set():
                        message = await ws.recv()
                        if isinstance(message, bytes):
                            message = message.decode()
                        preset_id = extract_preset_id(message)

                        if preset_id is not None:
                            await self._handle_preset(preset_id)

            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.logger.warning(
                    "Bose WebSocket error: %s. Reconnecting in %s seconds...",
                    err,
                    RECONNECT_DELAY,
                )
                if self._stop_event:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=RECONNECT_DELAY,
                        )

    async def _handle_preset(self, preset_id: int) -> None:
        """Handle a Bose physical favorite press."""
        if preset_id not in PRESET_IDS:
            self.logger.debug("Ignoring unsupported Bose SoundTouch favorite id: %s", preset_id)
            return

        player_id = str(self.config.get_value("target_player"))
        media_key = f"preset_{preset_id}_media"
        media_id = str(self.config.get_value(media_key) or "")
        media_type = str(self.config.get_value(f"preset_{preset_id}_media_type") or "playlist")

        speaker_name = self._bose_player.name if self._bose_player else player_id

        if not _is_real_media_selection(media_id):
            self.logger.warning(
                "[%s] Bose SoundTouch favorite_%s detected, but no media configured for %s",
                speaker_name,
                preset_id,
                media_key,
            )
            return

        self.logger.info(
            "[%s] Bose SoundTouch favorite_%s detected. Playing %s (%s) on player %s",
            speaker_name,
            preset_id,
            media_id,
            media_type,
            player_id,
        )

        play_kwargs = {
            "queue_id": player_id,
            "media": media_id,
            "media_type": _media_type_value(media_type),
        }

        for kwargs in (
            play_kwargs,
            {"queue_id": player_id, "media": media_id},
        ):
            try:
                await self.mass.player_queues.play_media(**kwargs)
                return
            except TypeError:
                continue
            except Exception:
                self.logger.exception(
                    "Unable to play configured media for Bose SoundTouch favorite_%s",
                    preset_id,
                )
                return

        self.logger.error("Unable to call play_media with the available Music Assistant API")
