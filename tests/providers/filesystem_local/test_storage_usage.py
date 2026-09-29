"""Tests for the storage locations a Local files source set up through its flow counts as using."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import FlowStepType
from music_assistant_models.errors import ActionUnavailable
from music_assistant_models.provider import ProviderManifest

from music_assistant.constants import CONF_STORAGE_FOLDERS
from music_assistant.controllers.storage import StorageController, StorageKind
from music_assistant.controllers.storage import controller as controller_module
from music_assistant.controllers.webserver.helpers.auth_middleware import set_current_user
from music_assistant.mass import MusicAssistant
from music_assistant.providers import filesystem_local
from music_assistant.providers.filesystem_local.constants import CONF_CONTENT_TYPE
from tests.controllers.storage.conftest import make_location, set_locations


async def test_a_source_set_up_through_the_flow_uses_its_location(
    mass_minimal: MusicAssistant,
    storage: StorageController,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The storage finds a source the flow created as a user of its location, loaded or not."""
    media = tmp_path / "media"
    (media / "Music").mkdir(parents=True)
    # a registered folder as the only location, with no mount table to discover others from
    monkeypatch.setattr(controller_module, "read_mountinfo", lambda: "")
    mass_minimal.config.set(CONF_STORAGE_FOLDERS, [str(media)])
    set_locations(storage, make_location(media, kind=StorageKind.MANUAL))
    mass_minimal._provider_manifests["filesystem_local"] = await ProviderManifest.parse(
        str(Path(filesystem_local.__file__).parent / "manifest.json")
    )
    # the flow finish touches the music controller once the source is created
    mass_minimal.music = MagicMock()
    set_current_user(None)
    try:
        with patch.object(mass_minimal, "load_provider_config", AsyncMock()):
            step = await mass_minimal.config.setup_provider("filesystem_local")
            finished = await mass_minimal.config.submit_setup_flow(
                step.flow_id, {CONF_CONTENT_TYPE: "music", "path": str(media / "Music")}
            )
    finally:
        if (sweep_handle := mass_minimal.config._flow_sweep_handle) is not None:
            sweep_handle.cancel()
    assert finished.type == FlowStepType.FINISH

    await storage.refresh()

    location = next(loc for loc in storage.get_locations() if loc.path == str(media))
    assert location.used_by == ["Local files"]
    with pytest.raises(ActionUnavailable) as exc_info:
        await storage.remove_local_folder(str(media))
    assert exc_info.value.translation_key == "location_in_use"
