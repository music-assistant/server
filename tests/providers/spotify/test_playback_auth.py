"""
Tests for the Spotify provider's playback (librespot) authorization.

Spotify's login5 endpoint only accepts a stored credential minted with the same client id
librespot presents, so the playback credential is obtained separately from the Web API tokens
and installed into librespot's cache directory on load. An install without one cannot stream
and must be sent back through the setup flow.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import LoginFailed

from music_assistant.providers.spotify.constants import (
    CONF_LIBRESPOT_CREDENTIALS,
    CREDENTIALS_FILE,
)
from music_assistant.providers.spotify.provider import SpotifyProvider

STORED_CREDENTIALS = '{"username": "tester", "auth_type": 1, "auth_data": "blob"}'


def _make_provider(credentials: str | None, cache_dir: str) -> SpotifyProvider:
    """Return a SpotifyProvider (bypassing __init__) with the given stored credential."""
    prov = object.__new__(SpotifyProvider)
    config = MagicMock(instance_id="spotify--test")
    config.get_value = MagicMock(return_value=None)
    config.values = {}
    prov.config = config
    prov.manifest = MagicMock(domain="spotify")
    prov.logger = MagicMock()
    prov.available = True
    prov.cache_dir = cache_dir
    prov._librespot_bin = "/bin/librespot"
    setup_data = {CONF_LIBRESPOT_CREDENTIALS: credentials} if credentials is not None else {}
    mass = MagicMock()
    # get_setup_value reads the live setup_data blob from the store
    mass.config.get = MagicMock(return_value=setup_data)
    mass.config.get_raw_provider_config_value = MagicMock(return_value=None)
    # the store keeps values encrypted; decrypt is an identity map for the test
    mass.config.decrypt_string = MagicMock(side_effect=lambda value: value)
    prov.mass = mass
    return prov


async def test_stored_credential_is_installed_for_librespot(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """The stored credential is written to librespot's cache so login5 accepts it."""
    cache_dir = Path(str(tmp_path)) / "cache"
    prov = _make_provider(STORED_CREDENTIALS, str(cache_dir))
    await prov._setup_librespot_auth()
    written = json.loads((cache_dir / CREDENTIALS_FILE).read_text(encoding="utf-8"))
    assert written["auth_data"] == "blob"


async def test_stale_cached_credential_is_replaced(tmp_path: pytest.TempPathFactory) -> None:
    """A credential left in the cache from an earlier (now rejected) mint is overwritten."""
    cache_dir = Path(str(tmp_path)) / "cache"
    cache_dir.mkdir()
    credentials_file = cache_dir / CREDENTIALS_FILE
    credentials_file.write_text('{"username": "tester", "auth_data": "stale"}', encoding="utf-8")
    prov = _make_provider(STORED_CREDENTIALS, str(cache_dir))
    await prov._setup_librespot_auth()
    written = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert written["auth_data"] == "blob"


async def test_missing_credential_requires_reauth(tmp_path: pytest.TempPathFactory) -> None:
    """Without a stored credential the provider fails with an auth error (AUTH_REQUIRED)."""
    prov = _make_provider(None, os.path.join(str(tmp_path), "cache"))
    with pytest.raises(LoginFailed) as err:
        await prov._setup_librespot_auth()
    # the auth-flavoured error code is what maps the provider to AUTH_REQUIRED, and the
    # translation key is what the user actually reads
    assert err.value.translation_key == "playback_auth_required"
