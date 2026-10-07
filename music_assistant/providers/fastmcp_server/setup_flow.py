"""Setup flow for connecting an MCP client."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant.models.setup_flow import SetupFlowError

from ._init_helpers import _dispatch_open_connect
from .config import build_config_entries
from .connect import mount_connect_wizard
from .constants import (
    CONF_EXTRA_ALLOWED_ORIGINS,
    CONF_MOUNT_PATH,
    CONF_TRUST_FORWARDED_PROTO,
    DEFAULT_MOUNT_PATH,
)
from .policy_config import build_policy_resolver

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ConfigValueType, ProviderConfig

    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Open the Connect Wizard and finish after it generates a client configuration."""
    values = _effective_values(session)
    mount_path = str(values.get(CONF_MOUNT_PATH) or DEFAULT_MOUNT_PATH)
    unmount: Callable[[], None] | None = None
    instance_id = session.context.instance_id
    provider = (
        session.mass.get_provider(instance_id, return_unavailable=True) if instance_id else None
    )
    if provider is None:
        unmount = await mount_connect_wizard(
            session.mass,
            mount_path,
            default_profile_provider=lambda: (
                build_policy_resolver(cast("ProviderConfig", _ValuesConfig(values)))
                .resolve(None)
                .profile.value
            ),
            extra_origins_csv=str(values.get(CONF_EXTRA_ALLOWED_ORIGINS) or ""),
            trust_forwarded_proto=bool(values.get(CONF_TRUST_FORWARDED_PROTO)),
        )
    try:
        wizard_url = await _dispatch_open_connect(
            session.mass,
            values,
            setup_callback_path=session.callback_path,
        )
        if wizard_url is None:
            raise SetupFlowError("Unable to open the MCP Connect Wizard")
        await session.external(wizard_url, step_id="connect")
    finally:
        if unmount is not None:
            unmount()
    await session.finish({})


def _effective_values(session: SetupSession) -> dict[str, ConfigValueType]:
    """Return stored option values overlaid on the MCP provider defaults."""
    entries = build_config_entries(session.mass, DEFAULT_MOUNT_PATH)
    values = {entry.key: entry.default_value for entry in entries}
    values.update(session.context.values)
    return values


class _ValuesConfig:
    """Minimal ProviderConfig-compatible view over setup-flow values."""

    def __init__(self, values: dict[str, ConfigValueType]) -> None:
        self._values = values

    def get_value(self, key: str, default: ConfigValueType = None) -> ConfigValueType:
        """Return one setup value or its caller-supplied default."""
        return self._values.get(key, default)
