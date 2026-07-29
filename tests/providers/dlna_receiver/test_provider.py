"""
Tests for the multi-player provider logic.

The standalone test bootstrap exposes the installed Music Assistant package,
so provider tests exercise the real implementation used upstream.
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from music_assistant_models.enums import ContentType, MediaType, QueueOption, StreamType
from music_assistant_models.streamdetails import StreamMetadata

from music_assistant.providers.dlna_receiver import __file__ as provider_package_file
from music_assistant.providers.dlna_receiver import get_config_entries
from music_assistant.providers.dlna_receiver.constants import (
    CONF_BIND_IP,
    CONF_TARGET_PLAYER,
    CONF_TARGET_PLAYERS,
    TRANSPORT_STATE_PAUSED,
    TRANSPORT_STATE_PLAYING,
    TRANSPORT_STATE_STOPPED,
)
from music_assistant.providers.dlna_receiver.provider import DLNAReceiverProvider, RendererInstance
from music_assistant.providers.dlna_receiver.renderer import UPnPRenderer


def test_deterministic_udn_same_input() -> None:
    """Same player_id always produces the same UDN."""
    udn1 = DLNAReceiverProvider._deterministic_udn("player_kitchen")
    udn2 = DLNAReceiverProvider._deterministic_udn("player_kitchen")
    assert udn1 == udn2
    assert udn1.startswith("uuid:")


def test_deterministic_udn_different_inputs() -> None:
    """Different player_ids produce different UDNs."""
    udn1 = DLNAReceiverProvider._deterministic_udn("player_kitchen")
    udn2 = DLNAReceiverProvider._deterministic_udn("player_bedroom")
    assert udn1 != udn2


def test_deterministic_udn_default() -> None:
    """Empty player_id produces a stable UDN for the default instance."""
    udn1 = DLNAReceiverProvider._deterministic_udn("")
    udn2 = DLNAReceiverProvider._deterministic_udn("")
    assert udn1 == udn2
    assert udn1 != DLNAReceiverProvider._deterministic_udn("some_player")


def test_deterministic_udn_is_valid_uuid() -> None:
    """Generated UDN contains a valid UUID5."""
    udn = DLNAReceiverProvider._deterministic_udn("test_player")
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
    udn = DLNAReceiverProvider._deterministic_udn("test_player")
    r = UPnPRenderer("Test", "127.0.0.1", 9999, udn=udn)
    assert r.udn == udn


# ---------------------------------------------------------------------
# Provider-level tests
# ---------------------------------------------------------------------


@pytest.fixture
def provider_cls() -> type[DLNAReceiverProvider]:
    """Return the real provider class."""
    provider_type: type[DLNAReceiverProvider] = DLNAReceiverProvider
    return provider_type


class _StubConfig:
    """Minimal ProviderConfig stand-in for testing config lookups."""

    def __init__(
        self,
        values: dict[str, str],
        instance_id: str = "dlna_receiver_test",
        name: str = "DLNA Receiver",
    ) -> None:
        self._values = {"log_level": "GLOBAL", **values}
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


def test_raw_target_defaults_to_all_players(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """No configured targets defaults to all available players."""
    inst = _make_provider(provider_cls, {})
    assert inst._raw_target() == "*"


def test_target_players_config_defaults_to_all() -> None:
    """New configurations default target_players to all players."""
    entries = asyncio.run(get_config_entries(cast("Any", None)))
    target_entry = next(entry for entry in entries if entry.key == CONF_TARGET_PLAYERS)
    assert target_entry.default_value == "*"


def test_get_all_players_filters_own_renderer_ids(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Raw UDN and universal IDs for our renderers must not become targets."""
    target_id = "player_kitchen"
    renderer_udn = provider_cls._deterministic_udn(target_id)
    universal_id = f"up{renderer_udn.replace(':', '').replace('-', '').lower()}"
    players = [
        types.SimpleNamespace(player_id=target_id, display_name="Kitchen", name="Kitchen"),
        types.SimpleNamespace(player_id=renderer_udn, display_name="DLNA raw", name="DLNA raw"),
        types.SimpleNamespace(
            player_id=universal_id,
            display_name="DLNA universal",
            name="DLNA universal",
        ),
    ]

    prov = _make_provider(provider_cls, {})
    prov.mass = types.SimpleNamespace(
        players=types.SimpleNamespace(all_players=lambda **_kwargs: players)
    )
    prov._instances = {}

    assert prov._get_all_players() == [(target_id, "Kitchen")]


