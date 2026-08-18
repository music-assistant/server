"""Tests for music-provider source-stream limit declarations."""

from __future__ import annotations

import pytest

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers._demo_music_provider import MyDemoMusicprovider
from music_assistant.providers.abc_radio_network.provider import ABCRadioProvider
from music_assistant.providers.apple_music.provider import AppleMusicProvider
from music_assistant.providers.ard_audiothek import ARDAudiothek
from music_assistant.providers.bbc_sounds import BBCSoundsProvider
from music_assistant.providers.filesystem_local import LocalFileSystemProvider
from music_assistant.providers.internet_archive.provider import InternetArchiveProvider
from music_assistant.providers.itunes_podcasts import ITunesPodcastsProvider
from music_assistant.providers.mammamiradio import MammamiradioProvider
from music_assistant.providers.nts import NTSProvider
from music_assistant.providers.orf_radiothek import RadiothekProvider
from music_assistant.providers.pandora.provider import PandoraProvider
from music_assistant.providers.phishin.provider import PhishInProvider
from music_assistant.providers.podcast_index.provider import PodcastIndexProvider
from music_assistant.providers.qobuz import QobuzProvider
from music_assistant.providers.radiobrowser import RadioBrowserProvider
from music_assistant.providers.radioparadise.provider import RadioParadiseProvider
from music_assistant.providers.rain_mood import RainyMoodProvider
from music_assistant.providers.somafm import SomaFMProvider
from music_assistant.providers.spotify.provider import SpotifyProvider
from music_assistant.providers.sverigesradio import SverigesRadio
from music_assistant.providers.tunein import TuneInProvider
from music_assistant.providers.ytmusic import YoutubeMusicProvider


def _limit(provider_cls: type[MusicProvider]) -> int | None:
    """Read a stateless provider limit without initializing external clients."""
    provider = provider_cls.__new__(provider_cls)
    return provider.max_concurrent_streams


@pytest.mark.parametrize(
    ("provider_cls", "expected"),
    [
        (MyDemoMusicprovider, 5),
        (QobuzProvider, 5),
        (LocalFileSystemProvider, None),
        (SpotifyProvider, 2),
        (YoutubeMusicProvider, 1),
        (AppleMusicProvider, 1),
        (PandoraProvider, 1),
    ],
)
def test_provider_stream_limit_defaults_and_overrides(
    provider_cls: type[MusicProvider], expected: int | None
) -> None:
    """Providers inherit or override the source limit for their source category."""
    assert _limit(provider_cls) == expected


@pytest.mark.parametrize(
    "provider_cls",
    [
        ABCRadioProvider,
        ARDAudiothek,
        BBCSoundsProvider,
        InternetArchiveProvider,
        ITunesPodcastsProvider,
        MammamiradioProvider,
        NTSProvider,
        RadiothekProvider,
        PhishInProvider,
        PodcastIndexProvider,
        RadioBrowserProvider,
        RadioParadiseProvider,
        RainyMoodProvider,
        SomaFMProvider,
        SverigesRadio,
        TuneInProvider,
    ],
)
def test_public_direct_providers_are_unlimited(
    provider_cls: type[MusicProvider],
) -> None:
    """Audited public/direct providers do not consume the default guarded pool."""
    assert _limit(provider_cls) is None
