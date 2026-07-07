"""Tests for the dedicated SECURE_STRING encryption key and legacy secret migration."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken

from music_assistant.constants import (
    CONF_ENCRYPTION_KEY,
    CONF_ENCRYPTION_KEY_MIGRATED,
    CONF_SERVER_ID,
    ENCRYPT_SUFFIX,
)
from music_assistant.controllers.config import ConfigController


def _make_controller(tmp_path: Path) -> ConfigController:
    mass = SimpleNamespace(storage_path=str(tmp_path))
    controller = ConfigController(mass)  # type: ignore[arg-type]
    controller.initialized = True
    controller.save = lambda **_kwargs: None  # type: ignore[method-assign]
    return controller


def _legacy_fernet(server_id: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(server_id.encode()[:32]))


def test_encryption_key_not_derived_from_server_id(tmp_path: Path) -> None:
    """The active key must not be the public, low-entropy server_id-derived key."""
    controller = _make_controller(tmp_path)
    server_id = uuid4().hex
    controller.set(CONF_SERVER_ID, server_id)
    controller._init_encryption()

    stored_key = controller.get(CONF_ENCRYPTION_KEY)
    assert stored_key
    assert stored_key != base64.urlsafe_b64encode(server_id.encode()[:32]).decode()


def test_encrypt_decrypt_round_trip(tmp_path: Path) -> None:
    """A value encrypted with the new key decrypts back to the original."""
    controller = _make_controller(tmp_path)
    controller.set(CONF_SERVER_ID, uuid4().hex)
    controller._init_encryption()

    encrypted = controller.encrypt_string("s3cr3t-token")
    assert encrypted.startswith(ENCRYPT_SUFFIX)
    assert controller.decrypt_string(encrypted) == "s3cr3t-token"


def test_legacy_encrypted_value_still_decrypts(tmp_path: Path) -> None:
    """Secrets encrypted with the old server_id-derived key must still decrypt after setup."""
    controller = _make_controller(tmp_path)
    server_id = uuid4().hex
    controller.set(CONF_SERVER_ID, server_id)
    legacy_value = ENCRYPT_SUFFIX + _legacy_fernet(server_id).encrypt(b"old-secret").decode()
    controller.set("providers/foo/token", legacy_value)

    controller._init_encryption()
    assert controller.decrypt_string(controller.get("providers/foo/token")) == "old-secret"


def test_existing_secrets_migrated_off_legacy_key(tmp_path: Path) -> None:
    """Secrets stored with the legacy key are proactively re-encrypted on setup."""
    controller = _make_controller(tmp_path)
    server_id = uuid4().hex
    controller.set(CONF_SERVER_ID, server_id)
    legacy_value = ENCRYPT_SUFFIX + _legacy_fernet(server_id).encrypt(b"old-secret").decode()
    controller.set("providers/foo/token", legacy_value)

    controller._init_encryption()

    migrated = controller.get("providers/foo/token")
    assert migrated != legacy_value
    # the migrated token must decrypt with the new key alone, not just the legacy one
    new_key: str = controller.get(CONF_ENCRYPTION_KEY)
    token = migrated[len(ENCRYPT_SUFFIX) :].encode()
    assert Fernet(new_key.encode()).decrypt(token) == b"old-secret"
    with pytest.raises(InvalidToken):
        _legacy_fernet(server_id).decrypt(token)


def test_invalid_stored_key_regenerates_without_crash(tmp_path: Path) -> None:
    """A corrupted/invalid stored encryption_key must not crash startup; a new key is generated."""
    controller = _make_controller(tmp_path)
    controller.set(CONF_SERVER_ID, uuid4().hex)
    controller.set(CONF_ENCRYPTION_KEY, "not-a-valid-fernet-key")

    controller._init_encryption()

    new_key = controller.get(CONF_ENCRYPTION_KEY)
    assert new_key != "not-a-valid-fernet-key"
    round_tripped = controller.decrypt_string(controller.encrypt_string("s3cr3t"))
    assert round_tripped == "s3cr3t"


def test_non_string_stored_key_regenerates_without_crash(tmp_path: Path) -> None:
    """A non-string encryption_key (e.g. settings hand-edited to a number) must not crash startup."""
    controller = _make_controller(tmp_path)
    controller.set(CONF_SERVER_ID, uuid4().hex)
    controller.set(CONF_ENCRYPTION_KEY, 12345)

    controller._init_encryption()

    new_key = controller.get(CONF_ENCRYPTION_KEY)
    assert isinstance(new_key, str)
    assert controller.decrypt_string(controller.encrypt_string("s3cr3t")) == "s3cr3t"


def test_legacy_secret_survives_invalid_stored_key(tmp_path: Path) -> None:
    """Even with an invalid stored key, legacy server_id-encrypted secrets must still decrypt."""
    controller = _make_controller(tmp_path)
    server_id = uuid4().hex
    controller.set(CONF_SERVER_ID, server_id)
    controller.set(CONF_ENCRYPTION_KEY, "")
    legacy_value = ENCRYPT_SUFFIX + _legacy_fernet(server_id).encrypt(b"old-secret").decode()
    controller.set("providers/foo/token", legacy_value)

    controller._init_encryption()

    assert controller.decrypt_string(controller.get("providers/foo/token")) == "old-secret"


def test_secrets_migrated_only_once(tmp_path: Path) -> None:
    """After the one-time migration, later inits must not re-scan or re-migrate stored values."""
    controller = _make_controller(tmp_path)
    server_id = uuid4().hex
    controller.set(CONF_SERVER_ID, server_id)
    controller._init_encryption()
    assert controller.get(CONF_ENCRYPTION_KEY_MIGRATED) is True

    # a legacy-encrypted value planted after migration completed is left untouched next init
    legacy_value = ENCRYPT_SUFFIX + _legacy_fernet(server_id).encrypt(b"late").decode()
    controller.set("providers/foo/token", legacy_value)
    controller._init_encryption()
    assert controller.get("providers/foo/token") == legacy_value


def test_invalid_key_regen_remigrates_legacy_secrets(tmp_path: Path) -> None:
    """Regenerating an invalid key must re-run migration so legacy secrets move onto the new key."""
    controller = _make_controller(tmp_path)
    server_id = uuid4().hex
    controller.set(CONF_SERVER_ID, server_id)
    controller.set(CONF_ENCRYPTION_KEY, "not-a-valid-fernet-key")
    controller.set(CONF_ENCRYPTION_KEY_MIGRATED, True)
    legacy_value = ENCRYPT_SUFFIX + _legacy_fernet(server_id).encrypt(b"old-secret").decode()
    controller.set("providers/foo/token", legacy_value)

    controller._init_encryption()

    migrated = controller.get("providers/foo/token")
    assert migrated != legacy_value
    assert controller.decrypt_string(migrated) == "old-secret"


def test_new_encryption_not_readable_with_legacy_key(tmp_path: Path) -> None:
    """New values must be encrypted with the new key, not the public legacy one."""
    controller = _make_controller(tmp_path)
    server_id = uuid4().hex
    controller.set(CONF_SERVER_ID, server_id)
    controller._init_encryption()

    encrypted = controller.encrypt_string("new-secret")
    token = encrypted.replace(ENCRYPT_SUFFIX, "").encode()

    with pytest.raises(InvalidToken):
        _legacy_fernet(server_id).decrypt(token)
