"""Tests for MusicAssistant.get_provider stale instance fallback."""

from __future__ import annotations

from unittest.mock import Mock

from music_assistant.mass import MusicAssistant


def _make_provider(
    instance_id: str, domain: str, *, available: bool = True, streaming: bool = True
) -> Mock:
    """Create a minimal provider mock."""
    prov = Mock()
    prov.instance_id = instance_id
    prov.domain = domain
    prov.available = available
    prov.is_streaming_provider = streaming
    return prov


def _make_mass(*providers: Mock) -> MusicAssistant:
    """Create a MusicAssistant with only _providers populated."""
    mass = MusicAssistant.__new__(MusicAssistant)
    mass._providers = {p.instance_id: p for p in providers}
    return mass


def test_lookup_by_instance_id() -> None:
    """Direct instance ID lookup returns the provider."""
    prov = _make_provider("ytmusic--abc123", "ytmusic")
    mass = _make_mass(prov)
    assert mass.get_provider("ytmusic--abc123") is prov


def test_lookup_by_domain() -> None:
    """Domain lookup returns the first matching provider."""
    prov = _make_provider("ytmusic--abc123", "ytmusic")
    mass = _make_mass(prov)
    assert mass.get_provider("ytmusic") is prov


def test_stale_streaming_instance_falls_back_to_new_instance() -> None:
    """A deleted streaming provider instance falls back to another instance of the same domain."""
    new_prov = _make_provider("ytmusic--newXYZ", "ytmusic", streaming=True)
    mass = _make_mass(new_prov)
    assert mass.get_provider("ytmusic--oldABC") is new_prov


def test_stale_nonstreaming_instance_returns_none() -> None:
    """A deleted non-streaming provider instance does not fall back."""
    prov = _make_provider("filesystem_smb--abc123", "filesystem_smb", streaming=False)
    mass = _make_mass(prov)
    assert mass.get_provider("filesystem_smb--oldXYZ") is None


def test_domain_lookup_nonstreaming_still_works() -> None:
    """Plain domain lookup for non-streaming providers is not broken by the stale-instance logic."""
    prov = _make_provider("filesystem_smb--abc123", "filesystem_smb", streaming=False)
    mass = _make_mass(prov)
    assert mass.get_provider("filesystem_smb") is prov


def test_unavailable_streaming_instance_falls_back() -> None:
    """An unavailable (but registered) streaming provider falls back to another instance."""
    old = _make_provider("ytmusic--old", "ytmusic", available=False, streaming=True)
    new = _make_provider("ytmusic--new", "ytmusic", available=True, streaming=True)
    mass = _make_mass(old, new)
    assert mass.get_provider("ytmusic--old") is new


def test_unavailable_nonstreaming_returns_none() -> None:
    """An unavailable non-streaming provider returns None (no cross-instance fallback)."""
    prov = _make_provider("filesystem_smb--abc", "filesystem_smb", available=False, streaming=False)
    mass = _make_mass(prov)
    assert mass.get_provider("filesystem_smb--abc") is None


def test_no_provider_returns_none() -> None:
    """Completely unknown provider returns None."""
    mass = _make_mass()
    assert mass.get_provider("nonexistent--xyz") is None
    assert mass.get_provider("nonexistent") is None
