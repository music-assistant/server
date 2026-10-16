"""Tests for Subsonic app password derivation and verification."""

from __future__ import annotations

from music_assistant.providers.subsonic_server.auth import (
    APP_PASSWORD_LENGTH,
    derive_app_password,
    generate_secret,
    verify_credentials,
)


def test_derive_app_password_is_stable_per_user() -> None:
    """The same secret and user always give the same password, other users another one."""
    secret = generate_secret()
    password = derive_app_password(secret, "user-1")
    assert password == derive_app_password(secret, "user-1")
    assert password != derive_app_password(secret, "user-2")
    assert len(password) == APP_PASSWORD_LENGTH


def test_new_secret_changes_password() -> None:
    """Regenerating the secret issues a different password."""
    assert derive_app_password(generate_secret(), "user-1") != derive_app_password(
        generate_secret(), "user-1"
    )


def test_token_auth_matches_subsonic_spec_example() -> None:
    """Token auth follows the example of the Subsonic API documentation."""
    assert verify_credentials("sesame", token="26719a1196d2a940705a59634eb18eab", salt="c19b2d")
    assert verify_credentials("sesame", token="26719A1196D2A940705A59634EB18EAB", salt="c19b2d")
    assert not verify_credentials("sesame", token="26719a1196d2a940705a59634eb18eab", salt="other")


def test_token_auth_takes_precedence_over_password() -> None:
    """A wrong token is rejected even when the plain password is correct."""
    assert not verify_credentials("sesame", password="sesame", token="0" * 32, salt="c19b2d")


def test_plain_and_hex_encoded_password() -> None:
    """The ``p`` parameter is accepted plain and hex encoded."""
    assert verify_credentials("sesame", password="sesame")
    assert verify_credentials("sesame", password="enc:736573616d65")
    assert not verify_credentials("sesame", password="wrong")
    assert not verify_credentials("sesame", password="enc:zz")
    assert not verify_credentials("sesame")
