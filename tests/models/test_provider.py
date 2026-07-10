"""Tests for the Provider base class serialization contract."""

from __future__ import annotations

from dataclasses import fields
from typing import cast
from unittest.mock import MagicMock

from music_assistant_models.enums import EventType, ProviderType
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


def test_signal_provider_event() -> None:
    """signal_provider_event() emits a PROVIDER_EVENT with the instance_id as object_id."""
    provider = _make_base_provider()
    provider.signal_provider_event({"foo": "bar"})
    cast("MagicMock", provider.mass).signal_event.assert_called_once_with(
        EventType.PROVIDER_EVENT, object_id="test_instance", data={"foo": "bar"}
    )


def test_signal_provider_event_with_sub_scope() -> None:
    """signal_provider_event() appends the sub_scope to the object_id."""
    provider = _make_base_provider()
    provider.signal_provider_event({"round": 1}, sub_scope="game_state")
    cast("MagicMock", provider.mass).signal_event.assert_called_once_with(
        EventType.PROVIDER_EVENT, object_id="test_instance/game_state", data={"round": 1}
    )
