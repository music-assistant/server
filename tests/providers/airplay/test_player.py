"""Unit tests for AirPlay player."""

import asyncio
import logging
import time
from typing import cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    CrossfadeMode,
    MediaType,
    PlaybackState,
    PlayerFeature,
    VolumeNormalizationMode,
)
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.controllers.streams.audio import StreamsAudio
from music_assistant.providers.airplay.constants import (
    AIRPLAY_PCM_FORMAT,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_ENCRYPTION,
    CONF_FORCE_RAOP,
    CONF_IGNORE_VOLUME,
    CONF_PASSWORD,
    CONF_PASSWORD_INVALID,
    CONF_RAOP_CREDENTIALS,
    CONF_STORED_VOLUME,
    StreamingProtocol,
)
from music_assistant.providers.airplay.player import AirPlayPlayer

# _airplay._tcp features bitmask with the AirPlay 2 feature bits set (bit 38/48).
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"
# audioFormat bits as advertised in a receiver's /info format tables.
ALAC_44100_16 = 1 << 18
ALAC_44100_24 = 1 << 19
ALAC_48000_24 = 1 << 21


def _stub_raw_config(provider: MagicMock, stored: dict[str, object] | None = None) -> None:
    """Serve raw player config values from a dict instead of an (always truthy) mock."""
    values = stored if stored is not None else {}
    provider.mass.config.get_raw_player_config_value.side_effect = (
        lambda _player_id, key, default=None: values.get(key, default)
    )
    provider.mass.config.set_raw_player_config_value.side_effect = lambda _player_id, key, value: (
        values.__setitem__(key, value)
    )


@pytest.fixture
def airplay_player() -> AirPlayPlayer:
    """Create a basic AirPlayPlayer with mock defaults."""
    provider = MagicMock()
    _stub_raw_config(provider)
    return AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )


@pytest.mark.parametrize(
    ("manufacturer", "model", "expected"),
    [
        ("Apple", "MacBookPro18,3", False),
        ("Apple Inc.", "MacBook Air (MacBookAir10,1)", False),
        ("Apple", "iMac (iMac21,1)", False),
        ("Apple", "Mac mini (Mac16,11)", False),
        ("Apple", "Mac Pro (MacPro7,1)", False),
        ("Apple", "Mac Studio (Mac14,13)", False),
        ("Apple", "HomePod Mini", True),
        ("Apple", "Apple TV 4K", True),
        ("Acme", "Mac-compatible receiver", True),
    ],
)
def test_macos_devices_are_disabled_by_default(
    manufacturer: str, model: str, expected: bool
) -> None:
    """Macs are disabled by default without affecting other AirPlay receivers."""
    provider = MagicMock()
    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer=manufacturer,
        model=model,
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )

    assert player.enabled_by_default is expected
    assert provider.mass.config.create_default_player_config.call_args.args[-1] is expected


@pytest.mark.parametrize(
    ("aiplay_properties", "raop_properties", "expected"),
    [
        ({b"flags": b"0x200"}, None, True),
        ({b"sf": b"0x201"}, None, True),
        ({b"flags": b"0x4"}, None, False),
        ({b"sf": b"0x8"}, None, True),
        ({b"flags": b"0x9"}, None, True),
        (None, {b"flags": "0x200"}, True),
        (None, {b"sf": b"0x201"}, True),
        (None, {b"flags": b"0x4"}, False),
        (None, {b"sf": b"0x8"}, True),
        (None, {b"flags": b"0x9"}, True),
        # Combined flags across discovery records should be OR-ed.
        ({b"sf": b"0x8"}, {b"sf": b"0x200"}, True),
        ({b"sf": b"0x200"}, {b"sf": b"0x8"}, True),
        ({b"flags": b"0x4"}, {b"flags": b"0x0"}, False),
        ({}, {}, False),
    ],
)
def test_requires_pin_pairing(
    airplay_player: AirPlayPlayer,
    aiplay_properties: dict[bytes, bytes] | None,
    raop_properties: dict[bytes, bytes] | None,
    expected: bool,
) -> None:
    """Test the _requires_pairing method of AirPlayPlayer."""
    if aiplay_properties is not None:
        aiplay_discovery_info = MagicMock()
        aiplay_discovery_info.properties = aiplay_properties
        airplay_player.airplay_discovery_info = aiplay_discovery_info
    if raop_properties is not None:
        raop_discovery_info = MagicMock()
        raop_discovery_info.properties = raop_properties
        airplay_player.raop_discovery_info = raop_discovery_info
    assert airplay_player._requires_pin_pairing() == expected


@pytest.mark.parametrize(
    ("aiplay_properties", "raop_properties", "expected"),
    [
        ({b"flags": b"0x80"}, None, True),
        ({b"sf": b"0x81"}, None, True),
        ({b"flags": b"0x4"}, None, False),
        ({b"sf": b"0x80"}, None, True),
        ({b"flags": b"0x90"}, None, True),
        ({b"flags": b"0x1000"}, None, True),
        (None, {b"flags": "0x80"}, True),
        (None, {b"sf": b"0x81"}, True),
        (None, {b"flags": b"0x4"}, False),
        (None, {b"sf": b"0x80"}, True),
        (None, {b"flags": b"0x90"}, True),
        ({}, {}, False),
    ],
)
def test_password_required(
    airplay_player: AirPlayPlayer,
    aiplay_properties: dict[bytes, bytes] | None,
    raop_properties: dict[bytes, bytes] | None,
    expected: bool,
) -> None:
    """Test the flags-based password announcements (non-Apple-TV model)."""
    if aiplay_properties is not None:
        aiplay_discovery_info = MagicMock()
        aiplay_discovery_info.properties = aiplay_properties
        aiplay_discovery_info.decoded_properties = {}
        airplay_player.airplay_discovery_info = aiplay_discovery_info
    if raop_properties is not None:
        raop_discovery_info = MagicMock()
        raop_discovery_info.properties = raop_properties
        raop_discovery_info.decoded_properties = {}
        airplay_player.raop_discovery_info = raop_discovery_info
    assert airplay_player.password_required == expected


def test_build_streaming_pairing_uses_discovered_ipv4_address() -> None:
    """HAP pairing falls back to a discovered IPv4 address when playback uses IPv6."""
    provider = MagicMock()
    provider.dacp_id = "test_dacp"
    airplay_info = MagicMock()
    airplay_info.properties = {b"flags": b"0x80"}
    airplay_info.port = 7000
    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="2001:db8::10",
        manufacturer="Apple",
        model="AppleTV",
        raop_discovery_info=None,
        airplay_discovery_info=airplay_info,
    )
    pairing_instance = MagicMock()

    with (
        patch(
            "music_assistant.providers.airplay.player.get_primary_ip_address_from_zeroconf",
            return_value="192.168.1.50",
        ),
        patch(
            "music_assistant.providers.airplay.pairing.AirPlayPairing",
            return_value=pairing_instance,
        ) as pairing_cls,
    ):
        result = player._build_streaming_pairing(StreamingProtocol.AIRPLAY2)

    assert result is pairing_instance
    assert pairing_cls.call_args.kwargs["address"] == "192.168.1.50"


