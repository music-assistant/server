"""Tests for the operator language hint the Sendspin server announces to clients."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from music_assistant.providers.sendspin.provider import SendspinProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


@pytest.mark.parametrize(
    ("locale", "expected"),
    [("nl_NL", ("nl-NL", "nl")), ("en", ("en",))],
)
def test_spoken_pin_languages_follow_the_metadata_locale(
    locale: str, expected: tuple[str, ...]
) -> None:
    """The operator's languages are the metadata locale, most preferred first."""
    provider = SendspinProvider.__new__(SendspinProvider)
    provider.mass = cast("MusicAssistant", SimpleNamespace(metadata=SimpleNamespace(locale=locale)))

    assert provider._spoken_pin_languages() == expected
