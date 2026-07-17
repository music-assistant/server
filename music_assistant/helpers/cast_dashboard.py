"""Helpers for casting MA dashboards to a display device."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.helpers.guest_access import get_or_create_guest_user, get_or_create_join_code

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

CAST_VIEWER_USERNAME = "cast_viewer"
CAST_VIEWER_DISPLAY_NAME = "Dashboard Cast Viewer"
CAST_CODE_EXPIRY_HOURS = 1


async def get_cast_code(mass: MusicAssistant) -> str:
    """
    Get a one-time code a cast receiver can exchange for a viewer token.

    :param mass: MusicAssistant instance.
    :return: A short join code, valid for a single exchange.
    """
    user = await get_or_create_guest_user(mass, CAST_VIEWER_USERNAME, CAST_VIEWER_DISPLAY_NAME)
    return await get_or_create_join_code(
        mass,
        user,
        expires_in_hours=CAST_CODE_EXPIRY_HOURS,
        max_uses=1,
        device_name="Cast Receiver",
    )
