"""Tests for greying out library podcasts and radio stations their provider no longer has."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from music_assistant_models.enums import ProviderType
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import Podcast, PodcastEpisode, ProviderMapping, Radio

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

INSTANCE = "rss_1"
PODCAST_ID = "show-1"
RADIO_ID = "station-1"


class _StubProvider(MusicProvider):
    """Music provider holding one podcast and one radio station, or failing to find them."""

    def __init__(self) -> None:
        """Initialize the stub provider."""
        self.error: Exception | None = None
        self.error_after_first: Exception | None = None
        self.lookups = 0
        self.config = MagicMock()
        self.config.instance_id = INSTANCE
        self.manifest = MagicMock()
        self.manifest.domain = "rss"
        self.manifest.type = ProviderType.MUSIC
        self.logger = MagicMock()
        self.available = True
        self.radio_available = True

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Return the podcast, or raise the error the test set."""
        self.lookups += 1
        if self.error:
            raise self.error
        return _podcast()

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """Yield the one episode of the podcast, or raise the error the test set."""
        if self.error:
            raise self.error
        yield PodcastEpisode(
            item_id="ep1",
            provider=INSTANCE,
            name="Episode 1",
            provider_mappings={
                ProviderMapping(item_id="ep1", provider_domain="rss", provider_instance=INSTANCE)
            },
            position=1,
            podcast=_podcast(),
        )
        if self.error_after_first:
            raise self.error_after_first

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Return the radio station, or raise the error the test set."""
        self.lookups += 1
        if self.error:
            raise self.error
        radio = _radio()
        for mapping in radio.provider_mappings:
            mapping.available = self.radio_available
        return radio


@pytest.fixture(name="mass")
def mass_fixture(music_mass_with_cache: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a cache store: episode listings are cached."""
    return music_mass_with_cache


def _mapping(item_id: str) -> ProviderMapping:
    return ProviderMapping(item_id=item_id, provider_domain="rss", provider_instance=INSTANCE)


def _podcast() -> Podcast:
    return Podcast(
        item_id=PODCAST_ID,
        provider=INSTANCE,
        name="My Podcast",
        provider_mappings={_mapping(PODCAST_ID)},
    )


def _radio() -> Radio:
    return Radio(
        item_id=RADIO_ID,
        provider=INSTANCE,
        name="My Station",
        provider_mappings={_mapping(RADIO_ID)},
    )


def _with_provider(mass: MusicAssistant, provider: _StubProvider | None) -> Any:
    """Load the given provider as the only one, or none at all."""

    def _get_provider(provider_id: str, *_args: Any, **_kwargs: Any) -> Any:
        if provider and provider_id in (provider.instance_id, provider.domain):
            return provider
        return None

    return patch.object(mass, "get_provider", side_effect=_get_provider)


async def _available(mass: MusicAssistant, item: Podcast | Radio) -> bool:
    ctrl = mass.music.podcasts if isinstance(item, Podcast) else mass.music.radio
    library_item = await ctrl.get_library_item(item.item_id)
    return all(x.available for x in library_item.provider_mappings)


async def _set_unavailable(mass: MusicAssistant, item: Podcast | Radio) -> None:
    await mass.music.update_provider_mapping(
        item.media_type,
        item.item_id,
        INSTANCE,
        next(iter(item.provider_mappings)).item_id,
        available=False,
    )


async def test_a_podcast_its_provider_no_longer_has_is_greyed_out(mass: MusicAssistant) -> None:
    """Listing a library podcast its provider does not find marks it unavailable."""
    provider = _StubProvider()
    provider.error = MediaNotFoundError("gone")
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    with _with_provider(mass, provider), pytest.raises(MediaNotFoundError):
        await mass.music.podcasts.episodes(db_podcast.item_id, "library")
    assert not await _available(mass, db_podcast)


async def test_walking_a_podcast_its_provider_no_longer_has_greys_it_out(
    mass: MusicAssistant,
) -> None:
    """Walking the episodes of a library podcast its provider does not find marks it too."""
    provider = _StubProvider()
    provider.error = MediaNotFoundError("gone")
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    with _with_provider(mass, provider), pytest.raises(MediaNotFoundError):
        _ = [x async for x in mass.music.podcasts.iter_episodes(db_podcast.item_id, "library")]
    assert not await _available(mass, db_podcast)


@pytest.mark.parametrize(
    "error", [ProviderUnavailableError("down"), TimeoutError("slow")], ids=["down", "timeout"]
)
async def test_a_podcast_whose_provider_fails_otherwise_stays_available(
    mass: MusicAssistant, error: Exception
) -> None:
    """Only a not found greys out a podcast, not an outage or a timeout."""
    provider = _StubProvider()
    provider.error = error
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    with _with_provider(mass, provider), pytest.raises(type(error)):
        await mass.music.podcasts.episodes(db_podcast.item_id, "library")
    assert await _available(mass, db_podcast)


async def test_a_podcast_failing_part_way_stays_available(mass: MusicAssistant) -> None:
    """A walk that fails after the first episode does not grey the podcast out."""
    provider = _StubProvider()
    provider.error_after_first = MediaNotFoundError("gone")
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    with _with_provider(mass, provider), pytest.raises(MediaNotFoundError):
        _ = [x async for x in mass.music.podcasts.iter_episodes(db_podcast.item_id, "library")]
    assert await _available(mass, db_podcast)


