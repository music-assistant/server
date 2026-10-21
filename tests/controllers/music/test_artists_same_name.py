"""Tests for keeping same-name artists of one streaming provider apart through MusicBrainz."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist, ItemMapping, ProviderMapping

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

    from music_assistant.mass import MusicAssistant

COMPOSER_MBID = "53b106e7-0cc6-42cc-ac95-ed8d30a3a98e"
GUITARIST_MBID = "8b8a38a9-a290-4560-84f6-3d4466e8d791"
NAME = "John Williams"
COMPOSER_ID = "805"
GUITARIST_ID = "5604436"
SEARCH_URL = "https://musicbrainz.org/search?type=artist&query=John+Williams"


def _url(item_id: str) -> str:
    """Return the Deezer artist URL MusicBrainz stores."""
    return f"https://www.deezer.com/artist/{item_id}"


def _artist(
    item_id: str,
    instance_id: str = "deezer_1",
    domain: str = "deezer",
    mbid: str | None = None,
) -> Artist:
    """Build a provider artist named John Williams."""
    artist = Artist(
        item_id=item_id,
        provider=instance_id,
        name=NAME,
        provider_mappings={
            ProviderMapping(item_id=item_id, provider_domain=domain, provider_instance=instance_id)
        },
    )
    if mbid:
        artist.mbid = mbid
    return artist


@dataclass
class _Harness:
    mass: MusicAssistant
    musicbrainz: Mock
    get_mbid_by_url: AsyncMock

    async def match(self, item: Artist | ItemMapping) -> int | None:
        """Return the library id the item matches, if any."""
        return await self.mass.music.artists._get_library_item_by_match(item)

    def looked_up(self) -> list[str]:
        """Return the URLs looked up on MusicBrainz, in order."""
        return [call.args[0] for call in self.get_mbid_by_url.await_args_list]


@contextmanager
def _harness(
    mass: MusicAssistant,
    mbids: dict[str, str] | None = None,
    musicbrainz_loaded: bool = True,
) -> Iterator[_Harness]:
    """
    Make deezer_1 a streaming provider, filesystem_1 a local one and answer URL lookups.

    :param mbids: MusicBrainz artist id per looked-up URL; other URLs are unknown.
    :param musicbrainz_loaded: Whether the MusicBrainz provider is available.
    """
    providers = {
        "deezer_1": Mock(domain="deezer", is_streaming_provider=True),
        "filesystem_1": Mock(domain="filesystem_local", is_streaming_provider=False),
    }
    known = mbids or {}
    get_mbid_by_url = AsyncMock(side_effect=lambda url, _media_type: known.get(url))
    musicbrainz = Mock(rate_limited=False, get_mbid_by_url=get_mbid_by_url)
    deezer_manifest = Mock()
    deezer_manifest.name = "Deezer"
    with (
        patch.object(
            mass,
            "get_provider",
            side_effect=lambda instance_id, **_kwargs: providers.get(instance_id),
        ),
        patch.object(mass, "get_provider_manifest", return_value=deezer_manifest),
        patch.object(
            mass.music.artists,
            "_musicbrainz_link_provider",
            return_value=musicbrainz if musicbrainz_loaded else None,
        ),
    ):
        yield _Harness(mass, musicbrainz, get_mbid_by_url)


async def _add_composer(mass: MusicAssistant, mbid: str | None = None) -> int:
    """Add the composer to the library before any provider is known and return its id."""
    library_item = await mass.music.artists.add_item_to_library(_artist(COMPOSER_ID, mbid=mbid))
    return int(library_item.item_id)


async def test_artists_musicbrainz_tells_apart_are_kept_apart(music_mass: MusicAssistant) -> None:
    """The guitarist becomes a library artist of its own, carrying the id MusicBrainz gave."""
    composer_id = await _add_composer(music_mass)
    mbids = {_url(COMPOSER_ID): COMPOSER_MBID, _url(GUITARIST_ID): GUITARIST_MBID}
    with _harness(music_mass, mbids) as harness:
        guitarist = _artist(GUITARIST_ID)
        assert await harness.match(guitarist) is None
    assert guitarist.mbid == GUITARIST_MBID
    assert harness.looked_up() == [_url(GUITARIST_ID), _url(COMPOSER_ID)]
    composer = await music_mass.music.artists.get_library_item(composer_id)
    assert composer.mbid is None


async def test_track_credit_musicbrainz_tells_apart_is_kept_apart(
    music_mass: MusicAssistant,
) -> None:
    """A simplified artist credit is checked like a full artist."""
    await _add_composer(music_mass)
    mbids = {_url(COMPOSER_ID): COMPOSER_MBID, _url(GUITARIST_ID): GUITARIST_MBID}
    with _harness(music_mass, mbids) as harness:
        credit = ItemMapping(
            media_type=MediaType.ARTIST, item_id=GUITARIST_ID, provider="deezer_1", name=NAME
        )
        assert await harness.match(credit) is None
    assert credit.mbid == GUITARIST_MBID


async def test_only_the_library_id_known_merges_without_looking_it_up(
    music_mass: MusicAssistant,
) -> None:
    """An incoming id MusicBrainz does not know merges on name, with no lookup for the library."""
    composer_id = await _add_composer(music_mass)
    with _harness(music_mass, {_url(COMPOSER_ID): COMPOSER_MBID}) as harness:
        assert await harness.match(_artist(GUITARIST_ID)) == composer_id
    assert harness.looked_up() == [_url(GUITARIST_ID)]


async def test_only_the_incoming_id_known_merges(music_mass: MusicAssistant) -> None:
    """
    When MusicBrainz knows only the incoming id, nothing tells the artists apart.

    The id found for the incoming artist is not kept, so it never lands on the library artist.
    """
    composer_id = await _add_composer(music_mass)
    with _harness(music_mass, {_url(GUITARIST_ID): GUITARIST_MBID}) as harness:
        guitarist = _artist(GUITARIST_ID)
        assert await harness.match(guitarist) == composer_id
    assert harness.looked_up() == [_url(GUITARIST_ID), _url(COMPOSER_ID)]
    assert guitarist.mbid is None


async def test_two_ids_of_one_artist_merge(music_mass: MusicAssistant) -> None:
    """A provider listing one artist under two ids keeps them as one library artist."""
    composer_id = await _add_composer(music_mass)
    mbids = {_url(COMPOSER_ID): COMPOSER_MBID, _url(GUITARIST_ID): COMPOSER_MBID}
    with _harness(music_mass, mbids) as harness:
        assert await harness.match(_artist(GUITARIST_ID)) == composer_id


async def test_known_mbids_are_not_looked_up(music_mass: MusicAssistant) -> None:
    """Only a side without a MusicBrainz id is looked up."""
    await _add_composer(music_mass, mbid=COMPOSER_MBID)
    with _harness(music_mass, {_url(GUITARIST_ID): GUITARIST_MBID}) as harness:
        assert await harness.match(_artist(GUITARIST_ID)) is None
    assert harness.looked_up() == [_url(GUITARIST_ID)]


async def test_incoming_mbid_only_looks_up_the_library_artist(
    music_mass: MusicAssistant,
) -> None:
    """An artist arriving with its MusicBrainz id costs one lookup, for the library side."""
    await _add_composer(music_mass)
    with _harness(music_mass, {_url(COMPOSER_ID): COMPOSER_MBID}) as harness:
        assert await harness.match(_artist(GUITARIST_ID, mbid=GUITARIST_MBID)) is None
    assert harness.looked_up() == [_url(COMPOSER_ID)]


async def test_local_artist_is_not_looked_up(music_mass: MusicAssistant) -> None:
    """A local file artist matches on name as before, without asking MusicBrainz."""
    composer_id = await _add_composer(music_mass)
    with _harness(music_mass) as harness:
        local = _artist("John Williams/artist.nfo", "filesystem_1", "filesystem_local")
        assert await harness.match(local) == composer_id
    assert harness.looked_up() == []


async def test_name_used_as_id_is_not_looked_up(music_mass: MusicAssistant) -> None:
    """A credit whose id is only the artist name says nothing about which artist it is."""
    composer_id = await _add_composer(music_mass)
    with _harness(music_mass) as harness:
        credit = ItemMapping(
            media_type=MediaType.ARTIST, item_id=NAME, provider="deezer_1", name=NAME
        )
        assert await harness.match(credit) == composer_id
    assert harness.looked_up() == []


async def test_without_musicbrainz_artists_merge(music_mass: MusicAssistant) -> None:
    """Without the MusicBrainz provider, matching works as before."""
    composer_id = await _add_composer(music_mass)
    with _harness(music_mass, musicbrainz_loaded=False) as harness:
        assert await harness.match(_artist(GUITARIST_ID)) == composer_id


async def test_lookup_errors_merge_as_before(music_mass: MusicAssistant) -> None:
    """A MusicBrainz failure never breaks matching."""
    composer_id = await _add_composer(music_mass)
    with _harness(music_mass) as harness:
        harness.get_mbid_by_url.side_effect = aiohttp.ClientError("mirror down")
        assert await harness.match(_artist(GUITARIST_ID)) == composer_id


async def test_rate_limited_musicbrainz_is_not_asked(music_mass: MusicAssistant) -> None:
    """A MusicBrainz cooldown is not waited out during matching."""
    composer_id = await _add_composer(music_mass)
    with _harness(music_mass) as harness:
        harness.musicbrainz.rate_limited = True
        assert await harness.match(_artist(GUITARIST_ID)) == composer_id
    assert harness.looked_up() == []


def _hints(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the missing MusicBrainz link hints logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "is not on MusicBrainz through" in record.getMessage()
    ]


def _hint(item_id: str, other_mbid: str | None = None) -> str:
    """Return the hint expected for a Deezer artist id MusicBrainz lacks."""
    hint = (
        f'Artist "{NAME}" is not on MusicBrainz through {_url(item_id)}, so it cannot be told '
        f'apart from the other "{NAME}" on Deezer; add {_url(item_id)} to their MusicBrainz '
        f"page: {SEARCH_URL}"
    )
    if other_mbid:
        hint += f' (the other "{NAME}" is https://musicbrainz.org/artist/{other_mbid})'
    return hint


async def test_unknown_link_is_hinted_once(
    music_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An artist page MusicBrainz lacks is hinted once, however often it is matched."""
    await _add_composer(music_mass)
    caplog.set_level(logging.INFO, logger=music_mass.music.artists.logger.name)
    with _harness(music_mass) as harness:
        await harness.match(_artist(GUITARIST_ID))
        await harness.match(_artist(GUITARIST_ID))
    assert _hints(caplog) == [_hint(GUITARIST_ID)]


