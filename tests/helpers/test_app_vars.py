"""Tests for the app_vars resolver."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from music_assistant.helpers import app_vars as app_vars_module
from music_assistant.helpers.app_vars import APP_VAR_NAMES, app_var


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    """Reset the module-level lookup caches around every test."""
    app_vars_module._bundled.cache_clear()
    app_vars_module._read_json_map.cache_clear()
    yield
    app_vars_module._bundled.cache_clear()
    app_vars_module._read_json_map.cache_clear()


def _bundle_blob(salt: bytes, values: dict[str, str]) -> str:
    """Build an app_secrets.json blob the way the generator does."""
    secrets = {
        name: base64.b64encode(app_vars_module._codec(salt, name, value.encode())).decode()
        for name, value in values.items()
    }
    return json.dumps({"salt": base64.b64encode(salt).decode(), "secrets": secrets})


def test_unprovisioned_returns_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no override, no bundle and no file, an app var resolves to ''."""
    monkeypatch.delenv("MASS_APP_VAR_SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setattr(app_vars_module, "_bundled", lambda: None)
    monkeypatch.setenv("MASS_APP_VARS_FILE", str(tmp_path / "does_not_exist.json"))
    assert app_var("spotify_client_id") == ""


def test_unknown_key_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown name resolves to '' rather than raising."""
    monkeypatch.delenv("MASS_APP_VAR_NOPE_NOT_A_KEY", raising=False)
    monkeypatch.setattr(app_vars_module, "_bundled", lambda: None)
    assert app_var("nope_not_a_key") == ""


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A MASS_APP_VAR_<NAME> override beats a value present in a local file."""
    file = tmp_path / "app_vars.json"
    file.write_text(json.dumps({"spotify_client_id": "from_file"}), encoding="utf-8")
    monkeypatch.setattr(app_vars_module, "_bundled", lambda: None)
    monkeypatch.setenv("MASS_APP_VARS_FILE", str(file))
    monkeypatch.setenv("MASS_APP_VAR_SPOTIFY_CLIENT_ID", "from_env")
    assert app_var("spotify_client_id") == "from_env"


def test_local_json_file_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A maintainer's app_vars.json is read when pointed at by MASS_APP_VARS_FILE."""
    file = tmp_path / "app_vars.json"
    file.write_text(json.dumps({"tidal_client_id": "abc123"}), encoding="utf-8")
    monkeypatch.delenv("MASS_APP_VAR_TIDAL_CLIENT_ID", raising=False)
    monkeypatch.setattr(app_vars_module, "_bundled", lambda: None)
    monkeypatch.setenv("MASS_APP_VARS_FILE", str(file))
    assert app_var("tidal_client_id") == "abc123"


def test_default_local_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When MASS_APP_VARS_FILE is unset, the default data-dir location is used."""
    file = tmp_path / "app_vars.json"
    file.write_text(json.dumps({"qobuz_app_id": "default_loc"}), encoding="utf-8")
    monkeypatch.delenv("MASS_APP_VARS_FILE", raising=False)
    monkeypatch.delenv("MASS_APP_VAR_QOBUZ_APP_ID", raising=False)
    monkeypatch.setattr(app_vars_module, "_bundled", lambda: None)
    monkeypatch.setattr(app_vars_module, "_DEFAULT_LOCAL_FILE", file)
    assert app_var("qobuz_app_id") == "default_loc"


def test_bundled_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real app_secrets.json blob parses and decodes back to the original values."""
    salt = os.urandom(16)
    values = {name: f"value-for-{name}" for name in APP_VAR_NAMES}
    monkeypatch.setattr(app_vars_module, "_bundled_text", lambda: _bundle_blob(salt, values))
    for name, expected in values.items():
        monkeypatch.delenv(f"MASS_APP_VAR_{name.upper()}", raising=False)
        assert app_var(name) == expected


def test_corrupt_bundle_does_not_crash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A malformed bundle resolves to None (and '') instead of raising."""
    monkeypatch.delenv("MASS_APP_VAR_SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setenv("MASS_APP_VARS_FILE", str(tmp_path / "does_not_exist.json"))
    for blob in ("{ not valid json", json.dumps({"secrets": {}}), json.dumps({"salt": "!!"})):
        app_vars_module._bundled.cache_clear()
        monkeypatch.setattr(app_vars_module, "_bundled_text", lambda blob=blob: blob)
        assert app_vars_module._bundled() is None
        assert app_var("spotify_client_id") == ""
