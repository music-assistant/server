"""Shared helpers for the Chromecast player tests."""

from __future__ import annotations

from functools import partial
from typing import cast
from unittest.mock import MagicMock

from music_assistant.providers.chromecast.player import ChromecastPlayer

# Private helpers that _handle_media_status delegates its work to. The handler is
# driven with a MagicMock as 'self', which would answer these calls itself instead of
# updating the player state, so they are bound to the real implementation.
# Extend this when the handler gains a helper, or its part is silently skipped.
MEDIA_STATUS_HELPERS = (
    "_reset_to_idle",
    "_report_media_error",
    "_update_playback_state",
    "_update_elapsed_time",
    "_update_active_source",
    "_update_current_media",
    "_update_multichannel_group_members",
)


def bind_media_status_helpers(fake: MagicMock) -> MagicMock:
    """
    Let a fake Cast player run the real media-status helpers, and return it.

    :param fake: MagicMock standing in for a ChromecastPlayer.
    """
    for name in MEDIA_STATUS_HELPERS:
        setattr(
            fake, name, partial(getattr(ChromecastPlayer, name), cast("ChromecastPlayer", fake))
        )
    return fake
