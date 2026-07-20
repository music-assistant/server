"""Unit tests for AirPlay player."""

import time
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from music_assistant_models.constants import PLAYER_CONTROL_NATIVE
from music_assistant_models.enums import ConfigEntryType, ContentType, PlayerFeature
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_SYNC_ADJUST
from music_assistant.providers.airplay.constants import (
    AIRPLAY_PCM_FORMAT,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_FORCE_RAOP,
    CONF_HIRES_PLAYBACK,
    CONF_IGNORE_VOLUME,
    CONF_RAOP_CREDENTIALS,
    CONF_STORED_VOLUME,
    StreamingProtocol,
)
from music_assistant.providers.airplay.player import AirPlayPlayer

# _airplay._tcp features bitmask with the AirPlay 2 feature bits set (bit 38/48).
AP2_FEATURES = "0x4A7FDFD5,0x3C177FDE"


@pytest.fixture
def airplay_player() -> AirPlayPlayer:
    """Create a basic AirPlayPlayer with mock defaults."""
    return AirPlayPlayer(
        provider=MagicMock(),
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=None,
    )


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
        (None, {b"flags": "0x80"}, True),
        (None, {b"sf": b"0x81"}, True),
        (None, {b"flags": b"0x4"}, False),
        (None, {b"sf": b"0x80"}, True),
        (None, {b"flags": b"0x90"}, True),
        ({}, {}, False),
    ],
)
def test_requires_password_pairing(
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
    assert airplay_player._requires_password_pairing() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flags", "pin_call_expected"),
    [
        (b"0x8", True),
        (b"0x80", False),
    ],
)
async def test_start_pairing__pin_decision(flags: bytes, pin_call_expected: bool) -> None:
    """Ensure _start_pairing skips the PIN request when only password pairing is required."""
    provider = MagicMock()
    provider.dacp_id = "test_dacp"

    aiplay_info = MagicMock()
    aiplay_info.properties = {b"flags": flags}
    aiplay_info.port = 7000

    player = AirPlayPlayer(
        provider=provider,
        player_id="test_player",
        display_name="Test Player",
        address="127.0.0.1",
        manufacturer="Test Manufacturer",
        model="Test Model",
        raop_discovery_info=None,
        airplay_discovery_info=aiplay_info,
    )

    pairing_instance = AsyncMock()
    pairing_instance.start_pairing_session = AsyncMock()
    pairing_instance.start_pin_pairing = AsyncMock()

    with patch(
        "music_assistant.providers.airplay.pairing.AirPlayPairing",
        return_value=pairing_instance,
    ):
        await player._start_pairing(StreamingProtocol.AIRPLAY2, "AirPlay2")

    pairing_instance.start_pairing_session.assert_called_once()
    if pin_call_expected:
        pairing_instance.start_pin_pairing.assert_called_once()
    else:
        pairing_instance.start_pin_pairing.assert_not_called()


@pytest.mark.asyncio
async def test_config_entries_include_ignore_volume(airplay_player: AirPlayPlayer) -> None:
    """The ignore_volume setting must be offered in the player config entries."""
    entries = await airplay_player.get_config_entries()
    assert any(entry.key == CONF_IGNORE_VOLUME for entry in entries)


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
    _configure_player(airplay_player, dict(stored_config))
    assert airplay_player.needs_setup is expected


# --- Hi-res playback tests ---


def _configure_player(player: AirPlayPlayer, values: dict[str, object]) -> None:
    """Stub the player config to return the given values."""
    player.config.get_value.side_effect = (  # type: ignore[attr-defined]
        lambda key, default=None: values.get(key, default)
    )


@pytest.mark.parametrize(
    ("hires_enabled", "force_raop", "has_airplay_info", "expected"),
    [
        # hi-res on an AirPlay 2 capable device advertises the 24-bit rates
        (True, False, True, [(44100, 24), (48000, 24)]),
        # forced RAOP cannot do 24-bit: falls back to the 16-bit base
        (True, True, True, [(44100, 16)]),
        # no _airplay._tcp service: hi-res not possible
        (True, False, False, [(44100, 16)]),
        # option disabled: the 16-bit default
        (False, False, True, [(44100, 16)]),
    ],
)
def test_hires_supported_sample_rates(
    airplay_player: AirPlayPlayer,
    hires_enabled: bool,
    force_raop: bool,
    has_airplay_info: bool,
    expected: list[tuple[int, int]],
) -> None:
    """The hi-res toggle drives the advertised sample rates (AirPlay 2, not forced to RAOP)."""
    _set_discovery_info(
        airplay_player, raop=True, airplay=has_airplay_info, airplay_features=AP2_FEATURES
    )
    _configure_player(
        airplay_player,
        {CONF_HIRES_PLAYBACK: hires_enabled, CONF_FORCE_RAOP: force_raop},
    )
    assert airplay_player.supported_sample_rates == expected


