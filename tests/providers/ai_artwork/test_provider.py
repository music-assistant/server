"""Tests for the AI Artwork metadata provider."""

# the seed labels of smart playlists separate title and artists with an en dash
# ruff: noqa: RUF001

from __future__ import annotations

import os
import time
from collections.abc import AsyncGenerator
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    Artist,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
)
from music_assistant_models.media_items.metadata import MediaItemMetadata
from music_assistant_models.unique_list import UniqueList
from PIL import Image

from music_assistant.constants import CONF_LOG_LEVEL
from music_assistant.providers.ai_artwork import AIArtworkProvider, _format_template
from music_assistant.providers.ai_artwork.constants import (
    CONF_ACTION_REGENERATE,
    CONF_CUSTOM_PROMPT,
    CONF_DESCRIPTIONS,
    CONF_EXTRA_PLAYLISTS,
    CONF_IMAGE_ENGINE,
    CONF_KEEP_EXISTING_ARTWORK,
    CONF_MAX_PER_HOUR,
    CONF_PLAYLISTS,
    CONF_STYLE,
    CONF_TEXT_ENGINE,
    CONF_TITLE_OVERLAY,
    CONF_USE_PROMPT_ENGINE,
    PLAYLISTS_ALL,
    PLAYLISTS_SMART,
)

INSTANCE_ID = "ai_artwork"
ENGINE_UID = "hass--test/ai_task.images"
TEXT_ENGINE_UID = "openai_compatible--test/gemini-flash"
DESCRIPTION = "Melodic house for the hours after midnight. RÜFÜS DU SOL, Lane 8 and more."

