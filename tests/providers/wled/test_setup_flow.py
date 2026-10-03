"""Tests for the WLED setup flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from music_assistant.constants import CONF_PORT
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.wled.constants import DEFAULT_PORT
from music_assistant.providers.wled.setup_flow import run_setup


def _session(used_ports: list[int], setup_data: dict[str, int] | None = None) -> MagicMock:
    session = MagicMock()
    session.context.domain = "wled"
    session.context.setup_data = setup_data or {}
    siblings = [MagicMock(instance_id=f"wled{i}") for i in range(len(used_ports))]
    ports = {f"wled{i}": port for i, port in enumerate(used_ports)}
    session.mass.config.get_provider_configs = AsyncMock(return_value=siblings)
    session.mass.config.get_provider_setup_value = MagicMock(
        side_effect=lambda instance_id, _key: ports[instance_id]
    )
    session.form = AsyncMock(
        side_effect=lambda entries, **_kw: {CONF_PORT: entries[0].default_value}
    )
    session.finish = AsyncMock()
    return session


async def test_first_zone_suggests_default_port() -> None:
    """The first zone gets the default port."""
    session = _session([])
    await run_setup(session)
    session.finish.assert_awaited_once_with({CONF_PORT: DEFAULT_PORT})


async def test_next_zone_skips_used_ports() -> None:
    """A new zone gets the lowest unused port."""
    session = _session([DEFAULT_PORT, DEFAULT_PORT + 1])
    await run_setup(session)
    session.finish.assert_awaited_once_with({CONF_PORT: DEFAULT_PORT + 2})


async def test_reconfigure_prefills_stored_port() -> None:
    """Reconfiguring keeps the stored port as the default."""
    session = _session([DEFAULT_PORT + 5], setup_data={CONF_PORT: DEFAULT_PORT + 5})
    await run_setup(session)
    session.finish.assert_awaited_once_with({CONF_PORT: DEFAULT_PORT + 5})


async def test_finish_error_reshows_form() -> None:
    """A rejected port shows the error and lets the user retry."""
    session = _session([])
    err = SetupFlowError("boom")
    session.finish = AsyncMock(side_effect=[err, None])
    await run_setup(session)
    assert session.form.await_count == 2
    assert session.form.await_args.kwargs["errors"] == {"base": err.translation_key or "boom"}
