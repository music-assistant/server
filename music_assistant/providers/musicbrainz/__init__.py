"""The Musicbrainz Metadata provider for Music Assistant."""

from __future__ import annotations

from .models import MusicBrainzReleaseGroup
from .provider import MusicbrainzProvider, setup

__all__ = [
    "MusicBrainzReleaseGroup",
    "MusicbrainzProvider",
    "setup",
]
