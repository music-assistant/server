"""Setup secrets, retry behavior and stable device identities."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from music_assistant.models.setup_flow import SetupFlowContext, SetupFlowError
from music_assistant.providers.feiniu_music.client import FeiNiuClient
from music_assistant.providers.feiniu_music.protocol import PROFILE
from music_assistant.providers.feiniu_music.setup_flow import run_setup


@pytest.mark.parametrize(
    "update",
    [
        {},
        {"password": "replacement-secret"},
    ],
)
async def test_reconfigure_blank_password_preserves_secret_and_device(
    update: dict[str, Any],
) -> None:
    """Secrets stay server-side and normal edits do not rotate the device ID."""
    entries_seen = []

    async def form(entries: Any, **_kwargs: Any) -> dict[str, Any]:
        entries_seen.extend(entries)
        return {"password": "", **update}

    saved = {
        "url": "http://test.invalid/music/",
        "username": "synthetic",
        "password": "synthetic-secret",
        "device_id": "a" * 32,
    }
    session: Any = SimpleNamespace(
        context=SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="feiniu_music",
            instance_id="saved-instance",
            setup_data=saved,
        ),
        form=form,
        finish=AsyncMock(),
    )
    await run_setup(session)
    password = next(entry for entry in entries_seen if entry.key == "password")
    assert password.value is None
    assert not password.required
    values = session.finish.call_args.args[0]
    assert values == {**saved, **update}
    # Reopening the saved instance must have the same non-echoing behavior.
    session.context.setup_data = values
    await run_setup(session)
    assert session.finish.call_args.args[0]["device_id"] == "a" * 32


@pytest.mark.parametrize(
    "identity", [{"url": "http://other.invalid/music/"}, {"username": "other"}]
)
@pytest.mark.parametrize("password", ["", "replacement-secret"])
async def test_reconfigure_rejects_identity_before_login_or_save(
    identity: dict[str, str],
    password: str,
) -> None:
    """Untrusted form values cannot replace the original saved identity."""
    saved = {
        "url": "http://test.invalid/music/",
        "username": "synthetic",
        "password": "synthetic-secret",
        "device_id": "a" * 32,
    }
    attempts = 0

    async def form(entries: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        assert attempts <= 3
        assert next(entry for entry in entries if entry.key == "password").value is None
        # finish is MA's validation/login/persistence boundary. No rejected identity reaches it.
        session.finish.assert_not_awaited()
        assert session.context.setup_data == saved
        if attempts > 1:
            assert kwargs["errors"] == {"base": "identity_change_not_supported"}
            for key in ("url", "username"):
                assert next(entry for entry in entries if entry.key == key).value == saved[key]
        if attempts < 3:
            return {**identity, "password": password}
        return {"password": ""}

    session: Any = SimpleNamespace(
        context=SetupFlowContext(
            kind="reconfigure",
            reason="user",
            domain="feiniu_music",
            instance_id="saved-instance",
            setup_data=dict(saved),
        ),
        form=form,
        finish=AsyncMock(),
    )
    await run_setup(session)
    assert attempts == 3
    session.finish.assert_awaited_once_with(saved)


async def test_first_setup_requires_password_and_new_instances_get_new_device() -> None:
    """An unconfigured form must not imply saved authentication."""
    devices = []
    for _ in range(2):

        async def form(entries: Any, **_kwargs: Any) -> dict[str, Any]:
            password = next(entry for entry in entries if entry.key == "password")
            assert password.required
            assert password.value is None
            return {
                "url": "http://test.invalid",
                "username": "synthetic",
                "password": "synthetic-secret",
            }

        session: Any = SimpleNamespace(
            context=SetupFlowContext(kind="setup", reason="user", domain="feiniu_music"),
            form=form,
            finish=AsyncMock(),
        )
        await run_setup(session)
        devices.append(session.finish.call_args.args[0]["device_id"])
    assert devices[0] != devices[1]


@pytest.mark.parametrize("replacement_password", ["", "corrected-secret"])
async def test_login_retry_retains_submitted_secret_without_prefill(
    replacement_password: str,
) -> None:
    """A corrected username can retry without echoing or discarding its password."""
    attempts = 0

    async def form(entries: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        password = next(entry for entry in entries if entry.key == "password")
        assert password.value is None
        if attempts == 1:
            return {
                "url": "http://test.invalid",
                "username": "wrong",
                "password": "synthetic-secret",
            }
        assert not password.required
        assert kwargs["errors"]
        return {
            "url": "http://corrected.invalid/music/",
            "username": "corrected",
            "password": replacement_password,
        }

    session: Any = SimpleNamespace(
        context=SetupFlowContext(kind="setup", reason="user", domain="feiniu_music"),
        form=form,
        finish=AsyncMock(side_effect=[SetupFlowError("Login failed"), None]),
    )
    await run_setup(session)
    values = session.finish.call_args.args[0]
    assert values["password"] == (replacement_password or "synthetic-secret")
    assert values["username"] == "corrected"
    assert values["url"] == "http://corrected.invalid/music/"


async def test_first_empty_password_cannot_authenticate() -> None:
    """The real client's input validation rejects empty credentials before I/O."""
    client = FeiNiuClient("http://test.invalid", PROFILE)
    with pytest.raises(ValueError, match="Missing credentials"):
        await client.login("synthetic", "", "a" * 32)