def test_build_streaming_pairing_fails_without_ipv4_address() -> None:
    """HAP pairing reports an actionable error when discovery has no IPv4 address."""
    provider = MagicMock()
    provider.dacp_id = "test_dacp"
    airplay_info = MagicMock()
    airplay_info.properties = {b"flags": b"0x80"}
    airplay_info.port = 7000
    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="2001:db8::10",
        manufacturer="Apple",
        model="AppleTV",
        raop_discovery_info=None,
        airplay_discovery_info=airplay_info,
    )

    with (
        patch(
            "music_assistant.providers.airplay.player.get_primary_ip_address_from_zeroconf",
            return_value="2001:db8::20",
        ),
        pytest.raises(PlayerCommandFailed, match="requires an IPv4"),
    ):
        player._build_streaming_pairing(StreamingProtocol.AIRPLAY2)


@pytest.mark.asyncio
async def test_config_entries_include_ignore_volume(airplay_player: AirPlayPlayer) -> None:
    """The ignore_volume setting must be offered in the player config entries."""
    entries = await airplay_player.get_config_entries()
    assert any(entry.key == CONF_IGNORE_VOLUME for entry in entries)


@pytest.mark.asyncio
async def test_config_entries_preserve_raop_encryption_setting(
    airplay_player: AirPlayPlayer,
) -> None:
    """RAOP keeps its advanced encryption toggle with the secure default enabled."""
    entries = await airplay_player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_ENCRYPTION)

    assert entry.default_value is True
    assert entry.hidden is False
    assert entry.advanced is True


@pytest.mark.asyncio
async def test_config_entries_sync_adjust_is_non_advanced(airplay_player: AirPlayPlayer) -> None:
    """AirPlay offers sync_adjust as a discoverable (non-advanced) setting."""
    entries = await airplay_player.get_config_entries()
    entry = next((entry for entry in entries if entry.key == CONF_SYNC_ADJUST), None)
    assert entry is not None
    # non-advanced so users can find it: it is the primary control for compensating
    # a device wired to a TV / AV receiver / amplifier that adds its own audio delay
    assert entry.advanced is False


def _set_discovery_info(
    player: AirPlayPlayer,
    *,
    raop: bool,
    airplay: bool,
    airplay_features: str | None = None,
) -> None:
    """
    Attach discovery mocks so the device advertises the given protocols.

    :param airplay_features: When set, the _airplay service advertises this
        ``features`` bitmask (e.g. to mark the device AirPlay 2 capable).
    """
    if raop:
        raop_info = MagicMock()
        raop_info.properties = {}
        raop_info.decoded_properties = {}
        player.raop_discovery_info = raop_info
    else:
        player.raop_discovery_info = None
    if airplay:
        airplay_info = MagicMock()
        airplay_info.properties = {}
        airplay_info.decoded_properties = {"features": airplay_features} if airplay_features else {}
        player.airplay_discovery_info = airplay_info
    else:
        player.airplay_discovery_info = None


def _make_apple_player() -> AirPlayPlayer:
    """Create an AirPlayPlayer that identifies as a genuine Apple device."""
    return AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Apple TV",
        address="127.0.0.1",
        manufacturer="Apple",
        model="Apple TV 4K",
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )


# --- Force-RAOP escape hatch: toggle visibility ---


@pytest.mark.asyncio
async def test_force_raop_toggle_offered_for_non_apple_airplay2(
    airplay_player: AirPlayPlayer,
) -> None:
    """A non-Apple AirPlay 2 device that also speaks RAOP gets the force-RAOP escape hatch."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    entries = await airplay_player.get_config_entries()
    entry = next((entry for entry in entries if entry.key == CONF_FORCE_RAOP), None)
    assert entry is not None
    assert entry.type == ConfigEntryType.BOOLEAN
    assert entry.default_value is False
    # advanced-only: it is a workaround, not a routine protocol choice
    assert entry.advanced is True


@pytest.mark.asyncio
async def test_force_raop_toggle_hidden_for_apple_airplay2() -> None:
    """Genuine Apple AirPlay 2 devices are always AirPlay 2, so no force-RAOP toggle is offered."""
    player = _make_apple_player()
    _set_discovery_info(player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    entries = await player.get_config_entries()
    assert all(entry.key != CONF_FORCE_RAOP for entry in entries)


@pytest.mark.asyncio
async def test_force_raop_toggle_hidden_for_raop_only(airplay_player: AirPlayPlayer) -> None:
    """A RAOP-only device has nothing to force, so no toggle is offered."""
    _set_discovery_info(airplay_player, raop=True, airplay=False)
    entries = await airplay_player.get_config_entries()
    assert all(entry.key != CONF_FORCE_RAOP for entry in entries)


@pytest.mark.asyncio
async def test_force_raop_toggle_hidden_for_airplay2_only(airplay_player: AirPlayPlayer) -> None:
    """An AirPlay-2-only device (no RAOP service) has nothing to fall back to: no toggle."""
    _set_discovery_info(airplay_player, raop=False, airplay=True, airplay_features=AP2_FEATURES)
    entries = await airplay_player.get_config_entries()
    assert all(entry.key != CONF_FORCE_RAOP for entry in entries)


# --- Protocol resolution ---


@pytest.mark.parametrize(
    ("airplay_props", "raop_props", "expected"),
    [
        # devices advertising the AirPlay 2 feature bits get AirPlay 2
        ({"features": AP2_FEATURES}, {}, StreamingProtocol.AIRPLAY2),
        # the _raop ft field is used as fallback when _airplay lacks features
        ({}, {"ft": "0x445F8A00,0x1C340"}, StreamingProtocol.AIRPLAY2),
        # legacy receivers without the AirPlay 2 feature bits stay on RAOP
        ({"features": "0x5A7FFFF7"}, {}, StreamingProtocol.RAOP),
        # no features advertised at all: RAOP (safe legacy default)
        ({}, {}, StreamingProtocol.RAOP),
    ],
)
def test_protocol_resolution_follows_capability(
    airplay_props: dict[str, str], raop_props: dict[str, str], expected: StreamingProtocol
) -> None:
    """Without the force toggle, protocol resolution follows the advertised AirPlay 2 bits."""
    raop_info = MagicMock()
    raop_info.decoded_properties = raop_props
    airplay_info = MagicMock()
    airplay_info.decoded_properties = airplay_props
    player = AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=raop_info,
        airplay_discovery_info=airplay_info,
    )
    _configure_player(player, {CONF_FORCE_RAOP: False})
    assert player.protocol == expected


def test_protocol_resolution_airplay_service_only() -> None:
    """A device advertising only the _airplay service is AirPlay 2 even without features."""
    airplay_info = MagicMock()
    airplay_info.decoded_properties = {}
    player = AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=airplay_info,
    )
    _configure_player(player, {CONF_FORCE_RAOP: False})
    assert player.protocol == StreamingProtocol.AIRPLAY2


def test_force_raop_resolves_to_raop_on_non_apple_airplay2(airplay_player: AirPlayPlayer) -> None:
    """Enabling the toggle on an eligible device forces RAOP for both resolution and stream args."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    _configure_player(airplay_player, {CONF_FORCE_RAOP: True})
    assert airplay_player.protocol == StreamingProtocol.RAOP
    assert airplay_player.protocol_override == StreamingProtocol.RAOP


