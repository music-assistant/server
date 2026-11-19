"""Soloist API keys shared between the Soloist setups of one server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.helpers.provider_access import source_owner

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

# every provider running Soloist stores its key and download consent under these setup keys
CONF_SOLOIST_API_KEY: Final = "soloist_api_key"
CONF_SOLOIST_CONSENT: Final = "soloist_download_consent"
SOLOIST_DOMAINS: Final = ("spotify", "spotify_connect")


def find_reusable_api_key(session: SetupSession) -> str | None:
    """
    Return the API key a new Soloist setup can take over, with its download consent.

    The server counts as one Soloist user. The caller's own personal Spotify instances
    come first; when none of them holds a key, the setups without an owner (shared
    Spotify instances and the Spotify Connect plugin) are used. Another user's personal
    key is never returned. Returns None when no setup holds a key, or when the setups
    that come first hold different keys.

    :param session: The setup session of the flow that needs a key.
    """
    mass = session.mass
    caller_id = session.context.user.user_id if session.context.user else None
    personal: set[str] = set()
    shared: set[str] = set()
    for instance_id, raw_conf in mass.config.get(CONF_PROVIDERS, {}).items():
        if instance_id == session.context.instance_id:
            continue
        if raw_conf.get("domain") not in SOLOIST_DOMAINS:
            continue
        if not mass.config.get_provider_setup_value(instance_id, CONF_SOLOIST_CONSENT):
            continue
        if not (api_key := mass.config.get_provider_setup_value(instance_id, CONF_SOLOIST_API_KEY)):
            continue
        owner = source_owner(mass, instance_id)
        if owner is None:
            shared.add(str(api_key))
        elif owner == caller_id:
            personal.add(str(api_key))
    keys = personal or shared
    return keys.pop() if len(keys) == 1 else None
