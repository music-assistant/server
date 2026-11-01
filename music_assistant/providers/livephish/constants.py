"""
Live Phish auth values taken from the live web player on 2026-10-30.

https://plus.livephish.com/ (LivePhish Web Player) sends an unauthenticated
browser to the identity server with ``CLIENT_ID`` and ``SCOPE``. That player's
SDK then POSTs the authorization code to ``TOKEN_URL`` as
``application/x-www-form-urlencoded`` with ``grant_type=authorization_code``,
``redirect_uri``, and a PKCE ``code_verifier`` (S256). The same token endpoint
is published at
``https://id.livephish.com/.well-known/openid-configuration``.

After the token is issued, that client calls Live Phish hosts:

- ``GET https://subscriptions.livephish.com/api/v3/me/subscriptions``
- ``GET https://stash.livephish.com/api/v1/stash``
- ``https://catalog.livephish.com/api/v1`` for search, shows, and recommendations
- ``https://stash.livephish.com/api/v1/me`` for library artists, releases, and playlists
- ``https://streamapi.livephish.com/bigriver/subplayer.aspx`` for playback
- ``https://streamapi.livephish.com/api.aspx`` for legacy song search

The identity server lists ``password`` in ``grant_types_supported``, but this
client answers the token endpoint with ``unauthorized_client`` for that grant.
The player does not use it. Login posts the identity server's own email form,
then exchanges the authorization code with PKCE.

Two other public clients on the same issuer, captured from their authorize
redirects, are not the player that performs this token POST:

- ``https://get.livephish.com/`` (the site's Start Streaming link):
  client id ``svYoGjyg2fR8vqXx2clk``, scope
  ``openid email offline_access nugsnet:api nugsnet:legacyapi``.
- ``https://account.livephish.com/``: client id ``lp5d6a7847f342fda524``,
  same narrower scope.
"""

from __future__ import annotations

TOKEN_URL = "https://id.livephish.com/connect/token"  # noqa: S105 - URL
AUTHORIZE_URL = "https://id.livephish.com/connect/authorize"
CLIENT_ID = "xHM339sCz41V"
SCOPE = "openid profile email roles offline_access nugsnet:api nugsnet:legacyapi"
GRANT_TYPE = "authorization_code"
REDIRECT_URI = "https://plus.livephish.com/signin-oidc.html"

# Host the player uses for the authenticated account call (subscriptions API v3).
ACCOUNT_HOST = "https://subscriptions.livephish.com"
ACCOUNT_URL = f"{ACCOUNT_HOST}/api/v3/me/subscriptions"

# Catalog and library hosts from the same client. Paths are /api/v1.
# Library routes sit under /me on the stash host.
CATALOG_HOST = "https://catalog.livephish.com"
CATALOG_API = f"{CATALOG_HOST}/api/v1"
STASH_HOST = "https://stash.livephish.com"
STASH_API = f"{STASH_HOST}/api/v1"
STASH_URL = f"{STASH_API}/stash"

# Legacy stream and search endpoints the player still ships.
STREAM_API_URL = "https://streamapi.livephish.com/bigriver/subplayer.aspx"
LEGACY_API_URL = "https://streamapi.livephish.com/api.aspx"

# Relative cover paths in show payloads are resolved against this CDN.
IMAGE_CDN = "https://s3.amazonaws.com/static.nugs.net"
