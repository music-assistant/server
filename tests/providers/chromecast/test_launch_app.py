"""
Tests for ChromecastPlayer receiver app launching.

Regression tests for https://github.com/music-assistant/support/issues/5981, where a
receiver refused the launch and Music Assistant carried on regardless, sending the LOAD
to an app that was not running. Playback never started and nothing was logged.

A failed launch must be reported, and the configured receiver app must not be swapped
for the other one.

A launch is also needed when the receiver still reports our app while a release of it
is already on the wire, since the LOAD would otherwise land in a dying session.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.providers.chromecast.constants import APP_MEDIA_RECEIVER, MASS_APP_ID
from music_assistant.providers.chromecast.player import ChromecastPlayer


async def _launch_app(fake: MagicMock) -> None:
    await ChromecastPlayer._launch_app(cast("ChromecastPlayer", fake))


def _fake_cast(
    *,
    running_app_id: str | None = None,
    use_mass_app: bool = True,
    launch_results: dict[str, bool | None],
    refusal_reason: str | None = None,
    app_id_after_launch: str | None = "same",
    app_quit_sent: bool = False,
) -> MagicMock:
    """
    Build a MagicMock Cast whose receiver answers launches per app id.

    :param running_app_id: App id the receiver is already running, if any.
    :param use_mass_app: Value of the ``use_mass_app`` config option.
    :param launch_results: Per app id: True to accept, False to refuse, None to leave
        the launch unanswered.
    :param refusal_reason: LAUNCH_ERROR reason reported by the receiver.
    :param app_id_after_launch: App the receiver reports running after accepting a
        launch. "same" means the app that was asked for.
    :param app_quit_sent: Whether a release of the receiver app is already on the wire.
    """
    fake = MagicMock()
    # the launch runs in an executor and is acknowledged from that thread, so the real
    # running loop is needed to bridge back to the waiting coroutine
    fake.mass.loop = asyncio.get_running_loop()
    fake.display_name = "Fake Cast"
    fake.cc.app_id = running_app_id
    fake.app_quit_sent = app_quit_sent
    fake.config.get_value = MagicMock(return_value=use_mass_app)
    fake.launch_attempts = []
    fake.cc.socket_client.receiver_controller.launch_failure.reason = refusal_reason

    def launch_app(
        app_id: str,
        *,
        force_launch: bool = False,  # noqa: ARG001
        callback_function: Any = None,
    ) -> None:
        fake.launch_attempts.append(app_id)
        result = launch_results.get(app_id)
        if result is None:
            # receiver ignores the request, so the callback is never invoked
            return
        if result:
            fake.cc.app_id = app_id if app_id_after_launch == "same" else app_id_after_launch
        callback_function(result, None)

    fake.cc.socket_client.receiver_controller.launch_app = launch_app
    fake._log_launch_failure = partial(ChromecastPlayer._log_launch_failure, fake)
    return fake


def _warnings(fake: MagicMock) -> str:
    return " ".join(str(call) for call in fake.logger.warning.call_args_list)


@pytest.fixture(autouse=True)
def _instant_launch_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the unanswered-launch cases from waiting out the real timeout."""
    monkeypatch.setattr("music_assistant.providers.chromecast.player.APP_LAUNCH_TIMEOUT", 0.01)


async def test_refused_launch_is_not_treated_as_success() -> None:
    """A refused launch must fail instead of letting a LOAD be sent."""
    fake = _fake_cast(launch_results={MASS_APP_ID: False})

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)


async def test_ignored_launch_fails() -> None:
    """An unanswered launch fails once the timeout expires."""
    fake = _fake_cast(launch_results={MASS_APP_ID: None})

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)


async def test_successful_launch_returns() -> None:
    """A confirmed launch completes without raising."""
    fake = _fake_cast(launch_results={MASS_APP_ID: True})

    await _launch_app(fake)

    assert fake.launch_attempts == [MASS_APP_ID]


async def test_configured_app_is_never_swapped() -> None:
    """A failed launch does not silently retry the other receiver app."""
    fake = _fake_cast(launch_results={MASS_APP_ID: False, APP_MEDIA_RECEIVER: True})

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)

    assert fake.launch_attempts == [MASS_APP_ID]