def test_force_raop_ignored_on_apple_airplay2() -> None:
    """A stray persisted force_raop is ignored on Apple AirPlay 2 devices (never eligible)."""
    player = _make_apple_player()
    _set_discovery_info(player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    _configure_player(player, {CONF_FORCE_RAOP: True})
    assert player.protocol == StreamingProtocol.AIRPLAY2
    assert player.protocol_override is None


@pytest.mark.parametrize(
    ("stored_config", "expected"),
    [
        # no credentials at all: pairing is required before the player is usable
        ({}, True),
        # a legacy RAOP pairing keeps the player usable after the device
        # resolves to AirPlay 2 (the binary streams RAOP-compat with the secret)
        ({CONF_RAOP_CREDENTIALS: "clientid:secret"}, False),
        # AirPlay 2 credentials obviously suffice as well
        ({CONF_AIRPLAY_CREDENTIALS: "a" * 192}, False),
    ],
)
def test_needs_setup_accepts_credentials_for_either_protocol(
    airplay_player: AirPlayPlayer, stored_config: dict[str, str], expected: bool
) -> None:
    """A PIN-pairing device needs setup only when no credentials are stored at all."""
    # PIN-required device that resolves to AirPlay 2 (Apple TV-like)
    airplay_info = MagicMock()
    airplay_info.properties = {b"flags": b"0x8"}
    airplay_info.decoded_properties = {"features": "0x4A7FDFD5,0x3C177FDE"}
    airplay_player.airplay_discovery_info = airplay_info
    # credentials now live in the player's setup_data, read via get_setup_value
    airplay_player.get_setup_value = (  # type: ignore[method-assign]
        lambda key, default=None: stored_config.get(key, default)
    )
    assert airplay_player.needs_setup is expected


# --- Hi-res playback tests ---


def _configure_player(player: AirPlayPlayer, values: dict[str, object]) -> None:
    """Stub the player config to return the given values."""
    player.config.get_value.side_effect = (  # type: ignore[attr-defined]
        lambda key, default=None: values.get(key, default)
    )


@pytest.mark.parametrize(
    ("advertised_audio_formats", "force_raop", "airplay2_capable", "expected"),
    [
        # 24-bit advertised on the realtime stream
        (ALAC_44100_24, False, True, [(44100, 24), (48000, 24)]),
        # the Apple TV advertises 24-bit for its buffered stream only
        (ALAC_48000_24, False, True, [(44100, 24), (48000, 24)]),
        # forced RAOP cannot do 24-bit: falls back to the 16-bit base
        (ALAC_44100_24, True, True, [(44100, 16)]),
        # a receiver that streams RAOP never gets 24-bit, whatever it advertises
        (ALAC_44100_24, False, False, [(44100, 16)]),
        # only 16-bit advertised: the 16-bit default
        (ALAC_44100_16, False, True, [(44100, 16)]),
        # nothing advertised (unreachable device or no format tables)
        (0, False, True, [(44100, 16)]),
    ],
)
def test_hires_supported_sample_rates(
    airplay_player: AirPlayPlayer,
    advertised_audio_formats: int,
    force_raop: bool,
    airplay2_capable: bool,
    expected: list[tuple[int, int]],
) -> None:
    """The formats the device advertises drive the advertised sample rates."""
    _set_discovery_info(
        airplay_player,
        raop=True,
        airplay=True,
        airplay_features=AP2_FEATURES if airplay2_capable else None,
    )
    airplay_player.advertised_audio_formats = advertised_audio_formats
    _configure_player(airplay_player, {CONF_FORCE_RAOP: force_raop})
    assert airplay_player.supported_sample_rates == expected


def test_get_stream_pcm_format_hires(airplay_player: AirPlayPlayer) -> None:
    """For a 24-bit capable device the stream format is 24-bit in a s32le container."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    airplay_player.advertised_audio_formats = ALAC_44100_24
    _configure_player(airplay_player, {CONF_FORCE_RAOP: False})

    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32
    )
    stream_format = airplay_player.get_stream_pcm_format(session_format)
    # the binary expects raw s32le on stdin for --bitdepth 24
    assert stream_format.content_type == ContentType.PCM_S32LE
    assert stream_format.sample_rate == 48000
    assert stream_format.bit_depth == 24

    # an unsupported session rate falls back to the 44.1 kHz base
    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=96000, bit_depth=32
    )
    stream_format = airplay_player.get_stream_pcm_format(session_format)
    assert stream_format.sample_rate == 44100
    assert stream_format.bit_depth == 24


def test_get_stream_pcm_format_default(airplay_player: AirPlayPlayer) -> None:
    """Without a 24-bit capable device the stream format is the 44.1/16 default."""
    _set_discovery_info(airplay_player, raop=True, airplay=True)
    _configure_player(airplay_player, {CONF_FORCE_RAOP: False})
    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32
    )
    assert airplay_player.get_stream_pcm_format(session_format) == AIRPLAY_PCM_FORMAT


@pytest.mark.asyncio
async def test_session_pcm_format_selection(airplay_player: AirPlayPlayer) -> None:
    """AirPlay delegates the complete session format decision to the shared selector."""
    selected_format = AudioFormat(
        content_type=ContentType.PCM_S24LE,
        sample_rate=48000,
        bit_depth=24,
    )
    streams_audio = cast("MagicMock", airplay_player.mass.streams.audio)
    streams_audio.select_flow_pcm_format = AsyncMock(return_value=selected_format)
    cast("MagicMock", airplay_player.mass.player_queues.get).return_value = None
    media = MagicMock()
    media.source_id = "queue1"
    media.queue_item_id = "item1"
    queue_item = MagicMock()
    queue_item.streamdetails.audio_format.sample_rate = 48000
    airplay_player.mass.player_queues.get_item.return_value = queue_item  # type: ignore[attr-defined]
    sync_clients = [airplay_player, airplay_player]

    fmt = await airplay_player._get_session_pcm_format(sync_clients, media)

    assert fmt is selected_format
    streams_audio.select_flow_pcm_format.assert_awaited_once_with(
        airplay_player,
        start_streamdetails=queue_item.streamdetails,
        crossfade_enabled=False,
        overlay_active=False,
        fallback_sample_rate=AIRPLAY_PCM_FORMAT.sample_rate,
        output_players=sync_clients,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("normalization_mode", "expected_content_type", "expected_bit_depth"),
    [
        (VolumeNormalizationMode.DISABLED, ContentType.PCM_S24LE, 24),
        (VolumeNormalizationMode.MEASUREMENT_ONLY, ContentType.PCM_F32LE, 32),
    ],
)
async def test_session_pcm_format_selects_processing_depth(
    airplay_player: AirPlayPlayer,
    normalization_mode: VolumeNormalizationMode,
    expected_content_type: ContentType,
    expected_bit_depth: int,
) -> None:
    """An AirPlay session only uses float PCM when processing needs headroom."""
    _set_discovery_info(airplay_player, raop=True, airplay=True, airplay_features=AP2_FEATURES)
    airplay_player.advertised_audio_formats = ALAC_48000_24
    _configure_player(airplay_player, {CONF_FORCE_RAOP: False})
    airplay_player.mass.streams.audio = StreamsAudio(airplay_player.mass)
    cast("MagicMock", airplay_player.mass.config.get_player_dsp_config).return_value = MagicMock(
        enabled=False
    )
    cast(
        "MagicMock", airplay_player.mass.streams.get_crossfade_mode
    ).return_value = CrossfadeMode.DISABLED

    streamdetails = MagicMock()
    streamdetails.audio_format = AudioFormat(
        content_type=ContentType.FLAC,
        sample_rate=48000,
        bit_depth=24,
    )
    streamdetails.media_type = MediaType.TRACK
    streamdetails.volume_normalization_mode = normalization_mode
    queue_item = MagicMock(streamdetails=streamdetails)
    queue = MagicMock(crossfade_enabled=False, overlay_enabled=False, overlay_source=None)
    cast("MagicMock", airplay_player.mass.player_queues.get).return_value = queue
    cast("MagicMock", airplay_player.mass.player_queues.get_item).return_value = queue_item
    media = MagicMock(source_id="queue1", queue_item_id="item1", media_type=MediaType.TRACK)

    fmt = await airplay_player._get_session_pcm_format([airplay_player], media)

    assert fmt.content_type == expected_content_type
    assert fmt.sample_rate == 48000
    assert fmt.bit_depth == expected_bit_depth


# --- Volume and Mute tests ---


def _setup_running_stream(player: AirPlayPlayer) -> AsyncMock:
    """Attach a mock running stream to the player and return the send_cli_command mock."""
    stream = MagicMock()
    stream.running = True
    send_cmd = AsyncMock()
    stream.send_cli_command = send_cmd
    player.stream = stream
    return send_cmd


@pytest.mark.asyncio
async def test_volume_mute_sends_zero(airplay_player: AirPlayPlayer) -> None:
    """Muting with a running stream should send VOLUME=0."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_level = 75

    await airplay_player.volume_mute(True)

    send_cmd.assert_called_once_with("VOLUME=0")
    assert airplay_player._attr_volume_muted is True


@pytest.mark.asyncio
async def test_volume_set_skipped_while_muted(airplay_player: AirPlayPlayer) -> None:
    """Volume changes while muted should NOT send a CLI command."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_muted = True

    await airplay_player.volume_set(60)

    send_cmd.assert_not_called()
    assert airplay_player._attr_volume_level == 60


@pytest.mark.asyncio
async def test_volume_set_records_level_before_sending(airplay_player: AirPlayPlayer) -> None:
    """A resync reading the level mid-send must observe the new volume, not the old one."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_level = 20
    observed: list[int | None] = []

    async def read_level_while_sending(_command: str) -> bool:
        # stands in for the connect-time volume resync, which reads the player's
        # level while this send is still suspended
        await asyncio.sleep(0)
        observed.append(airplay_player.volume_level)
        return True

    send_cmd.side_effect = read_level_while_sending

    await airplay_player.volume_set(80)

    assert observed == [80]
    assert airplay_player.volume_level == 80


@pytest.mark.asyncio
async def test_volume_set_records_level_when_the_send_fails(
    airplay_player: AirPlayPlayer,
) -> None:
    """A dropped command must not lose the requested level; the resync repairs the device."""
    send_cmd = _setup_running_stream(airplay_player)
    send_cmd.return_value = False
    airplay_player._attr_volume_level = 20

    await airplay_player.volume_set(80)

    send_cmd.assert_awaited_once_with("VOLUME=80")
    assert airplay_player.volume_level == 80


@pytest.mark.asyncio
async def test_volume_unmute_restores_volume(airplay_player: AirPlayPlayer) -> None:
    """Unmuting with a running stream should send VOLUME={current_volume}."""
    send_cmd = _setup_running_stream(airplay_player)
    airplay_player._attr_volume_level = 42
    airplay_player._attr_volume_muted = True

    await airplay_player.volume_mute(False)

    send_cmd.assert_called_once_with("VOLUME=42")
    assert airplay_player._attr_volume_muted is False


@pytest.mark.asyncio
async def test_volume_mute_no_stream(airplay_player: AirPlayPlayer) -> None:
    """Muting without a running stream should update state without CLI commands."""
    airplay_player.stream = None

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        await airplay_player.volume_mute(True)

        assert airplay_player._attr_volume_muted is True
        mock_update.assert_called_once()


def test_sync_volume_level_keeps_stored_volume_for_native_parent(
    airplay_player: AirPlayPlayer,
) -> None:
    """Keep the child AirPlay volume when the parent uses native volume control."""
    parent = MagicMock()
    parent.state.volume_level = 36
    parent.volume_control = PLAYER_CONTROL_NATIVE
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 48

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.sync_volume_level()

    assert airplay_player._attr_volume_level == 48
    airplay_player.mass.config.set_raw_player_config_value.assert_not_called()  # type: ignore[attr-defined]
    mock_update.assert_not_called()


def test_update_volume_from_device_keeps_native_parent_feedback(
    airplay_player: AirPlayPlayer,
) -> None:
    """Use DACP feedback to keep the child AirPlay volume current."""
    parent = MagicMock()
    parent.state.volume_level = 42
    parent.volume_control = PLAYER_CONTROL_NATIVE
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.config.get_value.return_value = False  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 57
    airplay_player.last_command_sent = time.time()

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.update_volume_from_device(57)

    assert airplay_player._attr_volume_level == 57
    airplay_player.mass.config.set_raw_player_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        airplay_player.player_id, CONF_STORED_VOLUME, 57
    )
    mock_update.assert_called_once()


