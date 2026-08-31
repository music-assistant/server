"""Constants for the Overcast provider."""

from __future__ import annotations

import re

from music_assistant_models.enums import ProviderFeature

BASE_URL = "https://overcast.fm"
LOGIN_URL = f"{BASE_URL}/login"
QR_VERIFY_URL = f"{BASE_URL}/main/login_qr_verify"
PODCASTS_URL = f"{BASE_URL}/podcasts"
OPML_EXPORT_URL = f"{BASE_URL}/account/export_opml/extended"

SESSION_COOKIE_NAME = "o"

CONF_SESSION_COOKIE = "session_cookie"
CONF_METHOD = "method"
METHOD_QR = "qr"
METHOD_PASSWORD = "password"

# deep link the Overcast iPhone app registers, carrying the token to approve
QR_AUTH_URL = "overcast:///auth?t={token}&l=browser"
# the login page hands out a fresh token on every load
QR_TOKEN_PATTERN = re.compile(
    r'id="qrcode"[^>]*\bdata-token="(?P<token>[^"]+)"[^>]*\bdata-then="(?P<then>[^"]*)"'
)
# how long a shown QR code is offered before a fresh one replaces it
QR_EXPIRES_IN = 300.0
# give up on an unscanned login after this long
QR_FLOW_TIMEOUT = 900.0
# poll delays mirroring the login page's own backoff, as (attempts, seconds) steps
QR_POLL_BACKOFF = ((60, 1.0), (180, 5.0), (280, 10.0))
QR_POLL_MAX_INTERVAL = 15.0
# a token that has not been approved yet comes back as a single placeholder character
UNAPPROVED_BODY_LENGTH = 1
CONF_MAX_NUM_EPISODES = "max_num_episodes"

CACHE_CATEGORY_OPML = 1
# feed url -> ISO datetime of the newest progress applied for that feed
CACHE_KEY_LAST_APPLIED = "last_applied_playback_ts"

# Overcast rate limits this endpoint to roughly 10 requests a day, and we only
# fetch when something needs the export (a library sync, browsing or playback),
# so 4 hours works out at no more than 6 requests a day. The old export is still
# used while the new one is on its way, so nothing has to wait for Overcast.
OPML_CACHE_EXPIRATION = 4 * 3600
RATE_LIMIT_FALLBACK_BACKOFF = 3600

# Overcast answers a request carrying a rejected session cookie with a redirect to
# the login page, so any redirect away from the export means the session is gone.
AUTH_REJECT_STATUSES = (301, 302, 303, 307, 308)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
}
