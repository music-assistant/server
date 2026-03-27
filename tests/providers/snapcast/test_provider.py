"""Tests for Snapcast provider stream naming."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bidict import bidict
from music_assistant_models.enums import MediaType
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.snapcast.player import SnapCastPlayer
from music_assistant.providers.snapcast.provider import SnapCastProvider
from music_assistant.providers.snapcast.stream_registry import SnapcastStreamRegistry
from music_assistant.providers.sync_group.constants import SGP_PREFIX


def _create_provider() -> Any:
    """Create a lightweight SnapCastProvider instance for unit tests."""
    provider = SnapCastProvider.__new__(SnapCastProvider)
    provider.mass = MagicMock()
    provider.logger = MagicMock()
    provider._stream_registry = SnapcastStreamRegistry()
    provider._snapcast_ma_streams = provider._stream_registry.streams_by_name
    provider._snapcast_ma_streams_lock = asyncio.Lock()
    provider._use_builtin_server = False
    provider._controlscript_available = False
    provider._snapserver_started = None
    provider._external_dedicated_fallback_group = None
    return provider


def test_resolve_sync_group_player_supports_player_id_and_custom_name_ref() -> None:
    """Resolve sync-group players by player id and configured group name."""
    provider = _create_provider()
    sync_group_player: Any = SimpleNamespace(
        player_id=f"{SGP_PREFIX}abc12345",
        config=SimpleNamespace(name="Kitchen Group"),
    )
    provider.mass.players.get_player.side_effect = lambda ref, raise_unavailable=False: (  # noqa: ARG005
        sync_group_player if ref == sync_group_player.player_id else None
    )
    provider.mass.players.__iter__.return_value = iter([sync_group_player])
    provider.mass.players.all_players.return_value = [sync_group_player]

    assert provider.resolve_sync_group_player(sync_group_player.player_id) is sync_group_player
    assert provider.resolve_sync_group_player("Kitchen Group") is sync_group_player
    assert provider.resolve_sync_group_player(f"{SGP_PREFIX}Kitchen Group") is sync_group_player
    assert provider.resolve_sync_group_player("normal_player") is None


def test_get_sync_group_stream_display_name_uses_custom_player_name() -> None:
    """Return the exact custom sync group name for queue and plugin source playback."""
    provider = _create_provider()
    sync_group_player = SimpleNamespace(config=SimpleNamespace(name="Woonkamer + Keuken"))
    provider.resolve_sync_group_player = MagicMock(return_value=sync_group_player)

    queue_media = PlayerMedia(uri="queue:test", source_id=f"{SGP_PREFIX}abc12345")
    plugin_media = PlayerMedia(
        uri="plugin:test",
        media_type=MediaType.PLUGIN_SOURCE,
        custom_data={"player_id": f"{SGP_PREFIX}abc12345"},
    )

    assert provider._get_sync_group_stream_display_name(queue_media) == "Woonkamer + Keuken"
    assert provider._get_sync_group_stream_display_name(plugin_media) == "Woonkamer + Keuken"


def test_get_snap_ma_stream_matches_internal_name_id_and_display_name() -> None:
    """Lookup should work for all central registry references."""
    provider = _create_provider()
    fake_stream: Any = SimpleNamespace(
        stream_id="snap-id-123",
        stream_display_name="Living Room",
        source_id="source-123",
        queue_id="queue-123",
    )
    provider._snapcast_ma_streams["mass_stream_internal"] = fake_stream

    assert provider.get_snap_ma_stream("mass_stream_internal") is fake_stream
    assert provider.get_snap_ma_stream("snap-id-123") is fake_stream
    assert provider.get_snap_ma_stream("Living Room") is fake_stream
    assert provider.get_snap_ma_stream("source-123") is fake_stream
    assert provider.get_snap_ma_stream("queue-123") is fake_stream
    assert provider.get_snap_ma_stream("missing") is None


def test_resolve_control_stream_requires_active_queue() -> None:
    """External control resolution should ignore streams without an active queue."""
    provider = _create_provider()
    fake_stream: Any = SimpleNamespace(
        queue_id=None,
        source_id="plugin-source",
        stream_id="snap-id-123",
        stream_name="mass_stream_internal",
        stream_display_name="Living Room",
    )
    provider._snapcast_ma_streams["mass_stream_internal"] = fake_stream
    provider.mass.player_queues.get.return_value = None

    assert provider.resolve_control_stream("Living Room") is None


def test_resolve_control_stream_returns_active_queue_payload() -> None:
    """External control resolution should expose the active queue metadata."""
    provider = _create_provider()
    fake_stream: Any = SimpleNamespace(
        queue_id="living-room-player",
        source_id="queue-source",
        stream_id="snap-id-123",
        stream_name="mass_stream_internal",
        stream_display_name="Living Room",
    )
    queue = SimpleNamespace(
        queue_id="living-room-player",
        to_dict=MagicMock(return_value={"queue_id": "living-room-player"}),
    )
    provider._snapcast_ma_streams["mass_stream_internal"] = fake_stream
    provider.mass.player_queues.get.return_value = queue

    assert provider.resolve_control_stream("Living Room") == {
        "player_id": "living-room-player",
        "queue_id": "living-room-player",
        "queue": {"queue_id": "living-room-player"},
        "stream_id": "snap-id-123",
        "stream_name": "mass_stream_internal",
        "stream_display_name": "Living Room",
    }


def test_resolve_control_stream_prefers_queue_backed_match_for_shared_display_name() -> None:
    """Visible stream-name resolution should skip stale idle matches and pick the active queue."""
    provider = _create_provider()
    idle_stream: Any = SimpleNamespace(
        queue_id=None,
        source_id=f"{SGP_PREFIX}abc12345",
        stream_id="broadcast",
        stream_name="mass_stream_idle",
        stream_display_name="broadcast",
    )
    active_stream: Any = SimpleNamespace(
        queue_id="syncgroup_active",
        source_id=f"{SGP_PREFIX}abc12345",
        stream_id="broadcast",
        stream_name="mass_stream_active",
        stream_display_name="broadcast",
    )
    queue = SimpleNamespace(
        queue_id="syncgroup_active",
        to_dict=MagicMock(return_value={"queue_id": "syncgroup_active"}),
    )
    provider._snapcast_ma_streams["mass_stream_idle"] = idle_stream
    provider._snapcast_ma_streams["mass_stream_active"] = active_stream
    provider.mass.player_queues.get.side_effect = lambda queue_id: (
        queue if queue_id == "syncgroup_active" else None
    )

    assert provider.resolve_control_stream("broadcast") == {
        "player_id": "syncgroup_active",
        "queue_id": "syncgroup_active",
        "queue": {"queue_id": "syncgroup_active"},
        "stream_id": "broadcast",
        "stream_name": "mass_stream_active",
        "stream_display_name": "broadcast",
    }


def test_get_snap_ma_stream_prefers_active_queue_backed_match_for_shared_ref() -> None:
    """Shared refs like a visible stream name should prefer the active queue-backed stream."""
    provider = _create_provider()
    idle_stream: Any = SimpleNamespace(
        stream_name="mass_stream_idle",
        stream_id="broadcast",
        stream_display_name="broadcast",
        source_id=f"{SGP_PREFIX}abc12345",
        queue_id=None,
        is_streaming=False,
    )
    active_stream: Any = SimpleNamespace(
        stream_name="mass_stream_active",
        stream_id="broadcast",
        stream_display_name="broadcast",
        source_id="broadcast",
        queue_id="broadcast",
        is_streaming=True,
    )
    provider._snapcast_ma_streams["mass_stream_idle"] = idle_stream
    provider._snapcast_ma_streams["mass_stream_active"] = active_stream

    assert provider.get_snap_ma_stream("broadcast") is active_stream
    assert provider.get_snap_ma_stream("mass_stream_idle") is idle_stream


def test_update_stream_usage_marks_all_matching_stream_variants_in_use() -> None:
    """Shared visible refs should not idle-stop the active queue-backed stream."""
    provider = _create_provider()
    idle_stream = SimpleNamespace(
        stream_name="mass_stream_idle",
        stream_id="broadcast",
        stream_display_name="broadcast",
        set_in_use=MagicMock(),
    )
    active_stream = SimpleNamespace(
        stream_name="mass_stream_active",
        stream_id="broadcast",
        stream_display_name="broadcast",
        set_in_use=MagicMock(),
    )
    other_stream = SimpleNamespace(
        stream_name="mass_stream_other",
        stream_id="elsewhere",
        stream_display_name="Elsewhere",
        set_in_use=MagicMock(),
    )
    provider._snapcast_ma_streams["mass_stream_idle"] = idle_stream
    provider._snapcast_ma_streams["mass_stream_active"] = active_stream
    provider._snapcast_ma_streams["mass_stream_other"] = other_stream
    provider._snapserver = SimpleNamespace(groups=[SimpleNamespace(stream="broadcast")])

    provider.update_stream_usage()

    idle_stream.set_in_use.assert_called_once_with(True)
    active_stream.set_in_use.assert_called_once_with(True)
    other_stream.set_in_use.assert_called_once_with(False)


def test_handle_update_primes_group_member_ids_before_player_setup() -> None:
    """Grouped clients should be resolvable before player setup computes members."""
    provider = _create_provider()
    provider._ids_map = bidict()

    snap_client_1 = SimpleNamespace(
        identifier="PC_VAIO_2",
        friendly_name="PC_VAIO_2",
        set_callback=MagicMock(),
    )
    snap_client_2 = SimpleNamespace(
        identifier="thor-speakers",
        friendly_name="Thor",
        set_callback=MagicMock(),
    )
    provider._snapserver = SimpleNamespace(clients=[snap_client_1, snap_client_2])
    provider.get_snap_player = MagicMock(return_value=None)
    provider._update_group_callbacks = MagicMock()

    seen_clients: list[str] = []

    def fake_handle_player_init(snap_client: Any) -> None:
        assert provider._get_ma_id("PC_VAIO_2") == "PC_VAIO_2"
        assert provider._get_ma_id("thor-speakers") == "thor-speakers"
        seen_clients.append(snap_client.identifier)

    provider._handle_player_init = fake_handle_player_init

    provider._handle_update()

    assert seen_clients == ["PC_VAIO_2", "thor-speakers"]


@pytest.mark.asyncio
async def test_isolate_player_to_dedicated_group_reuses_stream_display_name_for_others() -> None:
    """Removed players should inherit the current stream display name, not a hardcoded default."""
    provider = _create_provider()
    provider._get_snapclient_id = MagicMock(return_value="snap-target")
    provider._get_ma_id = MagicMock(return_value="ma-other")
    provider.ensure_player_owned_group = AsyncMock()
    provider._snapcast_ma_streams["mass_stream_internal"] = SimpleNamespace(
        stream_id="snap-stream-123",
        stream_display_name="Kitchen Group",
    )

    target_group = SimpleNamespace(
        identifier="group-target",
        clients=["snap-target", "snap-other"],
        stream="snap-stream-123",
        set_callback=MagicMock(),
        set_stream=AsyncMock(),
    )
    other_group = SimpleNamespace(
        set_name=AsyncMock(),
        set_stream=AsyncMock(),
    )
    other_client = SimpleNamespace(
        group=other_group,
        set_callback=MagicMock(),
    )
    provider.ensure_player_owned_group.return_value = target_group
    provider._snapserver = SimpleNamespace(
        client=MagicMock(
            side_effect=lambda client_id: other_client if client_id == "snap-other" else None
        ),
        group_clients=AsyncMock(return_value={"server": {}}),
        synchronize=MagicMock(),
    )  # pyright: ignore[reportAttributeAccessIssue]
    provider.mass.players.get_player.return_value = SimpleNamespace(
        _handle_player_update=MagicMock()
    )

    await provider.isolate_player_to_dedicated_group("ma-target")

    other_group.set_name.assert_awaited_once_with("ma-other")
    other_group.set_stream.assert_awaited_once_with("Kitchen Group")
    target_group.set_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_player_to_fallback_group_uses_existing_named_group() -> None:
    """Removed players should join the configured fallback group when it exists."""
    provider = _create_provider()
    provider._external_dedicated_fallback_group = "Media"

    fallback_group = SimpleNamespace(
        identifier="group-media",
        name="Media",
        add_client=AsyncMock(),
    )
    target_client = SimpleNamespace(
        identifier="snap-thor",
        group=SimpleNamespace(identifier="group-broadcast"),
    )
    provider._snapserver = SimpleNamespace(groups=[fallback_group])
    provider.get_snap_client = MagicMock(return_value=target_client)

    moved = await provider.move_player_to_fallback_group("thor-speakers")

    assert moved is True
    fallback_group.add_client.assert_awaited_once_with("snap-thor")


@pytest.mark.asyncio
async def test_set_members_reuses_current_stream_for_detached_player() -> None:
    """Detached players should inherit the current stable stream reference."""
    player_group = SimpleNamespace(
        clients=["snap-target", "snap-other"],
        stream="snap-stream-123",
        set_callback=MagicMock(),
        add_client=AsyncMock(),
    )
    provider: Any = SimpleNamespace(
        instance_id="snapcast-test",
        ensure_player_owned_group=AsyncMock(return_value=player_group),
        get_snap_ma_stream=MagicMock(return_value=None),
        move_player_to_fallback_group=AsyncMock(return_value=False),
        isolate_player_to_dedicated_group=AsyncMock(),
        _get_ma_id=MagicMock(
            side_effect=lambda client_id: {
                "snap-target": "ma-target",
                "snap-other": "ma-other",
            }.get(client_id)
        ),
        _get_stable_stream_reference=MagicMock(return_value="Kitchen Group"),
        _update_group_callbacks=MagicMock(),
    )

    player: Any = SnapCastPlayer.__new__(SnapCastPlayer)
    player.snap_client = SimpleNamespace(group=player_group)
    player.mass = MagicMock()
    player.logger = MagicMock()
    player._provider = provider
    player._player_id = "ma-target"
    player._state_update_lock = asyncio.Lock()
    player._process_snapcast_client_state = AsyncMock(return_value=False)
    await player.set_members(player_ids_to_remove=["ma-other"])

    provider._get_stable_stream_reference.assert_called_once_with("snap-stream-123")
    provider.isolate_player_to_dedicated_group.assert_awaited_once_with(
        "ma-other", target_stream_id="Kitchen Group"
    )


@pytest.mark.asyncio
async def test_set_members_prefers_configured_fallback_group_for_removed_player() -> None:
    """Detached players should use the configured fallback group before isolate fallback."""
    player_group = SimpleNamespace(
        clients=["snap-target", "snap-other"],
        stream="snap-stream-123",
        set_callback=MagicMock(),
        add_client=AsyncMock(),
    )
    provider: Any = SimpleNamespace(
        instance_id="snapcast-test",
        ensure_player_owned_group=AsyncMock(return_value=player_group),
        get_snap_ma_stream=MagicMock(return_value=None),
        move_player_to_fallback_group=AsyncMock(return_value=True),
        isolate_player_to_dedicated_group=AsyncMock(),
        _get_ma_id=MagicMock(
            side_effect=lambda client_id: {
                "snap-target": "ma-target",
                "snap-other": "ma-other",
            }.get(client_id)
        ),
        _get_stable_stream_reference=MagicMock(return_value="Kitchen Group"),
        _update_group_callbacks=MagicMock(),
    )

    player: Any = SnapCastPlayer.__new__(SnapCastPlayer)
    player.snap_client = SimpleNamespace(group=player_group)
    player.mass = MagicMock()
    player.logger = MagicMock()
    player._provider = provider
    player._player_id = "ma-target"
    player._state_update_lock = asyncio.Lock()
    player._process_snapcast_client_state = AsyncMock(return_value=False)
    await player.set_members(player_ids_to_remove=["ma-other"])

    provider.move_player_to_fallback_group.assert_awaited_once_with("ma-other")
    provider.isolate_player_to_dedicated_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_members_keeps_remaining_syncgroup_member_on_current_stream_during_handoff() -> (
    None
):
    """Leader handoff should keep the remaining member on the active Snapcast stream."""
    player_group = SimpleNamespace(
        clients=["snap-target", "snap-other"],
        stream="snap-stream-123",
        set_callback=MagicMock(),
        add_client=AsyncMock(),
    )
    sync_group_player = SimpleNamespace(group_members=["ma-other"])
    provider: Any = SimpleNamespace(
        instance_id="snapcast-test",
        ensure_player_owned_group=AsyncMock(return_value=player_group),
        get_snap_ma_stream=MagicMock(
            return_value=SimpleNamespace(
                media=PlayerMedia(
                    uri="snapcast-syncgroup://test",
                    media_type=MediaType.PLUGIN_SOURCE,
                    custom_data={"player_id": f"{SGP_PREFIX}abc12345"},
                )
            )
        ),
        move_player_to_fallback_group=AsyncMock(return_value=True),
        isolate_player_to_dedicated_group=AsyncMock(),
        _get_ma_id=MagicMock(
            side_effect=lambda client_id: {
                "snap-target": "ma-target",
                "snap-other": "ma-other",
            }.get(client_id)
        ),
        _get_stable_stream_reference=MagicMock(return_value="Kitchen Group"),
        _update_group_callbacks=MagicMock(),
    )

    player: Any = SnapCastPlayer.__new__(SnapCastPlayer)
    player.snap_client = SimpleNamespace(group=player_group)
    player.mass = MagicMock()
    player.mass.players.get_player.return_value = sync_group_player
    player.logger = MagicMock()
    player._provider = provider
    player._player_id = "ma-target"
    player._state_update_lock = asyncio.Lock()
    player._process_snapcast_client_state = AsyncMock(return_value=False)
    await player.set_members(player_ids_to_remove=["ma-other"])

    provider.move_player_to_fallback_group.assert_awaited_once_with("ma-target")
    provider.isolate_player_to_dedicated_group.assert_awaited_once_with(
        "ma-other", target_stream_id="Kitchen Group"
    )


@pytest.mark.asyncio
async def test_stop_reuses_current_stream_reference_instead_of_default() -> None:
    """Stop should keep using the stable current stream reference, not a hardcoded default."""
    player_group = SimpleNamespace(
        stream="snap-stream-123",
        set_stream=AsyncMock(),
    )
    provider: Any = SimpleNamespace(
        instance_id="snapcast-test",
        ensure_player_owned_group=AsyncMock(return_value=player_group),
        get_snap_ma_stream=MagicMock(return_value=None),
        _get_stable_stream_reference=MagicMock(return_value="Kitchen Group"),
    )

    player: Any = SnapCastPlayer.__new__(SnapCastPlayer)
    player.snap_client = SimpleNamespace(group=player_group)
    player.mass = MagicMock()
    player.logger = MagicMock()
    player._provider = provider
    player._player_id = "ma-target"
    player._poke_evt = asyncio.Event()

    await player.stop()

    provider._get_stable_stream_reference.assert_called_once_with("snap-stream-123")
    player_group.set_stream.assert_awaited_once_with("Kitchen Group")
    assert player._poke_evt.is_set()


@pytest.mark.asyncio
async def test_get_snapcast_media_stream_recreates_idle_stream_on_display_name_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle stream should be recreated when the desired visible Snapcast name changes."""
    provider = _create_provider()

    class FakeStream:
        """Minimal stand-in for SnapcastMAStream."""

        def __init__(
            self,
            provider: SnapCastProvider,
            media: PlayerMedia,
            stream_name: str,
            stream_display_name: str | None = None,
            source_id: str | None = None,
            queue_id: str | None = None,
            filter_settings_owner: str | None = None,
            use_cntrl_script: bool = False,
            destroy_on_stop: bool = False,
        ) -> None:
            self.provider = provider
            self.media = media
            self.stream_name = stream_name
            self.stream_display_name = stream_display_name or stream_name
            self.source_id = source_id
            self.queue_id = queue_id
            self.filter_settings_owner = filter_settings_owner
            self.use_cntrl_script = use_cntrl_script
            self.destroy_on_stop = destroy_on_stop
            self.is_streaming = False
            self.stream_id = None
            self.destroy_called = False
            self.setup_called = False

        async def setup(self) -> None:
            self.setup_called = True

        async def destroy(self) -> None:
            self.destroy_called = True

        def update_media(self, media: PlayerMedia) -> None:
            self.media = media

    monkeypatch.setattr(
        "music_assistant.providers.snapcast.provider.SnapcastMAStream",
        FakeStream,
    )
    provider._get_sync_group_stream_display_name = MagicMock(
        side_effect=["Kitchen Group", "Living Room"]
    )

    media = PlayerMedia(uri="https://example.test/stream.mp3")
    first_stream = await provider.get_snapcast_media_stream(media)
    second_stream = await provider.get_snapcast_media_stream(media)

    assert first_stream is not None
    assert second_stream is not None
    assert first_stream.stream_display_name == "Kitchen Group"
    assert first_stream.destroy_called is True
    assert second_stream is not first_stream
    assert second_stream.stream_display_name == "Living Room"
    assert second_stream.setup_called is True