DEFAULT_CONFIG: dict[str, Any] = {
    CONF_LOG_LEVEL: "GLOBAL",
    CONF_IMAGE_ENGINE: ENGINE_UID,
    CONF_STYLE: "music_assistant",
    CONF_CUSTOM_PROMPT: "",
    CONF_TITLE_OVERLAY: False,
    CONF_PLAYLISTS: PLAYLISTS_SMART,
    CONF_EXTRA_PLAYLISTS: [],
    CONF_KEEP_EXISTING_ARTWORK: True,
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
async def provider(tmp_path: Path) -> AsyncGenerator[AIArtworkProvider]:
    """Return an initialized provider with mocked image and text engines selected."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()
    with (
        patch(
            "music_assistant.providers.ai_artwork.select_image_engine",
            AsyncMock(return_value=provider.test_engine),  # type: ignore[attr-defined]
        ),
        patch(
            "music_assistant.providers.ai_artwork.select_ai_engine",
            AsyncMock(return_value=provider.test_text_engine),  # type: ignore[attr-defined]
        ),
    ):
        yield provider


async def test_generates_and_stores_a_cover_in_front_of_the_other_images(
    provider: AIArtworkProvider,
) -> None:
    """A smart playlist gets a cover, placed first so it is the one shown."""
    collage = _image("collage.jpg", provider="playlist_metadata")
    playlist = _smart_playlist(images=[collage])

    await provider.get_playlist_metadata(playlist)

    images = list(playlist.metadata.images or [])
    assert images[0].provider == INSTANCE_ID
    assert images[0].type == ImageType.THUMB
    assert images[1] == collage
    assert playlist.image == images[0]
    stored = Path(provider._images_dir, images[0].path)
    with Image.open(stored) as cover:
        assert cover.size == (1024, 1024)
        assert cover.format == "JPEG"


async def test_spotify_prompt_matches_the_cronicle_script(provider: AIArtworkProvider) -> None:
    """The Spotify preset with the engine drawing the title reproduces the original prompt."""
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
    _set_config(provider, **{CONF_STYLE: "spotify"})

    await provider.get_playlist_metadata(playlist)

    assert _sent_prompt(provider) == (
        'Create square album cover art for a music playlist called "2000s Emo & Alt Rock". '
        "Seed songs/playlists that define the vibe: EMO Songs of the 2000s; Mid-2000s Alt Rock "
        "Swim; Are we in High School?. Era: 2000-2012, evoke that decade's look. Capture the "
        "mood of the playlist name as a striking visual scene or concept (do not depict real "
        "artists). Style: a Spotify editorial playlist cover. One striking full-bleed image "
        "(stylized photo or bold illustration) with a vivid duotone or saturated color wash "
        "that sets the mood, simple uncluttered composition, cohesive with a series of playlist "
        'covers. Typography: the playlist title "2000s Emo & Alt Rock" as the only text, set '
        "large in a bold modern geometric sans-serif (like Spotify's Circular or Gotham Black), "
        "tight letter spacing, clean and crisp, high contrast against the image, placed "
        "confidently in the upper-left or lower-left area. Spell the title exactly. No other "
        "text, subtitles, logos, watermarks or artist names. Full-bleed: the art fills the "
        "entire square edge to edge, no border, frame, mockup or margins."
    )


async def test_smart_playlist_rules_shape_the_prompt(provider: AIArtworkProvider) -> None:
    """Seeds, era, artists and genres of the rules all reach the prompt."""
    await provider.get_playlist_metadata(_smart_playlist())

    prompt = _sent_prompt(provider)
    assert "Innerbloom – RÜFÜS DU SOL; Midnight Sun – Lane 8, Jerro" in prompt
    assert "Era: 2015-2019" in prompt
    assert "Artists/genres: Ben Böhmer, RÜFÜS DU SOL, Lane 8, Jerro, Melodic House." in prompt


@pytest.mark.parametrize(
    ("style", "expected"),
    [
        ("spotify", "Spotify editorial playlist cover"),
        ("music_assistant", "Music Assistant's brand blues"),
        ("apple_music", "mesh gradient"),
    ],
)
async def test_each_style_has_its_own_prompt(
    provider: AIArtworkProvider, style: str, expected: str
) -> None:
    """Every preset sends its own art direction."""
    _set_config(provider, **{CONF_STYLE: style})

    await provider.get_playlist_metadata(_smart_playlist())

    assert expected in _sent_prompt(provider)


async def test_title_overlay_asks_for_artwork_without_text(provider: AIArtworkProvider) -> None:
    """When the title is drawn afterwards, the engine is told to leave text out."""
    _set_config(provider, **{CONF_TITLE_OVERLAY: True})

    await provider.get_playlist_metadata(_smart_playlist())

    prompt = _sent_prompt(provider)
    assert "No text, letters, numbers" in prompt
    assert "Typography" not in prompt


async def test_custom_template_fills_known_placeholders(provider: AIArtworkProvider) -> None:
    """A custom prompt gets the playlist values, unknown placeholders stay as typed."""
    _set_config(
        provider,
        **{CONF_STYLE: "custom", CONF_CUSTOM_PROMPT: "Poster for {name} ({era}) {genres} {nope}"},
    )

    await provider.get_playlist_metadata(_smart_playlist(name="Sunrise Trance"))

    assert _sent_prompt(provider) == "Poster for Sunrise Trance (2015-2019) Melodic House {nope}"


def test_format_template_takes_unbalanced_braces_literally() -> None:
    """A template a formatter can not parse is sent as written rather than failing."""
    assert _format_template("cover {name", {"name": "x"}) == "cover {name"


async def test_a_stored_cover_is_reused_without_calling_the_engine(
    provider: AIArtworkProvider,
) -> None:
    """The second refresh of an unchanged playlist does not pay for a new image."""
    playlist = _smart_playlist()
    await provider.get_playlist_metadata(playlist)
    first = playlist.image

    await provider.get_playlist_metadata(playlist)

    assert _engine_calls(provider) == 1
    assert playlist.image == first
    assert [img.provider for img in playlist.metadata.images or []] == [INSTANCE_ID]


async def test_changing_the_style_replaces_the_cover(provider: AIArtworkProvider) -> None:
    """A new style generates a new cover and removes the old one, file included."""
    playlist = _smart_playlist()
    await provider.get_playlist_metadata(playlist)
    old = playlist.image
    assert old is not None

    _set_config(provider, **{CONF_STYLE: "apple_music"})
    await provider.get_playlist_metadata(playlist)

    assert _engine_calls(provider) == 2
    assert playlist.image is not None
    assert playlist.image.path != old.path
    assert old not in (playlist.metadata.images or [])
    assert not Path(provider._images_dir, old.path).exists()


async def test_renaming_the_playlist_replaces_the_cover(provider: AIArtworkProvider) -> None:
    """The title is part of the artwork, so a new name needs a new cover."""
    playlist = _smart_playlist()
    await provider.get_playlist_metadata(playlist)
    playlist.name = "Renamed"

    await provider.get_playlist_metadata(playlist)

    assert _engine_calls(provider) == 2


async def test_regular_playlists_need_to_be_opted_in(provider: AIArtworkProvider) -> None:
    """Only smart playlists get a cover unless a playlist is picked or all are included."""
    playlist = _playlist("builtin", item_id="42", is_editable=True)
    await provider.get_playlist_metadata(playlist)
    assert _engine_calls(provider) == 0

    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    await provider.get_playlist_metadata(playlist)
    assert _engine_calls(provider) == 1

    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: [], CONF_PLAYLISTS: PLAYLISTS_ALL})
    other = _playlist("builtin", item_id="43", is_editable=True)
    await provider.get_playlist_metadata(other)
    assert _engine_calls(provider) == 2


async def test_a_playlist_leaving_the_scope_loses_its_cover(provider: AIArtworkProvider) -> None:
    """Opting a playlist out takes its generated cover off again."""
    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    playlist = _playlist("builtin", item_id="42", is_editable=True)
    await provider.get_playlist_metadata(playlist)
    assert playlist.image is not None

    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: []})
    await provider.get_playlist_metadata(playlist)

    assert playlist.image is None


async def test_builtin_playlists_that_can_not_be_edited_are_skipped(
    provider: AIArtworkProvider,
) -> None:
    """Music Assistant's own generated playlists keep their static artwork."""
    _set_config(provider, **{CONF_PLAYLISTS: PLAYLISTS_ALL})

    await provider.get_playlist_metadata(_playlist("builtin", is_editable=False))

    assert _engine_calls(provider) == 0


async def test_existing_artwork_is_kept(provider: AIArtworkProvider) -> None:
    """A cover of the playlist's own (service artwork, an added image) is left alone."""
    own_cover = _image("/data/playlist_art/234_1791525935.jpg", provider="builtin")
    playlist = _smart_playlist(images=[own_cover])

    await provider.get_playlist_metadata(playlist)

    assert _engine_calls(provider) == 0
    assert list(playlist.metadata.images or []) == [own_cover]


async def test_existing_artwork_is_replaced_when_not_kept(provider: AIArtworkProvider) -> None:
    """With keeping turned off, the generated cover goes in front of the existing one."""
    _set_config(provider, **{CONF_KEEP_EXISTING_ARTWORK: False})
    own_cover = _image("/data/playlist_art/234_1791525935.jpg", provider="builtin")
    playlist = _smart_playlist(images=[own_cover])

    await provider.get_playlist_metadata(playlist)

    images = list(playlist.metadata.images or [])
    assert images[0].provider == INSTANCE_ID
    assert images[1] == own_cover


async def test_the_hourly_limit_postpones_generation(provider: AIArtworkProvider) -> None:
    """Covers over the limit are not generated now but scheduled for later."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1})
    await provider.get_playlist_metadata(_smart_playlist(item_id="1"))
    second = _smart_playlist(item_id="2")

    await provider.get_playlist_metadata(second)

    assert _engine_calls(provider) == 1
    assert second.image is None
    delay = _mass(provider).call_later.call_args.args[0]
    assert 3500 < delay <= 3605
    assert _mass(provider).call_later.call_args.args[2] == second.uri


async def test_a_failing_engine_leaves_the_playlist_untouched(
    provider: AIArtworkProvider,
) -> None:
    """An engine error is logged, not raised into the metadata refresh."""
    provider.test_engine.provider.ai_generate_image = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("quota exceeded")
    )
    collage = _image("collage.jpg", provider="playlist_metadata")
    playlist = _smart_playlist(images=[collage])

    await provider.get_playlist_metadata(playlist)

    assert list(playlist.metadata.images or []) == [collage]
    assert list(Path(provider._images_dir).iterdir()) == []


async def test_without_an_image_engine_nothing_is_generated(tmp_path: Path) -> None:
    """With no engine available the provider does nothing."""
    provider = _make_provider(tmp_path)
    await provider.handle_async_init()
    with (
        patch(
            "music_assistant.providers.ai_artwork.select_image_engine",
            AsyncMock(return_value=None),
        ),
        patch(
            "music_assistant.providers.ai_artwork.select_ai_engine", AsyncMock(return_value=None)
        ),
    ):
        await provider.get_playlist_metadata(_smart_playlist())

    assert _engine_calls(provider) == 0


async def test_the_prompt_engine_writes_the_visual_concept(provider: AIArtworkProvider) -> None:
    """With a prompt engine, its scene description replaces the generic concept sentence."""
    _set_config(provider, **{CONF_USE_PROMPT_ENGINE: True, CONF_DESCRIPTIONS: False})
    provider.test_text_engine.provider.ai_query = AsyncMock(  # type: ignore[attr-defined]
        return_value=' "A neon pier at dusk." '
    )

    await provider.get_playlist_metadata(_smart_playlist())

    prompt = _sent_prompt(provider)
    assert "Visual concept: A neon pier at dusk (do not depict real artists)." in prompt
    assert "Capture the mood" not in prompt


async def test_regular_playlist_context_comes_from_its_tracks(
    provider: AIArtworkProvider,
) -> None:
    """A playlist without rules is described by its most common artists and genres."""
    _set_config(provider, **{CONF_EXTRA_PLAYLISTS: ["42"]})
    tracks = [
        _track("Poker Face", "Lady Gaga", {"Pop"}),
        _track("Just Dance", "Lady Gaga", {"Pop"}),
        _track("Vogue", "Madonna", {"Dance"}),
    ]
    _mass(provider).music.playlists.tracks = MagicMock(return_value=_async_iter(tracks))

    await provider.get_playlist_metadata(_playlist("builtin", item_id="42", is_editable=True))

    prompt = _sent_prompt(provider)
    assert (
        "Seed songs/playlists that define the vibe: Poker Face – Lady Gaga; Vogue – Madonna."
        in (prompt)
    )
    assert "Artists/genres: Lady Gaga, Madonna, Pop, Dance." in prompt


async def test_resolve_image_returns_a_stored_cover(provider: AIArtworkProvider) -> None:
    """A stored cover is served from the images directory."""
    Path(provider._images_dir, "1_abc.jpg").write_bytes(b"jpeg")

    assert await provider.resolve_image("1_abc.jpg") == b"jpeg"


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "sub/../../secret.txt"])
async def test_resolve_image_rejects_paths_outside_the_images_dir(
    provider: AIArtworkProvider, path: str
) -> None:
    """A path from the library must never reach outside our directory."""
    Path(provider._images_dir).parent.joinpath("secret.txt").write_text("secret")

    with pytest.raises(FileNotFoundError):
        await provider.resolve_image(path)


async def test_scheduled_refresh_generates_missing_covers_and_removes_orphans(
    provider: AIArtworkProvider,
) -> None:
    """The hourly pass refreshes playlists lacking their cover and deletes unreferenced files."""
    orphan = Path(provider._images_dir, "9_dead.jpg")
    orphan.write_bytes(b"old")
    os.utime(orphan, (time.time() - 3600, time.time() - 3600))
    fresh_orphan = Path(provider._images_dir, "9_new.jpg")
    fresh_orphan.write_bytes(b"new")
    missing = _smart_playlist(item_id="1")
    out_of_scope = _playlist("builtin", item_id="2", is_editable=True)
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        return_value=_async_iter([missing, out_of_scope])
    )
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._refresh_covers()

    _mass(provider).metadata.update_metadata.assert_awaited_once_with(missing, force_refresh=True)
    assert not orphan.exists()
    # written moments ago, so possibly not stored in the library yet
    assert fresh_orphan.exists()


async def test_writes_a_description_for_a_smart_playlist(provider: AIArtworkProvider) -> None:
    """A smart playlist gets a description from its rules and the listener's play counts."""
    _mass(provider).music.database.get_rows_from_query = AsyncMock(
        return_value=[{"artist": "Lane 8", "track": "Brightest Lights", "plays": 42}]
    )
    playlist = _smart_playlist()
    playlist.metadata.description = "[Smart Playlist] Similar to Innerbloom"

    await provider.get_playlist_metadata(playlist)

    assert playlist.metadata.description == DESCRIPTION
    query = str(provider.test_text_engine.provider.ai_query.await_args.args[0])  # type: ignore[attr-defined]
    assert 'a personal playlist called "Indie Dance Sunset"' in query
    assert (
        "What defines it: Innerbloom – RÜFÜS DU SOL; Midnight Sun – Lane 8, Jerro "
        "(genres Melodic House) (years 2015-2019)"
    ) in query
    assert "(use for flavor, mention at most one): Lane 8 - Brightest Lights (42 plays)" in query
    assert "140 characters max" in query
    artists = _mass(provider).music.database.get_rows_from_query.await_args.args[1]["artists"]
    assert artists == ["Ben Böhmer", "RÜFÜS DU SOL", "Lane 8", "Jerro"]


async def test_a_stored_description_is_reapplied_without_calling_the_engine(
    provider: AIArtworkProvider,
) -> None:
    """A description replaced by something else is put back from the cache."""
    await provider.get_playlist_metadata(_smart_playlist())
    playlist = _smart_playlist()
    playlist.metadata.description = "[Smart Playlist] Similar to Innerbloom"

    await provider.get_playlist_metadata(playlist)

    assert playlist.metadata.description == DESCRIPTION
    assert _text_calls(provider) == 1


async def test_renaming_the_playlist_rewrites_the_description(
    provider: AIArtworkProvider,
) -> None:
    """The stored description belongs to the playlist name it was written for."""
    await provider.get_playlist_metadata(_smart_playlist())

    await provider.get_playlist_metadata(_smart_playlist(name="Sunrise Set"))

    assert _text_calls(provider) == 2


async def test_descriptions_can_be_turned_off(provider: AIArtworkProvider) -> None:
    """With descriptions off the playlist keeps its own."""
    _set_config(provider, **{CONF_DESCRIPTIONS: False})
    playlist = _smart_playlist()
    playlist.metadata.description = "Mine"

    await provider.get_playlist_metadata(playlist)

    assert playlist.metadata.description == "Mine"
    assert _text_calls(provider) == 0


async def test_the_hourly_limit_postpones_descriptions(provider: AIArtworkProvider) -> None:
    """Descriptions over the limit are left to a later refresh."""
    _set_config(provider, **{CONF_MAX_PER_HOUR: 1})
    await provider.get_playlist_metadata(_smart_playlist(item_id="1"))
    second = _smart_playlist(item_id="2")

    await provider.get_playlist_metadata(second)

    assert _text_calls(provider) == 1
    assert second.metadata.description is None


async def test_a_failing_text_engine_leaves_the_description_untouched(
    provider: AIArtworkProvider,
) -> None:
    """A text engine error is logged, not raised into the metadata refresh."""
    provider.test_text_engine.provider.ai_query = AsyncMock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("quota exceeded")
    )
    playlist = _smart_playlist()
    playlist.metadata.description = "Mine"

    await provider.get_playlist_metadata(playlist)

    assert playlist.metadata.description == "Mine"


