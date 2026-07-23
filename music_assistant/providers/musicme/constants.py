"""Constants for the MusicMe provider."""

import json
import re

from music_assistant_models.enums import ProviderFeature

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.BROWSE,
    ProviderFeature.RECOMMENDATIONS,
}

SIGNIN_BY_API = "signin_by_api" # Custom key for ConfigEntry
DATASERVICE_BASE = "https://dataservice.musicme.com/dataservice/v3"
STREAM_BASE = "https://stream.hosting-media.net/musicme"
WEB_BASE = "https://www.musicme.com"
LOGIN_URL = "https://www.musicme.com/mon-musicme/connexion/"
PARTNER_ID = "22876"
CLIENT_JSON = json.dumps(
    {"type": "desktop-web", "context": "www.musicme.com", "app": "mmplayer"},
    separators=(",", ":"),
)
VALID_ID_RE = re.compile(r"^[\w.-]+$")
