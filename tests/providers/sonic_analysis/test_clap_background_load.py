"""Tests for the synchronous CLAP model load in SonicAnalysisProvider."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import SetupFailedError
from music_assistant_models.media_items import AudioFormat

from music_assistant.providers.sonic_analysis import (
    CLAP_SAMPLING_FAST,
    SonicAnalysisProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider() -> SonicAnalysisProvider:
    """
    Construct a SonicAnalysisProvider with mocked MA infrastructure.

    Uses ``__new__`` to bypass ``__init__`` (no model downloads) and manually
    sets the attributes the load + start-analysis paths touch.
    """
    mass = MagicMock()

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


@pytest.fixture(autouse=True)
def _stub_ml_inference_gate() -> Generator[None]:
    """Stub the hardware gate so these unit tests never spawn the real capability probe."""
    with patch(
        "music_assistant.providers.sonic_analysis.verify_system_meets_requirements",
        new=AsyncMock(),
    ):
        yield


# ---------------------------------------------------------------------------
# handle_async_init populates state on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_async_init_populates_state_on_success() -> None:
    """On success ``handle_async_init`` must populate model/embeddings/prompt order."""
    provider = _make_provider()
    fake_model = MagicMock(name="clap_model")
    fake_embeddings = MagicMock(name="text_embeddings")
    fake_prompt_order: list[tuple[str, tuple[str, str]]] = [
        ("danceable", ("danceable", "not danceable")),
        ("energetic", ("energetic", "calm")),
    ]

    with patch.object(
        provider,
        "_load_clap",
        return_value=(fake_model, fake_embeddings, fake_prompt_order),
    ):
        await provider.handle_async_init()

    assert provider._clap_model is fake_model
    assert provider._clap_text_embeddings is fake_embeddings
    assert provider._clap_prompt_order == fake_prompt_order

    provider.logger.info.assert_called_once()  # type: ignore[attr-defined]
    info_call = provider.logger.info.call_args  # type: ignore[attr-defined]
    assert info_call.args[1] == len(fake_prompt_order)


# ---------------------------------------------------------------------------
# handle_async_init propagates load failure so provider.available stays False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_async_init_propagates_load_failure() -> None:
    """
    Synchronous load: failures must propagate, not be swallowed.

    The AudioAnalysisController gates work on ``provider.available``, which
    stays ``False`` if ``handle_async_init`` raises. Swallowing here would
    flip the provider to ``available=True`` despite ``_clap_model is None``.
    """
    provider = _make_provider()
    err = RuntimeError("hf network unreachable")

    with (
        patch.object(provider, "_load_clap", side_effect=err),
        pytest.raises(RuntimeError, match="hf network unreachable"),
    ):
        await provider.handle_async_init()

    assert provider._clap_model is None


# ---------------------------------------------------------------------------
# handle_async_init offloads the blocking load to a worker thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_async_init_offloads_load_to_thread() -> None:
    """``handle_async_init`` must offload ``_load_clap`` via asyncio.to_thread."""
    provider = _make_provider()
    fake_state: tuple[Any, Any, list[Any]] = (MagicMock(), MagicMock(), [])

    with patch(
        "music_assistant.providers.sonic_analysis.asyncio.to_thread",
        new=AsyncMock(return_value=fake_state),
    ) as to_thread_mock:
        await provider.handle_async_init()

    to_thread_mock.assert_called_once()
    # First positional arg passed to to_thread is the callable being offloaded.
    # Use ``==`` (not ``is``): each ``provider._load_clap`` access yields a fresh
    # bound-method object, but bound methods of the same (instance, function)
    # compare equal.
    assert to_thread_mock.call_args.args[0] == provider._load_clap


# ---------------------------------------------------------------------------
# _start_analysis gating: declines tracks while CLAP is unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_analysis_returns_false_when_clap_not_loaded() -> None:
    """
    ``_start_analysis`` must decline tracks while CLAP is unavailable.

    Defensive: with synchronous loading + raise-on-failure, the normal path
    keeps ``_clap_model`` populated whenever the provider is available. This
    guards the edge case of being invoked after ``unload`` cleared state.
    """
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


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [None, 0, 0.0])
async def test_start_analysis_returns_false_without_duration(
    duration: float | None,
) -> None:
    """
    ``_start_analysis`` must decline tracks without a usable duration.

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


async def test_handle_async_init_raises_when_requirements_not_met() -> None:
    """Setup fails before any model load when the system does not meet requirements."""
    provider = _make_provider()
    with (
        patch(
            "music_assistant.providers.sonic_analysis.verify_system_meets_requirements",
            side_effect=SetupFailedError("unsupported system"),
        ),
        patch.object(SonicAnalysisProvider, "_load_clap") as load_clap_mock,
        pytest.raises(SetupFailedError),
    ):
        await provider.handle_async_init()
    load_clap_mock.assert_not_called()
