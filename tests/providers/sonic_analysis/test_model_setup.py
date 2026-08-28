"""Tests for the CLAP model load that gates SonicAnalysisProvider setup."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from music_assistant_models.enums import ContentType
from music_assistant_models.errors import SetupFailedError, UnsupportedSystemError
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.sonic_analysis import (
    CLAP_SAMPLING_FAST,
    SonicAnalysisProvider,
)

SETUP_TASK_ID = "sonic_analysis.model_setup.instance-1"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMass:
    """
    Stand-in for MusicAssistant reproducing the ``create_task`` dedupe setup relies on.

    A MagicMock would return one from ``create_task``, leaving the coroutine unawaited.
    """

    def __init__(self) -> None:
        self.streams = MagicMock()
        self.cache = MagicMock()
        self.tracked: dict[str, asyncio.Task[Any]] = {}
        self.task_ids: list[str | None] = []

    def create_task(
        self, target: Any, *args: Any, task_id: str | None = None, **kwargs: Any
    ) -> asyncio.Task[Any]:
        """Return the live task registered under task_id, else start and track a new one."""
        self.task_ids.append(task_id)
        if task_id and (existing := self.tracked.get(task_id)) and not existing.done():
            target.close()
            return existing
        task: asyncio.Task[Any] = asyncio.ensure_future(target)
        # mass.create_task installs a done callback that retrieves the exception; without
        # an equivalent here a load nobody waits on logs "exception was never retrieved".
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        if task_id:
            self.tracked[task_id] = task
        return task

    async def drain(self) -> None:
        """Cancel and await every tracked task, so no test leaks one."""
        for task in self.tracked.values():
            task.cancel()
        await asyncio.gather(*self.tracked.values(), return_exceptions=True)


def _make_provider(mass: _FakeMass | None = None) -> SonicAnalysisProvider:
    """
    Construct a SonicAnalysisProvider with mocked MA infrastructure.

    :param mass: Shared stand-in, for tests needing two providers on one task registry.
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

    provider = SonicAnalysisProvider(mass or _FakeMass(), manifest, config)  # type: ignore[arg-type]
    provider.logger = MagicMock()
    return provider


def _fake_models() -> tuple[Any, Any, list[tuple[str, tuple[str, str]]]]:
    """Return a stand-in for what a completed CLAP load hands back."""
    return MagicMock(name="clap_model"), MagicMock(name="text_embeddings"), []


def _make_audio_format() -> AudioFormat:
    """Return a real AudioFormat for 16-bit mono PCM."""
    return AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=22050, bit_depth=16, channels=1
    )


def _make_streamdetails(item_id: str = "track-1", duration: float | None = 60.0) -> MagicMock:
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
# handle_async_init
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_load_populates_state() -> None:
    """A load that finishes in time must populate model state and mark models loaded."""
    provider = _make_provider()
    model, embeddings, prompt_order = _fake_models()

    with patch.object(provider, "_load_clap", return_value=(model, embeddings, prompt_order)):
        await provider.handle_async_init()

    assert provider._clap_model is model
    assert provider._clap_text_embeddings is embeddings
    assert provider._clap_prompt_order == prompt_order
    assert provider._models_loaded is True


@pytest.mark.asyncio
async def test_load_failure_propagates() -> None:
    """Load failures must propagate, so the provider is left unavailable."""
    provider = _make_provider()

    with (
        patch.object(provider, "_load_clap", side_effect=RuntimeError("checkpoint is corrupt")),
        pytest.raises(RuntimeError, match="checkpoint is corrupt"),
    ):
        await provider.handle_async_init()

    assert provider._clap_model is None
    assert provider._models_loaded is False


@pytest.mark.asyncio
async def test_load_is_offloaded_to_a_thread() -> None:
    """The download and model build must run off the event loop."""
    provider = _make_provider()

    with patch(
        "music_assistant.providers.sonic_analysis.asyncio.to_thread",
        new=AsyncMock(return_value=_fake_models()),
    ) as to_thread_mock:
        await provider.handle_async_init()

    # Use ``==`` (not ``is``): each attribute access yields a fresh bound-method object,
    # but bound methods of the same (instance, function) compare equal.
    assert to_thread_mock.call_args.args[0] == provider._load_clap


@pytest.mark.asyncio
async def test_unsupported_system_fails_before_any_download() -> None:
    """An unsupported host must fail before the checkpoint is fetched."""
    provider = _make_provider()

    with (
        patch(
            "music_assistant.providers.sonic_analysis.verify_system_meets_requirements",
            side_effect=UnsupportedSystemError("unsupported system"),
        ),
        patch.object(SonicAnalysisProvider, "_load_clap") as load_clap_mock,
        pytest.raises(UnsupportedSystemError),
    ):
        await provider.handle_async_init()

    load_clap_mock.assert_not_called()