def test_sync_volume_level_uses_parent_volume_when_control_is_self(
    airplay_player: AirPlayPlayer,
) -> None:
    """Pull the parent volume when the parent's volume control is this player."""
    parent = MagicMock()
    parent.state.volume_level = 42
    parent.volume_control = "test_player"
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 48

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.sync_volume_level()

    assert airplay_player._attr_volume_level == 42
    airplay_player.mass.config.set_raw_player_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        airplay_player.player_id,
        CONF_STORED_VOLUME,
        42,
    )
    mock_update.assert_called_once()


def test_sync_volume_level_uses_parent_volume_via_bridge_control(
    airplay_player: AirPlayPlayer,
) -> None:
    """Pull the parent volume when the volume control is a bridge riding on this player."""
    parent = MagicMock()
    parent.state.volume_level = 42
    parent.volume_control = "sendspin_bridge"
    bridge = MagicMock()
    bridge.underlying_player_id = "test_player"
    airplay_player.mass.players.get_player.side_effect = {  # type: ignore[attr-defined]
        "parent": parent,
        "sendspin_bridge": bridge,
    }.get
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 48

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.sync_volume_level()

    assert airplay_player._attr_volume_level == 42
    airplay_player.mass.config.set_raw_player_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        airplay_player.player_id,
        CONF_STORED_VOLUME,
        42,
    )
    mock_update.assert_called_once()


