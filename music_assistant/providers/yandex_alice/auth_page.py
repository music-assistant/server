"""
Yandex Passport Device Flow sign-in for the skill pipeline.

Thin wrapper over the shared Music Assistant auth layer in
``ya_passport_auth.ma``. The form-side click on *Sign in to Yandex
Passport* is a single **blocking** action: ``perform_device_auth`` starts a
Device Flow, opens the MA-hosted device-code page (localized, with an
honest countdown and terminal states) as an ``AuthenticationHelper``
popup, polls Passport until the user confirms in Yandex, and returns the
resulting ``x_token``.

The blocking pattern is dictated by MA's frontend: in *add-provider* mode
hidden ``ConfigEntry``s do not round-trip between successive ``ACTION``
clicks until the provider is saved, so a multi-click self-resuming flow is
impossible there. All yandex providers share the same pattern via the
library layer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ya_passport_auth.ma import DevicePageConfig, run_device_flow

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

_LOGGER = logging.getLogger(__name__)

__all__ = ["DEVICE_CODE_PAGE_BASE_PATH", "perform_device_auth"]

# Route namespace is derived from the page config's domain by the library;
# kept as a constant for URL assertions and log grepping.
DEVICE_CODE_PAGE_BASE_PATH = "/yandex_alice/device_code"


def _page_config(skill_name: str) -> DevicePageConfig:
    """Build the page branding for a sign-in on behalf of *skill_name*."""
    name = skill_name or "Music Assistant"
    return DevicePageConfig(
        domain="yandex_alice",
        title={"en": "Sign in to Yandex Passport", "ru": "Вход в Яндекс Паспорт"},
        context_text={
            "en": (
                f"Music Assistant will register the {name} dialog skill on "
                "your behalf. Enter the code below in Yandex Passport."
            ),
            "ru": (
                f"Music Assistant зарегистрирует диалоговый навык {name} от "
                "вашего имени. Введите код ниже в Яндекс Паспорте."
            ),
        },
    )


async def perform_device_auth(
    mass: MusicAssistant,
    session_id: str,
    *,
    skill_name: str = "Music Assistant",
) -> tuple[str, str]:
    """
    Run a complete Yandex Passport Device Flow.

    Returns ``(x_token, display_login)`` — the long-lived auth token
    plus the user-visible Yandex login (used in the "Authorized as
    <name>" banner). ``display_login`` is ``""`` when Yandex didn't
    return one.

    Blocks for the lifetime of the user's confirmation step (up to
    ~10 min). Hosts the device-code page at
    ``/yandex_alice/device_code/<session_id>`` for the duration and
    forwards it to the user via ``AuthenticationHelper`` popup; the page
    routes stay alive in the background for a grace window afterwards so
    the popup can observe the terminal state and close itself.

    ``session_id`` **must** be the ``values["session_id"]`` value MA's
    frontend supplies on every ACTION invocation — popping a popup on
    any other channel results in a popup the frontend isn't listening
    for, so it never appears.

    :raises InvalidDataError: ``session_id`` is empty or unsafe for a
        route path.
    :raises LoginFailed: the Device Flow timed out, was rejected by
        Yandex, or another terminal Passport error occurred.
    :raises ResourceTemporarilyUnavailable: transient Passport failure —
        retry later.
    """
    result = await run_device_flow(
        mass,
        session_id,
        _page_config(skill_name),
        device_name="Music Assistant",
    )
    x_token = result.credentials.x_token.get_secret()
    _LOGGER.debug(
        "Device flow complete, captured x_token (len=%d) for %r",
        len(x_token),
        result.display_login or "<unknown>",
    )
    return x_token, result.display_login