async def test_regenerate_makes_new_covers_and_descriptions(
    provider: AIArtworkProvider,
) -> None:
    """The regenerate action keys every cover and description anew, and starts a refresh."""
    first = _smart_playlist()
    await provider.get_playlist_metadata(first)
    _mass(provider).create_task = MagicMock(side_effect=lambda coro, **_kw: coro.close())

    with patch("music_assistant.providers.ai_artwork.time.time", return_value=2_000_000_000.0):
        await provider.handle_config_action(CONF_ACTION_REGENERATE)
    second = _smart_playlist(images=list(first.metadata.images or []))
    await provider.get_playlist_metadata(second)

    assert _engine_calls(provider) == 2
    assert _text_calls(provider) == 2
    assert second.image is not None
    assert first.image is not None
    assert second.image.path != first.image.path
    _mass(provider).create_task.assert_called_once()


async def test_scheduled_refresh_puts_back_replaced_descriptions(
    provider: AIArtworkProvider,
) -> None:
    """A playlist whose cover is current is only refreshed when its description was replaced."""
    current = _smart_playlist(item_id="1")
    await provider.get_playlist_metadata(current)
    replaced = _smart_playlist(item_id="1")
    replaced.metadata.images = current.metadata.images
    replaced.metadata.description = "[Smart Playlist] Similar to Innerbloom"
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        side_effect=lambda: _async_iter([current])
    )
    _mass(provider).metadata.update_metadata = AsyncMock()

    await provider._refresh_covers()
    _mass(provider).music.playlists.iter_library_items = MagicMock(
        return_value=_async_iter([replaced])
    )
    await provider._refresh_covers()

    _mass(provider).metadata.update_metadata.assert_awaited_once_with(replaced, force_refresh=True)