def test_sync_volume_level_unity_gain_when_other_control_owns_volume(
    airplay_player: AirPlayPlayer,
) -> None:
    """Play at unity gain when the parent volume is owned by another (hardware) control."""
    parent = MagicMock()
    parent.state.volume_level = 30
    parent.volume_control = "dlna_player"
    dlna_player = MagicMock()
    dlna_player.underlying_player_id = None
    airplay_player.mass.players.get_player.side_effect = {  # type: ignore[attr-defined]
        "parent": parent,
        "dlna_player": dlna_player,
    }.get
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 48

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.sync_volume_level()

    assert airplay_player._attr_volume_level == 100
    airplay_player.mass.config.set_raw_player_config_value.assert_not_called()  # type: ignore[attr-defined]
    mock_update.assert_called_once()


def test_sync_volume_level_ignores_parent_volume_zero(
    airplay_player: AirPlayPlayer,
) -> None:
    """
    Keep the last known volume when the parent reports volume 0.

    An idle sibling interface (e.g. the cast side of the same device in standby)
    may feed the parent a volume of 0 that doesn't reflect the real device volume;
    adopting it would start the stream hard muted.
    """
    parent = MagicMock()
    parent.state.volume_level = 0
    parent.volume_control = "test_player"
    airplay_player.mass.players.get_player.return_value = parent  # type: ignore[attr-defined]
    airplay_player.set_protocol_parent_id("parent")
    airplay_player._attr_volume_level = 48

    with patch.object(AirPlayPlayer, "update_state") as mock_update:
        airplay_player.sync_volume_level()

    assert airplay_player._attr_volume_level == 48
    airplay_player.mass.config.set_raw_player_config_value.assert_not_called()  # type: ignore[attr-defined]
    mock_update.assert_not_called()


# --- Pause / stop dispatch tests ---


def test_supported_features_always_includes_pause(airplay_player: AirPlayPlayer) -> None:
    """
    PAUSE stays advertised whether or not the player is grouped.

    Keeping PAUSE keeps the AirPlay player itself as the pause control target, so a
    grouped pause parks the complete session (see pause()) instead of the players
    controller falling through to a linked native player's pause, which would only
    pause the sync leader while the other members keep playing.
    """
    airplay_player._attr_group_members = []
    assert PlayerFeature.PAUSE in airplay_player.supported_features
    # sync leader: still advertises PAUSE
    airplay_player._attr_group_members = ["test_player", "child"]
    assert PlayerFeature.PAUSE in airplay_player.supported_features


@pytest.mark.asyncio
async def test_single_player_play_sends_action_play(airplay_player: AirPlayPlayer) -> None:
    """An unsynced player resumes its paused stream in place with ACTION=PLAY."""
    airplay_player._attr_group_members = []
    send_cmd = _setup_running_stream(airplay_player)
    send_cmd.return_value = True

    await airplay_player.play()

    send_cmd.assert_awaited_once_with("ACTION=PLAY")
    # resume re-anchors the binary, so the tracked re-anchor shift is reset to match
    cast("MagicMock", airplay_player.stream).reset_reanchor_shift.assert_called_once_with()


@pytest.mark.asyncio
async def test_single_player_play_keeps_shift_when_resume_not_delivered(
    airplay_player: AirPlayPlayer,
) -> None:
    """An undelivered ACTION=PLAY leaves the tracked re-anchor shift untouched."""
    airplay_player._attr_group_members = []
    send_cmd = _setup_running_stream(airplay_player)
    send_cmd.return_value = False

    await airplay_player.play()

    send_cmd.assert_awaited_once_with("ACTION=PLAY")
    cast("MagicMock", airplay_player.stream).reset_reanchor_shift.assert_not_called()


@pytest.mark.asyncio
async def test_grouped_play_resumes_active_native_queue(airplay_player: AirPlayPlayer) -> None:
    """A linked AirPlay group resumes the queue owned by its native parent."""
    airplay_player._attr_group_members = ["test_player", "child"]
    send_cmd = _setup_running_stream(airplay_player)
    active_queue = MagicMock(queue_id="native_parent")

    with (
        patch.object(
            airplay_player.mass.players, "get_active_queue", return_value=active_queue
        ) as get_active_queue,
        patch.object(
            airplay_player.mass.player_queues, "resume", new_callable=AsyncMock
        ) as resume_queue,
    ):
        await airplay_player.play()

    get_active_queue.assert_called_once_with(airplay_player)
    resume_queue.assert_awaited_once_with("native_parent", fade_in=False)
    send_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_player_pause_sends_action_pause(airplay_player: AirPlayPlayer) -> None:
    """An unsynced player pauses the stream in place with ACTION=PAUSE."""
    airplay_player._attr_group_members = []
    airplay_player.mass.players.iter_players.return_value = []  # type: ignore[attr-defined]
    send_cmd = _setup_running_stream(airplay_player)

    with patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as mock_stop:
        await airplay_player.pause()

    send_cmd.assert_called_once_with("ACTION=PAUSE")
    mock_stop.assert_not_called()


