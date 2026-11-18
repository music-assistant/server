"""Tests for the manifests in the list of community providers."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from music_assistant.controllers.community_catalog.listing import (
    available_provider,
    installed_manifest,
)
from music_assistant.controllers.community_catalog.models import (
    IndexProvider,
    IndexStatus,
    IndexVersion,
)
from tests.controllers.community_catalog.conftest import ADDED, RELEASED

if TYPE_CHECKING:
    from pathlib import Path

DOMAIN = "comm_frame"
VALID = {
    "id": "send_to_frame",
    "label": "Show on the picture frame",
    "media_types": ["album"],
    "scope": "library.read",
}
# one valid action next to a broken one, an unknown scope and a label of two lines
MANIFEST: dict[str, Any] = {
    "type": "plugin",
    "domain": DOMAIN,
    "name": "Frame",
    "description": "test provider",
    "codeowners": ["@someone"],
    "menu_actions": [
        {"id": "broken", "label": None},
        VALID,
        {**VALID, "id": "unknown_scope", "scope": "everything"},
        {**VALID, "id": "two_lines", "label": "Show\non the frame"},
    ],
}
LOGGER = logging.getLogger(__name__)


def test_index_manifest_carries_only_checked_actions() -> None:
    """A provider of the index is listed with only the menu actions that pass the rules."""
    entry = IndexProvider(
        status=IndexStatus.LISTED,
        repository="someone/frame",
        manifest=MANIFEST,
        added=ADDED,
        latest="1.0.0",
        versions={"1.0.0": IndexVersion("commit", DOMAIN, "hash", [], RELEASED)},
    )

    provider = available_provider(DOMAIN, entry, LOGGER)

    assert provider is not None
    assert [action.id for action in provider.manifest.menu_actions] == ["send_to_frame"]


async def test_folder_manifest_carries_only_checked_actions(tmp_path: Path) -> None:
    """An installed provider listed from its folder carries only the checked menu actions."""
    folder = tmp_path / DOMAIN
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps(MANIFEST))
    mass = MagicMock()
    mass.get_provider_manifests.return_value = []
    installer = MagicMock()
    installer.live_folder.return_value = folder

    manifest = await installed_manifest(mass, installer, DOMAIN, None, LOGGER)

    assert manifest is not None
    assert [action.id for action in manifest.menu_actions] == ["send_to_frame"]
