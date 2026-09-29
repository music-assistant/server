"""Test-only compatibility shims for the standalone provider environment."""

from __future__ import annotations

import sys
import types


def _install_hass_client_stub() -> None:
    """Stub the unused Home Assistant client imported transitively by MA."""
    if "hass_client" in sys.modules:
        return

    package = types.ModuleType("hass_client")
    package.__path__ = []
    package.HomeAssistantClient = object  # type: ignore[attr-defined]

    exceptions = types.ModuleType("hass_client.exceptions")
    exceptions.BaseHassClientError = type(  # type: ignore[attr-defined]
        "BaseHassClientError", (Exception,), {}
    )

    utils = types.ModuleType("hass_client.utils")
    for name in ("base_url", "get_auth_url", "get_token", "get_websocket_url"):
        setattr(utils, name, lambda *_args, **_kwargs: None)

    sys.modules.update(
        {
            "hass_client": package,
            "hass_client.exceptions": exceptions,
            "hass_client.utils": utils,
        }
    )


_install_hass_client_stub()
