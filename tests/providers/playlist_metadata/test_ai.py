"""Tests for the AI generated covers and descriptions of the playlist_metadata provider."""

# the seed labels of smart playlists separate title and artists with an en dash
# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.errors import (
    ActionUnavailable,
    MediaNotFoundError,
    ProviderUnavailableError,
    RateLimited,
)
from music_assistant_models.media_items import (
    Artist,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.media_items.metadata import MediaItemMetadata
from music_assistant_models.unique_list import UniqueList
from PIL import Image, ImageDraw

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.playlist_metadata import PlaylistMetadataProvider
from music_assistant.providers.playlist_metadata.ai import _fit_title, _format_template
from music_assistant.providers.playlist_metadata.ai_constants import (
    CONF_ACTION_PREVIEW,
    CONF_ACTION_REGENERATE,
    CONF_CUSTOM_PROMPT,
    CONF_DESCRIPTIONS,
    CONF_EXTRA_PLAYLISTS,
    CONF_IMAGE_ENGINE,
    CONF_MAX_PER_HOUR,
    CONF_PLAYLISTS,
    CONF_PREVIEW_CUSTOM_PROMPT,
    CONF_PREVIEW_DESCRIPTION,
    CONF_PREVIEW_IMAGE,
    CONF_PREVIEW_PLAYLIST,
    CONF_PREVIEW_STYLE,
    CONF_STYLE,
    CONF_TEXT_ENGINE,
    CONF_TITLE_OVERLAY,
    CONF_USE_PROMPT_ENGINE,
    MAX_OUTAGE_RETRIES,
    PLAYLISTS_ALL,
    PLAYLISTS_SMART,
    TEMPLATE_AI,
    Style,
)

INSTANCE_ID = "playlist_metadata"
ENGINE_UID = "hass--test/ai_task.images"
TEXT_ENGINE_UID = "openai_compatible--test/gemini-flash"
DESCRIPTION = "Melodic house for the hours after midnight. RÜFÜS DU SOL, Lane 8 and more."

DEFAULT_CONFIG: dict[str, Any] = {
    CONF_LOG_LEVEL: "GLOBAL",
    "template": TEMPLATE_AI,
    "fallback_template": "album_grid",
    "skip_provider_playlists": True,
    "enable_genre_detection": False,
    CONF_IMAGE_ENGINE: ENGINE_UID,
    CONF_STYLE: "music_assistant",
    CONF_CUSTOM_PROMPT: "",
    CONF_TITLE_OVERLAY: False,
    CONF_PLAYLISTS: PLAYLISTS_SMART,
    CONF_EXTRA_PLAYLISTS: [],
    CONF_DESCRIPTIONS: True,
    CONF_TEXT_ENGINE: TEXT_ENGINE_UID,
    CONF_USE_PROMPT_ENGINE: False,
    CONF_MAX_PER_HOUR: 10,
}

SMART_RULES = {
    "seed_names": {"uri1": "Innerbloom – RÜFÜS DU SOL", "uri2": "Midnight Sun – Lane 8, Jerro"},
    "artist_names": {"1": "Ben Böhmer"},
    "genre_names": {"5": "Melodic House"},
    "year_from": 2015,
    "year_to": 2019,
}


@pytest.fixture
async def provider(tmp_path: Path) -> AsyncGenerator[PlaylistMetadataProvider]:
    """Return an initialized provider with mocked image and text engines selected."""
    provider = _make_provider(tmp_path)
    await provider.loaded_in_mass()
    with (
        patch(
            "music_assistant.providers.playlist_metadata.ai.select_image_engine",
            AsyncMock(return_value=provider.test_engine),  # type: ignore[attr-defined]
        ),
        patch(
            "music_assistant.providers.playlist_metadata.ai.select_ai_engine",
            AsyncMock(return_value=provider.test_text_engine),  # type: ignore[attr-defined]
        ),
    ):
        yield provider
    for task in _tasks(provider).values():
        task.cancel()


async def test_generates_and_stores_a_cover_in_place_of_the_collage(
    provider: PlaylistMetadataProvider,
) -> None:
    """A smart playlist gets a cover, which takes the place of its collage."""
    collage = _image("1_1791525935_thumb.jpg", provider=INSTANCE_ID)
    Path(provider._images_dir, collage.path).write_bytes(b"collage")
    fanart = MediaItemImage(type=ImageType.FANART, path="fanart.jpg", provider="spotify")
    playlist = _smart_playlist(images=[collage, fanart])

    await _metadata(provider, playlist)

    images = list(playlist.metadata.images or [])
    assert images[0].provider == INSTANCE_ID
    assert images[0].type == ImageType.THUMB
    assert images[0].path.startswith("ai_")
    assert images[1:] == [fanart]
    assert playlist.image == images[0]
    assert not Path(provider._images_dir, collage.path).exists()
    stored = Path(provider._images_dir, images[0].path)
    with Image.open(stored) as cover:
        assert cover.size == (1024, 1024)
        assert cover.format == "JPEG"


async def test_bold_prompt_matches_the_cronicle_script(
    provider: PlaylistMetadataProvider,
) -> None:
    """The Bold preset with the engine drawing the title reproduces the original prompt."""
    playlist = _smart_playlist(name="2000s Emo & Alt Rock")
    rules = {
        "seed_names": {
            "a": "EMO Songs of the 2000s",
            "b": "Mid-2000s Alt Rock Swim",
            "c": "Are we in High School?",
        },
        "year_from": 2000,
        "year_to": 2012,
    }
    _mass(provider).get_provider("smart_playlist").get_smart_playlist_rules = AsyncMock(
        return_value=rules
    )
    _set_config(provider, **{CONF_STYLE: "bold"})

    await _metadata(provider, playlist)

    assert _sent_prompt(provider) == (
        'Create square album cover art for a music playlist called "2000s Emo & Alt Rock". '
        "Seed songs/playlists that define the vibe: EMO Songs of the 2000s; Mid-2000s Alt Rock "
        "Swim; Are we in High School?. Era: 2000-2012, evoke that decade's look. Capture the "
        "mood of the playlist name as a striking visual scene or concept (do not depict real "
        "artists). Style: a bold editorial playlist cover. One striking full-bleed image "
        "(stylized photo or bold illustration) with a vivid duotone or saturated color wash "
        "that sets the mood, simple uncluttered composition, cohesive with a series of playlist "
        'covers. Typography: the playlist title "2000s Emo & Alt Rock" as the only text, set '
        "large in a bold modern geometric sans-serif (like Circular or Gotham Black), "
        "tight letter spacing, clean and crisp, high contrast against the image, placed "
        "confidently in the upper-left or lower-left area. Spell the title exactly. No other "
        "text, subtitles, logos, watermarks or artist names. Full-bleed: the art fills the "
        "entire square edge to edge, no border, frame, mockup or margins."
    )


async def test_smart_playlist_rules_shape_the_prompt(provider: PlaylistMetadataProvider) -> None:
    """Seeds, era, artists and genres of the rules all reach the prompt."""
    await _metadata(provider, _smart_playlist())

    prompt = _sent_prompt(provider)
    assert "Innerbloom – RÜFÜS DU SOL; Midnight Sun – Lane 8, Jerro" in prompt
    assert "Era: 2015-2019" in prompt
    assert "Artists/genres: Ben Böhmer, RÜFÜS DU SOL, Lane 8, Jerro, Melodic House." in prompt


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("bold", "bold editorial playlist cover"),
        ("music_assistant", "line-art spot illustration"),
        ("neon", "glowing neon light lines"),
        ("soft_gradient", "mesh gradient"),
        ("risograph", "two-color risograph print"),
        ("gouache", "painterly editorial illustration in gouache"),
    ],
)
async def test_each_style_has_its_own_prompt(
    provider: PlaylistMetadataProvider, style: str, expected: str
) -> None:
    """Every preset sends its own art direction."""
    _set_config(provider, **{CONF_STYLE: style})

    await _metadata(provider, _smart_playlist())

    assert expected in _sent_prompt(provider)


async def test_title_overlay_asks_for_artwork_without_text(
    provider: PlaylistMetadataProvider,
) -> None:
    """When the title is drawn afterwards, the engine is told to leave text out."""
    _set_config(provider, **{CONF_TITLE_OVERLAY: True})

    await _metadata(provider, _smart_playlist())

    prompt = _sent_prompt(provider)
    assert "No text, letters, numbers" in prompt
    assert "Typography" not in prompt


async def test_custom_template_fills_known_placeholders(provider: PlaylistMetadataProvider) -> None:
    """A custom prompt gets the playlist values, unknown placeholders stay as typed."""
    _set_config(
        provider,
        **{CONF_STYLE: "custom", CONF_CUSTOM_PROMPT: "Poster for {name} ({era}) {genres} {nope}"},
    )

    await _metadata(provider, _smart_playlist(name="Sunrise Trance"))

    assert _sent_prompt(provider) == "Poster for Sunrise Trance (2015-2019) Melodic House {nope}"


def test_format_template_takes_unbalanced_braces_literally() -> None:
    """A template a formatter can not parse is sent as written rather than failing."""
    assert _format_template("cover {name", {"name": "x"}) == "cover {name"