@pytest.mark.asyncio
async def test_slow_load_fails_setup_but_keeps_running() -> None:
    """A load that outlives the grace period fails setup but keeps running."""
    mass = _FakeMass()
    provider = _make_provider(mass)

    with (
        patch("music_assistant.providers.sonic_analysis.MODEL_SETUP_GRACE_SECONDS", 0.05),
        patch(
            "music_assistant.providers.sonic_analysis.asyncio.to_thread",
            new=lambda *_a: asyncio.Event().wait(),
        ),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        await provider.handle_async_init()

    assert exc_info.value.translation_key == "model_setup_pending"
    assert exc_info.value.translation_owner == "provider.sonic_analysis"
    assert provider._models_loaded is False
    assert not mass.tracked[SETUP_TASK_ID].done(), "the load must survive the timeout"

    await mass.drain()


@pytest.mark.asyncio
async def test_retry_joins_the_running_load_and_gets_its_result() -> None:
    """A retry joins the running load and comes away with its result."""
    mass = _FakeMass()
    release = asyncio.Event()
    models = _fake_models()
    load_calls = 0

    async def _blocked_load(*_args: Any) -> tuple[Any, Any, list[Any]]:
        nonlocal load_calls
        load_calls += 1
        await release.wait()
        return models

    with patch("music_assistant.providers.sonic_analysis.asyncio.to_thread", new=_blocked_load):
        first = _make_provider(mass)
        with (
            patch("music_assistant.providers.sonic_analysis.MODEL_SETUP_GRACE_SECONDS", 0.05),
            pytest.raises(SetupFailedError),
        ):
            await first.handle_async_init()

        second = _make_provider(mass)
        # the load lands partway through the retry's own grace period
        asyncio.get_running_loop().call_soon(release.set)
        await second.handle_async_init()

    assert load_calls == 1, "the retry must not start a second load"
    assert mass.task_ids == [SETUP_TASK_ID, SETUP_TASK_ID], "the key must be stable"
    assert second._clap_model is models[0]
    assert second._models_loaded is True


# ---------------------------------------------------------------------------
# _load_clap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "err",
    [OSError("disk full"), httpx.ConnectError("connection refused")],
    ids=["oserror", "httpx_connect"],
)
def test_download_failures_become_retryable_setup_errors(err: Exception) -> None:
    """A failed download must reach MA as a typed error, which is what gets it retried."""
    provider = _make_provider()

    with (
        patch.object(provider, "_try_load_cached_prompt_embeddings", return_value=MagicMock()),
        patch("music_assistant.providers.sonic_analysis.vendored_clap.CLAP", side_effect=err),
        pytest.raises(SetupFailedError) as exc_info,
    ):
        provider._load_clap()

    assert exc_info.value.translation_key == "model_assets_download_failed"
    assert exc_info.value.__cause__ is err


def test_missing_prompt_embeddings_fail_instead_of_downloading_a_text_encoder() -> None:
    """Missing or stale prompt embeddings must fail, not fall back to the text encoder."""
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
# _start_analysis gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_analysis_declines_while_clap_is_unavailable() -> None:
    """``_start_analysis`` must decline tracks while CLAP is unavailable."""
    provider = _make_provider()
    provider._clap_model = None

    result = await provider._start_analysis(
        "session-skip", _make_streamdetails("skip-me"), _make_audio_format()
    )

    assert result is False
    assert provider._sessions == {}


@pytest.mark.asyncio
async def test_start_analysis_proceeds_when_clap_loaded() -> None:
    """``_start_analysis`` must create a session when CLAP is available."""
    provider = _make_provider()
    provider._clap_model = MagicMock(name="clap_model")

    result = await provider._start_analysis(
        "session-ok", _make_streamdetails("go-ahead"), _make_audio_format()
    )

    assert result is True
    assert "session-ok" in provider._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [None, 0, 0.0])
async def test_start_analysis_declines_without_duration(duration: float | None) -> None:
    """``_start_analysis`` must decline tracks without a usable duration."""
    provider = _make_provider()
    provider._clap_model = MagicMock(name="clap_model")

    result = await provider._start_analysis(
        "session-no-duration", _make_streamdetails("no-duration", duration), _make_audio_format()
    )

    assert result is False
    assert provider._sessions == {}
