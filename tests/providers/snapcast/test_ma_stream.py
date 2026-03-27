"""Tests for Snapcast MA stream registration behavior."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.snapcast.ma_stream import SnapcastMAStream


def _create_provider(snapserver: object) -> object:
    """Create a lightweight provider stub for SnapcastMAStream tests."""
    return SimpleNamespace(
        logger=MagicMock(),
        mass=SimpleNamespace(closing=False),
        _snapserver=snapserver,
        _snapcast_server_host="192.168.10.3",
        _snapcast_stream_idle_threshold=60000,
        _use_builtin_server=False,
        poke_group_members=MagicMock(),
    )


@pytest.mark.asyncio
async def test_setup_reuses_existing_idle_snapserver_stream_by_display_name() -> None:
    """Existing idle external streams should be reused after an MA restart."""
    existing_stream = SimpleNamespace(
        identifier="broadcast",
        friendly_name="broadcast",
        status="idle",
        path="tcp://0.0.0.0:4978",
        _stream={"uri": {"host": "0.0.0.0:4978"}},
        set_callback=MagicMock(),
    )
    snapserver = SimpleNamespace(
        streams=[existing_stream],
        stream_add_stream=AsyncMock(),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
    )

    await stream.setup()

    assert stream.snap_stream is existing_stream
    assert stream.lifecycle_state == "attached"
    existing_stream.set_callback.assert_called_once()
    snapserver.stream_add_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_attaches_existing_stream_after_duplicate_stream_name_error() -> None:
    """A duplicate-name response should attach to the already existing Snapserver stream."""
    existing_stream = SimpleNamespace(
        identifier="broadcast",
        friendly_name="broadcast",
        status="playing",
        path="tcp://0.0.0.0:4978",
        _stream={"uri": {"host": "0.0.0.0:4978"}},
        set_callback=MagicMock(),
    )
    snapserver = SimpleNamespace(
        streams=[existing_stream],
        stream_add_stream=AsyncMock(
            return_value={
                "code": -32603,
                "data": "Stream with name 'broadcast' already exists",
                "message": "Internal error",
            }
        ),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
    )

    await stream.setup()

    assert stream.snap_stream is existing_stream
    assert stream.lifecycle_state == "attached"
    assert snapserver.stream_add_stream.await_count == 1


@pytest.mark.asyncio
async def test_setup_stops_retrying_on_duplicate_name_without_attachable_stream() -> None:
    """Duplicate stream names should fail fast instead of retrying 50 random ports."""
    snapserver = SimpleNamespace(
        streams=[],
        stream_add_stream=AsyncMock(
            return_value={
                "code": -32603,
                "data": "Stream with name 'broadcast' already exists",
                "message": "Internal error",
            }
        ),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
    )

    with pytest.raises(RuntimeError, match="already exists"):
        await stream.setup()

    assert stream.lifecycle_state == "unresolved"
    assert snapserver.stream_add_stream.await_count == 1


@pytest.mark.asyncio
async def test_setup_stops_retrying_on_non_retryable_error() -> None:
    """Non-retryable add-stream failures should fail immediately."""
    snapserver = SimpleNamespace(
        streams=[],
        stream_add_stream=AsyncMock(
            return_value={
                "code": -32603,
                "data": "Unhandled Snapserver failure",
                "message": "Internal error",
            }
        ),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
    )

    with pytest.raises(RuntimeError, match="Unhandled Snapserver failure"):
        await stream.setup()

    assert stream.lifecycle_state == "unresolved"
    assert snapserver.stream_add_stream.await_count == 1


@pytest.mark.asyncio
async def test_setup_retries_on_retryable_port_conflict_then_marks_created() -> None:
    """Retryable bind/port conflicts should retry and mark the final stream as created."""
    created_stream = SimpleNamespace(
        identifier="snap-stream-123",
        friendly_name="broadcast",
        status="idle",
        path="tcp://0.0.0.0:4978",
        _stream={"uri": {"host": "0.0.0.0:4978"}},
        set_callback=MagicMock(),
    )
    snapserver = SimpleNamespace(
        streams=[],
        stream_add_stream=AsyncMock(
            side_effect=[
                {
                    "code": -32603,
                    "data": "Address already in use",
                    "message": "Bind failed",
                },
                {"id": "snap-stream-123"},
            ]
        ),
        stream=MagicMock(return_value=created_stream),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
    )

    await stream.setup()

    assert stream.snap_stream is created_stream
    assert stream.lifecycle_state == "created"
    assert snapserver.stream_add_stream.await_count == 2
    created_stream.set_callback.assert_called_once()


@pytest.mark.asyncio
async def test_setup_external_queue_backed_stream_adds_mass_bridge_controlscript() -> None:
    """External queue-backed streams should start the standalone mass_bridge control script."""
    created_stream = SimpleNamespace(
        identifier="broadcast",
        friendly_name="broadcast",
        status="idle",
        path="tcp://0.0.0.0:4978",
        _stream={"uri": {"host": "0.0.0.0:4978"}},
        set_callback=MagicMock(),
    )
    snapserver = SimpleNamespace(
        streams=[],
        stream_add_stream=AsyncMock(return_value={"id": "broadcast"}),
        stream=MagicMock(return_value=created_stream),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
        queue_id="broadcast",
    )

    await stream.setup()

    snapserver.stream_add_stream.assert_awaited_once()
    add_stream_uri = snapserver.stream_add_stream.await_args.args[0]
    assert "&controlscript=mass_bridge.py" in add_stream_uri
    assert "&controlscriptparams=--stream=broadcast" in add_stream_uri
    assert add_stream_uri.endswith("&name=broadcast")


@pytest.mark.asyncio
async def test_setup_external_queue_backed_stream_replaces_idle_stream_without_mass_bridge() -> (
    None
):
    """Queue-backed playback should replace an older idle stream that lacks mass_bridge."""
    old_idle_stream = SimpleNamespace(
        identifier="broadcast-old",
        friendly_name="broadcast",
        status="idle",
        path="tcp://0.0.0.0:4978",
        _stream={"uri": {"host": "0.0.0.0:4978", "raw": "tcp://0.0.0.0:4978?name=broadcast"}},
        set_callback=MagicMock(),
    )
    created_stream = SimpleNamespace(
        identifier="broadcast",
        friendly_name="broadcast",
        status="idle",
        path="tcp://0.0.0.0:4988",
        _stream={
            "uri": {
                "host": "0.0.0.0:4988",
                "raw": "tcp://0.0.0.0:4988?controlscript=mass_bridge.py&name=broadcast",
            }
        },
        set_callback=MagicMock(),
    )
    snapserver = SimpleNamespace(
        groups=[
            SimpleNamespace(
                stream="broadcast-old",
                set_stream=AsyncMock(),
            )
        ],
        streams=[old_idle_stream],
        stream_add_stream=AsyncMock(return_value={"id": "broadcast"}),
        stream=MagicMock(return_value=created_stream),
        stream_remove_stream=AsyncMock(),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
        queue_id="broadcast",
    )

    await stream.setup()

    snapserver.groups[0].set_stream.assert_not_awaited()
    snapserver.stream_remove_stream.assert_awaited_once_with("broadcast-old")
    add_stream_uri = snapserver.stream_add_stream.await_args.args[0]
    assert "&controlscript=mass_bridge.py" in add_stream_uri
    assert stream.snap_stream is created_stream


@pytest.mark.asyncio
async def test_destroy_external_idle_stream_does_not_force_default_stream() -> None:
    """External Snapserver cleanup should not hardcode a default stream during removal."""
    snap_stream = SimpleNamespace(
        identifier="broadcast",
        friendly_name="broadcast",
        status="idle",
        path="tcp://0.0.0.0:4978",
        _stream={"uri": {"host": "0.0.0.0:4978"}},
        set_callback=MagicMock(),
    )
    group = SimpleNamespace(
        name="PC_VAIO_2",
        stream="broadcast",
        set_stream=AsyncMock(),
    )
    snapserver = SimpleNamespace(
        groups=[group],
        stream_remove_stream=AsyncMock(),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
    )
    stream.snap_stream = snap_stream

    await stream.destroy()

    group.set_stream.assert_not_awaited()
    snapserver.stream_remove_stream.assert_awaited_once_with("broadcast")


@pytest.mark.asyncio
async def test_destroy_marks_stream_as_destroyed() -> None:
    """Destroy should end with an explicit destroyed lifecycle state."""
    snap_stream = SimpleNamespace(
        identifier="snap-stream-123",
        friendly_name="broadcast",
        status="idle",
        path="tcp://0.0.0.0:4978",
        _stream={"uri": {"host": "0.0.0.0:4978"}},
        set_callback=MagicMock(),
    )
    snapserver = SimpleNamespace(
        groups=[],
        stream_remove_stream=AsyncMock(),
    )
    stream = SnapcastMAStream(
        provider=_create_provider(snapserver),  # type: ignore[arg-type]
        media=PlayerMedia(uri="queue:broadcast"),
        stream_name="mass_stream_broadcast",
        stream_display_name="broadcast",
    )
    stream.snap_stream = snap_stream

    await stream.destroy()

    assert stream.lifecycle_state == "destroyed"
    snapserver.stream_remove_stream.assert_awaited_once_with("snap-stream-123")