async def test_a_stored_cover_is_reused_without_calling_the_engine(
    provider: PlaylistMetadataProvider,
) -> None:
    """The second refresh of an unchanged playlist does not pay for a new image."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    first = playlist.image

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 1
    assert playlist.image == first
    assert [img.provider for img in playlist.metadata.images or []] == [INSTANCE_ID]


async def test_changing_the_style_replaces_the_cover(provider: PlaylistMetadataProvider) -> None:
    """A new style generates a new cover and removes the old one, file included."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    old = playlist.image
    assert old is not None

    _set_config(provider, **{CONF_STYLE: "soft_gradient"})
    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 2
    assert playlist.image is not None
    assert playlist.image.path != old.path
    assert old not in (playlist.metadata.images or [])
    assert not Path(provider._images_dir, old.path).exists()


async def test_renaming_the_playlist_replaces_the_cover(provider: PlaylistMetadataProvider) -> None:
    """The title is part of the artwork, so a new name needs a new cover."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    playlist.name = "Renamed"

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 2


async def test_regular_playlists_need_to_be_opted_in(provider: PlaylistMetadataProvider) -> None:
    """Only smart playlists get a cover unless a playlist is picked or all are included."""
    playlist = _playlist("builtin", item_id="42", is_editable=True)
    await _metadata(provider, playlist)
    assert _engine_calls(provider) == 0

    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    await _metadata(provider, playlist)
    assert _engine_calls(provider) == 1

    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: [], CONF_PLAYLISTS: PLAYLISTS_ALL})
    other = _playlist("builtin", item_id="43", is_editable=True)
    await _metadata(provider, other)
    assert _engine_calls(provider) == 2


async def test_a_playlist_leaving_the_scope_loses_its_cover(
    provider: PlaylistMetadataProvider,
) -> None:
    """Opting a playlist out takes its generated cover off again."""
    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    playlist = _playlist("builtin", item_id="42", is_editable=True)
    await _metadata(provider, playlist)
    assert playlist.image is not None

    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: []})
    await _metadata(provider, playlist)

    assert playlist.image is None


async def test_builtin_playlists_that_can_not_be_edited_are_skipped(
    provider: PlaylistMetadataProvider,
) -> None:
    """Music Assistant's own generated playlists keep their static artwork."""
    _set_config(provider, **{CONF_PLAYLISTS: PLAYLISTS_ALL})

    await _metadata(provider, _playlist("builtin", is_editable=False))

    assert _engine_calls(provider) == 0


async def test_existing_artwork_is_kept(provider: PlaylistMetadataProvider) -> None:
    """A cover of the playlist's own (service artwork, an added image) is left alone."""
    own_cover = _image("/data/playlist_art/234_1791525935.jpg", provider="builtin")
    playlist = _smart_playlist(images=[own_cover])

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 0
    assert list(playlist.metadata.images or []) == [own_cover]


async def test_artwork_that_can_not_be_shown_is_not_kept(
    provider: PlaylistMetadataProvider,
) -> None:
    """A local file the built-in provider refuses to serve counts as no artwork at all."""
    _mass(provider).get_provider("builtin").resolve_image = AsyncMock(
        side_effect=FileNotFoundError("Invalid image reference")
    )
    broken_cover = _image("/data/playlist_art/234_1791525935.jpg", provider="builtin")
    playlist = _smart_playlist(images=[broken_cover])

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 1
    images = list(playlist.metadata.images or [])
    assert images[0].provider == INSTANCE_ID
    assert images[1] == broken_cover


async def test_the_cover_is_returned_in_front_of_the_other_images(
    provider: PlaylistMetadataProvider,
) -> None:
    """A refresh returns the cover, and merging what it returns keeps the cover first."""
    _mass(provider).get_provider("builtin").resolve_image = AsyncMock(
        side_effect=FileNotFoundError("Invalid image reference")
    )
    broken_cover = _image("/data/playlist_art/234_1791525935.jpg", provider="builtin")
    playlist = _smart_playlist(images=[broken_cover])
    await _metadata(provider, playlist)
    cover = playlist.image

    metadata = await provider.get_playlist_metadata(playlist)

    assert metadata is not None
    assert list(metadata.images or [])[:2] == [cover, broken_cover]
    playlist.metadata.update(metadata)
    assert list(playlist.metadata.images or []) == [cover, broken_cover]


async def test_service_artwork_is_kept(provider: PlaylistMetadataProvider) -> None:
    """A provider playlist with a cover of its own keeps it, also with all playlists in scope."""
    _set_config(provider, **{CONF_PLAYLISTS: PLAYLISTS_ALL})
    service_cover = _image("https://example.com/cover.jpg", provider="spotify")
    playlist = _playlist("spotify")
    playlist.metadata.images = UniqueList([service_cover])

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 0
    assert list(playlist.metadata.images or []) == [service_cover]


async def test_the_collage_is_used_without_the_ai_template(
    provider: PlaylistMetadataProvider,
) -> None:
    """With a collage template picked, nothing is generated with AI."""
    _set_config(provider, template="album_fan")

    with patch.object(provider, "_generate_and_write", AsyncMock(return_value=None)) as collage:
        await _metadata(provider, _smart_playlist())

    assert _engine_calls(provider) == 0
    assert collage.await_args_list[0].kwargs["template"] == "album_fan"


async def test_the_fallback_template_makes_the_collage_meanwhile(
    provider: PlaylistMetadataProvider,
) -> None:
    """A playlist out of scope gets the collage, made with the fallback template."""
    _set_config(provider, fallback_template="artist_radio")
    thumb = MediaItemImage(
        type=ImageType.THUMB,
        path=os.path.join(provider._images_dir, "42_1_thumb.jpg"),
        provider=INSTANCE_ID,
    )

    with patch.object(provider, "_generate_and_write", AsyncMock(return_value=thumb)) as collage:
        metadata = await _metadata(provider, _playlist("builtin", item_id="42", is_editable=True))

    assert _engine_calls(provider) == 0
    assert {call.kwargs["template"] for call in collage.await_args_list} == {"artist_radio"}
    assert metadata is not None
    assert thumb in (metadata.images or [])


async def test_a_failed_generation_keeps_the_previous_cover(
    provider: PlaylistMetadataProvider,
) -> None:
    """A playlist keeps its generated cover while a new one can not be made."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    first = playlist.image
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("quota exceeded")
    )
    playlist.name = "Renamed"

    await _metadata(provider, playlist)

    assert playlist.image == first


async def test_the_hourly_limit_postpones_generation(provider: PlaylistMetadataProvider) -> None:
    """Covers over the limit are not generated now, the worker waits for a free slot."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1})
    await _metadata(provider, _smart_playlist(item_id="1"))
    second = _smart_playlist(item_id="2")

    await _metadata(provider, second)

    assert _engine_calls(provider) == 1
    assert second.image is None
    assert provider._ai.waiting


async def test_the_hourly_limit_survives_a_reload(provider: PlaylistMetadataProvider) -> None:
    """Covers generated before a reload (as a settings change causes) still count."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1})
    await _metadata(provider, _smart_playlist(item_id="1"))

    await provider._ai.setup()
    second = _smart_playlist(item_id="2")
    await _metadata(provider, second)

    assert _engine_calls(provider) == 1
    assert second.image is None


async def test_the_hourly_limit_forgets_generations_older_than_an_hour(
    provider: PlaylistMetadataProvider,
) -> None:
    """Stored generations from over an hour ago no longer count after a reload."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1})
    with patch(
        "music_assistant.providers.playlist_metadata.ai.time.time", return_value=1_000_000.0
    ):
        await _metadata(provider, _smart_playlist(item_id="1"))

    await provider._ai.setup()
    await _metadata(provider, _smart_playlist(item_id="2"))

    assert _engine_calls(provider) == 2


async def test_a_failing_engine_leaves_the_collage_in_place(
    provider: PlaylistMetadataProvider,
) -> None:
    """An engine error is logged, not raised into the metadata refresh."""
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("quota exceeded")
    )
    collage = _image("1_1791525935_thumb.jpg", provider=INSTANCE_ID)
    playlist = _smart_playlist(images=[collage])

    await _metadata(provider, playlist)

    assert list(playlist.metadata.images or []) == [collage]
    assert list(Path(provider._images_dir).iterdir()) == []


async def test_without_an_image_engine_nothing_is_generated(tmp_path: Path) -> None:
    """With no engine available the provider does nothing."""
    provider = _make_provider(tmp_path)
    await provider.loaded_in_mass()
    with (
        patch(
            "music_assistant.providers.playlist_metadata.ai.select_image_engine",
            AsyncMock(return_value=None),
        ),
        patch(
            "music_assistant.providers.playlist_metadata.ai.select_ai_engine",
            AsyncMock(return_value=None),
        ),
    ):
        await _metadata(provider, _smart_playlist())

    assert _engine_calls(provider) == 0