async def test_default_receiver_used_when_mass_app_disabled() -> None:
    """With the option disabled the default media receiver is launched."""
    fake = _fake_cast(use_mass_app=False, launch_results={APP_MEDIA_RECEIVER: True})

    await _launch_app(fake)

    assert fake.launch_attempts == [APP_MEDIA_RECEIVER]


async def test_configured_app_already_running_is_left_alone() -> None:
    """No LAUNCH is sent when the configured receiver app is already active."""
    fake = _fake_cast(running_app_id=MASS_APP_ID, launch_results={MASS_APP_ID: True})

    await _launch_app(fake)

    assert fake.launch_attempts == []


async def test_app_that_is_being_quit_is_relaunched() -> None:
    """A receiver still reporting our app is no proof of a usable session after a quit."""
    fake = _fake_cast(
        running_app_id=MASS_APP_ID,
        launch_results={MASS_APP_ID: True},
        app_quit_sent=True,
    )

    await _launch_app(fake)

    assert fake.launch_attempts == [MASS_APP_ID]


async def test_confirmed_launch_clears_the_pending_quit() -> None:
    """The new session is not the one that was quit, so it is usable again."""
    fake = _fake_cast(
        running_app_id=MASS_APP_ID,
        launch_results={MASS_APP_ID: True},
        app_quit_sent=True,
    )

    await _launch_app(fake)

    assert fake.app_quit_sent is False


@pytest.mark.parametrize(
    ("launch_results", "app_id_after_launch"),
    [
        ({MASS_APP_ID: False}, "same"),
        ({MASS_APP_ID: None}, "same"),
        ({MASS_APP_ID: True}, None),
    ],
    ids=["refused", "unanswered", "not_started"],
)
async def test_failed_launch_keeps_the_pending_quit(
    launch_results: dict[str, bool | None], app_id_after_launch: str | None
) -> None:
    """No new session came up, so the one that was quit is still the doomed one."""
    fake = _fake_cast(
        running_app_id=MASS_APP_ID,
        launch_results=launch_results,
        app_id_after_launch=app_id_after_launch,
        app_quit_sent=True,
    )

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)

    assert fake.app_quit_sent is True


async def test_other_receiver_app_is_replaced_by_the_configured_one() -> None:
    """
    The other compatible receiver app is not accepted as-is.

    Treating both receiver apps as interchangeable made the use_mass_app setting a
    no-op for as long as the other app was running on the device.
    """
    fake = _fake_cast(running_app_id=APP_MEDIA_RECEIVER, launch_results={MASS_APP_ID: True})

    await _launch_app(fake)

    assert fake.launch_attempts == [MASS_APP_ID]


async def test_disabled_mass_app_is_replaced_by_the_default_receiver() -> None:
    """With the option disabled, a running MA app is replaced by the default receiver."""
    fake = _fake_cast(
        running_app_id=MASS_APP_ID,
        use_mass_app=False,
        launch_results={APP_MEDIA_RECEIVER: True},
    )

    await _launch_app(fake)

    assert fake.launch_attempts == [APP_MEDIA_RECEIVER]


async def test_refusal_reason_is_logged() -> None:
    """The reason the receiver gave is included in the warning."""
    fake = _fake_cast(launch_results={MASS_APP_ID: False}, refusal_reason="NOT_FOUND")

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)

    assert "NOT_FOUND" in _warnings(fake)


async def test_failure_suggests_disabling_the_mass_app() -> None:
    """A failed Music Assistant app launch suggests turning the option off."""
    fake = _fake_cast(launch_results={MASS_APP_ID: False})

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)

    assert "disabling" in _warnings(fake)


async def test_failure_suggests_enabling_the_mass_app() -> None:
    """A failed default receiver launch suggests turning the option on."""
    fake = _fake_cast(use_mass_app=False, launch_results={APP_MEDIA_RECEIVER: False})

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)

    assert "enabling" in _warnings(fake)


async def test_acknowledged_launch_that_did_not_start_the_app_fails() -> None:
    """A launch the receiver accepted but did not act on must still fail."""
    fake = _fake_cast(launch_results={MASS_APP_ID: True}, app_id_after_launch=None)

    with pytest.raises(PlayerUnavailableError):
        await _launch_app(fake)
