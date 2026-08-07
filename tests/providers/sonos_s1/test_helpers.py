"""Unit tests for the Sonos S1 helpers."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from soco import SoCo
from soco.exceptions import SoCoException, SoCoUPnPException

from music_assistant.providers.sonos_s1.helpers import SonosUpdateError, soco_error
from music_assistant.providers.sonos_s1.player import SonosPlayer

IGNORED_ERROR_CODE = "501"
OTHER_ERROR_CODE = "701"

SOCO_ERRORS = [
    pytest.param(SoCoException("boom"), id="soco"),
    pytest.param(SoCoUPnPException("boom", OTHER_ERROR_CODE, b""), id="upnp"),
    pytest.param(OSError("boom"), id="oserror"),
    pytest.param(TimeoutError("boom"), id="timeout"),
]

SYNC_AND_ASYNC = ["probe", "regroup"]
SYNC_AND_ASYNC_FILTERED = ["probe_filtered", "regroup_filtered"]


class _Speaker(SonosPlayer):
    """Minimal speaker exposing a decorated service call in a sync and an async flavour."""

    def __init__(self, error: Exception | None = None) -> None:
        self.soco = SoCo("192.168.99.1")
        self.soco._player_name = "Kitchen"
        self._error = error

    @soco_error()
    def probe(self) -> str:
        """Sync service call."""
        return self._service_call()

    @soco_error()
    async def regroup(self) -> str:
        """Async service call."""
        return self._service_call()

    @soco_error([IGNORED_ERROR_CODE])
    def probe_filtered(self) -> str:
        """Sync service call that tolerates one error code."""
        return self._service_call()

    @soco_error([IGNORED_ERROR_CODE])
    async def regroup_filtered(self) -> str:
        """Async service call that tolerates one error code."""
        return self._service_call()

    def _service_call(self) -> str:
        if self._error:
            raise self._error
        return "ok"


async def _invoke(speaker: _Speaker, method: str) -> Any:
    """Call the named decorated method, awaiting it for the async flavour."""
    result = getattr(speaker, method)()
    return await result if inspect.isawaitable(result) else result


@pytest.mark.parametrize("method", SYNC_AND_ASYNC)
async def test_result_is_returned_untouched(method: str) -> None:
    """A call that succeeds returns its own result."""
    assert await _invoke(_Speaker(), method) == "ok"


@pytest.mark.parametrize("error", SOCO_ERRORS)
@pytest.mark.parametrize("method", SYNC_AND_ASYNC)
async def test_soco_errors_become_a_sonos_update_error(method: str, error: Exception) -> None:
    """Every kind of soco failure is reported as a SonosUpdateError naming the speaker."""
    with pytest.raises(SonosUpdateError, match="Kitchen") as exc_info:
        await _invoke(_Speaker(error), method)

    assert exc_info.value.__cause__ is error


@pytest.mark.parametrize("method", SYNC_AND_ASYNC_FILTERED)
async def test_ignored_error_code_is_swallowed(method: str) -> None:
    """An error code the caller opted to ignore yields None instead of an exception."""
    error = SoCoUPnPException("boom", IGNORED_ERROR_CODE, b"")

    assert await _invoke(_Speaker(error), method) is None


@pytest.mark.parametrize("method", SYNC_AND_ASYNC_FILTERED)
async def test_other_error_codes_are_still_raised(method: str) -> None:
    """Error codes outside the ignore list are reported as usual."""
    error = SoCoUPnPException("boom", OTHER_ERROR_CODE, b"")

    with pytest.raises(SonosUpdateError, match="Kitchen"):
        await _invoke(_Speaker(error), method)


def test_async_method_stays_a_coroutine_function() -> None:
    """Decorated coroutine methods remain awaitable for their callers."""
    assert inspect.iscoroutinefunction(_Speaker.regroup)
    assert not inspect.iscoroutinefunction(_Speaker.probe)