def _make_provider(tmp_path: Path) -> AIArtworkProvider:
    """Construct the provider on a mocked Music Assistant."""
    mass = MagicMock()
    mass.storage_path = str(tmp_path / "data")
    mass.call_later = MagicMock()
    mass.cache = _FakeCache()
    mass.music.database.get_rows_from_query = AsyncMock(return_value=[])
    mass.music.playlists.tracks = MagicMock(side_effect=lambda *_: _async_iter([]))
    smart_playlist_provider = MagicMock()
    smart_playlist_provider.domain = "smart_playlist"
    smart_playlist_provider.get_smart_playlist_rules = AsyncMock(return_value=SMART_RULES)
    collage_provider = MagicMock()
    collage_provider.domain = "playlist_metadata"
    builtin_provider = MagicMock()
    builtin_provider.domain = "builtin"
    providers = {
        "smart_playlist": smart_playlist_provider,
        "playlist_metadata": collage_provider,
        "builtin": builtin_provider,
    }
    mass.get_provider = MagicMock(side_effect=lambda instance_id: providers.get(instance_id))

    manifest = MagicMock()
    manifest.domain = "ai_artwork"
    config = MagicMock()
    config.instance_id = INSTANCE_ID
    config.get_value = MagicMock(
        side_effect=lambda key, default=None: DEFAULT_CONFIG.get(key, default)
    )
    provider = AIArtworkProvider(mass, manifest, config, set())
    _set_config(provider)

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


def _set_config(provider: AIArtworkProvider, **overrides: Any) -> None:
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


def _mass(provider: AIArtworkProvider) -> MagicMock:
    """Return the mocked Music Assistant of the provider."""
    return cast("MagicMock", provider.mass)


def _text_calls(provider: AIArtworkProvider) -> int:
    """Return how often the text engine was asked for a description."""
    return int(provider.test_text_engine.provider.ai_query.await_count)  # type: ignore[attr-defined]


def _engine_calls(provider: AIArtworkProvider) -> int:
    """Return how often the image engine was asked for an image."""
    return int(provider.test_engine.provider.ai_generate_image.await_count)  # type: ignore[attr-defined]


def _sent_prompt(provider: AIArtworkProvider) -> str:
    """Return the prompt of the last image request."""
    return str(provider.test_engine.provider.ai_generate_image.await_args.args[0])  # type: ignore[attr-defined]


async def _async_iter(items: list[Any]) -> AsyncGenerator[Any]:
    """Yield the given items asynchronously."""
    for item in items:
        yield item