@pytest.mark.parametrize("walk", [False, True], ids=["listing", "walk"])
async def test_a_podcast_asked_for_by_provider_id_marks_nothing(
    mass: MusicAssistant, walk: bool
) -> None:
    """A not found for a podcast asked for by its provider id leaves the library item alone."""
    provider = _StubProvider()
    provider.error = MediaNotFoundError("gone")
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())

    async def _fetch() -> None:
        if walk:
            _ = [x async for x in mass.music.podcasts.iter_episodes(PODCAST_ID, INSTANCE)]
        else:
            await mass.music.podcasts.episodes(PODCAST_ID, INSTANCE)

    with (
        _with_provider(mass, provider),
        patch.object(mass.music, "mark_provider_mapping_unavailable") as mark_unavailable,
        patch.object(mass.music, "mark_provider_mapping_available") as mark_available,
        pytest.raises(MediaNotFoundError),
    ):
        await _fetch()
    mark_unavailable.assert_not_called()
    mark_available.assert_not_called()
    assert await _available(mass, db_podcast)


async def test_a_greyed_out_podcast_comes_back_when_listed_again(mass: MusicAssistant) -> None:
    """Listing a greyed out library podcast its provider finds again marks it available."""
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    await _set_unavailable(mass, db_podcast)
    with _with_provider(mass, _StubProvider()):
        episodes = await mass.music.podcasts.episodes(db_podcast.item_id, "library")
    assert [x.item_id for x in episodes] == ["ep1"]
    assert await _available(mass, db_podcast)


async def test_a_greyed_out_podcast_comes_back_when_walked_again(mass: MusicAssistant) -> None:
    """Walking a greyed out library podcast its provider finds again marks it available."""
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    await _set_unavailable(mass, db_podcast)
    with _with_provider(mass, _StubProvider()):
        _ = [x async for x in mass.music.podcasts.iter_episodes(db_podcast.item_id, "library")]
    assert await _available(mass, db_podcast)


async def test_the_daily_check_brings_back_what_the_provider_has_again(
    mass: MusicAssistant,
) -> None:
    """The daily check marks greyed out podcasts and radio stations found again available."""
    provider = _StubProvider()
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    db_radio = await mass.music.radio.add_item_to_library(_radio())
    await _set_unavailable(mass, db_podcast)
    await _set_unavailable(mass, db_radio)
    with _with_provider(mass, provider):
        await mass.music._check_unavailable_mappings()
    assert await _available(mass, db_podcast)
    assert await _available(mass, db_radio)


async def test_the_daily_check_keeps_what_the_provider_reports_unavailable(
    mass: MusicAssistant,
) -> None:
    """A station its provider finds but still reports unavailable stays greyed out."""
    provider = _StubProvider()
    provider.radio_available = False
    db_radio = await mass.music.radio.add_item_to_library(_radio())
    await _set_unavailable(mass, db_radio)
    with _with_provider(mass, provider):
        await mass.music._check_unavailable_mappings()
    assert provider.lookups == 1
    assert not await _available(mass, db_radio)


@pytest.mark.parametrize(
    "error", [MediaNotFoundError("gone"), ProviderUnavailableError("down")], ids=["gone", "down"]
)
async def test_the_daily_check_keeps_what_the_provider_still_lacks(
    mass: MusicAssistant, error: Exception
) -> None:
    """The daily check leaves greyed out items it can not find greyed out, never deleting them."""
    provider = _StubProvider()
    provider.error = error
    db_podcast = await mass.music.podcasts.add_item_to_library(_podcast())
    db_radio = await mass.music.radio.add_item_to_library(_radio())
    await _set_unavailable(mass, db_podcast)
    await _set_unavailable(mass, db_radio)
    with _with_provider(mass, provider):
        await mass.music._check_unavailable_mappings()
    assert provider.lookups == 2
    assert not await _available(mass, db_podcast)
    assert not await _available(mass, db_radio)
    assert await mass.music.podcasts.library_count() == 1
    assert await mass.music.radio.library_count() == 1


async def test_the_daily_check_skips_providers_not_loaded(mass: MusicAssistant) -> None:
    """The daily check asks no provider that is not loaded, and keeps the item greyed out."""
    db_radio = await mass.music.radio.add_item_to_library(_radio())
    await _set_unavailable(mass, db_radio)
    with _with_provider(mass, None):
        await mass.music._check_unavailable_mappings()
    assert not await _available(mass, db_radio)


async def test_the_daily_check_asks_the_provider_fresh(mass: MusicAssistant) -> None:
    """A cached answer from before the item went missing does not bring it back."""
    provider = _StubProvider()
    db_radio = await mass.music.radio.add_item_to_library(_radio())
    await _set_unavailable(mass, db_radio)
    with (
        _with_provider(mass, provider),
        patch.object(mass.cache, "handle_refresh", wraps=mass.cache.handle_refresh) as refresh,
    ):
        await mass.music._check_unavailable_mappings()
    refresh.assert_called_once_with(True)
