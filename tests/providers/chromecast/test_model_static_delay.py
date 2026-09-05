"""
Tests for the per-model Sendspin static delay defaults of Cast devices.

The lookup is an exact match on the manufacturer and model a device reports:
``model`` comes from the mDNS ``md=`` field and ``manufacturer`` from the DIAL
device description, both stored on the player's ``device_info``. Unlike
``SENDSPIN_CAST_BLOCKLIST`` this table holds no wildcards, so an entry only ever
applies to the exact pair of strings it is keyed on.
"""

from __future__ import annotations

from music_assistant.providers.chromecast.constants import (
    CAST_FALLBACK_STATIC_DELAY,
    get_cast_model_static_delay,
)


def test_lg_spx_uses_its_measured_delay() -> None:
    """The LG SPx soundbar gets its own measured delay instead of the fallback."""
    delay = get_cast_model_static_delay("LG Electronics", "LG SPx")
    assert delay == 370
    assert delay != CAST_FALLBACK_STATIC_DELAY


def test_known_google_model_is_unaffected() -> None:
    """Adding a non-Google entry leaves the existing Google defaults in place."""
    assert get_cast_model_static_delay("Google Inc.", "Google Nest Mini") == 427


def test_unknown_model_uses_the_fallback() -> None:
    """A model without a measurement falls back to the generic default."""
    assert get_cast_model_static_delay("LG Electronics", "LG S95QR") == CAST_FALLBACK_STATIC_DELAY


def test_manufacturer_must_match_as_well() -> None:
    """The lookup keys on both strings: there is no wildcard for the manufacturer."""
    assert get_cast_model_static_delay("LG", "LG SPx") == CAST_FALLBACK_STATIC_DELAY


def test_missing_device_info_uses_the_fallback() -> None:
    """
    Empty strings resolve to the fallback rather than raising.

    The Sendspin bridge passes ``device_info.manufacturer or ""`` and
    ``device_info.model or ""``, so both can reach the lookup empty for a device
    whose DIAL description has not been fetched yet.
    """
    assert get_cast_model_static_delay("", "") == CAST_FALLBACK_STATIC_DELAY