async def test_the_prompt_engine_writes_the_visual_concept(
    provider: PlaylistMetadataProvider,
) -> None:
    """With a prompt engine, its scene description replaces the generic concept sentence."""
    _set_config(provider, **{CONF_USE_PROMPT_ENGINE: True, CONF_DESCRIPTIONS: False})
    provider.test_text_engine.provider.ai_query = AsyncMock(  # type: ignore[attr-defined]
        return_value=' "A neon pier at dusk." '
    )

    await _metadata(provider, _smart_playlist())

    prompt = _sent_prompt(provider)
    assert "Visual concept: A neon pier at dusk (do not depict real artists)." in prompt
    assert "Capture the mood" not in prompt


async def test_regular_playlist_context_comes_from_its_tracks(
    provider: PlaylistMetadataProvider,
) -> None:
    """A playlist without rules is described by its most common artists and genres."""
    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    tracks = [
        _track("Poker Face", "Lady Gaga", {"Pop"}),
        _track("Just Dance", "Lady Gaga", {"Pop"}),
        _track("Vogue", "Madonna", {"Dance"}),
    ]
    _mass(provider).music.playlists.iter_tracks = MagicMock(
        side_effect=lambda *_: _async_iter(tracks)
    )

    await _metadata(provider, _playlist("builtin", item_id="42", is_editable=True))

    prompt = _sent_prompt(provider)
    assert (
        "Seed songs/playlists that define the vibe: Poker Face – Lady Gaga; Vogue – Madonna."
        in (prompt)
    )
    assert "Artists/genres: Lady Gaga, Madonna, Pop, Dance." in prompt


async def test_equally_common_genres_are_picked_in_a_stable_order(
    provider: PlaylistMetadataProvider,
) -> None:
    """Genres come as a set, so ties are broken alphabetically rather than by hash order."""
    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    tracks = [_track("Vogue", "Madonna", {"Pop", "Dance", "House"})]
    _mass(provider).music.playlists.iter_tracks = MagicMock(
        side_effect=lambda *_: _async_iter(tracks)
    )

    await _metadata(provider, _playlist("builtin", item_id="42", is_editable=True))

    assert "Artists/genres: Madonna, Dance, House, Pop." in _sent_prompt(provider)


async def test_a_metadata_refresh_never_waits_for_a_generation(
    provider: PlaylistMetadataProvider,
) -> None:
    """The refresh returns at once and the worker applies the cover once it is ready."""
    release = asyncio.Event()

    async def generate(*_args: Any, **_kwargs: Any) -> bytes:
        await release.wait()
        return _png()

    provider.test_engine.provider.ai_generate_image = AsyncMock(side_effect=generate)  # type: ignore[attr-defined]
    playlist = _smart_playlist()
    _register(provider, playlist)

    async with asyncio.timeout(1):
        await provider.get_playlist_metadata(playlist)

    assert playlist.image is None
    release.set()
    await _settle(provider)
    cover = _cover(playlist)
    assert cover is not None
    assert cover.provider == INSTANCE_ID


async def test_a_blocked_cover_does_not_hold_up_other_work(
    provider: PlaylistMetadataProvider,
) -> None:
    """While covers wait for the hourly limit, descriptions are still written."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1, CONF_DESCRIPTIONS: False})
    await _metadata(provider, _smart_playlist(item_id="1"))
    _set_config(provider, **{CONF_DESCRIPTIONS: True})
    second = _smart_playlist(item_id="2")

    await _metadata(provider, second)

    assert provider._ai.waiting
    assert second.image is None
    assert second.metadata.description == DESCRIPTION


async def test_a_failing_cover_backs_off_and_then_gives_up(
    provider: PlaylistMetadataProvider,
) -> None:
    """Each failure waits longer before the next try; after the last one it stops trying."""
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("content policy")
    )
    playlist = _smart_playlist()
    start = time.time()
    hour = 3600
    for offset in (0, 10, hour + 10, 7 * hour + 20, 31 * hour + 30, 100 * hour):
        with patch(
            "music_assistant.providers.playlist_metadata.ai.time.time", return_value=start + offset
        ):
            await _metadata(provider, playlist)

    # at 0, after 1h and after 6h more and after 24h more; never within a backoff or after
    assert _engine_calls(provider) == 4
    assert playlist.image is None

    # a change to what shapes the cover is tried afresh
    playlist.name = "Renamed"
    with patch(
        "music_assistant.providers.playlist_metadata.ai.time.time",
        return_value=start + 101 * hour,
    ):
        await _metadata(provider, playlist)
    assert _engine_calls(provider) == 5


async def test_an_oversized_image_is_refused_before_decoding(
    provider: PlaylistMetadataProvider,
) -> None:
    """An image with more pixels than the limit is not decoded, and counts as a failure."""
    buffer = BytesIO()
    Image.new("1", (5000, 5000)).save(buffer, "PNG")
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        return_value=buffer.getvalue()
    )
    playlist = _smart_playlist()

    await _metadata(provider, playlist)

    assert playlist.image is None
    assert list(Path(provider._images_dir).iterdir()) == []


async def test_a_description_from_elsewhere_is_kept(provider: PlaylistMetadataProvider) -> None:
    """A playlist's own description, such as a streaming service's, is never replaced."""
    _set_config(provider, **{CONF_PLAYLISTS: PLAYLISTS_ALL})
    playlist = _playlist("spotify")
    playlist.metadata.description = "The biggest songs right now."

    await _metadata(provider, playlist)

    assert playlist.metadata.description == "The biggest songs right now."
    assert _text_calls(provider) == 0


async def test_a_written_description_is_replaced_when_the_playlist_changes(
    provider: PlaylistMetadataProvider,
) -> None:
    """A description written here is the playlist's to update, also outside smart playlists."""
    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    playlist = _playlist("builtin", item_id="42", is_editable=True)
    await _metadata(provider, playlist)
    assert playlist.metadata.description == DESCRIPTION
    provider.test_text_engine.provider.ai_query = AsyncMock(return_value="A new one.")  # type: ignore[attr-defined]

    playlist.name = "Renamed"
    await _metadata(provider, playlist)

    assert playlist.metadata.description == "A new one."


async def test_changed_smart_playlist_rules_make_a_new_cover_and_description(
    provider: PlaylistMetadataProvider,
) -> None:
    """The rules shape both, so editing them keys both anew."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    first = playlist.image
    rules = _mass(provider).get_provider("smart_playlist").get_smart_playlist_rules
    rules.return_value = {**SMART_RULES, "year_from": 2001}
    playlist.metadata.description = "[Smart Playlist] Similar to Innerbloom, from 2001"

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 2
    assert _text_calls(provider) == 2
    assert playlist.image != first
    assert playlist.metadata.description == DESCRIPTION


async def test_refresh_passes_do_not_overlap(provider: PlaylistMetadataProvider) -> None:
    """A refresh asked for during one is covered by it, instead of running alongside it."""
    passes = 0
    running = 0
    overlapped = False

    async def iter_library_items() -> AsyncGenerator[Any]:
        nonlocal passes, running, overlapped
        passes += 1
        running += 1
        overlapped = overlapped or running > 1
        await asyncio.sleep(0.01)
        running -= 1
        # an empty library
        for playlist in cast("list[Playlist]", []):
            yield playlist

    _mass(provider).music.playlists.iter_library_items = iter_library_items

    await asyncio.gather(provider._ai.refresh(), provider._ai.refresh(), provider._ai.refresh())

    assert passes == 1
    assert not overlapped

    # a regenerate during a pass has it run again, as the pass may have used the old keys
    _mass(provider).create_task = MagicMock(side_effect=lambda coro, **_kw: coro.close())
    await asyncio.gather(
        provider._ai.refresh(), provider.handle_config_action(CONF_ACTION_REGENERATE)
    )

    assert passes == 3


async def test_unloading_stops_the_worker(provider: PlaylistMetadataProvider) -> None:
    """A worker waiting for the hourly limit is cancelled and its queue dropped on unload."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1})
    await _metadata(provider, _smart_playlist(item_id="1"))
    await _metadata(provider, _smart_playlist(item_id="2"))
    assert provider._ai.waiting

    await provider.unload()

    _mass(provider).cancel_task.assert_any_call(f"{INSTANCE_ID}_ai_worker")
    assert not provider._ai._pending


