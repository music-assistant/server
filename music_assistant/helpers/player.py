"""Helpers for player behavior."""

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlayerType, ProviderFeature
from music_assistant_models.media_items import AudioSource

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.player_queue import PlayerQueue

    from music_assistant.mass import MusicAssistant

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


def get_queue_audio_source(
    mass: MusicAssistant, queue: PlayerQueue | None
) -> tuple[AudioSource, PluginProvider] | None:
    """
    Return the AudioSource current on the given queue and its owning PluginProvider.

    Returns None when the queue's current item is not a MediaType.AUDIO_SOURCE
    or when the owning plugin provider is no longer available.

    :param mass: The MusicAssistant instance.
    :param queue: The queue whose current item to inspect (None allowed).
    """
    if queue is None:
        return None
    current_item = queue.current_item
    if current_item is None or current_item.media_item is None:
        return None
    media_item = current_item.media_item
    # isinstance check defends against a non-AudioSource subclass that
    # somehow has media_type=AUDIO_SOURCE set (mutated or constructed wrong)
    # — the media_type guard alone would let it through and crash later.
    if not isinstance(media_item, AudioSource):
        return None
    provider = mass.get_provider(media_item.provider)
    if not isinstance(provider, PluginProvider):
        return None
    # Belt-and-suspenders: a queue item carrying media_type=AUDIO_SOURCE
    # can only have come from a provider that declared the feature, but
    # a feature flag flipped off at runtime (provider reload, config
    # change) would leave on_source_control / on_volume_change raising
    # NotImplementedError. Skip cleanly.
    if ProviderFeature.AUDIO_SOURCE not in provider.supported_features:
        return None
    return media_item, provider
