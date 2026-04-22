"""Tests for the multi-player provider logic.

Provider module imports music_assistant.models which is only available
when running inside MA.  We test the pure utility functions directly
and guard the full-provider import with pytest.importorskip.
"""

from __future__ import annotations

import uuid

import pytest

from music_assistant.providers.dlna_receiver.constants import (
    CONF_TARGET_PLAYER,
    CONF_TARGET_PLAYERS,
    UDN_NAMESPACE,
)
from music_assistant.providers.dlna_receiver.renderer import UPnPRenderer


def _deterministic_udn(player_id: str) -> str:
    """Replicate the UDN generation logic for standalone testing."""
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, UDN_NAMESPACE)
    return f"uuid:{uuid.uuid5(namespace, player_id or '__default__')}"


def test_deterministic_udn_same_input() -> None:
    """Same player_id always produces the same UDN."""
    udn1 = _deterministic_udn("player_kitchen")
    udn2 = _deterministic_udn("player_kitchen")
    assert udn1 == udn2
    assert udn1.startswith("uuid:")


def test_deterministic_udn_different_inputs() -> None:
    """Different player_ids produce different UDNs."""
    udn1 = _deterministic_udn("player_kitchen")
    udn2 = _deterministic_udn("player_bedroom")
    assert udn1 != udn2


def test_deterministic_udn_default() -> None:
    """Empty player_id produces a stable UDN for the default instance."""
    udn1 = _deterministic_udn("")
    udn2 = _deterministic_udn("")
    assert udn1 == udn2
    assert udn1 != _deterministic_udn("some_player")


def test_deterministic_udn_is_valid_uuid() -> None:
    """Generated UDN contains a valid UUID5."""
    udn = _deterministic_udn("test_player")
    uuid_str = udn.replace("uuid:", "")
    parsed = uuid.UUID(uuid_str)
    assert parsed.version == 5


def test_multiple_renderers_different_ports() -> None:
    """Verify multiple renderers can bind to different ports."""
    r1 = UPnPRenderer("Player 1", "127.0.0.1", 9001)
    r2 = UPnPRenderer("Player 2", "127.0.0.1", 9002)
    assert r1.http_port != r2.http_port
    assert r1.udn != r2.udn


def test_renderer_with_explicit_udn() -> None:
    """Renderer uses provided UDN instead of generating one."""
    udn = _deterministic_udn("test_player")
    r = UPnPRenderer("Test", "127.0.0.1", 9999, udn=udn)
    assert r.udn == udn


# ---------------------------------------------------------------------
# Provider-level tests (require full MA import; skip otherwise)
# ---------------------------------------------------------------------


@pytest.fixture
def provider_cls():  # type: ignore[no-untyped-def]
    """Return DLNAReceiverProvider class, skipping if MA isn't installed."""
    provider_mod = pytest.importorskip("music_assistant.providers.dlna_receiver.provider")
    return provider_mod.DLNAReceiverProvider


class _StubConfig:
    """Minimal ProviderConfig stand-in for testing config lookups."""

    def __init__(
        self,
        values: dict[str, str],
        instance_id: str = "dlna_receiver_test",
        name: str = "DLNA Receiver",
    ) -> None:
        self._values = values
        self.instance_id = instance_id
        self.name = name

    def get_value(self, key: str) -> str | None:
        return self._values.get(key)


def _make_provider(cls, values: dict[str, str]):  # type: ignore[no-untyped-def]
    inst = cls.__new__(cls)
    inst.config = _StubConfig(values)
    return inst


def test_raw_target_prefers_new_key(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """_raw_target uses CONF_TARGET_PLAYERS when set."""
    inst = _make_provider(provider_cls, {CONF_TARGET_PLAYERS: "p1,p2"})
    assert inst._raw_target() == "p1,p2"


def test_raw_target_falls_back_to_legacy_key(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Legacy CONF_TARGET_PLAYER with '*' must surface via _raw_target."""
    inst = _make_provider(
        provider_cls,
        {CONF_TARGET_PLAYERS: "", CONF_TARGET_PLAYER: "*"},
    )
    assert inst._raw_target() == "*"


def test_raw_target_empty_when_neither_set(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """No configured targets → empty string (single unbound renderer)."""
    inst = _make_provider(provider_cls, {})
    assert inst._raw_target() == ""


def test_get_source_uses_unknown_content_type(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """PluginSource.audio_format must let ffmpeg probe incoming codec.

    Upstream DLNA senders push FLAC/MP3/AAC/PCM etc.; declaring a concrete
    PCM format would cause ffmpeg to misread compressed bytes as raw PCM.
    """
    from music_assistant_models.enums import ContentType  # noqa: PLC0415

    inst = _make_provider(provider_cls, {})
    inst._plugin_source = None

    source = provider_cls.get_source(inst)

    assert source.audio_format is not None
    assert source.audio_format.content_type == ContentType.UNKNOWN
    assert source.audio_format.codec_type == ContentType.UNKNOWN
