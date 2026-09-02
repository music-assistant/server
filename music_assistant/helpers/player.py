"""Helpers for player behavior."""

from music_assistant_models.enums import PlayerType

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


def get_default_player_icon(
    player_type: PlayerType,
    provider_domain: str,
    manufacturer: str | None,
    model: str | None,
) -> str:
    """
    Return the most appropriate default icon for a player.

    :param player_type: The player's functional type.
    :param provider_domain: The domain of the player provider.
    :param manufacturer: The device manufacturer reported by the provider.
    :param model: The device model reported by the provider.
    """
    if player_type in (PlayerType.GROUP, PlayerType.STEREO_PAIR):
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

    if provider_icon := _DEFAULT_ICONS_BY_PROVIDER.get(provider_domain):
        return provider_icon
    return "speaker"
