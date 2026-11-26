"""Helpers for player behavior."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from music_assistant_models.enums import EventType, PlayerType

from music_assistant.helpers.config_entries import PLAYBACK_TARGET_TYPES

if TYPE_CHECKING:
    from music_assistant_models.event import MassEvent
    from music_assistant_models.player import PlayerMedia

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.player import Player

_DEFAULT_ICONS_BY_PROVIDER = {
    "airplay": "airplay",
    "chromecast": "cast",
    "fully_kiosk": "tablet",
    "msx_bridge": "tv",
    "roku_media_assistant": "tv",
    "sonos": "sonos",
    "sonos_s1": "sonos",
    "wiim": "wiim",
}

_DEFAULT_ICONS_BY_PLAYER_TYPE = {
    PlayerType.CONTROLLER: "monitor",
    PlayerType.DISPLAY: "monitor",
    PlayerType.LIGHT: "sun",
    PlayerType.SOURCE: "vinyl",
    PlayerType.VISUALIZER: "monitor",
}

_DEFAULT_ICON_MATCHES = (
    ("homepod-mini", ("homepod",)),
    ("apple-tv", ("apple tv", "appletv")),
    (
        "google-nest",
        ("google home", "google nest", "nest audio", "nest hub", "nest mini"),
    ),
    ("voice-pe", ("home assistant voice", "voice pe", "voice preview edition")),
    ("sonos", ("sonos",)),
    ("wiim", ("wiim",)),
    ("soundbar", ("sound bar", "soundbar")),
    ("tv", (" tv", "smarttv", "television")),
    ("monitor", ("web browser",)),
    ("laptop", ("laptop", "notebook")),
    ("smartphone", ("iphone", "mobile application", "smartphone")),
    ("tablet", ("ipad", "tablet")),
    ("headphones", ("earbud", "headphone", "headset")),
    ("bluetooth", ("bluetooth",)),
    ("radio", ("radio",)),
    ("car", ("carplay",)),
)


# asked for at every state update of a player, with the few values its device reports
@lru_cache(maxsize=512)
def get_default_player_icon(
    player_type: PlayerType,
    provider_domain: str | None,
    manufacturer: str | None,
    model: str | None,
) -> str:
    """
    Return the most appropriate default icon for a player.

    :param player_type: The player's functional type.
    :param provider_domain: The domain of the provider of the player's own protocol, None
        for a player without one.
    :param manufacturer: The device manufacturer reported by the provider.
    :param model: The device model reported by the provider.
    """
    if player_type in (PlayerType.GROUP, PlayerType.MULTI_CHANNEL):
        return "speakers"
    if player_type_icon := _DEFAULT_ICONS_BY_PLAYER_TYPE.get(player_type):
        return player_type_icon

    manufacturer_name = (manufacturer or "").casefold()
    model_name = (model or "").casefold()
    if manufacturer_name.startswith("apple") and model_name.startswith(("imac", "mac")):
        return "mac"

    device_description = f"{manufacturer_name} {model_name}"
    for icon, matches in _DEFAULT_ICON_MATCHES:
        if any(match in device_description for match in matches):
            return icon

    if provider_domain and (provider_icon := _DEFAULT_ICONS_BY_PROVIDER.get(provider_domain)):
        return provider_icon
    return "speaker"


def served_duration(media: PlayerMedia, stream_url: str) -> int | None:
    """
    Return the length in seconds to tell a player for the given stream, if known.

    After a seek, a stream of one item holds only the part that is left, but a flow stream
    carries on past it into the next track, so a flow gets the whole track's duration.

    :param media: The media the stream plays.
    :param stream_url: The URL the player gets the stream from.
    """
    if "/flow/" in stream_url:
        return media.duration
    return media.stream_duration or media.duration


def get_available_player(mass: MusicAssistant, player_id: str) -> Player | None:
    """
    Return the player if it is available, else None (also for an unknown player).

    Available is what clients see as available: a player switched off at its power
    control counts as available, because playing to it powers it on.

    :param mass: The MusicAssistant instance.
    :param player_id: Id of the player to look up.
    """
    player = mass.players.get_player(player_id)
    if player is None or not player.available:
        return None
    return player


def can_start_playback(mass: MusicAssistant, player_id: str) -> bool:
    """
    Return whether a playback start on the player has a speaker to play on now.

    That is the player itself while it is available (see get_available_player). A group
    player plays on its members, so it needs one that is available and can render audio:
    a display or lighting member follows the group's playback but cannot play it.

    :param mass: The MusicAssistant instance.
    :param player_id: Id of the player to check.
    """
    if (player := get_available_player(mass, player_id)) is None:
        return False
    if player.type != PlayerType.GROUP:
        return True
    # the static members count too: a group can leave the ones that were off out of its
    # current members until it stops
    return any(
        (member := get_available_player(mass, member_id)) is not None
        and member.type in PLAYBACK_TARGET_TYPES
        for member_id in {*player.group_members, *player.static_group_members}
        if member_id != player.player_id
    )


class PlayerAvailabilityTracker:
    """Tell from player events when a player becomes available, see get_available_player."""

    def __init__(self, mass: MusicAssistant) -> None:
        """
        Initialize the tracker; players that are available now are not reported.

        :param mass: The MusicAssistant instance.
        """
        self._mass = mass
        self._available = {
            player.player_id for player in mass.players.iter_players(return_unavailable=False)
        }

    def became_available(self, event: MassEvent) -> bool:
        """
        Return whether the event's player became available since the previous event passed.

        A player comes back by registering (PLAYER_ADDED) or by turning available while
        registered (PLAYER_UPDATED), so pass every PLAYER_ADDED, PLAYER_UPDATED and
        PLAYER_REMOVED event of the players to follow. A removed player counts as
        unavailable.

        :param event: A player event.
        """
        player_id = event.object_id
        if player_id is None:
            return False
        available = (
            event.event != EventType.PLAYER_REMOVED
            and get_available_player(self._mass, player_id) is not None
        )
        if not available:
            self._available.discard(player_id)
            return False
        if player_id in self._available:
            return False
        self._available.add(player_id)
        return True