@pytest.mark.asyncio
async def test_grouped_leader_pause_parks_session(airplay_player: AirPlayPlayer) -> None:
    """A sync leader pauses by parking the session (standby), never sending ACTION=PAUSE."""
    airplay_player._attr_group_members = ["test_player", "child"]
    send_cmd = _setup_running_stream(airplay_player)
    assert airplay_player.stream is not None
    session = cast("MagicMock", airplay_player.stream.session)
    session.standby = AsyncMock(return_value=True)

    with patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as mock_stop:
        await airplay_player.pause()

    session.standby.assert_awaited_once()
    mock_stop.assert_not_called()
    send_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_grouped_pause_falls_back_to_stop(airplay_player: AirPlayPlayer) -> None:
    """When a member cannot be parked, grouped pause stops the session."""
    airplay_player._attr_group_members = ["test_player", "child"]
    send_cmd = _setup_running_stream(airplay_player)
    assert airplay_player.stream is not None
    session = cast("MagicMock", airplay_player.stream.session)
    session.standby = AsyncMock(return_value=False)

    with patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as mock_stop:
        await airplay_player.pause()

    mock_stop.assert_called_once()
    send_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_synced_child_pause_parks_session(airplay_player: AirPlayPlayer) -> None:
    """A synced child also pauses by parking the shared session, never ACTION=PAUSE."""
    airplay_player._attr_group_members = []
    send_cmd = _setup_running_stream(airplay_player)
    assert airplay_player.stream is not None
    session = cast("MagicMock", airplay_player.stream.session)
    session.standby = AsyncMock(return_value=True)

    with (
        patch.object(AirPlayPlayer, "synced_to", new_callable=PropertyMock, return_value="parent"),
        patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as mock_stop,
    ):
        await airplay_player.pause()

    session.standby.assert_awaited_once()
    mock_stop.assert_not_called()
    send_cmd.assert_not_called()


# --- Automatic group re-join after unexpected stream loss ---


def _make_idle_player(player_id: str = "test_player") -> AirPlayPlayer:
    """Create an idle, ungrouped AirPlayPlayer wired for the re-join tests."""
    player = AirPlayPlayer(
        provider=MagicMock(),
        player_id=player_id,
        display_name=f"Player {player_id}",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )
    # the synced_to property scans all players of the provider
    _players_mock(player).iter_players.return_value = []
    player._attr_group_members = []
    player._attr_playback_state = PlaybackState.IDLE
    player.stream = None
    return player


def _make_playing_leader(player_id: str = "leader") -> AirPlayPlayer:
    """Create an AirPlayPlayer that looks like the playing leader of a live session."""
    leader = _make_idle_player(player_id)
    leader._attr_playback_state = PlaybackState.PLAYING
    stream = MagicMock()
    stream.running = True
    stream.session = MagicMock()
    leader.stream = stream
    return leader


def _players_mock(player: AirPlayPlayer) -> MagicMock:
    """Return the mocked players controller of the given player."""
    return cast("MagicMock", player.mass.players)


def _attach_running_session(player: AirPlayPlayer, sync_clients: list[AirPlayPlayer]) -> None:
    """Attach a mock running stream whose session carries the given members."""
    stream = MagicMock()
    stream.running = True
    stream.session = MagicMock()
    stream.session.sync_clients = sync_clients
    player.stream = stream


_NO_DELAYS = "music_assistant.providers.airplay.player.AIRPLAY_REJOIN_ATTEMPT_DELAYS"


@pytest.mark.asyncio
async def test_rejoin_succeeds_on_first_attempt() -> None:
    """A re-join attempt joins the player back to the playing leader's session."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)

    async def cmd_group(player_id: str, target_id: str) -> None:
        assert player_id == player.player_id
        assert target_id == leader.player_id
        _attach_running_session(player, [leader, player])

    players_mock.cmd_group = AsyncMock(side_effect=cmd_group)
    players_mock.cmd_ungroup = AsyncMock()

    with patch(_NO_DELAYS, (0, 0, 0)):
        await player._group_rejoin_attempts(["leader"])

    assert players_mock.cmd_group.await_count == 1
    players_mock.cmd_ungroup.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejoin_retries_after_failure_then_succeeds() -> None:
    """A failed attempt (device still unreachable) is retried with backoff."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)
    attempts: list[int] = []

    async def cmd_group(_player_id: str, _target_id: str) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise PlayerCommandFailed("device unreachable")
        _attach_running_session(player, [leader, player])

    players_mock.cmd_group = AsyncMock(side_effect=cmd_group)

    with patch(_NO_DELAYS, (0, 0, 0)):
        await player._group_rejoin_attempts(["leader"])

    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_rejoin_gives_up_after_all_attempts() -> None:
    """Without a playing group to re-join, the attempts run out and stop cleanly."""
    player = _make_idle_player()
    players_mock = _players_mock(player)
    players_mock.get_player.return_value = None
    players_mock.cmd_group = AsyncMock()

    with patch(_NO_DELAYS, (0, 0, 0)):
        await player._group_rejoin_attempts(["leader"])

    players_mock.cmd_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejoin_aborts_when_player_used_meanwhile() -> None:
    """A player that was grouped or repurposed meanwhile is left alone."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)
    players_mock.cmd_group = AsyncMock()
    # the user started something else on the player during the backoff
    stream = MagicMock()
    stream.running = True
    player.stream = stream

    with patch(_NO_DELAYS, (0, 0, 0)):
        await player._group_rejoin_attempts(["leader"])

    players_mock.cmd_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejoin_undoes_dangling_membership() -> None:
    """A join that yields no running stream is rolled back before the next attempt."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)
    # cmd_group "succeeds" but the late-join failed internally: no stream appears
    players_mock.cmd_group = AsyncMock()
    players_mock.cmd_ungroup = AsyncMock()

    with patch(_NO_DELAYS, (0, 0, 0)):
        await player._group_rejoin_attempts(["leader"])

    # every attempt rolled its dangling membership back
    assert players_mock.cmd_group.await_count == 3
    assert players_mock.cmd_ungroup.await_count == 3


@pytest.mark.asyncio
async def test_rejoin_cancelled_when_player_unavailable() -> None:
    """An offline player abandons the re-join right away instead of attempting."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)
    players_mock.cmd_group = AsyncMock()
    player._attr_available = False

    # the later long delays prove the loop returns on the first pass
    with patch(_NO_DELAYS, (0, 60, 60)):
        await asyncio.wait_for(player._group_rejoin_attempts(["leader"]), timeout=5)

    players_mock.cmd_group.assert_not_awaited()
    players_mock.get_player.assert_not_called()


@pytest.mark.asyncio
async def test_rejoin_aborts_when_synced_into_foreign_group() -> None:
    """A player the user grouped elsewhere meanwhile is left alone."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    foreign_leader = MagicMock()
    foreign_leader.player_id = "other"
    foreign_leader.group_members = ["other", player.player_id]
    # the player reports it is now synced to a leader outside the original group
    _players_mock(player).iter_players.return_value = [foreign_leader]
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)
    players_mock.cmd_group = AsyncMock()

    with patch(_NO_DELAYS, (0, 60, 60)):
        await asyncio.wait_for(player._group_rejoin_attempts(["leader"]), timeout=5)

    players_mock.cmd_group.assert_not_awaited()


def test_resolve_rejoin_target_finds_promoted_sibling() -> None:
    """When the old leader is gone, a promoted (now leading) sibling is the target."""
    player = _make_idle_player()
    sibling = _make_playing_leader("sibling")
    sibling._attr_group_members = ["sibling", "other_member"]
    _players_mock(player).get_player.side_effect = lambda player_id: {"sibling": sibling}.get(
        player_id
    )

    assert player._resolve_rejoin_target(["old_leader", "sibling"]) is sibling