def test_manifest_has_provider_icon() -> None:
    """The provider manifest declares an icon for the UI."""
    manifest = json.loads(Path(provider_package_file).with_name("manifest.json").read_text())
    assert manifest["icon"] == "cast-audio"


async def test_loaded_without_players_does_not_create_unbound_renderer(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """The all-players default waits instead of advertising a dead renderer."""

    def _all_players(**_kwargs: object) -> list[object]:
        return []

    mass = types.SimpleNamespace(
        cache=None,
        players=types.SimpleNamespace(all_players=_all_players),
    )
    prov = provider_cls(
        cast("Any", mass),
        cast("Any", types.SimpleNamespace(domain="dlna_receiver")),
        cast("Any", _StubConfig({CONF_BIND_IP: "192.168.1.20"})),
    )

    await prov.loaded_in_mass()
    try:
        assert prov._instances == {}
        assert prov._discovery_task is not None
    finally:
        await prov.unload()


# ---------------------------------------------------------------------
# New plugin-sources contract (AudioSource MediaItems)
# ---------------------------------------------------------------------


def _make_instance(player_id: str, player_name: str, url: str | None = None) -> RendererInstance:
    """Build a RendererInstance with stubbed renderer/ssdp for contract tests."""
    from music_assistant.providers.dlna_receiver.ssdp import SSDPAdvertiser  # noqa: PLC0415

    friendly = f"Music Assistant — {player_name}" if player_name else "Music Assistant"
    return RendererInstance(
        player_id=player_id,
        player_name=player_name,
        renderer=cast("UPnPRenderer", types.SimpleNamespace(friendly_name=friendly)),
        ssdp=cast("SSDPAdvertiser", types.SimpleNamespace()),
        current_stream_url=url,
    )


def _make_contract_provider(
    cls: type[DLNAReceiverProvider], instances: dict[str, RendererInstance]
) -> DLNAReceiverProvider:
    """
    Build a provider carrying renderer instances for contract tests.

    Uses the real ``__init__`` with stub mass/manifest/config so the tests
    never drift from the constructor's state initialization.
    """
    prov = cls(
        cast("Any", types.SimpleNamespace(cache=None)),
        cast("Any", types.SimpleNamespace(domain="dlna_receiver")),
        cast("Any", _StubConfig({})),
    )
    prov._instances = instances
    return prov


def test_get_audio_sources_one_per_instance(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Each renderer instance is exposed as its own AudioSource item."""
    prov = _make_contract_provider(
        provider_cls,
        {
            "player_kitchen": _make_instance("player_kitchen", "Kitchen"),
            "player_bedroom": _make_instance("player_bedroom", "Bedroom"),
        },
    )

    sources = asyncio.run(prov.get_audio_sources())

    assert {s.item_id for s in sources} == {"player_kitchen", "player_bedroom"}
    for source in sources:
        assert source.provider == prov.instance_id
        assert source.exclusive is True
        assert source.can_initiate is False
        assert source.allow_external_trigger is True


def test_get_audio_sources_use_unknown_content_type(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """
    AudioSource mappings must let ffmpeg probe the incoming codec.

    Upstream DLNA senders push FLAC/MP3/AAC/PCM etc.; declaring a concrete
    PCM format would cause ffmpeg to misread compressed bytes as raw PCM.
    """
    prov = _make_contract_provider(
        provider_cls,
        {"__default__": _make_instance("", "")},
    )

    (source,) = asyncio.run(prov.get_audio_sources())

    mapping = next(iter(source.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.UNKNOWN
    assert mapping.audio_format.codec_type == ContentType.UNKNOWN


def test_get_stream_details_returns_custom_stream(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """StreamDetails for an active DLNA stream use a CUSTOM probe-able stream."""
    prov = _make_contract_provider(
        provider_cls,
        {
            "player_kitchen": _make_instance(
                "player_kitchen", "Kitchen", "http://cp.local/track.flac"
            )
        },
    )

    sd = asyncio.run(prov.get_stream_details("player_kitchen", "queue1"))

    assert sd.provider == prov.instance_id
    assert sd.item_id == "player_kitchen"
    assert sd.media_type == MediaType.AUDIO_SOURCE
    assert sd.stream_type == StreamType.CUSTOM
    assert sd.audio_format.content_type == ContentType.UNKNOWN
    assert sd.audio_format.codec_type == ContentType.UNKNOWN
    assert sd.decoded_audio_format is None


def test_get_stream_details_unknown_source_raises(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Requesting an unknown source id raises MediaNotFoundError."""
    from music_assistant_models.errors import MediaNotFoundError  # noqa: PLC0415

    prov = _make_contract_provider(provider_cls, {})

    with pytest.raises(MediaNotFoundError):
        asyncio.run(prov.get_stream_details("nope", "queue1"))


def test_get_stream_details_without_active_stream_raises(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """No DLNA sender pushed a URL yet — streaming must be refused."""
    from music_assistant_models.errors import AudioError  # noqa: PLC0415

    prov = _make_contract_provider(
        provider_cls,
        {"player_kitchen": _make_instance("player_kitchen", "Kitchen")},
    )

    with pytest.raises(AudioError):
        asyncio.run(prov.get_stream_details("player_kitchen", "queue1"))


def test_get_stream_details_is_side_effect_free(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Preload may fetch streamdetails without claiming the source."""
    prov = _make_contract_provider(
        provider_cls,
        {
            "player_kitchen": _make_instance(
                "player_kitchen", "Kitchen", "http://cp.local/track.flac"
            )
        },
    )

    asyncio.run(prov.get_stream_details("player_kitchen", "queue1"))

    assert prov._claims == {}


def test_on_source_selected_claims_source(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Selecting a source records the owning queue and session token."""
    prov = _make_contract_provider(
        provider_cls,
        {
            "player_kitchen": _make_instance(
                "player_kitchen", "Kitchen", "http://cp.local/track.flac"
            )
        },
    )

    asyncio.run(prov.on_source_selected("player_kitchen", "player_kitchen", "queue1", "sess-a"))

    assert prov._claims["player_kitchen"] == ("queue1", "sess-a")


def test_on_source_unselected_releases_claim(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Matching session teardown releases the claim."""
    prov = _make_contract_provider(
        provider_cls,
        {
            "player_kitchen": _make_instance(
                "player_kitchen", "Kitchen", "http://cp.local/track.flac"
            )
        },
    )

    asyncio.run(prov.on_source_selected("player_kitchen", "player_kitchen", "queue1", "sess-a"))
    asyncio.run(prov.on_source_unselected("player_kitchen", "queue1", "sess-a"))

    assert "player_kitchen" not in prov._claims


def test_on_source_unselected_ignores_stale_session(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """A superseded request's late teardown must not clear the live claim."""
    prov = _make_contract_provider(
        provider_cls,
        {
            "player_kitchen": _make_instance(
                "player_kitchen", "Kitchen", "http://cp.local/track.flac"
            )
        },
    )

    asyncio.run(prov.on_source_selected("player_kitchen", "player_kitchen", "queue1", "sess-a"))
    asyncio.run(prov.on_source_selected("player_kitchen", "player_kitchen", "queue1", "sess-b"))
    asyncio.run(prov.on_source_unselected("player_kitchen", "queue1", "sess-a"))

    assert prov._claims["player_kitchen"] == ("queue1", "sess-b")


def test_on_play_routes_through_play_media(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """DLNA Play triggers the standard play_media flow with the source uri."""
    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/track.flac")
    inst.current_metadata = {
        "title": "Song",
        "artist": None,
        "album": None,
        "image_url": None,
        "duration": None,
    }
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})

    calls: list[tuple[str, str, object]] = []

    async def _record_play_media(player_id: str, media: str, option: object = None) -> None:
        calls.append((player_id, media, option))

    prov.mass = cast(
        "Any",
        types.SimpleNamespace(
            player_queues=types.SimpleNamespace(play_media=_record_play_media),
        ),
    )

    asyncio.run(prov._on_play(inst, TRANSPORT_STATE_STOPPED))

    assert len(calls) == 1
    player_id, media_uri, option = calls[0]
    assert player_id == "player_kitchen"
    assert media_uri == f"{prov.instance_id}://audio_source/player_kitchen"
    assert option == QueueOption.PLAY


def test_on_play_resumes_paused_player_without_restarting_stream(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """DLNA Play after Pause resumes the player and preserves stream progress."""
    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/track.flac")
    metadata = StreamMetadata(title="Song", elapsed_time=37)
    inst.stream_metadata = metadata
    inst.elapsed_offset = 37
    inst.metadata_dirty = False
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})

    play_commands: list[str] = []
    play_media_calls: list[tuple[object, ...]] = []

    async def _record_cmd_play(player_id: str) -> None:
        play_commands.append(player_id)

    async def _record_play_media(*args: object, **_kwargs: object) -> None:
        play_media_calls.append(args)

    prov.mass = cast(
        "Any",
        types.SimpleNamespace(
            players=types.SimpleNamespace(cmd_play=_record_cmd_play),
            player_queues=types.SimpleNamespace(play_media=_record_play_media),
        ),
    )

    asyncio.run(prov._on_play(inst, TRANSPORT_STATE_PAUSED))

    assert play_commands == ["player_kitchen"]
    assert play_media_calls == []
    assert inst.current_stream_url == "http://cp.local/track.flac"
    assert inst.stream_metadata is metadata
    assert inst.elapsed_offset == 37
    assert inst.play_start_time is not None
    assert inst.metadata_dirty is True


def test_on_play_is_idempotent_while_already_playing(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """A duplicate DLNA Play command does not restart active playback."""
    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/track.flac")
    inst.play_start_time = 123.0
    inst.elapsed_offset = 9
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})
    calls: list[str] = []

    async def _record(*_args: object, **_kwargs: object) -> None:
        calls.append("called")

    prov.mass = cast(
        "Any",
        types.SimpleNamespace(
            players=types.SimpleNamespace(cmd_play=_record),
            player_queues=types.SimpleNamespace(play_media=_record),
        ),
    )

    asyncio.run(prov._on_play(inst, TRANSPORT_STATE_PLAYING))

    assert calls == []
    assert inst.play_start_time == 123.0
    assert inst.elapsed_offset == 9


def test_on_stop_clears_only_that_instance(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Stopping one renderer must not wipe another renderer's playback state."""
    inst_a = _make_instance("player_kitchen", "Kitchen", "http://cp.local/a.flac")
    inst_b = _make_instance("player_bedroom", "Bedroom", "http://cp.local/b.flac")
    prov = _make_contract_provider(
        provider_cls, {"player_kitchen": inst_a, "player_bedroom": inst_b}
    )

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    prov.mass = cast(
        "Any",
        types.SimpleNamespace(
            player_queues=types.SimpleNamespace(play_media=_noop),
            players=types.SimpleNamespace(cmd_stop=_noop),
        ),
    )

    async def _scenario() -> None:
        await prov._on_play(inst_a, TRANSPORT_STATE_STOPPED)
        await prov._on_play(inst_b, TRANSPORT_STATE_STOPPED)
        await prov._on_stop(inst_a)

    asyncio.run(_scenario())

    assert inst_a.stream_metadata is None
    assert inst_a.current_stream_url == "http://cp.local/a.flac"
    assert inst_b.stream_metadata is not None


def test_on_source_unselected_stops_elapsed_tracking(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """MA-side stream teardown must clear the instance's playback state."""
    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/a.flac")
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    prov.mass = cast(
        "Any",
        types.SimpleNamespace(
            player_queues=types.SimpleNamespace(play_media=_noop),
            players=types.SimpleNamespace(cmd_stop=_noop),
        ),
    )

    async def _scenario() -> None:
        await prov._on_play(inst, TRANSPORT_STATE_STOPPED)
        await prov.on_source_selected("player_kitchen", "player_kitchen", "queue1", "sess-a")
        await prov.on_source_unselected("player_kitchen", "queue1", "sess-a")

    asyncio.run(_scenario())

    assert inst.stream_metadata is None
    assert inst.renderer.transport_state == TRANSPORT_STATE_STOPPED


def test_position_for_reports_elapsed_and_duration(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Renderer position comes from the instance's tracked playback state."""
    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/a.flac")
    inst.elapsed_offset = 65
    inst.current_metadata = {"duration": "00:04:05"}
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})

    assert prov._position_for(inst) == (65, 245)


def test_get_audio_stream_raises_without_url(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """A cached StreamDetails replay after Stop must fail loudly, not stream nothing."""
    from music_assistant_models.errors import AudioError  # noqa: PLC0415

    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/a.flac")
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})

    sd = asyncio.run(prov.get_stream_details("player_kitchen", "queue1"))
    inst.current_stream_url = None

    async def _consume() -> None:
        async for _chunk in prov.get_audio_stream(sd):
            pass

    with pytest.raises(AudioError):
        asyncio.run(_consume())


def test_on_source_selected_stops_previous_queue_on_handoff(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Cross-queue takeover of the exclusive source stops the previous consumer."""
    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/a.flac")
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})

    stopped: list[str] = []

    async def _record_stop(player_id: str) -> None:
        stopped.append(player_id)

    prov.mass = cast(
        "Any",
        types.SimpleNamespace(players=types.SimpleNamespace(cmd_stop=_record_stop)),
    )

    async def _scenario() -> None:
        await prov.on_source_selected("player_kitchen", "player_kitchen", "queue1", "sess-a")
        await prov.on_source_selected("player_kitchen", "player_other", "queue2", "sess-b")

    asyncio.run(_scenario())

    assert stopped == ["queue1"]
    assert prov._claims["player_kitchen"] == ("queue2", "sess-b")


def test_metadata_push_gating(provider_cls) -> None:  # type: ignore[no-untyped-def]
    """Elapsed-only ticks are not pushed; changes and periodic resync are."""
    inst = _make_instance("player_kitchen", "Kitchen", "http://cp.local/a.flac")
    prov = _make_contract_provider(provider_cls, {"player_kitchen": inst})

    inst.metadata_dirty = True
    assert prov._should_push_metadata(inst, now=100.0) is True

    inst.metadata_dirty = False
    inst.last_metadata_push = 100.0
    assert prov._should_push_metadata(inst, now=102.0) is False
    assert prov._should_push_metadata(inst, now=131.0) is True


# ---------------------------------------------------------------------
# _is_concrete_ipv4 helper
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["192.168.1.5", "10.0.0.1", "8.8.8.8", "172.16.0.1"],
)
def test_is_concrete_ipv4_accepts_routable_addresses(value: str) -> None:
    """Concrete non-wildcard, non-loopback IPv4 addresses are accepted."""
    from music_assistant.providers.dlna_receiver.provider import _is_concrete_ipv4  # noqa: PLC0415

    assert _is_concrete_ipv4(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0.0.0.0",  # wildcard — SSDP would join multicast on wrong interface
        "127.0.0.1",  # IPv4 loopback — multicast on lo never reaches real CPs
        "127.1.2.3",  # entire 127.0.0.0/8 is loopback
        "::1",  # IPv6 loopback — inet_aton rejects
        "fe80::1",  # IPv6 link-local
        "2001:db8::1",  # IPv6 documentation
        "localhost",  # hostname, not an IP literal
        "192.168.1",  # malformed
        "not-an-ip",
    ],
)
def test_is_concrete_ipv4_rejects_non_routable(value: str) -> None:
    """Empty / wildcard / loopback / IPv6 / hostname / garbage all rejected."""
    from music_assistant.providers.dlna_receiver.provider import _is_concrete_ipv4  # noqa: PLC0415

    assert _is_concrete_ipv4(value) is False
