"""Tests for reading Yandex Music credentials from provider setup data."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import LoginFailed, ResourceTemporarilyUnavailable
from ya_passport_auth import SecretStr

from music_assistant.providers.yandex_ynison.credential_source import YandexMusicCredentialSource


def test_reads_only_setup_owned_tokens() -> None:
    """Credentials must flow through the linked provider's public setup accessor only."""
    owner = MagicMock()
    owner.domain = "yandex_music"
    owner.type = ProviderType.MUSIC
    owner.get_setup_value.side_effect = {
        "token": "music-token",
        "x_token": "x-token",
    }.get
    owner.config.get_value.side_effect = AssertionError("ordinary config must not be read")
    mass = MagicMock()
    mass.get_provider.return_value = owner
    mass.config = MagicMock()

    music_token, x_token = YandexMusicCredentialSource(mass, "ym-primary").read_tokens()

    assert isinstance(music_token, SecretStr)
    assert music_token.get_secret() == "music-token"
    assert isinstance(x_token, SecretStr)
    assert x_token.get_secret() == "x-token"
    assert mass.config.mock_calls == []


def test_preserves_secret_values_and_normalizes_empty_values() -> None:
    """Replacing SecretStr handling must not double-wrap or expose empty credentials."""
    source_secret = SecretStr("music-token")
    owner = MagicMock()
    owner.domain = "yandex_music"
    owner.type = ProviderType.MUSIC
    owner.get_setup_value.side_effect = {"token": source_secret, "x_token": ""}.get
    mass = MagicMock()
    mass.get_provider.return_value = owner

    music_token, x_token = YandexMusicCredentialSource(mass, "ym-primary").read_tokens()

    assert music_token is source_secret
    assert x_token is None


def test_unloaded_owner_is_temporarily_unavailable() -> None:
    """Treating load order as invalid auth must not force an unnecessary reconfigure."""
    mass = MagicMock()
    mass.get_provider.return_value = None

    with pytest.raises(ResourceTemporarilyUnavailable, match="not loaded yet"):
        YandexMusicCredentialSource(mass, "ym-primary").read_tokens()


def test_resolves_exact_configured_owner_including_unavailable() -> None:
    """An available instance must not replace the exact configured owner."""
    configured_owner = MagicMock(domain="yandex_music", type=ProviderType.MUSIC)
    configured_owner.get_setup_value.side_effect = {
        "token": "configured-token",
        "x_token": None,
    }.get
    fallback_owner = MagicMock(domain="yandex_music", type=ProviderType.MUSIC)
    fallback_owner.get_setup_value.side_effect = {
        "token": "fallback-token",
        "x_token": None,
    }.get
    mass = MagicMock()
    mass.get_provider.side_effect = lambda _instance_id, return_unavailable=False: (
        configured_owner if return_unavailable else fallback_owner
    )

    music_token, _ = YandexMusicCredentialSource(mass, "ym-configured").read_tokens()

    assert music_token is not None
    assert music_token.get_secret() == "configured-token"
    mass.get_provider.assert_called_once_with("ym-configured", return_unavailable=True)


def test_rejects_non_yandex_music_owner() -> None:
    """Dropping domain/type validation must not allow credentials from another provider."""
    for domain, provider_type in (
        ("spotify", ProviderType.MUSIC),
        ("yandex_music", ProviderType.PLUGIN),
    ):
        mass = MagicMock()
        mass.get_provider.return_value = SimpleNamespace(
            domain=domain,
            type=provider_type,
            get_setup_value=lambda _key: "token",
        )

        with pytest.raises(LoginFailed, match="Reconfigure this Ynison instance"):
            YandexMusicCredentialSource(mass, "wrong-owner").read_tokens()


def test_rejects_owner_without_setup_data_accessor() -> None:
    """Falling back to ordinary config must not hide an incompatible owner API."""
    mass = MagicMock()
    mass.get_provider.return_value = SimpleNamespace(
        domain="yandex_music",
        type=ProviderType.MUSIC,
    )

    with pytest.raises(LoginFailed, match="Upgrade Music Assistant or the Yandex Music provider"):
        YandexMusicCredentialSource(mass, "ym-primary").read_tokens()