async def test_unknown_link_hint_names_the_other_artist(
    music_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The hint names the artist the link must not be added to, when its id is known."""
    await _add_composer(music_mass, mbid=COMPOSER_MBID)
    caplog.set_level(logging.INFO, logger=music_mass.music.artists.logger.name)
    with _harness(music_mass) as harness:
        await harness.match(_artist(GUITARIST_ID))
    assert _hints(caplog) == [_hint(GUITARIST_ID, COMPOSER_MBID)]


async def test_unknown_library_link_is_hinted(
    music_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A library artist page MusicBrainz lacks is hinted, naming the incoming artist's id."""
    await _add_composer(music_mass)
    caplog.set_level(logging.INFO, logger=music_mass.music.artists.logger.name)
    with _harness(music_mass, {_url(GUITARIST_ID): GUITARIST_MBID}) as harness:
        await harness.match(_artist(GUITARIST_ID))
    assert _hints(caplog) == [_hint(COMPOSER_ID, GUITARIST_MBID)]


async def test_no_hint_when_musicbrainz_knows_both_links(
    music_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Links MusicBrainz knows need no hint."""
    await _add_composer(music_mass)
    caplog.set_level(logging.INFO, logger=music_mass.music.artists.logger.name)
    mbids = {_url(COMPOSER_ID): COMPOSER_MBID, _url(GUITARIST_ID): GUITARIST_MBID}
    with _harness(music_mass, mbids) as harness:
        await harness.match(_artist(GUITARIST_ID))
    assert _hints(caplog) == []


async def test_no_hint_when_the_lookup_is_skipped(
    music_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing is hinted for a lookup that never ran."""
    await _add_composer(music_mass)
    caplog.set_level(logging.INFO, logger=music_mass.music.artists.logger.name)
    with _harness(music_mass) as harness:
        await harness.match(_artist(GUITARIST_ID, mbid=GUITARIST_MBID))
        await harness.match(
            ItemMapping(media_type=MediaType.ARTIST, item_id=NAME, provider="deezer_1", name=NAME)
        )
    with _harness(music_mass, musicbrainz_loaded=False) as harness:
        await harness.match(_artist(GUITARIST_ID))
    with _harness(music_mass) as harness:
        harness.musicbrainz.rate_limited = True
        await harness.match(_artist(GUITARIST_ID))
    # the incoming id was known, so only the library side was looked up, and hinted
    assert _hints(caplog) == [_hint(COMPOSER_ID, GUITARIST_MBID)]


async def test_no_hint_when_the_lookup_fails(
    music_mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed lookup is not the same as MusicBrainz lacking the link."""
    await _add_composer(music_mass)
    caplog.set_level(logging.INFO, logger=music_mass.music.artists.logger.name)
    with _harness(music_mass) as harness:
        harness.get_mbid_by_url.side_effect = TimeoutError
        await harness.match(_artist(GUITARIST_ID))
    assert _hints(caplog) == []