async def test_preview_leaves_the_description_out_when_descriptions_are_off(
    provider: PlaylistMetadataProvider,
) -> None:
    """With descriptions off, the preview spends no text request on one."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(
        provider,
        **{CONF_PREVIEW_PLAYLIST: "234", CONF_PREVIEW_STYLE: ["bold"], CONF_DESCRIPTIONS: False},
    )

    with _no_engine_pickers():
        entries = await provider.handle_config_action(CONF_ACTION_PREVIEW)

    assert _text_calls(provider) == 0
    assert CONF_PREVIEW_DESCRIPTION not in [entry.key for entry in entries or ()]


async def test_the_cover_key_names_the_text_engine_picked_for_the_concept(
    provider: PlaylistMetadataProvider,
) -> None:
    """The key holds the engine the concept is written with, also before one was picked."""
    _set_config(provider, **{CONF_USE_PROMPT_ENGINE: True, CONF_DESCRIPTIONS: False})
    # nothing picked yet: the stored selection is empty until an engine is adopted
    provider.config.get_value.side_effect = (  # type: ignore[attr-defined]
        lambda key, default=None: (
            None if key == CONF_TEXT_ENGINE else provider._test_config.get(key, default)  # type: ignore[attr-defined]
        )
    )
    playlist = _smart_playlist()

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 1
    assert playlist.image is not None
    assert playlist.image.path == await provider._ai._file_name(
        playlist,
        provider.test_engine,  # type: ignore[attr-defined]
    )


async def test_a_missing_concept_engine_keeps_the_current_cover(
    provider: PlaylistMetadataProvider,
) -> None:
    """While the picked text engine is away, the cover is kept instead of keyed anew."""
    _set_config(provider, **{CONF_USE_PROMPT_ENGINE: True, CONF_DESCRIPTIONS: False})
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    cover = playlist.image

    with patch(
        "music_assistant.providers.playlist_metadata.ai.select_ai_engine",
        AsyncMock(return_value=None),
    ):
        await _metadata(provider, playlist)

    assert playlist.image == cover
    assert _engine_calls(provider) == 1


async def test_preview_asks_for_the_visual_concept_once(
    provider: PlaylistMetadataProvider,
) -> None:
    """The concept is the same for every style, so one text request serves them all."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(
        provider,
        **{
            CONF_PREVIEW_PLAYLIST: "234",
            CONF_PREVIEW_STYLE: ["bold", "neon", "gouache"],
            CONF_USE_PROMPT_ENGINE: True,
            CONF_DESCRIPTIONS: False,
        },
    )

    with _no_engine_pickers():
        await provider.handle_config_action(CONF_ACTION_PREVIEW)

    assert _engine_calls(provider) == 3
    assert _text_calls(provider) == 1


async def test_refresh_queues_a_missing_description(provider: PlaylistMetadataProvider) -> None:
    """With only descriptions on, the hourly pass writes one for a playlist that has none."""
    _set_config(provider, template="album_grid")
    playlist = _smart_playlist()
    _register(provider, playlist)
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        side_effect=lambda: _async_iter([playlist])
    )

    await provider._ai.refresh()
    await _settle(provider)

    assert _text_calls(provider) == 1
    assert playlist.metadata.description == DESCRIPTION


@pytest.mark.parametrize(
    ("description", "replaced"),
    [
        ("[Smart Playlist] Similar to Innerbloom", True),
        ("Deep melodic house, written by the smart playlist provider's AI.", False),
        ("   ", True),
    ],
)
async def test_which_smart_playlist_descriptions_are_replaced(
    provider: PlaylistMetadataProvider, description: str, replaced: bool
) -> None:
    """The plain rules summary or a blank one is replaced; a written one is kept."""
    playlist = _smart_playlist()
    playlist.metadata.description = description

    await _metadata(provider, playlist)

    assert (playlist.metadata.description == DESCRIPTION) is replaced
    assert _text_calls(provider) == int(replaced)


async def test_unreadable_rules_keep_the_current_cover_and_description(
    provider: PlaylistMetadataProvider,
) -> None:
    """While a smart playlist's rules can not be read, nothing is made from a guess."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    cover = playlist.image
    rules = _mass(provider).get_provider("smart_playlist").get_smart_playlist_rules
    rules.return_value = None
    playlist.metadata.description = "[Smart Playlist] Similar to Innerbloom"

    await _metadata(provider, playlist)

    assert playlist.image == cover
    assert _engine_calls(provider) == 1
    assert _text_calls(provider) == 1
    assert Path(provider._images_dir, cover.path).exists()  # type: ignore[union-attr]


async def test_rule_changes_that_do_not_shape_the_prompt_keep_the_cover(
    provider: PlaylistMetadataProvider,
) -> None:
    """Excluding a track or changing the limit does not pay for a new cover."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    rules = _mass(provider).get_provider("smart_playlist").get_smart_playlist_rules
    rules.return_value = {**SMART_RULES, "limit": 50, "excluded_track_uris": ["x://1"]}

    await _metadata(provider, playlist)

    assert _engine_calls(provider) == 1


async def test_an_unreachable_engine_is_retried_far_longer(
    provider: PlaylistMetadataProvider,
) -> None:
    """An outage leaves the engine alone for a while, and gives up only after many tries."""
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        side_effect=ProviderUnavailableError("Home Assistant is not reachable")
    )
    _set_config(provider, **{CONF_DESCRIPTIONS: False})
    first, second = _smart_playlist(item_id="1"), _smart_playlist(item_id="2")

    await _metadata(provider, first)
    await _metadata(provider, second)

    # the second playlist waits for the engine instead of trying it as well
    assert _engine_calls(provider) == 1
    assert provider._ai.waiting

    # tried well past where a failed generation is given up on, until the outages run out
    for _task in _tasks(provider).values():
        _task.cancel()
    provider._ai._pending.clear()
    start = time.time()
    for day in range(1, 12):
        provider._ai._paused_until.clear()
        with patch(
            "music_assistant.providers.playlist_metadata.ai.time.time",
            return_value=start + day * 25 * 3600,
        ):
            await _metadata(provider, first)
    assert _engine_calls(provider) == MAX_OUTAGE_RETRIES + 1


async def test_outages_and_failed_generations_are_counted_apart(
    provider: PlaylistMetadataProvider,
) -> None:
    """Outages before a failed generation do not make it give up sooner."""
    playlist = _smart_playlist()
    ai = provider._ai
    for _ in range(3):
        await ai._record_failure("cover", playlist, "key", transient=True)
    await ai._record_failure("cover", playlist, "key")

    failure = await _mass(provider).cache.get(
        "failed_cover_234", provider=INSTANCE_ID, category=2, checksum="key"
    )
    assert failure["failures"] == 1
    assert failure["outages"] == 3
    assert failure["retry_at"] is not None


async def test_blocked_work_does_not_wake_the_worker(provider: PlaylistMetadataProvider) -> None:
    """Queueing a cover while covers wait for the limit leaves the worker waiting."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1, CONF_DESCRIPTIONS: False})
    await _metadata(provider, _smart_playlist(item_id="1"))
    await _metadata(provider, _smart_playlist(item_id="2"))
    assert provider._ai.waiting

    _register(provider, third := _smart_playlist(item_id="3"))
    await provider.get_playlist_metadata(third)

    assert not provider._ai._wakeup.is_set()
    assert set(provider._ai._pending) == {"2", "3"}


async def test_a_rate_limit_pauses_as_long_as_the_service_asks(
    provider: PlaylistMetadataProvider,
) -> None:
    """A Retry-After longer than the default pause is honoured."""
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RateLimited("slow down", backoff_time=3600)
    )
    _set_config(provider, **{CONF_DESCRIPTIONS: False})

    await _metadata(provider, _smart_playlist())

    assert provider._ai._paused_until["cover"] - time.time() > 3500


async def test_the_newest_generation_times_are_stored_last(
    provider: PlaylistMetadataProvider,
) -> None:
    """Stores of the generation times can not finish out of order."""
    cache = _mass(provider).cache
    store = cache.set

    async def slow_set(key: str, data: Any, **kwargs: Any) -> None:
        # the first store is the slowest, so without the lock it would finish last
        await asyncio.sleep(0.05 if len(data) == 1 else 0)
        await store(key, data, **kwargs)

    cache.set = slow_set
    ai = provider._ai

    await asyncio.gather(
        ai._record(ai._generated_at, "generated_at"),
        ai._record(ai._generated_at, "generated_at"),
    )

    stored = await cache.get("generated_at", provider=INSTANCE_ID)
    assert len(stored) == 2


async def test_new_work_wakes_a_waiting_worker(provider: PlaylistMetadataProvider) -> None:
    """Work that is not blocked does not wait out the hourly limit of other work."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1, CONF_DESCRIPTIONS: False})
    await _metadata(provider, _smart_playlist(item_id="1"))
    await _metadata(provider, _smart_playlist(item_id="2"))
    assert provider._ai.waiting
    _set_config(provider, **{CONF_DESCRIPTIONS: True})
    third = _smart_playlist(item_id="3")

    await _metadata(provider, third)

    assert third.metadata.description == DESCRIPTION
    assert provider._ai.waiting


async def test_a_worker_that_queues_work_as_it_starts_stays_the_only_one(
    provider: PlaylistMetadataProvider,
) -> None:
    """An eagerly started worker queueing more work at once does not start a second worker."""
    ai = provider._ai
    second = _smart_playlist(item_id="2")
    processed: list[str] = []

    async def process(item_id: str, _kinds: set[str]) -> tuple[set[str], int]:
        # runs in the worker's first, eager step, before its task is tracked
        processed.append(item_id)
        if item_id == "1":
            ai._enqueue(second, "cover")
        return set(), 0

    with patch.object(ai, "_process", side_effect=process):
        ai._enqueue(_smart_playlist(item_id="1"), "cover")
        await _settle(provider)

    worker_starts = [
        call
        for call in _mass(provider).create_task.call_args_list
        if call.kwargs.get("task_id") == f"{INSTANCE_ID}_ai_worker"
    ]
    assert len(worker_starts) == 1
    assert processed == ["1", "2"]


