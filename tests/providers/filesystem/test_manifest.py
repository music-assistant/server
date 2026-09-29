"""Tests for the manifest of the local filesystem provider."""

from __future__ import annotations

from pathlib import Path

from music_assistant_models.provider import ProviderManifest

from music_assistant.providers import filesystem_local


async def test_members_can_set_up_a_local_files_source() -> None:
    """
    Members may add a Local files source of their own.

    That is safe because the setup flow checks every picked folder on the server: it must lie
    in a storage location the member may use (tests/providers/filesystem_local/test_setup_flow.py).
    """
    manifest = await ProviderManifest.parse(
        str(Path(filesystem_local.__file__).parent / "manifest.json")
    )

    assert manifest.self_service is True