def test_resolve_rejoin_target_skips_candidate_in_foreign_group() -> None:
    """A candidate absorbed into another group is never followed there."""
    player = _make_idle_player()
    foreign_leader = _make_playing_leader("foreign")
    foreign_leader._attr_group_members = ["foreign", "old_leader"]
    old_leader = _make_playing_leader("old_leader")
    # the old leader reports it is now synced to the foreign leader
    _players_mock(old_leader).iter_players.return_value = [foreign_leader]
    _players_mock(player).get_player.side_effect = lambda player_id: {
        "old_leader": old_leader,
        "foreign": foreign_leader,
    }.get(player_id)

    assert player._resolve_rejoin_target(["old_leader"]) is None


@pytest.mark.parametrize(
    ("playback_state", "stream_running", "available", "expected"),
    [
        (PlaybackState.PLAYING, True, True, True),
        # a parked (paused) session has no live timeline to late-join
        (PlaybackState.PAUSED, True, True, False),
        (PlaybackState.IDLE, False, True, False),
        # target device itself dropped off the network
        (PlaybackState.PLAYING, True, False, False),
    ],
)
def test_resolve_rejoin_target_requires_playing_session(
    playback_state: PlaybackState, stream_running: bool, available: bool, expected: bool
) -> None:
    """Only an available target with an actively playing session is accepted."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    leader._attr_playback_state = playback_state
    cast("MagicMock", leader.stream).running = stream_running
    leader._attr_available = available
    _players_mock(player).get_player.side_effect = lambda player_id: {"leader": leader}.get(
        player_id
    )

    target = player._resolve_rejoin_target(["leader"])
    assert (target is leader) is expected


@pytest.mark.asyncio
async def test_rejoin_heals_session_when_membership_survived() -> None:
    """A player still holding sync membership (static group) heals the session only."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    # the sync membership survived the stream loss: the player is still listed
    # as a member of (and synced to) the leader
    leader._attr_group_members = ["leader", player.player_id]
    _players_mock(player).iter_players.return_value = [leader]
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)
    players_mock.cmd_group = AsyncMock()
    players_mock.cmd_ungroup = AsyncMock()
    session = cast("MagicMock", leader.stream).session

    async def add_client(joiner: AirPlayPlayer) -> None:
        assert joiner is player
        _attach_running_session(player, [leader, player])

    session.add_client = AsyncMock(side_effect=add_client)

    with patch(_NO_DELAYS, (0,)):
        await player._group_rejoin_attempts(["leader"])

    session.add_client.assert_awaited_once()
    players_mock.cmd_group.assert_not_awaited()
    players_mock.cmd_ungroup.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejoin_session_heal_failure_keeps_membership() -> None:
    """A failed session heal never touches the (configured) group membership."""
    player = _make_idle_player()
    leader = _make_playing_leader()
    leader._attr_group_members = ["leader", player.player_id]
    _players_mock(player).iter_players.return_value = [leader]
    players_mock = _players_mock(player)
    players_mock.get_player.side_effect = lambda player_id: {"leader": leader}.get(player_id)
    players_mock.cmd_group = AsyncMock()
    players_mock.cmd_ungroup = AsyncMock()
    session = cast("MagicMock", leader.stream).session
    # the late-join fails internally: no stream appears on the player
    session.add_client = AsyncMock()

    with patch(_NO_DELAYS, (0,)):
        await player._group_rejoin_attempts(["leader"])

    session.add_client.assert_awaited_once()
    players_mock.cmd_ungroup.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_and_cancel_group_rejoin() -> None:
    """Scheduling replaces a pending task and cancelling stops it."""
    player = _make_idle_player()
    _players_mock(player).get_player.return_value = None
    cast("MagicMock", player.mass).create_task = lambda coro: (
        asyncio.get_running_loop().create_task(coro)
    )

    with patch(_NO_DELAYS, (60, 60, 60)):
        player.schedule_group_rejoin(["leader"])
        first_task = player._rejoin_task
        assert first_task is not None
        # a second death replaces the pending schedule
        player.schedule_group_rejoin(["leader"])
        second_task = player._rejoin_task
        assert second_task is not None
        assert second_task is not first_task
        await asyncio.sleep(0)
        assert first_task.cancelled()
        # any deliberate use of the player cancels the pending re-join
        player.cancel_group_rejoin()
        assert player._rejoin_task is None
        await asyncio.sleep(0)
        assert second_task.cancelled()


@pytest.mark.asyncio
async def test_stop_cancels_pending_rejoin() -> None:
    """An explicit stop command on the player drops the pending re-join."""
    player = _make_idle_player()
    _players_mock(player).get_player.return_value = None
    cast("MagicMock", player.mass).create_task = lambda coro: (
        asyncio.get_running_loop().create_task(coro)
    )

    with (
        patch(_NO_DELAYS, (60,)),
        patch.object(AirPlayPlayer, "update_state"),
    ):
        player.schedule_group_rejoin(["leader"])
        rejoin_task = player._rejoin_task
        assert rejoin_task is not None
        await player.stop()
        assert player._rejoin_task is None
        await asyncio.sleep(0)
        assert rejoin_task.cancelled()


# --- Group membership and the leader's stream session ---


@pytest.mark.asyncio
async def test_set_members_adds_the_child_to_the_running_session() -> None:
    """A member joining a leader with a live session is added to that session."""
    leader = _make_playing_leader()
    child = _make_idle_player("child")
    _attach_running_session(leader, [leader])
    session = cast("MagicMock", leader.stream).session
    session.add_client = AsyncMock()
    _players_mock(leader).get_player.side_effect = lambda player_id: {"child": child}.get(player_id)

    await leader.set_members(player_ids_to_add=["child"])

    session.add_client.assert_awaited_once_with(child)
    assert leader.group_members == ["leader", "child"]