async def test_nothing_is_queued_after_unloading(provider: PlaylistMetadataProvider) -> None:
    """A metadata refresh still running on an unloaded provider starts no worker."""
    await provider.unload()

    await _metadata(provider, _smart_playlist())

    assert _engine_calls(provider) == 0
    assert not provider._ai._pending


async def test_resolve_image_returns_a_stored_cover(provider: PlaylistMetadataProvider) -> None:
    """A stored cover is served from the images directory."""
    Path(provider._images_dir, "1_abc.jpg").write_bytes(b"jpeg")

    assert await provider.resolve_image("1_abc.jpg") == b"jpeg"


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "sub/../../secret.txt"])
async def test_resolve_image_rejects_paths_outside_the_images_dir(
    provider: PlaylistMetadataProvider, path: str
) -> None:
    """A path from the library must never reach outside our directory."""
    Path(provider._images_dir).parent.joinpath("secret.txt").write_text("secret")

    with pytest.raises(FileNotFoundError):
        await provider.resolve_image(path)


async def test_scheduled_refresh_generates_missing_covers(
    provider: PlaylistMetadataProvider,
) -> None:
    """The hourly pass has the covers made that playlists in scope lack, then applies them."""
    _set_config(provider, **{CONF_DESCRIPTIONS: False})
    missing = _smart_playlist(item_id="1")
    out_of_scope = _playlist("builtin", item_id="2", is_editable=True)
    _register(provider, missing, out_of_scope)
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        return_value=_async_iter([missing, out_of_scope])
    )
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._ai.refresh()
    await _settle(provider)

    assert _engine_calls(provider) == 1
    _mass(provider).metadata.update_metadata.assert_awaited_once_with(
        missing.uri, force_refresh=True
    )


async def test_the_cleanup_keeps_covers_in_use_and_removes_orphans(
    provider: PlaylistMetadataProvider,
) -> None:
    """Generated covers share the collage cleanup: only unreferenced old files are deleted."""
    in_use = Path(provider._images_dir, "ai_1_abc.jpg")
    orphan = Path(provider._images_dir, "ai_9_dead.jpg")
    fresh_orphan = Path(provider._images_dir, "ai_9_new.jpg")
    for path in (in_use, orphan):
        path.write_bytes(b"old")
        os.utime(path, (time.time() - 3600, time.time() - 3600))
    fresh_orphan.write_bytes(b"new")
    playlist = _smart_playlist(item_id="1", images=[_image(in_use.name, provider=INSTANCE_ID)])
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        return_value=_async_iter([playlist])
    )

    await provider._cleanup_stale_images()

    assert in_use.exists()
    assert not orphan.exists()
    # written moments ago, so possibly not stored in the library yet
    assert fresh_orphan.exists()


async def test_refreshes_soon_after_loading(tmp_path: Path) -> None:
    """Besides the hourly run, loading (also after a settings change) refreshes soon after."""
    provider = _make_provider(tmp_path)
    mass = _mass(provider)

    await provider.loaded_in_mass()

    handlers = [
        call.kwargs["handler"] for call in mass.tasks.register_scheduled_task.call_args_list
    ]
    assert provider._ai.refresh in handlers
    mass.call_later.assert_called_once_with(
        120, provider._ai.refresh, task_id=f"{INSTANCE_ID}_ai_catch_up"
    )

    await provider.unload()

    mass.cancel_timer.assert_called_once_with(f"{INSTANCE_ID}_ai_catch_up")


async def test_no_hourly_refresh_without_ai(tmp_path: Path) -> None:
    """With AI turned off, only the catch-up after loading runs, to swap out old covers."""
    provider = _make_provider(tmp_path)
    _set_config(provider, template="album_grid", **{CONF_DESCRIPTIONS: False})
    mass = _mass(provider)

    await provider.loaded_in_mass()

    handlers = [
        call.kwargs["handler"] for call in mass.tasks.register_scheduled_task.call_args_list
    ]
    assert provider._ai.refresh not in handlers
    mass.call_later.assert_called_once_with(
        120, provider._ai.refresh, task_id=f"{INSTANCE_ID}_ai_catch_up"
    )


async def test_refresh_takes_the_cover_off_playlists_that_left_the_scope(
    provider: PlaylistMetadataProvider,
) -> None:
    """A playlist no longer in scope is refreshed so its cover makes way for the collage."""
    _set_config(provider, **{CONF_PLAYLISTS: PLAYLISTS_SMART})
    left = _playlist("spotify", item_id="5")
    left.metadata.images = UniqueList([_image("ai_5_cover.jpg", provider=INSTANCE_ID)])
    _mass(provider).music.playlists.iter_library_items = MagicMock(return_value=_async_iter([left]))
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._ai.refresh()

    _mass(provider).metadata.update_metadata.assert_awaited_once_with(left.uri, force_refresh=True)


async def test_refresh_puts_the_collage_back_after_switching_templates(
    provider: PlaylistMetadataProvider,
) -> None:
    """Picking a collage template swaps the generated covers out on the next refresh."""
    _set_config(provider, template="album_grid")
    playlist = _smart_playlist(images=[_image("ai_234_cover.jpg", provider=INSTANCE_ID)])
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        return_value=_async_iter([playlist])
    )
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._ai.refresh()

    _mass(provider).metadata.update_metadata.assert_awaited_once_with(
        playlist.uri, force_refresh=True
    )


def test_a_long_title_is_fitted_whole() -> None:
    """A title too long for the overlay shrinks the font instead of losing words."""
    title = "The Very Long Name Of A Playlist That Keeps Going Well Past Three Lines Of Text"

    font, font_size, lines = _fit_title(title, 123, 880)

    assert " ".join(lines).split() == title.split()
    assert len(lines) <= 3
    assert font_size < 123
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    assert all(measure.textlength(line, font=font) <= 880 for line in lines)


def test_a_word_wider_than_the_cover_is_split_not_dropped() -> None:
    """Even at the smallest size, a word that does not fit is split over lines."""
    title = "Supercalifragilisticexpialidocious" * 3

    _font, _size, lines = _fit_title(title, 123, 200)

    assert "".join(lines) == title


async def test_writes_a_description_for_a_smart_playlist(
    provider: PlaylistMetadataProvider,
) -> None:
    """A smart playlist gets a description from its rules."""
    playlist = _smart_playlist()
    playlist.metadata.description = "[Smart Playlist] Similar to Innerbloom"

    await _metadata(provider, playlist)

    assert playlist.metadata.description == DESCRIPTION
    query = str(provider.test_text_engine.provider.ai_query.await_args.args[0])  # type: ignore[attr-defined]
    assert 'a personal playlist called "Indie Dance Sunset"' in query
    assert (
        "What defines it: Innerbloom – RÜFÜS DU SOL; Midnight Sun – Lane 8, Jerro "
        "(genres Melodic House) (years 2015-2019)"
    ) in query
    assert "140 characters max" in query


async def test_a_stored_description_is_reapplied_without_calling_the_engine(
    provider: PlaylistMetadataProvider,
) -> None:
    """A description replaced by something else is put back from the cache."""
    await _metadata(provider, _smart_playlist())
    playlist = _smart_playlist()
    playlist.metadata.description = "[Smart Playlist] Similar to Innerbloom"

    await _metadata(provider, playlist)

    assert playlist.metadata.description == DESCRIPTION
    assert _text_calls(provider) == 1


async def test_renaming_the_playlist_rewrites_the_description(
    provider: PlaylistMetadataProvider,
) -> None:
    """The stored description belongs to the playlist name it was written for."""
    await _metadata(provider, _smart_playlist())

    await _metadata(provider, _smart_playlist(name="Sunrise Set"))

    assert _text_calls(provider) == 2


async def test_descriptions_can_be_turned_off(provider: PlaylistMetadataProvider) -> None:
    """With descriptions off the playlist keeps its own."""
    _set_config(provider, **{CONF_DESCRIPTIONS: False})
    playlist = _smart_playlist()
    playlist.metadata.description = "Mine"

    await _metadata(provider, playlist)

    assert playlist.metadata.description == "Mine"
    assert _text_calls(provider) == 0


