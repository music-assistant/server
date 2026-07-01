"""The Musicbrainz Metadata provider for Music Assistant."""

from __future__ import annotations

from .models import MusicBrainzReleaseGroup
from .provider import MusicbrainzProvider, get_config_entries, setup

__all__ = [
    "MusicBrainzReleaseGroup",
    "MusicbrainzProvider",
    "get_config_entries",
    "setup",
]
