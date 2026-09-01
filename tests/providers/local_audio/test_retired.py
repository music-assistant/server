"""Tests for the local_audio tombstone: the retired provider refuses to load."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ProviderStatus
from music_assistant_models.errors import UnsupportedSystemError

from music_assistant.controllers.config.helpers import _provider_status
from music_assistant.mass import _provider_error_from_exc
from music_assistant.providers.local_audio import setup
from tests.controllers.config.test_provider_status import _conf

MANIFEST = Path("music_assistant/providers/local_audio/manifest.json")
STRINGS = Path("music_assistant/providers/local_audio/strings.json")


async def test_setup_raises_the_retirement_notice() -> None:
    """Loading the retired provider fails with a localizable retirement error."""
    with pytest.raises(UnsupportedSystemError) as excinfo:
        await setup(MagicMock(), MagicMock(), MagicMock())
    err = excinfo.value
    assert err.translation_key == "provider_retired"
    assert err.translation_owner == "provider.local_audio"


async def test_a_retired_config_lands_as_incompatible() -> None:
    """The failure maps to INCOMPATIBLE, which is never retried and offers Remove."""
    with pytest.raises(UnsupportedSystemError) as excinfo:
        await setup(MagicMock(), MagicMock(), MagicMock())
    last_error = _provider_error_from_exc(excinfo.value)
    assert _provider_status(_conf(last_error=last_error), is_loaded=False) == (
        ProviderStatus.INCOMPATIBLE
    )
    assert "retired" in last_error.message


def test_manifest_carries_no_load_bearing_flags() -> None:
    """The tombstone must not be builtin, depend on sendspin, or pull requirements."""
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["stage"] == "deprecated"
    assert manifest["requirements"] == []
    # builtin would re-create the config on every boot and block the Remove button;
    # depends_on would make the load fail silently before setup() ever runs
    assert "builtin" not in manifest
    assert "depends_on" not in manifest
    assert "allow_disable" not in manifest


def test_strings_cover_both_surfaces() -> None:
    """The notice is defined for the failed load and for the add-provider flow."""
    strings = json.loads(STRINGS.read_text())
    assert "provider_retired" in strings["errors"]
    assert "provider_retired" in strings["setup_flow"]["abort"]
    assert "config_entries" not in strings