async def test_the_hourly_limit_postpones_descriptions(provider: PlaylistMetadataProvider) -> None:
    """Descriptions over the limit are left to a later refresh."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1})
    await _metadata(provider, _smart_playlist(item_id="1"))
    second = _smart_playlist(item_id="2")

    await _metadata(provider, second)

    assert _text_calls(provider) == 1
    assert second.metadata.description is None


async def test_a_failing_text_engine_leaves_the_description_untouched(
    provider: PlaylistMetadataProvider,
) -> None:
    """A text engine error is logged, not raised into the metadata refresh."""
    provider.test_text_engine.provider.ai_query = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("quota exceeded")
    )
    playlist = _smart_playlist()
    playlist.metadata.description = "Mine"

    await _metadata(provider, playlist)

    assert playlist.metadata.description == "Mine"


async def test_regenerate_makes_new_covers_and_descriptions(
    provider: PlaylistMetadataProvider,
) -> None:
    """The regenerate action keys every cover and description anew, and starts a refresh."""
    first = _smart_playlist()
    await _metadata(provider, first)
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        side_effect=lambda: _async_iter([])
    )

    with patch(
        "music_assistant.providers.playlist_metadata.ai.time.time", return_value=2_000_000_000.0
    ):
        await provider.handle_config_action(CONF_ACTION_REGENERATE)
    second = _smart_playlist(images=list(first.metadata.images or []))
    await _metadata(provider, second)

    assert _engine_calls(provider) == 2
    assert _text_calls(provider) == 2
    assert second.image is not None
    assert first.image is not None
    assert second.image.path != first.image.path


async def test_regenerating_twice_within_a_second_still_regenerates(
    provider: PlaylistMetadataProvider,
) -> None:
    """Every press of regenerate keys the covers anew, also within the same second."""
    _mass(provider).create_task = MagicMock(side_effect=lambda coro, **_kw: coro.close())
    with patch(
        "music_assistant.providers.playlist_metadata.ai.time.time", return_value=2_000_000_000.0
    ):
        await provider.handle_config_action(CONF_ACTION_REGENERATE)
        first = provider._ai._generation
        await provider.handle_config_action(CONF_ACTION_REGENERATE)

    assert provider._ai._generation > first


async def test_a_failed_write_leaves_no_cover_file(provider: PlaylistMetadataProvider) -> None:
    """A cover that could not be written in full is not kept, so the next refresh retries."""
    playlist = _smart_playlist()
    with patch.object(Path, "replace", side_effect=OSError("No space left on device")):
        await _metadata(provider, playlist)

    assert playlist.image is None
    assert list(Path(provider._images_dir).iterdir()) == []

    # tried again once the first backoff is over
    with patch(
        "music_assistant.providers.playlist_metadata.ai.time.time", return_value=time.time() + 3700
    ):
        await _metadata(provider, playlist)

    assert _engine_calls(provider) == 2
    assert playlist.image is not None


async def test_refresh_takes_the_cover_off_a_playlist_that_got_its_own_artwork(
    provider: PlaylistMetadataProvider,
) -> None:
    """A playlist that got artwork of its own is refreshed so its generated cover comes off."""
    own_cover = _image("https://example.com/mine.jpg", provider="builtin")
    playlist = _smart_playlist(images=[_image("ai_234_cover.jpg", provider=INSTANCE_ID), own_cover])
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        return_value=_async_iter([playlist])
    )
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._ai.refresh()

    _mass(provider).metadata.update_metadata.assert_awaited_once_with(
        playlist.uri, force_refresh=True
    )

    await _metadata(provider, playlist)

    assert list(playlist.metadata.images or []) == [own_cover]
    assert _engine_calls(provider) == 0


async def test_preview_shows_covers_and_description_without_storing(
    provider: PlaylistMetadataProvider,
) -> None:
    """The preview shows a labelled cover per style and the description, and keeps neither."""
    playlist = _smart_playlist(name="Late Night Drive")
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=playlist)
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        side_effect=lambda: _async_iter([playlist])
    )
    _set_config(provider, **{CONF_PREVIEW_PLAYLIST: "234", CONF_PREVIEW_STYLE: ["gouache"]})

    with _no_engine_pickers():
        entries = await provider.handle_config_action(CONF_ACTION_PREVIEW)

    assert entries is not None
    keys = [entry.key for entry in entries]
    image_key = f"{CONF_PREVIEW_IMAGE}_gouache"
    assert keys[keys.index(CONF_ACTION_PREVIEW) + 1 :][:2] == [image_key, CONF_PREVIEW_DESCRIPTION]
    preview = next(entry for entry in entries if entry.key == image_key)
    with _decode_preview(str(preview.value)) as image:
        # the cover at preview size, with its style named in a strip above it
        assert image.size == (512, 512 + 64)
    description = next(entry for entry in entries if entry.key == CONF_PREVIEW_DESCRIPTION)
    assert description.translation_params == [DESCRIPTION]
    assert 'called "Late Night Drive"' in _sent_prompt(provider)
    assert not any(Path(provider._images_dir).iterdir())
    assert not playlist.metadata.images
    assert not playlist.metadata.description


async def test_preview_uses_its_own_style_settings(provider: PlaylistMetadataProvider) -> None:
    """The preview tries its own styles and prompt, and the configured title setting."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(
        return_value=_smart_playlist(name="Late Night Drive")
    )
    _set_config(
        provider,
        **{
            CONF_PREVIEW_PLAYLIST: "234",
            CONF_PREVIEW_STYLE: ["custom"],
            CONF_PREVIEW_CUSTOM_PROMPT: "A tiny watercolor of {name}.",
            CONF_TITLE_OVERLAY: True,
        },
    )

    with (
        _no_engine_pickers(),
        patch(
            "music_assistant.providers.playlist_metadata.ai._render_cover", return_value=_png()
        ) as render_cover,
    ):
        await provider.handle_config_action(CONF_ACTION_PREVIEW)

    assert _sent_prompt(provider) == "A tiny watercolor of Late Night Drive."
    assert render_cover.call_args.args[1] is not None


async def test_preview_shows_every_preset_by_default(provider: PlaylistMetadataProvider) -> None:
    """Without styles picked, every preset is previewed once, each as its own image."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(provider, **{CONF_PREVIEW_PLAYLIST: "234"})

    with _no_engine_pickers():
        entries = await provider.handle_config_action(CONF_ACTION_PREVIEW)

    presets = [style for style in Style if style != Style.CUSTOM]
    assert _engine_calls(provider) == len(presets)
    assert [entry.key for entry in entries or () if entry.key.startswith(CONF_PREVIEW_IMAGE)] == [
        f"{CONF_PREVIEW_IMAGE}_{style.value}" for style in presets
    ]


async def test_preview_still_shows_the_styles_that_worked(
    provider: PlaylistMetadataProvider,
) -> None:
    """One style failing leaves it out instead of failing the whole preview."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(
        provider,
        **{CONF_PREVIEW_PLAYLIST: "234", CONF_PREVIEW_STYLE: ["type_cutout", "bold", "gouache"]},
    )
    engine = provider.test_engine.provider  # type: ignore[attr-defined]

    async def generate(prompt: str, **_kwargs: Any) -> bytes:
        if "gouache" in prompt:
            raise RuntimeError("engine down")
        return _png()

    engine.ai_generate_image = AsyncMock(side_effect=generate)

    with _no_engine_pickers():
        entries = await provider.handle_config_action(CONF_ACTION_PREVIEW)

    # "type_cutout" is no style (anymore), so only Bold and Gouache were asked for
    assert engine.ai_generate_image.await_count == 2
    assert [entry.key for entry in entries or () if entry.key.startswith(CONF_PREVIEW_IMAGE)] == [
        f"{CONF_PREVIEW_IMAGE}_bold"
    ]


async def test_previews_count_towards_the_hourly_limit(
    provider: PlaylistMetadataProvider,
) -> None:
    """A preview that would go over the hourly limit is refused before anything is generated."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(
        provider,
        **{
            CONF_PREVIEW_PLAYLIST: "234",
            CONF_PREVIEW_STYLE: ["bold", "neon"],
            CONF_MAX_PER_HOUR: 3,
        },
    )

    with _no_engine_pickers():
        await provider.handle_config_action(CONF_ACTION_PREVIEW)
        with pytest.raises(ActionUnavailable, match="leaves 1 cover"):
            await provider.handle_config_action(CONF_ACTION_PREVIEW)

    assert _engine_calls(provider) == 2
    # the background generation is held to the same limit
    second = _smart_playlist(item_id="2")
    await _metadata(provider, second)
    await _metadata(provider, _smart_playlist(item_id="3"))
    assert _engine_calls(provider) == 3


async def test_a_background_cover_can_not_take_a_slot_a_preview_reserved(
    provider: PlaylistMetadataProvider,
) -> None:
    """A preview reserves all its slots at once, so nothing slips in while it stores them."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(
        provider,
        **{
            CONF_PREVIEW_PLAYLIST: "234",
            CONF_PREVIEW_STYLE: ["bold", "neon"],
            CONF_MAX_PER_HOUR: 2,
            CONF_DESCRIPTIONS: False,
        },
    )
    cache = _mass(provider).cache
    store = cache.set

    async def slow_set(*args: Any, **kwargs: Any) -> None:
        # storing the reservation hands control to the background generation
        await asyncio.sleep(0)
        await store(*args, **kwargs)

    cache.set = slow_set
    background = _smart_playlist(item_id="9")

    with _no_engine_pickers():
        await asyncio.gather(
            provider.handle_config_action(CONF_ACTION_PREVIEW),
            provider.get_playlist_metadata(background),
        )

    assert _engine_calls(provider) == 2
    assert background.image is None


async def test_refresh_regenerates_a_cover_whose_file_is_gone(
    provider: PlaylistMetadataProvider,
) -> None:
    """A cover still named by the playlist but deleted from disk is made again."""
    playlist = _smart_playlist()
    await _metadata(provider, playlist)
    assert playlist.image is not None
    Path(provider._images_dir, playlist.image.path).unlink()
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        side_effect=lambda: _async_iter([playlist])
    )
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._ai.refresh()
    await _settle(provider)

    assert _engine_calls(provider) == 2
    _mass(provider).metadata.update_metadata.assert_awaited_once_with(
        playlist.uri, force_refresh=True
    )


