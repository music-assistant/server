"""Tests that one-off items never reach the playlog, whichever provider owns them."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import AudioSource, ProviderMapping, SoundEffect, Track

from music_assistant.constants import DB_TABLE_PLAYLOG
from music_assistant.mass import MusicAssistant


def _provider_mapping(provider: str, item_id: str) -> set[ProviderMapping]:
    """Create a single provider mapping for the given provider and item id."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain=provider,
            provider_instance=provider,
        )
    }


async def _playlog_rows(
    mass: MusicAssistant, media_type: MediaType, item_id: str
) -> list[Mapping[str, Any]]:
    """Return every playlog row for the given media type and item id."""
    return await mass.music.database.get_rows(
        DB_TABLE_PLAYLOG,
        {"media_type": media_type.value, "item_id": item_id},
    )


async def test_sound_effect_from_other_provider_gets_no_playlog_row(
    mass: MusicAssistant,
) -> None:
    """A sound effect is a one-off clip and stays out of the playlog whoever owns it."""
    user = await mass.webserver.auth.create_user("effectother")
    effect = SoundEffect(
        item_id="doorbell",
        provider="test_effects--instance",
        name="Doorbell",
        provider_mappings=_provider_mapping("test_effects--instance", "doorbell"),
    )

    await mass.music.mark_item_played(effect, fully_played=True, userid=user.user_id)

    assert not await _playlog_rows(mass, MediaType.SOUND_EFFECT, effect.item_id)


async def test_sound_effect_from_builtin_provider_gets_no_playlog_row(
    mass: MusicAssistant,
) -> None:
    """The pre-existing builtin behaviour is unchanged."""
    user = await mass.webserver.auth.create_user("effectbuiltin")
    effect = SoundEffect(
        item_id="http://example.com/intro.mp3",
        provider="builtin",
        name="Intro",
        provider_mappings=_provider_mapping("builtin", "http://example.com/intro.mp3"),
    )

    await mass.music.mark_item_played(effect, fully_played=True, userid=user.user_id)

    assert not await _playlog_rows(mass, MediaType.SOUND_EFFECT, effect.item_id)


async def test_audio_source_gets_no_playlog_row(mass: MusicAssistant) -> None:
    """A live input is not a catalogue item, so it never lands in the playlog either."""
    user = await mass.webserver.auth.create_user("audiosourceplugin")
    source = AudioSource(
        item_id="main",
        provider="test_receiver--instance",
        name="Test Receiver",
        provider_mappings=_provider_mapping("test_receiver--instance", "main"),
    )

    await mass.music.mark_item_played(source, fully_played=True, userid=user.user_id)

    assert not await _playlog_rows(mass, MediaType.AUDIO_SOURCE, source.item_id)


async def test_regular_provider_track_still_gets_playlog_row(mass: MusicAssistant) -> None:
    """Control case: the media-type guard must not suppress ordinary provider items."""
    user = await mass.webserver.auth.create_user("regulartrack")
    item_id = uuid4().hex
    track = Track(
        item_id=item_id,
        provider="test_music_prov",
        name="Some Track",
        provider_mappings=_provider_mapping("test_music_prov", item_id),
    )

    await mass.music.mark_item_played(track, fully_played=True, userid=user.user_id)

    assert await _playlog_rows(mass, MediaType.TRACK, item_id)