@pytest.mark.asyncio
async def test_set_members_warns_when_the_leader_has_no_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A member joining a leader that renders through a protocol gets no audio, loudly."""
    leader = _make_idle_player("leader")
    leader.logger = logging.getLogger("test.airplay.player")
    child = _make_idle_player("child")
    _players_mock(leader).get_player.side_effect = lambda player_id: {"child": child}.get(player_id)
    # the leader hands its audio to one of its output protocols, so it has no session
    leader.set_active_output_protocol("bridge_leader")

    with caplog.at_level(logging.WARNING):
        await leader.set_members(player_ids_to_add=["child"])

    assert leader.group_members == ["leader", "child"]
    assert "no stream session to join" in caplog.text


# --- Device password ---


def _set_password_discovery(
    player: AirPlayPlayer,
    *,
    flags: str = "0x0",
    pw: str = "",
    password: str | None = None,
    paired: bool = False,
) -> None:
    """
    Attach an AirPlay 2 + RAOP device announcing password protection.

    :param flags: The _airplay service sf/flags bitmask (0x80 marks a password).
    :param pw: The legacy ``pw`` boolean published by the _raop service.
    :param password: The device password stored in the player config, if any.
    :param paired: Whether AirPlay 2 pairing credentials are stored for the device.
    """
    airplay_info = MagicMock()
    airplay_info.decoded_properties = {"features": AP2_FEATURES, "flags": flags}
    airplay_info.properties = {b"flags": flags.encode()}
    player.airplay_discovery_info = airplay_info
    raop_info = MagicMock()
    raop_info.decoded_properties = {"pw": pw} if pw else {}
    raop_info.properties = {}
    player.raop_discovery_info = raop_info
    _configure_player(player, {CONF_FORCE_RAOP: False, CONF_PASSWORD: password})
    credentials = {CONF_AIRPLAY_CREDENTIALS: "a" * 192} if paired else {}
    player.get_setup_value = (  # type: ignore[method-assign]
        lambda key, default=None: credentials.get(key, default)
    )


@pytest.mark.asyncio
async def test_password_entry_is_never_offered_in_the_settings(
    airplay_player: AirPlayPlayer,
) -> None:
    """The password is storage only: the setup flow is the sole way to enter it."""
    _set_password_discovery(airplay_player, flags="0x80")
    assert airplay_player.password_required is True

    entries = await airplay_player.get_config_entries()
    entry = next(entry for entry in entries if entry.key == CONF_PASSWORD)
    assert entry.hidden is True


@pytest.mark.parametrize(
    ("flags", "pw"),
    [
        # AirPlay 2 announces password protection through the flags bit...
        ("0x80", ""),
        # ...a legacy RAOP receiver through the classic pw boolean
        ("0x0", "true"),
    ],
)
def test_announced_password_without_one_stored_needs_setup(
    airplay_player: AirPlayPlayer, flags: str, pw: str
) -> None:
    """A device that asks for a password it never got must be set up first."""
    _set_password_discovery(airplay_player, flags=flags, pw=pw)

    assert airplay_player.password_required is True
    assert airplay_player.needs_setup is True
    assert airplay_player.setup_reason == "password_required"


def test_apple_tv_password_bit_alone_does_not_need_setup(airplay_player: AirPlayPlayer) -> None:
    """Apple TVs raise the generic password bit at all times; it means nothing there."""
    # a paired Apple TV without a password set must not be sent back into setup
    _set_password_discovery(airplay_player, flags="0x4c4", paired=True)
    airplay_player.device_info.model = "AppleTV14,1"

    assert airplay_player.password_required is False
    assert airplay_player.needs_setup is False


def test_apple_tv_with_a_password_set_needs_setup(airplay_player: AirPlayPlayer) -> None:
    """The tvOS-specific flags bit is the Apple TV's only password announcement."""
    _set_password_discovery(airplay_player, flags="0x14c4")
    airplay_player.device_info.model = "AppleTV11,1"

    assert airplay_player.password_required is True
    assert airplay_player.needs_setup is True
    assert airplay_player.setup_reason == "password_required"


@pytest.mark.parametrize(
    ("flags", "pw", "paired"),
    [
        # AirPlay 2 collects the password as part of pairing, so it ends up with both
        ("0x80", "", True),
        # a legacy RAOP receiver has no pairing at all: the password is enough
        ("0x0", "true", False),
    ],
)
def test_stored_password_clears_the_setup_requirement(
    airplay_player: AirPlayPlayer, flags: str, pw: str, paired: bool
) -> None:
    """Once the password is stored the player is ready to use again."""
    _set_password_discovery(airplay_player, flags=flags, pw=pw, password="hunter2", paired=paired)

    assert airplay_player.needs_setup is False
    assert airplay_player.setup_reason is None


def test_rejected_password_marker_forces_setup(airplay_player: AirPlayPlayer) -> None:
    """A password the device rejected sends an otherwise ready player back into setup."""
    # the migration case: a paired device that gained password protection later
    _set_password_discovery(airplay_player, flags="0x80", password="wrong", paired=True)
    ready_before = airplay_player.needs_setup

    airplay_player.set_password_invalid(True)

    assert ready_before is False
    assert airplay_player.password_invalid is True
    assert airplay_player.needs_setup is True
    assert airplay_player.setup_reason == "password_required"


def test_rejected_password_marker_survives_a_restart(airplay_player: AirPlayPlayer) -> None:
    """The marker is persisted as a raw player config value, not just in memory."""
    _set_password_discovery(airplay_player, flags="0x80", password="wrong", paired=True)

    airplay_player.set_password_invalid(True)

    airplay_player.mass.config.set_raw_player_config_value.assert_called_once_with(  # type: ignore[attr-defined]
        "test_player", CONF_PASSWORD_INVALID, True
    )


def test_clearing_the_marker_only_writes_when_it_was_set(airplay_player: AirPlayPlayer) -> None:
    """Every successful connect clears the marker, but must not write the config."""
    _set_password_discovery(airplay_player, flags="0x80", password="hunter2", paired=True)
    set_raw = airplay_player.mass.config.set_raw_player_config_value

    airplay_player.set_password_invalid(False)
    set_raw.assert_not_called()  # type: ignore[attr-defined]

    airplay_player.set_password_invalid(True)
    assert airplay_player.needs_setup is True
    airplay_player.set_password_invalid(False)

    assert airplay_player.password_invalid is False
    assert airplay_player.needs_setup is False


def test_rejected_password_is_published_to_clients(airplay_player: AirPlayPlayer) -> None:
    """The new setup requirement must reach the wire state, not just the property."""
    _set_password_discovery(airplay_player, flags="0x80", password="wrong", paired=True)
    airplay_player.update_state()
    before = airplay_player.state
    assert before.needs_setup is False
    assert before.available is True

    airplay_player.set_password_invalid(True)

    # needs_setup/setup_reason are part of the player's own state inputs, so the
    # update is neither short-circuited nor left to the next unrelated update
    after = airplay_player.state
    assert after.needs_setup is True
    assert after.setup_reason == "password_required"
    assert after.available is False
    changed = airplay_player.mass.players.signal_player_state_update.call_args[0][1]  # type: ignore[attr-defined]
    assert "needs_setup" in changed


def test_pin_pairing_keeps_its_own_setup_reason(airplay_player: AirPlayPlayer) -> None:
    """A device that only needs PIN pairing is not reported as a password problem."""
    airplay_info = MagicMock()
    airplay_info.decoded_properties = {"features": AP2_FEATURES}
    airplay_info.properties = {b"flags": b"0x8"}
    airplay_player.airplay_discovery_info = airplay_info
    airplay_player.get_setup_value = lambda key, default=None: default  # type: ignore[method-assign]  # noqa: ARG005

    assert airplay_player.needs_setup is True
    assert airplay_player.setup_reason == "pairing_required"