async def test_only_one_preview_runs_at_a_time(provider: PlaylistMetadataProvider) -> None:
    """Pressing preview again while one runs is refused, and covers render two at a time."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(provider, **{CONF_PREVIEW_PLAYLIST: "234"})
    release = asyncio.Event()
    running = 0
    most_running = 0

    async def generate(*_args: Any, **_kwargs: Any) -> bytes:
        nonlocal running, most_running
        running += 1
        most_running = max(most_running, running)
        await release.wait()
        running -= 1
        return _png()

    provider.test_engine.provider.ai_generate_image = AsyncMock(side_effect=generate)  # type: ignore[attr-defined]

    with _no_engine_pickers():
        first = asyncio.create_task(provider.handle_config_action(CONF_ACTION_PREVIEW))
        while not running:
            await asyncio.sleep(0)
        with pytest.raises(ActionUnavailable, match="already being generated"):
            await provider.handle_config_action(CONF_ACTION_PREVIEW)
        release.set()
        await first

    assert most_running == 2


async def test_unloading_stops_a_running_preview(provider: PlaylistMetadataProvider) -> None:
    """Covers still waiting in a preview are not generated after the provider unloads."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(
        provider,
        **{CONF_PREVIEW_PLAYLIST: "234", CONF_PREVIEW_STYLE: ["bold", "neon", "gouache"]},
    )
    release = asyncio.Event()

    async def generate(*_args: Any, **_kwargs: Any) -> bytes:
        await release.wait()
        return _png()

    provider.test_engine.provider.ai_generate_image = AsyncMock(side_effect=generate)  # type: ignore[attr-defined]

    with _no_engine_pickers():
        preview = asyncio.create_task(provider.handle_config_action(CONF_ACTION_PREVIEW))
        while _engine_calls(provider) < 2:
            await asyncio.sleep(0)
        await provider.unload()
        release.set()
        with contextlib.suppress(BaseException):
            await preview

    # the third style was still waiting for a slot, so it never reached the engine
    assert _engine_calls(provider) == 2


@pytest.mark.parametrize(
    ("held_key", "use_prompt_engine"),
    [("described_at", False), ("generated_at", True)],
    ids=["description", "concept"],
)
async def test_unloading_stops_a_preview_before_the_text_engine(
    provider: PlaylistMetadataProvider, held_key: str, use_prompt_engine: bool
) -> None:
    """A preview that is storing its reservation when the provider unloads asks no text."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(
        provider,
        **{
            CONF_PREVIEW_PLAYLIST: "234",
            CONF_PREVIEW_STYLE: ["bold"],
            CONF_USE_PROMPT_ENGINE: use_prompt_engine,
        },
    )
    cache = _mass(provider).cache
    store = cache.set
    held = asyncio.Event()
    release = asyncio.Event()

    async def held_set(key: str, *args: Any, **kwargs: Any) -> None:
        if key == held_key:
            held.set()
            await release.wait()
        await store(key, *args, **kwargs)

    cache.set = held_set

    with _no_engine_pickers():
        preview = asyncio.create_task(provider.handle_config_action(CONF_ACTION_PREVIEW))
        async with asyncio.timeout(1):
            await held.wait()
        await provider.unload()
        release.set()
        with pytest.raises(ActionUnavailable, match="unloading"):
            await preview

    assert _text_calls(provider) == 0


async def test_the_newest_generation_counter_is_stored_last(
    provider: PlaylistMetadataProvider,
) -> None:
    """Two quick regenerates store the newer counter last."""
    _mass(provider).create_task = MagicMock(side_effect=lambda coro, **_kw: coro.close())
    cache = _mass(provider).cache
    store = cache.set
    first = True

    async def slow_set(key: str, data: Any, **kwargs: Any) -> None:
        nonlocal first
        if key == "generation" and first:
            # the first store is the slowest, so without the lock it would finish last
            first = False
            await asyncio.sleep(0.05)
        await store(key, data, **kwargs)

    cache.set = slow_set

    await asyncio.gather(
        provider.handle_config_action(CONF_ACTION_REGENERATE),
        provider.handle_config_action(CONF_ACTION_REGENERATE),
    )

    assert await cache.get("generation", provider=INSTANCE_ID) == provider._ai._generation


async def test_preview_without_a_description(provider: PlaylistMetadataProvider) -> None:
    """A text engine that fails leaves the description out and still shows the covers."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(provider, **{CONF_PREVIEW_PLAYLIST: "234", CONF_PREVIEW_STYLE: ["bold"]})
    provider.test_text_engine.provider.ai_query = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("text engine down")
    )

    with _no_engine_pickers():
        entries = await provider.handle_config_action(CONF_ACTION_PREVIEW)

    keys = [entry.key for entry in entries or ()]
    assert f"{CONF_PREVIEW_IMAGE}_bold" in keys
    assert CONF_PREVIEW_DESCRIPTION not in keys


async def test_preview_fails_when_no_style_worked(provider: PlaylistMetadataProvider) -> None:
    """When every style fails, the preview reports the engine's error."""
    _mass(provider).music.playlists.get_library_item = AsyncMock(return_value=_smart_playlist())
    _set_config(provider, **{CONF_PREVIEW_PLAYLIST: "234", CONF_PREVIEW_STYLE: ["bold"]})
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("engine down")
    )

    with pytest.raises(RuntimeError, match="engine down"):
        await provider.handle_config_action(CONF_ACTION_PREVIEW)


async def test_preview_needs_a_playlist(provider: PlaylistMetadataProvider) -> None:
    """Pressing preview without a playlist picked asks for one and generates nothing."""
    with pytest.raises(ActionUnavailable, match="Pick a playlist"):
        await provider.handle_config_action(CONF_ACTION_PREVIEW)

    assert _engine_calls(provider) == 0


async def test_settings_page_has_no_preview_until_asked(provider: PlaylistMetadataProvider) -> None:
    """The regular config entries offer the preview button but no preview image."""
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        side_effect=lambda: _async_iter([_smart_playlist(name="Late Night Drive")])
    )

    with _no_engine_pickers():
        entries = await provider.get_config_entries()

    keys = [entry.key for entry in entries]
    assert CONF_ACTION_PREVIEW in keys
    assert not any(key.startswith(CONF_PREVIEW_IMAGE) for key in keys)
    picker = next(entry for entry in entries if entry.key == CONF_PREVIEW_PLAYLIST)
    assert picker.immediate_apply
    assert [(option.value, option.title) for option in picker.options] == [
        ("234", "Late Night Drive")
    ]


async def test_changing_preview_settings_does_not_reload(
    provider: PlaylistMetadataProvider,
) -> None:
    """Only preview settings changing keeps the provider loaded; anything else reloads."""
    config = MagicMock()
    with patch(
        "music_assistant.models.provider.Provider.update_config", AsyncMock()
    ) as base_update:
        await provider.update_config(
            config, {f"values/{CONF_PREVIEW_PLAYLIST}", f"values/{CONF_PREVIEW_STYLE}"}
        )
        await provider.update_config(config, {f"values/{CONF_PREVIEW_CUSTOM_PROMPT}"})
        assert provider.config is config
        base_update.assert_not_awaited()

        await provider.update_config(
            config, {f"values/{CONF_PREVIEW_STYLE}", f"values/{CONF_STYLE}"}
        )
        base_update.assert_awaited_once()


async def test_scheduled_refresh_puts_back_replaced_descriptions(
    provider: PlaylistMetadataProvider,
) -> None:
    """A playlist whose cover is current is only refreshed when its description was replaced."""
    current = _smart_playlist(item_id="1")
    await _metadata(provider, current)
    replaced = _smart_playlist(item_id="1")
    replaced.metadata.images = current.metadata.images
    replaced.metadata.description = "[Smart Playlist] Similar to Innerbloom"
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        side_effect=lambda: _async_iter([current])
    )
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._ai.refresh()
    await _settle(provider)
    _mass(provider).metadata.update_metadata.assert_not_awaited()
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        return_value=_async_iter([replaced])
    )
    await provider._ai.refresh()
    await _settle(provider)

    _mass(provider).metadata.update_metadata.assert_awaited_once_with(
        replaced.uri, force_refresh=True
    )
    assert _text_calls(provider) == 1