def test_get_stream_pcm_format_hires(airplay_player: AirPlayPlayer) -> None:
    """With hi-res enabled the stream format is 24-bit in a s32le container."""
    _set_discovery_info(airplay_player, raop=True, airplay=True)
    _configure_player(airplay_player, {CONF_HIRES_PLAYBACK: True, CONF_FORCE_RAOP: False})

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
    """Without hi-res the stream format is the 44.1/16 default."""
    _set_discovery_info(airplay_player, raop=True, airplay=True)
    _configure_player(airplay_player, {CONF_HIRES_PLAYBACK: False, CONF_FORCE_RAOP: False})
    session_format = AudioFormat(
        content_type=ContentType.PCM_F32LE, sample_rate=48000, bit_depth=32
    )
    assert airplay_player.get_stream_pcm_format(session_format) == AIRPLAY_PCM_FORMAT


def test_session_pcm_format_selection(airplay_player: AirPlayPlayer) -> None:
    """The session runs at 48 kHz only for 48k content when every member supports it."""
    hires_client = MagicMock()
    hires_client.supported_sample_rates = [(44100, 24), (48000, 24)]
    cd_client = MagicMock()
    cd_client.supported_sample_rates = [(44100, 16)]
    media = MagicMock()
    media.source_id = "queue1"
    media.queue_item_id = "item1"
    queue_item = MagicMock()
    queue_item.streamdetails.audio_format.sample_rate = 48000
    airplay_player.mass.player_queues.get_item.return_value = queue_item  # type: ignore[attr-defined]

    # all members hi-res capable + 48k-family content: session lifts to 48 kHz
    fmt = airplay_player._get_session_pcm_format([hires_client, hires_client], media)
    assert fmt.sample_rate == 48000

    # a 16-bit member pins the session at the 44.1 kHz base
    fmt = airplay_player._get_session_pcm_format([hires_client, cd_client], media)
    assert fmt.sample_rate == 44100

    # 44.1-family content stays at 44.1 kHz even for an all-hi-res group
    queue_item.streamdetails.audio_format.sample_rate = 88200
    fmt = airplay_player._get_session_pcm_format([hires_client], media)
    assert fmt.sample_rate == 44100


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


def test_sync_volume_level_uses_parent_volume_without_native_parent(
    airplay_player: AirPlayPlayer,
) -> None:
    """Keep existing behavior for protocol parents without native volume control."""
    parent = MagicMock()
    parent.state.volume_level = 42
    parent.volume_control = None
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


# --- Pause / stop dispatch tests ---


def test_supported_features_always_includes_pause(airplay_player: AirPlayPlayer) -> None:
    """
    PAUSE stays advertised whether or not the player is grouped.

    Keeping PAUSE keeps the AirPlay player itself as the pause control target, so a
    grouped pause maps to a full session stop (see pause()) instead of the players
    controller falling through to a linked native player's pause - which would only
    pause the sync leader while the other members keep playing.
    """
    airplay_player._attr_group_members = []
    assert PlayerFeature.PAUSE in airplay_player.supported_features
    # sync leader: still advertises PAUSE
    airplay_player._attr_group_members = ["test_player", "child"]
    assert PlayerFeature.PAUSE in airplay_player.supported_features


@pytest.mark.asyncio
async def test_single_player_pause_sends_action_pause(airplay_player: AirPlayPlayer) -> None:
    """An unsynced player pauses the stream in place with ACTION=PAUSE."""
    airplay_player._attr_group_members = []
    airplay_player.mass.players.all_players.return_value = []  # type: ignore[attr-defined]
    send_cmd = _setup_running_stream(airplay_player)

    with patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as mock_stop:
        await airplay_player.pause()

    send_cmd.assert_called_once_with("ACTION=PAUSE")
    mock_stop.assert_not_called()


@pytest.mark.asyncio
async def test_grouped_leader_pause_stops_session(airplay_player: AirPlayPlayer) -> None:
    """A sync leader pauses by stopping the whole session, never sending ACTION=PAUSE."""
    airplay_player._attr_group_members = ["test_player", "child"]
    send_cmd = _setup_running_stream(airplay_player)

    with patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as mock_stop:
        await airplay_player.pause()

    mock_stop.assert_called_once()
    send_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_synced_child_pause_stops_session(airplay_player: AirPlayPlayer) -> None:
    """A synced child also pauses by stopping the shared session, never ACTION=PAUSE."""
    airplay_player._attr_group_members = []
    send_cmd = _setup_running_stream(airplay_player)

    with (
        patch.object(AirPlayPlayer, "synced_to", new_callable=PropertyMock, return_value="parent"),
        patch.object(AirPlayPlayer, "stop", new=AsyncMock()) as mock_stop,
    ):
        await airplay_player.pause()

    mock_stop.assert_called_once()
    send_cmd.assert_not_called()
