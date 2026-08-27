"""Tests for CLAP model setup in SonicAnalysisProvider: asset fetch, then load."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from huggingface_hub.errors import XetDownloadError
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import SetupFailedError, UnsupportedSystemError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.sonic_analysis import (
    CLAP_SAMPLING_FAST,
    SonicAnalysisProvider,
)

FETCH_TASK_ID = "sonic_analysis.model_assets.instance-1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMass:
    """
    Stand-in for MusicAssistant reproducing the ``create_task`` behaviour the fetch relies on.

    A plain MagicMock would return a MagicMock from ``create_task``, which ``join_task``
    walks straight through while the coroutine is never awaited — the dedupe this module
    tests would go unexercised.
    """

    def __init__(self) -> None:
        self.streams = MagicMock()
        self.cache = MagicMock()
        self.tracked: dict[str, asyncio.Task[Any]] = {}
        self.created: list[asyncio.Task[Any]] = []
        self.create_task_kwargs: list[dict[str, Any]] = []

    def create_task(
        self,
        target: Any,
        *args: Any,
        task_id: str | None = None,
        abort_existing: bool = False,
        **kwargs: Any,
    ) -> asyncio.Task[Any]:
        """Return the live task registered under task_id, else start and track a new one."""
        self.create_task_kwargs.append({"task_id": task_id, "abort_existing": abort_existing})
        if task_id and (existing := self.tracked.get(task_id)) and not existing.done():
            if abort_existing:
                existing.cancel()
            else:
                target.close()
                return existing
        task: asyncio.Task[Any] = asyncio.ensure_future(target)
        # mass.create_task installs a done callback that retrieves the exception; without
        # an equivalent here a fetch nobody waits on logs "exception was never retrieved".
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        if task_id:
            self.tracked[task_id] = task
        self.created.append(task)
        return task

    async def drain(self) -> None:
        """Cancel and await every task still running, so no test leaks one."""
        for task in self.created:
            task.cancel()
        await asyncio.gather(*self.created, return_exceptions=True)


def _make_provider(mass: _FakeMass | None = None) -> SonicAnalysisProvider:
    """
    Construct a SonicAnalysisProvider with mocked MA infrastructure.

    Runs the real ``__init__`` (which touches nothing external) so the instance carries
    whatever state the provider actually declares, rather than a hand-copied subset.

    :param mass: Shared MusicAssistant stand-in, for tests that need two provider
        instances to see the same task registry — as consecutive load attempts do.
    """
    manifest = MagicMock()
    manifest.domain = "sonic_analysis"

    config = MagicMock()
    config.instance_id = "instance-1"
    config.get_value = MagicMock(
        side_effect=lambda key, *_a, **_kw: (
            "GLOBAL" if key == CONF_LOG_LEVEL else CLAP_SAMPLING_FAST
        )
    )

    p = SonicAnalysisProvider(mass or _FakeMass(), manifest, config)  # type: ignore[arg-type]
    p.logger = MagicMock()
    return p


async def _never_finishes() -> str:
    """Stand in for a download that is still in flight when setup gives up on it."""
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


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
# handle_async_init: cached weights complete setup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_async_init_populates_state_on_success() -> None:
    """With the checkpoint cached, setup must populate model state and mark models loaded."""
    provider = _make_provider()
    fake_model = MagicMock(name="clap_model")
    fake_embeddings = MagicMock(name="text_embeddings")
    fake_prompt_order: list[tuple[str, tuple[str, str]]] = [
        ("danceable", ("danceable", "not danceable")),
        ("energetic", ("energetic", "calm")),
    ]

    with (
        patch.object(provider, "_fetch_model_assets", new=AsyncMock()),
        patch.object(
            provider,
            "_load_clap",
            return_value=(fake_model, fake_embeddings, fake_prompt_order),
        ),
    ):
        await provider.handle_async_init()

    assert provider._clap_model is fake_model
    assert provider._clap_text_embeddings is fake_embeddings
    assert provider._clap_prompt_order == fake_prompt_order
    assert provider._models_loaded is True


@pytest.mark.asyncio
async def test_handle_async_init_propagates_load_failure() -> None:
    """
    Load failures must propagate, not be swallowed.

    The AudioAnalysisController gates work on ``provider.available``, which
    stays ``False`` if ``handle_async_init`` raises. Swallowing here would
    flip the provider to ``available=True`` despite ``_clap_model is None``.
    """
    provider = _make_provider()
    err = RuntimeError("checkpoint is corrupt")

    with (
        patch.object(provider, "_fetch_model_assets", new=AsyncMock()),
        patch.object(provider, "_load_clap", side_effect=err),
        pytest.raises(RuntimeError, match="checkpoint is corrupt"),
    ):
        await provider.handle_async_init()

    assert provider._clap_model is None
    assert provider._models_loaded is False


@pytest.mark.asyncio
async def test_handle_async_init_offloads_load_to_thread() -> None:
    """``_load_models`` must offload ``_load_clap`` via asyncio.to_thread."""
    provider = _make_provider()
    fake_state: tuple[Any, Any, list[Any]] = (MagicMock(), MagicMock(), [])

    with (
        patch.object(provider, "_fetch_model_assets", new=AsyncMock()),
        patch(
            "music_assistant.providers.sonic_analysis.asyncio.to_thread",
            new=AsyncMock(return_value=fake_state),
        ) as to_thread_mock,
    ):
        await provider.handle_async_init()

    # The module has two to_thread call sites; the other one lives in
    # _fetch_model_assets, which is mocked out above, so this call is unambiguous.
    to_thread_mock.assert_called_once()
    # First positional arg passed to to_thread is the callable being offloaded.
    # Use ``==`` (not ``is``): each ``provider._load_clap`` access yields a fresh
    # bound-method object, but bound methods of the same (instance, function)
    # compare equal.
    assert to_thread_mock.call_args.args[0] == provider._load_clap


@pytest.mark.asyncio
async def test_handle_async_init_raises_when_requirements_not_met() -> None:
    """
    An unsupported host must fail before anything is downloaded or loaded.

    UnsupportedSystemError is the one setup failure MA treats as permanent, so ordering
    the gate first is what keeps an incapable host from pulling the checkpoint on a loop.
    """
    provider = _make_provider()
    with (
        patch(
            "music_assistant.providers.sonic_analysis.verify_system_meets_requirements",
            side_effect=UnsupportedSystemError("unsupported system"),
        ),
        patch.object(SonicAnalysisProvider, "_fetch_model_assets") as fetch_mock,
        patch.object(SonicAnalysisProvider, "_load_clap") as load_clap_mock,
        pytest.raises(UnsupportedSystemError),
    ):
        await provider.handle_async_init()
    fetch_mock.assert_not_called()
    load_clap_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _ensure_model_assets: the grace period, and reuse across retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_fetch_raises_setup_failed_and_leaves_task_running() -> None:
    """
    A fetch that outlives the grace period must fail setup without cancelling the download.

    MA retries the provider load; cancelling here would restart the transfer every time.
    """
    provider = _make_provider()

    with (
        patch("music_assistant.providers.sonic_analysis.MODEL_FETCH_GRACE_SECONDS", 0.05),
        patch.object(provider, "_fetch_model_assets", side_effect=_never_finishes),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    assert exc_info.value.translation_key == "model_assets_downloading"
    assert exc_info.value.translation_owner == "provider.sonic_analysis"
    assert provider._models_loaded is False

    fetch_task = provider.mass.tracked[FETCH_TASK_ID]  # type: ignore[attr-defined]
    assert not fetch_task.done(), "the fetch must survive the setup timeout"

    await provider.mass.drain()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fetch_is_started_under_a_stable_dedupe_key() -> None:
    """
    The fetch task must be registered so a later load attempt can find and join it.

    The key is tied to the config instance, which outlives any one load attempt. Keying
    it on the provider object instead would let every retry start its own download.
    """
    mass = _FakeMass()
    provider = _make_provider(mass)

    with (
        patch("music_assistant.providers.sonic_analysis.MODEL_FETCH_GRACE_SECONDS", 0.05),
        patch.object(provider, "_fetch_model_assets", side_effect=_never_finishes),
        pytest.raises(SetupFailedError),
    ):
        await provider.handle_async_init()

    assert mass.create_task_kwargs == [
        {"task_id": FETCH_TASK_ID, "abort_existing": False},
    ]

    await mass.drain()


@pytest.mark.asyncio
async def test_retry_joins_in_flight_fetch_and_receives_its_result() -> None:
    """
    A retry must join the running download and come away with the checkpoint path.

    MA builds a fresh provider instance per load attempt, so the instance that finishes
    setup is never the one that started the fetch. If the path only reached the starting
    instance, the retry would load with model_fp=None and download all over again.
    """
    mass = _FakeMass()
    release = asyncio.Event()
    fetch_calls = 0

    async def _blocked_fetch() -> str:
        nonlocal fetch_calls
        fetch_calls += 1
        await release.wait()
        return "/cache/CLAP_weights_2023.pth"

    first = _make_provider(mass)
    with (
        patch("music_assistant.providers.sonic_analysis.MODEL_FETCH_GRACE_SECONDS", 0.05),
        patch.object(first, "_fetch_model_assets", side_effect=_blocked_fetch),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await first.handle_async_init()
    assert exc_info.value.translation_key == "model_assets_downloading"

    second = _make_provider(mass)
    with (
        patch.object(second, "_fetch_model_assets", side_effect=_blocked_fetch),
        patch.object(second, "_load_clap", return_value=(MagicMock(), MagicMock(), [])),
    ):
        # the download lands partway through the retry's own grace period
        asyncio.get_running_loop().call_soon(release.set)
        await second.handle_async_init()

    assert fetch_calls == 1, "the retry must not start a second download"
    assert second._clap_model_fp == "/cache/CLAP_weights_2023.pth"
    assert second._models_loaded is True


@pytest.mark.asyncio
async def test_fetch_failure_surfaces_as_setup_failed() -> None:
    """A failed download must reach the caller as a typed, retryable MA error."""
    provider = _make_provider()

    with (
        patch.object(provider, "_load_clap") as load_clap_mock,
        patch.object(
            provider,
            "_download_clap_weights",
            side_effect=SetupFailedError("boom", translation_key="model_assets_download_failed"),
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    assert exc_info.value.translation_key == "model_assets_download_failed"
    load_clap_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _download_clap_weights: what the hub can throw, and what MA sees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_offloads_the_download_and_returns_its_path() -> None:
    """
    The download must be offloaded, and its path come back as the task's result.

    Offloading is what keeps the vendored wrapper's torch/transformers imports off the
    event loop: mass.create_task starts a task eagerly, so this coroutine runs inline
    until its first await. Returning the path (rather than storing it) is what lets a
    retry — always a different provider object — pick it up.
    """
    provider = _make_provider()

    with patch(
        "music_assistant.providers.sonic_analysis.asyncio.to_thread",
        new=AsyncMock(return_value="/cache/CLAP_weights_2023.pth"),
    ) as to_thread_mock:
        assert await provider._fetch_model_assets() == "/cache/CLAP_weights_2023.pth"

    assert to_thread_mock.call_args.args[0] == provider._download_clap_weights


def test_cached_checkpoint_skips_the_network() -> None:
    """
    An already-downloaded checkpoint must resolve without any network call.

    mass.create_task drops a task from its registry once it finishes, so a setup attempt
    that follows a completed download starts a fresh fetch. Going back to
    hf_hub_download there would re-run its entry-tag revalidation over the network on
    every provider load, and would fail outright while the hub is unreachable.
    """
    provider = _make_provider()

    with (
        patch(
            "music_assistant.providers.sonic_analysis.vendored_clap.CLAP.cached_weights",
            return_value="/cache/CLAP_weights_2023.pth",
        ),
        patch(
            "music_assistant.providers.sonic_analysis.vendored_clap.CLAP.download_weights"
        ) as download_mock,
    ):
        assert provider._download_clap_weights() == "/cache/CLAP_weights_2023.pth"

    download_mock.assert_not_called()


def test_absent_checkpoint_is_downloaded() -> None:
    """With nothing cached, the fetch falls through to the real download."""
    provider = _make_provider()

    with (
        patch(
            "music_assistant.providers.sonic_analysis.vendored_clap.CLAP.cached_weights",
            return_value=None,
        ),
        patch(
            "music_assistant.providers.sonic_analysis.vendored_clap.CLAP.download_weights",
            return_value="/cache/CLAP_weights_2023.pth",
        ) as download_mock,
    ):
        assert provider._download_clap_weights() == "/cache/CLAP_weights_2023.pth"

    download_mock.assert_called_once()


@pytest.mark.parametrize(
    "err",
    [
        OSError("disk full"),
        httpx.ConnectError("connection refused"),
        httpx.TimeoutException("read timed out"),
        XetDownloadError("xet backend unavailable"),
    ],
    ids=["oserror", "httpx_connect", "httpx_timeout", "xet"],
)
def test_download_failures_become_typed_setup_errors(err: Exception) -> None:
    """
    Every way the fetch can fail must map to a retryable MA error.

    huggingface_hub streams the body through httpx and re-raises its transport errors
    verbatim once its own retries are spent. Those are not OSError, so letting them
    through would leave MA treating a flaky link as an unhandled bug and never retrying.
    """
    provider = _make_provider()

    with (
        patch(
            "music_assistant.providers.sonic_analysis.vendored_clap.CLAP.cached_weights",
            return_value=None,
        ),
        patch(
            "music_assistant.providers.sonic_analysis.vendored_clap.CLAP.download_weights",
            side_effect=err,
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        provider._download_clap_weights()

    assert exc_info.value.translation_key == "model_assets_download_failed"
    assert exc_info.value.__cause__ is err


def test_free_models_keeps_checkpoint_path() -> None:
    """
    Freeing models must release memory only.

    The checkpoint path is cheap state that has to survive an idle unload, otherwise the
    reload goes back to the network — the trip this provider resolves once at setup.
    """
    provider = _make_provider()
    provider._clap_model = MagicMock()
    provider._clap_text_embeddings = MagicMock()
    provider._clap_model_fp = "/cache/CLAP_weights_2023.pth"

    provider._free_models()

    assert provider._clap_model is None
    assert provider._clap_text_embeddings is None
    assert provider._clap_model_fp == "/cache/CLAP_weights_2023.pth"


# ---------------------------------------------------------------------------
# _load_clap: no network, ever
# ---------------------------------------------------------------------------


def test_load_clap_passes_cached_checkpoint_path() -> None:
    """``_load_clap`` must build the wrapper from the resolved path, text encoder off."""
    provider = _make_provider()
    provider._clap_model_fp = "/cache/CLAP_weights_2023.pth"
    embeddings = MagicMock(name="cached_embeddings")

    with (
        patch.object(provider, "_try_load_cached_prompt_embeddings", return_value=embeddings),
        patch("music_assistant.providers.sonic_analysis.vendored_clap.CLAP") as clap_cls,
        patch("torch.from_numpy", return_value=embeddings),
    ):
        model, _text_embeddings, _prompt_order = provider._load_clap()

    assert model is clap_cls.return_value
    assert clap_cls.call_args.kwargs["model_fp"] == "/cache/CLAP_weights_2023.pth"
    assert clap_cls.call_args.kwargs["text_enabled"] is False


def test_load_clap_raises_when_prompt_embeddings_unavailable() -> None:
    """
    Missing or stale prompt embeddings must fail, not fall back to the text encoder.

    That fallback constructs a text-enabled CLAP, which downloads the GPT2 text model —
    a second unbounded fetch inside the step that is supposed to be local-only.
    """
    provider = _make_provider()

    with (
        patch.object(provider, "_try_load_cached_prompt_embeddings", return_value=None),
        patch("music_assistant.providers.sonic_analysis.vendored_clap.CLAP") as clap_cls,
        pytest.raises(SetupFailedError) as exc_info,
    ):
        provider._load_clap()

    assert exc_info.value.translation_key == "prompt_embeddings_unavailable"
    clap_cls.assert_not_called()


# ---------------------------------------------------------------------------
# _start_analysis gating: declines tracks while CLAP is unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_analysis_returns_false_when_clap_not_loaded() -> None:
    """
    ``_start_analysis`` must decline tracks while CLAP is unavailable.

    Defensive: with the load gating setup, the normal path keeps ``_clap_model``
    populated whenever the provider is available. This guards the edge case of
    being invoked after ``unload`` cleared state.
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
