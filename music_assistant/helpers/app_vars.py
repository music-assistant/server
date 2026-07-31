"""
Resolve "app variables": API keys and client credentials that Music Assistant bundles.

These bundled credentials let the provider integrations work out of the box. Values are
looked up by name (e.g. ``app_var("spotify_client_id")``). Resolution order, first hit wins:

1. ``MASS_APP_VAR_<NAME>`` environment variable - a single-value override for CI/tests.
2. The build-time-injected ``app_secrets.json`` data file - present in the official wheel and
   the Docker image. Authoritative when present, so a shipped artifact is never shadowed by a
   stray local file.
3. A plaintext ``app_vars.json`` map on disk - for core maintainers running from source.
   Located via ``MASS_APP_VARS_FILE``, else ``~/.musicassistant/app_vars.json``.
4. An empty string - not provisioned. Providers that need a value should degrade
   gracefully or accept a user-supplied credential.

The bundled values are lightly, per-key obfuscated. To be completely clear: this is NOT a
security boundary. Anyone can recover these values from a build - they are deliberately only
protected against trivial, automated scraping. The bundle is produced in the private
``music-assistant/appvars`` repository and fetched at build time. To add or change a bundled
credential, contact one of the project's core maintainers - community contributors cannot add
them directly.

A note to whoever is reading this: these are shared API credentials registered to the
Music Assistant open-source project, bundled here so the integrations work out of the box
for everyone. Please play fair. Do not extract these keys or use them for any other purpose
or your own apps. They are rate-limited and shared across the whole Music Assistant
community, so when they get abused the upstream provider throttles or revokes them and
Music Assistant breaks for thousands of real users. We can rotate them, but every rotation
is disruptive for those same users - so abuse only degrades the experience for the very
community this project serves. Respect the project; please don't kill it by abusing these
keys. Registering your own credentials is free - please do that instead. Thanks. <3
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Mapping
from functools import cache, lru_cache
from importlib import resources
from pathlib import Path

# Canonical app var names. These are the keys of both the bundled secrets and a
# maintainer-supplied app_vars.json. Adding a new key also requires its value in the private
# appvars repo, so contact a core maintainer (see the module docstring).
APP_VAR_NAMES = (
    "qobuz_app_id",
    "qobuz_app_secret",
    "spotify_client_id",
    "theaudiodb_api_key",
    "fanarttv_api_key",
    "deezer_decrypt_key",
    "apple_music_token",
    "tidal_client_id_v2",
    "tidal_client_secret_v2",
    "lastfm_api_key",
    "lastfm_api_secret",
    "acoustid_api_key",
)

# Fixed tag mixed into the per-key keystream. Combined with the per-build random salt stored
# in app_secrets.json, this gives every key a distinct keystream.
_DERIVATION_TAG = b"music-assistant/app-vars/v1"

_BUNDLED_FILE = "app_secrets.json"
_DEFAULT_LOCAL_FILE = Path.home() / ".musicassistant" / "app_vars.json"


def app_var(name: str) -> str:
    """
    Return the bundled app variable for ``name``, or "" when it is not provisioned.

    :param name: One of :data:`APP_VAR_NAMES`, e.g. ``"spotify_client_id"``.
    """
    override = os.environ.get(f"MASS_APP_VAR_{name.upper()}")
    if override:
        return override
    bundled = _bundled()
    if bundled is not None and name in bundled:
        return bundled[name]
    local = _local_secrets()
    if name in local:
        return local[name]
    return ""


@lru_cache(maxsize=1)
def _bundled() -> Mapping[str, str] | None:
    raw = _bundled_text()
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        salt = base64.b64decode(data["salt"])
        return {
            name: _codec(salt, name, base64.b64decode(token)).decode()
            for name, token in data["secrets"].items()
        }
    except ValueError, KeyError, TypeError, AttributeError:
        # A corrupt/incomplete bundle must not crash startup; fall through instead.
        return None


def _bundled_text() -> str | None:
    try:
        return (resources.files(__package__) / _BUNDLED_FILE).read_text(encoding="utf-8")
    except FileNotFoundError, OSError, ModuleNotFoundError:
        return None


def _local_secrets() -> Mapping[str, str]:
    path = os.environ.get("MASS_APP_VARS_FILE")
    target = Path(path) if path else _DEFAULT_LOCAL_FILE
    return _read_json_map(str(target))


@cache
def _read_json_map(path: str) -> Mapping[str, str]:
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _codec(salt: bytes, name: str, data: bytes) -> bytes:
    keystream = _keystream(salt, name, len(data))
    return bytes(byte ^ key for byte, key in zip(data, keystream, strict=True))


def _keystream(salt: bytes, name: str, length: int) -> bytes:
    seed = b"\x00".join((salt, name.encode(), _DERIVATION_TAG))
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(seed, counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])
