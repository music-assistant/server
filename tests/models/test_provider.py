"""Tests for the Provider base class serialization contract."""

from __future__ import annotations

from dataclasses import fields
from unittest.mock import MagicMock

from music_assistant_models.enums import ProviderType
from music_assistant_models.provider import ProviderInstance

from music_assistant.models.provider import Provider


def _make_base_provider() -> Provider:
    """Construct a minimal base Provider with stubbed mass/manifest/config."""
    mass = MagicMock()
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test_provider"
    config = MagicMock()
    config.name = "Test Provider"
    config.instance_id = "test_instance"
    config.get_value = MagicMock(return_value="GLOBAL")
    return Provider(mass, manifest, config, supported_features=set())


def test_to_dict_matches_provider_instance_schema() -> None:
    """to_dict() emits exactly the fields declared by the ProviderInstance model."""
    result = _make_base_provider().to_dict()
    assert set(result) == {f.name for f in fields(ProviderInstance)}
    # the served payload must also deserialize back into the model
    ProviderInstance.from_dict(result)