def _make_provider(tmp_path: Path) -> PlaylistMetadataProvider:
    """Construct the provider on a mocked Music Assistant."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path / "data")
    mass.call_later = MagicMock()
    mass.cache = _FakeCache()
    mass.music.playlists.iter_tracks = MagicMock(side_effect=lambda *_: _async_iter([]))
    smart_playlist_provider = MagicMock()
    smart_playlist_provider.domain = "smart_playlist"
    smart_playlist_provider.get_smart_playlist_rules = AsyncMock(return_value=SMART_RULES)
    builtin_provider = MagicMock()
    builtin_provider.domain = "builtin"
    builtin_provider.resolve_image = AsyncMock(side_effect=lambda path: path)
    providers = {
        "smart_playlist": smart_playlist_provider,
        "builtin": builtin_provider,
    }
    mass.get_provider = MagicMock(side_effect=lambda instance_id: providers.get(instance_id))

    manifest = MagicMock()
    manifest.domain = "playlist_metadata"
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.get_value = MagicMock(
        side_effect=lambda key, default=None: DEFAULT_CONFIG.get(key, default)
    )
    provider = PlaylistMetadataProvider(mass, manifest, config, set())
    _set_config(provider)

    _add_worker_fakes(provider)

    engine = MagicMock()
    engine.id = "ai_task.images"
    engine.uid = ENGINE_UID
    engine.provider.ai_generate_image = AsyncMock(side_effect=lambda *_a, **_k: _png())
    provider.test_engine = engine  # type: ignore[attr-defined]
    text_engine = MagicMock()
    text_engine.id = "gemini-flash"
    text_engine.uid = TEXT_ENGINE_UID
    text_engine.provider.ai_query = AsyncMock(return_value=f' "{DESCRIPTION}"\n')
    provider.test_text_engine = text_engine  # type: ignore[attr-defined]
    return provider


def _add_worker_fakes(provider: PlaylistMetadataProvider) -> None:
    """
    Give the mocked Music Assistant what the worker needs, behaving like the real thing.

    Tasks really run (started eagerly, like Music Assistant does), playlists are fetched from
    a small library as fresh copies, and a metadata refresh runs the provider's on a fresh copy
    and stores the result back in the library playlist.
    """
    mass = _mass(provider)
    # the library the worker fetches playlists from, filled by _metadata and _register
    library: dict[str, Playlist] = {}
    mass.test_library = library

    async def get_library_item(item_id: str) -> Playlist:
        if (playlist := library.get(str(item_id))) is None:
            raise MediaNotFoundError(item_id)
        return Playlist.from_dict(playlist.to_dict())

    mass.music.playlists.get_library_item = AsyncMock(side_effect=get_library_item)
    tasks: dict[str, asyncio.Task[Any]] = {}
    mass.test_tasks = tasks

    def create_task(target: Any, *args: Any, task_id: str | None = None, **_kwargs: Any) -> Any:
        coro = target if asyncio.iscoroutine(target) else target(*args)
        if task_id and (existing := tasks.get(task_id)) and not existing.done():
            coro.close()
            return existing
        task = asyncio.Task(coro, eager_start=True)
        tasks[task_id or str(id(task))] = task
        return task

    mass.create_task = MagicMock(side_effect=create_task)

    async def update_metadata(item: Any, **_kwargs: Any) -> None:
        # like the metadata controller: the library item as it is now, the provider's metadata
        # merged into it, and the result stored
        stored = next(pl for pl in library.values() if item in (pl.uri, pl))
        playlist = Playlist.from_dict(stored.to_dict())
        if metadata := await provider.get_playlist_metadata(playlist):
            playlist.metadata.update(metadata)
        stored.metadata = playlist.metadata

    mass.metadata.update_metadata = AsyncMock(side_effect=update_metadata)


class _FakeCache:
    """An in-memory stand-in for the cache controller, honouring checksums."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str, int], tuple[Any, str | None]] = {}

    async def get(
        self,
        key: str,
        provider: str = "default",
        category: int = 0,
        checksum: str | None = None,
        default: Any = None,
        **_kwargs: Any,
    ) -> Any:
        value, stored_checksum = self.data.get((key, provider, category), (default, None))
        if checksum is not None and stored_checksum != checksum:
            return default
        return value

    async def set(
        self,
        key: str,
        data: Any,
        provider: str = "default",
        category: int = 0,
        checksum: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self.data[(key, provider, category)] = (data, checksum)


def _set_config(provider: PlaylistMetadataProvider, **overrides: Any) -> None:
    """Make the provider read the default config with the given overrides."""
    current = getattr(provider, "_test_config", dict(DEFAULT_CONFIG))
    current.update(overrides)
    provider._test_config = current  # type: ignore[attr-defined]
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: current.get(key, default)
    )


def _smart_playlist(
    *,
    item_id: str = "234",
    name: str = "Indie Dance Sunset",
    images: list[MediaItemImage] | None = None,
) -> Playlist:
    """Return a library smart playlist."""
    playlist = _playlist("smart_playlist", item_id=item_id, name=name, is_editable=True)
    playlist.metadata.images = UniqueList(images or [])
    return playlist


def _playlist(
    provider_domain: str,
    *,
    item_id: str = "234",
    name: str = "Indie Dance Sunset",
    is_editable: bool = False,
) -> Playlist:
    """Return a library playlist backed by the given provider."""
    return Playlist(
        item_id=item_id,
        provider="library",
        name=name,
        is_editable=is_editable,
        provider_mappings={
            ProviderMapping(
                item_id=f"prov-{item_id}",
                provider_domain=provider_domain,
                provider_instance=provider_domain,
            )
        },
        metadata=MediaItemMetadata(images=UniqueList()),
    )


def _track(name: str, artist: str, genres: set[str]) -> Track:
    """Return a playlist track by the given artist."""
    return Track(
        item_id=name,
        provider="library",
        name=name,
        provider_mappings={
            ProviderMapping(item_id=name, provider_domain="test", provider_instance="test")
        },
        artists=UniqueList(
            [
                Artist(
                    item_id=artist,
                    provider="library",
                    name=artist,
                    provider_mappings={
                        ProviderMapping(
                            item_id=artist, provider_domain="test", provider_instance="test"
                        )
                    },
                )
            ]
        ),
        metadata=MediaItemMetadata(genres=genres),
    )


def _image(path: str, *, provider: str) -> MediaItemImage:
    """Return a thumb image."""
    return MediaItemImage(type=ImageType.THUMB, path=path, provider=provider)


def _png() -> bytes:
    """Return a non-square PNG, like an engine may answer with."""
    buffer = BytesIO()
    Image.new("RGB", (1200, 900), (200, 40, 120)).save(buffer, "PNG")
    return buffer.getvalue()


def _decode_preview(data_url: str) -> Image.Image:
    """Return the image of a preview data url."""
    assert data_url.startswith("data:image/jpeg;base64,")
    return Image.open(BytesIO(base64.b64decode(data_url.split(",", 1)[1])))


@contextmanager
def _no_engine_pickers() -> Iterator[None]:
    """Leave the engine pickers out of the config entries, they have tests of their own."""
    with (
        patch(
            "music_assistant.providers.playlist_metadata.ai.create_image_engine_config_entries",
            AsyncMock(return_value=()),
        ),
        patch(
            "music_assistant.providers.playlist_metadata.ai.create_ai_engine_config_entries",
            AsyncMock(return_value=()),
        ),
    ):
        yield


async def _metadata(
    provider: PlaylistMetadataProvider, playlist: Playlist
) -> MediaItemMetadata | None:
    """
    Refresh the playlist's metadata, and let the worker finish what that queued.

    The returned metadata is merged into the playlist, and the worker applies what it made
    with another metadata refresh, as Music Assistant does.
    """
    _register(provider, playlist)
    if metadata := await provider.get_playlist_metadata(playlist):
        playlist.metadata.update(metadata)
    await _settle(provider)
    return metadata


def _cover(playlist: Playlist) -> MediaItemImage | None:
    """Return the playlist's cover, as it is now."""
    return playlist.image


def _register(provider: PlaylistMetadataProvider, *playlists: Playlist) -> None:
    """Put the playlists in the library the worker fetches them from."""
    for playlist in playlists:
        _mass(provider).test_library[str(playlist.item_id)] = playlist


async def _settle(provider: PlaylistMetadataProvider) -> None:
    """Wait until the worker is done, or waits for the hourly limit."""
    # the worker renders and writes the covers in threads, so this waits for real time
    for _ in range(1000):
        tasks = [task for task in _tasks(provider).values() if not task.done()]
        if not tasks or (provider._ai.waiting and len(tasks) == 1):
            return
        await asyncio.sleep(0.01)
    msg = "the worker did not settle"
    raise AssertionError(msg)


def _tasks(provider: PlaylistMetadataProvider) -> dict[str, asyncio.Task[Any]]:
    """Return the tasks the provider started."""
    return cast("dict[str, asyncio.Task[Any]]", _mass(provider).test_tasks)


def _mass(provider: PlaylistMetadataProvider) -> MagicMock:
    """Return the mocked Music Assistant of the provider."""
    return cast("MagicMock", provider.mass)


def _text_calls(provider: PlaylistMetadataProvider) -> int:
    """Return how often the text engine was asked for a description."""
    return int(provider.test_text_engine.provider.ai_query.await_count)  # type: ignore[attr-defined]


def _engine_calls(provider: PlaylistMetadataProvider) -> int:
    """Return how often the image engine was asked for an image."""
    return int(provider.test_engine.provider.ai_generate_image.await_count)  # type: ignore[attr-defined]


def _sent_prompt(provider: PlaylistMetadataProvider) -> str:
    """Return the prompt of the last image request."""
    return str(provider.test_engine.provider.ai_generate_image.await_args.args[0])  # type: ignore[attr-defined]


async def _async_iter(items: list[Any]) -> AsyncGenerator[Any]:
    """Yield the given items asynchronously."""
    for item in items:
        yield item
