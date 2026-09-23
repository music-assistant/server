"""Tests for the manifest of the local filesystem provider."""

from __future__ import annotations

from pathlib import Path

from music_assistant_models.provider import ProviderManifest

from music_assistant.providers import filesystem_local


async def test_members_can_not_set_up_a_local_disk_source() -> None:
    """Local disk opts out of self-service, as its setup takes any folder on the server."""
    manifest = await ProviderManifest.parse(
        str(Path(filesystem_local.__file__).parent / "manifest.json")
    )

    assert manifest.self_service is False
