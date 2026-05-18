"""Tests for the deferred/background CLAP model load in SonicAnalysisProvider."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.sonic_analysis import (
    CLAP_SAMPLING_FAST,
    SonicAnalysisProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> SonicAnalysisProvider:
    """Construct a SonicAnalysisProvider with mocked MA infrastructure.

    Uses ``__new__`` to bypass ``__init__`` (no model downloads) and manually
    sets the attributes the background-load + start-analysis paths touch.
    """
    mass = MagicMock()
    mass.create_task = MagicMock()

    manifest = MagicMock()
    manifest.domain = "sonic_analysis"

    p = SonicAnalysisProvider.__new__(SonicAnalysisProvider)
    p.logger = MagicMock()
    p.mass = mass
    p.manifest = manifest
    p.config = MagicMock()
    p.config.get_value = MagicMock(return_value=CLAP_SAMPLING_FAST)
    p._sessions = {}
    p._clap_model = None
    p._clap_text_embeddings = None
    p._clap_prompt_order = []
    p._clap_load_task = None
    p.analysis_version = 1
    return p


def _make_audio_format(
    sample_rate: int = 22050,
    bit_depth: int = 16,
    channels: int = 1,
) -> AudioFormat:
    """Return a real AudioFormat for 16-bit mono PCM."""
    return AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=channels,
    )


def _make_streamdetails(
    item_id: str = "track-1",
    duration: float | None = 60.0,
) -> MagicMock:
    """Return a minimal streamdetails mock."""
    sd = MagicMock()
    sd.item_id = item_id
    sd.provider = "test_provider"
    sd.duration = duration
    return sd


# ---------------------------------------------------------------------------
# Test 1a: handle_async_init schedules the load via mass.create_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_async_init_schedules_load_via_mass_create_task() -> None:
    """``handle_async_init`` must hand the load coroutine to ``mass.create_task``."""
    provider = _make_provider()
    captured: list[Any] = []

    def _capture(coro: Any) -> MagicMock:
        captured.append(coro)
        # Close the coroutine so we don't get "never awaited" warnings.
        coro.close()
        return MagicMock()

    provider.mass.create_task = MagicMock(side_effect=_capture)  # type: ignore[method-assign]

    await provider.handle_async_init()

    provider.mass.create_task.assert_called_once()
    assert len(captured) == 1
    coro = captured[0]
    assert asyncio.iscoroutine(coro)
    assert coro.__qualname__.endswith("_load_clap_in_background")


# ---------------------------------------------------------------------------
# Test 1b: _load_clap_in_background offloads the blocking load to a thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_clap_in_background_offloads_load_to_thread() -> None:
    """``_load_clap_in_background`` must invoke ``_load_clap`` (proves offload runs)."""
    provider = _make_provider()
    fake_model = MagicMock(name="clap_model")
    fake_embeddings = MagicMock(name="text_embeddings")
    fake_prompt_order: list[Any] = []

    with patch.object(
        provider,
        "_load_clap",
        return_value=(fake_model, fake_embeddings, fake_prompt_order),
    ) as load_mock:
        await provider._load_clap_in_background()

    load_mock.assert_called_once()
    assert provider._clap_model is fake_model


# ---------------------------------------------------------------------------
# Test 2: handle_async_init still validates calibration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_async_init_validates_calibration() -> None:
    """``handle_async_init`` must still call ``validate_calibration_freshness``."""
    provider = _make_provider()
    provider.mass.create_task = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda coro: coro.close() or MagicMock()
    )

    with patch(
        "music_assistant.providers.sonic_analysis.validate_calibration_freshness"
    ) as validate_mock:
        await provider.handle_async_init()

    validate_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: background load populates state on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_clap_in_background_populates_state_on_success() -> None:
    """On success the background loader must populate model/embeddings/prompt order."""
    provider = _make_provider()
    fake_model = MagicMock(name="clap_model")
    fake_embeddings = MagicMock(name="text_embeddings")
    fake_prompt_order = [
        ("danceable", ("danceable", "not danceable")),
        ("energetic", ("energetic", "calm")),
    ]

    with patch.object(
        provider,
        "_load_clap",
        return_value=(fake_model, fake_embeddings, fake_prompt_order),
    ):
        await provider._load_clap_in_background()

    assert provider._clap_model is fake_model
    assert provider._clap_text_embeddings is fake_embeddings
    assert provider._clap_prompt_order == fake_prompt_order

    provider.logger.info.assert_called_once()  # type: ignore[attr-defined]
    info_call = provider.logger.info.call_args  # type: ignore[attr-defined]
    assert info_call.args[1] == len(fake_prompt_order)


# ---------------------------------------------------------------------------
# Test 4: background load logs error on failure and leaves state cleared
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_clap_in_background_logs_error_on_failure() -> None:
    """On generic failure the background loader logs an ERROR with ``exc_info``."""
    provider = _make_provider()
    err = RuntimeError("hf network unreachable")

    with patch.object(provider, "_load_clap", side_effect=err):
        await provider._load_clap_in_background()

    assert provider._clap_model is None

    provider.logger.error.assert_called_once()  # type: ignore[attr-defined]
    call = provider.logger.error.call_args  # type: ignore[attr-defined]
    assert call.kwargs.get("exc_info") is err
    fmt = call.args[0]
    assert "CLAP" in fmt
    # The underlying exception is passed as a positional formatting arg.
    assert err in call.args[1:]


# ---------------------------------------------------------------------------
# Test 5: background load propagates CancelledError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_clap_in_background_propagates_cancellation() -> None:
    """``CancelledError`` must propagate without invoking the failure handler.

    Guards the *behavioural* contract on cancellation: the failure-path
    side effects (logging an error, clearing ``_clap_model``) must NOT run.
    Combined with ``test_load_clap_in_background_runs_failure_handler_on_exception``
    below, this triangulates the contract: ``CancelledError`` skips the
    handler entirely; generic ``Exception`` triggers it.
    """
    provider = _make_provider()
    # Sentinel value the failure handler would overwrite to ``None`` if it ran.
    provider._clap_model = "sentinel"

    with (
        patch.object(provider, "_load_clap", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await provider._load_clap_in_background()

    assert provider._clap_model == "sentinel"
    provider.logger.error.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_load_clap_in_background_runs_failure_handler_on_exception() -> None:
    """Generic ``Exception`` must trigger the failure handler (logger + clear state)."""
    provider = _make_provider()
    provider._clap_model = "sentinel"

    with patch.object(provider, "_load_clap", side_effect=RuntimeError("boom")):
        await provider._load_clap_in_background()

    assert provider._clap_model is None
    provider.logger.error.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test 6: _start_analysis returns False when CLAP not yet loaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_analysis_returns_false_when_clap_not_loaded() -> None:
    """``_start_analysis`` must decline tracks while CLAP is unavailable."""
    provider = _make_provider()
    provider._clap_model = None

    af = _make_audio_format()
    sd = _make_streamdetails(item_id="skip-me")

    result = await provider._start_analysis("session-skip", sd, af)

    assert result is False
    assert provider._sessions == {}

    provider.logger.debug.assert_called()  # type: ignore[attr-defined]
    debug_msgs = [str(c) for c in provider.logger.debug.call_args_list]  # type: ignore[attr-defined]
    assert any("CLAP model not yet available" in c for c in debug_msgs)


# ---------------------------------------------------------------------------
# Test 7: _start_analysis proceeds when CLAP is loaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_analysis_proceeds_when_clap_loaded() -> None:
    """``_start_analysis`` must create a session when CLAP is available."""
    provider = _make_provider()
    provider._clap_model = MagicMock(name="clap_model")

    af = _make_audio_format()
    sd = _make_streamdetails(item_id="go-ahead")

    result = await provider._start_analysis("session-ok", sd, af)

    assert result is True
    assert "session-ok" in provider._sessions


# ---------------------------------------------------------------------------
# Test 7b: _start_analysis returns False when streamdetails.duration is missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [None, 0, 0.0])
async def test_start_analysis_returns_false_without_duration(
    duration: float | None,
) -> None:
    """``_start_analysis`` must decline tracks without a usable duration.

    Without duration, CLAP windows can't be planned and the resulting record
    would be librosa-only. Rejecting at start keeps the retry path open for a
    later analysis attempt once duration is known.
    """
    provider = _make_provider()
    provider._clap_model = MagicMock(name="clap_model")

    af = _make_audio_format()
    sd = _make_streamdetails(item_id="no-duration", duration=duration)

    result = await provider._start_analysis("session-no-duration", sd, af)

    assert result is False
    assert provider._sessions == {}

    debug_msgs = [str(c) for c in provider.logger.debug.call_args_list]  # type: ignore[attr-defined]
    assert any("duration missing or zero" in c for c in debug_msgs)


# ---------------------------------------------------------------------------
# Test 8: unload cancels in-flight load task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unload_cancels_in_flight_load_task() -> None:
    """``unload`` must cancel an in-flight CLAP load task and clear the handle."""
    provider = _make_provider()

    async def _long_sleep() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_long_sleep())
    # Yield once so the task actually starts.
    await asyncio.sleep(0)
    provider._clap_load_task = task

    # Stub the AudioAnalysisProvider base unload — we only care about the new logic.
    with patch(
        "music_assistant.providers.sonic_analysis.AudioAnalysisProvider.unload",
        new=AsyncMock(),
    ):
        await provider.unload()

    assert task.cancelled() or (
        task.done() and isinstance(task.exception(), asyncio.CancelledError)
    )
    assert provider._clap_load_task is None


# ---------------------------------------------------------------------------
# Test 9: unload tolerates an already-completed load task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unload_when_load_task_already_completed() -> None:
    """``unload`` must not raise when the load task has already finished."""
    provider = _make_provider()

    task = asyncio.create_task(asyncio.sleep(0))
    await task
    provider._clap_load_task = task

    with patch(
        "music_assistant.providers.sonic_analysis.AudioAnalysisProvider.unload",
        new=AsyncMock(),
    ):
        await provider.unload()

    assert provider._clap_load_task is None
