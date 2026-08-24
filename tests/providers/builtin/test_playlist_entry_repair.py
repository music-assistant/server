"""Tests for detecting playlist entries that lost their manually set name or artwork."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import ImageType, MediaType

from music_assistant.helpers.playlists import (
    ArtistInfo,
    ImageInfo,
    PlaylistItem,
    ProviderMappingInfo,
)
from music_assistant.providers.builtin import BuiltinProvider, _is_orphaned_entry_path
from music_assistant.providers.builtin.constants import StoredItem

STREAM_URL = "http://stream.example.com/live"


def _make_provider() -> BuiltinProvider:
    """Create a minimal BuiltinProvider with a mocked manifest and mass."""
    prov = object.__new__(BuiltinProvider)
    prov.mass = MagicMock()
    prov.logger = MagicMock()
    prov.manifest = MagicMock()
    prov.manifest.domain = "builtin"
    return prov


def _make_entry(
    name: str | None = "My Station",
    image_path: str | None = None,
    domain: str = "builtin",
    media_type: str = MediaType.RADIO.value,
) -> PlaylistItem:
    """Create a playlist entry as parsed from an M3U file."""
    metadata = {"media_type": media_type}
    if name is not None:
        metadata["name"] = name
    return PlaylistItem(
        path=f"builtin://radio/{STREAM_URL}",
        title=name,
        metadata=metadata,
        providers=[ProviderMappingInfo(domain=domain, item_id=STREAM_URL)],
        images=[ImageInfo(type=ImageType.THUMB.value, path=image_path, provider="builtin")]
        if image_path
        else [],
    )


def _stored(
    name: str = "My Station", image_url: str | None = None
) -> dict[str, dict[str, StoredItem]]:
    """Create the per-media-type lookup of stored items keyed on item_id."""
    stored_item = StoredItem(item_id=STREAM_URL, name=name)
    if image_url:
        stored_item["image_url"] = image_url
    return {
        MediaType.RADIO.value: {STREAM_URL: stored_item},
        MediaType.TRACK.value: {},
    }


def test_no_builtin_mapping_is_never_stale() -> None:
    """An entry without a builtin #EXTPROV is not ours to repair."""
    prov = _make_provider()

    assert prov._stored_details_differ(_make_entry(domain="spotify"), _stored()) is False


def test_no_stored_item_is_never_stale() -> None:
    """An entry that was not added manually has no stored details to compare against."""
    prov = _make_provider()
    lookup: dict[str, dict[str, StoredItem]] = {
        MediaType.RADIO.value: {},
        MediaType.TRACK.value: {},
    }

    assert prov._stored_details_differ(_make_entry(), lookup) is False


def test_other_media_type_is_never_stale() -> None:
    """Only manually added tracks and radio stations carry stored details."""
    prov = _make_provider()

    entry = _make_entry(media_type=MediaType.PODCAST_EPISODE.value)
    assert prov._stored_details_differ(entry, _stored()) is False


def test_matching_details_are_not_stale() -> None:
    """An entry that already carries the stored name and image is left alone."""
    prov = _make_provider()

    entry = _make_entry(name="My Station", image_path="http://img.example.com/logo.png")
    lookup = _stored(name="My Station", image_url="http://img.example.com/logo.png")
    assert prov._stored_details_differ(entry, lookup) is False


def test_differing_name_is_stale() -> None:
    """An entry holding the name broadcast by the stream needs repair."""
    prov = _make_provider()

    entry = _make_entry(name="ICY BROADCAST NAME")
    assert prov._stored_details_differ(entry, _stored(name="My Station")) is True


def test_missing_stored_image_is_stale() -> None:
    """An entry that lost the manually set artwork needs repair."""
    prov = _make_provider()

    entry = _make_entry(image_path=STREAM_URL)
    lookup = _stored(image_url="http://img.example.com/logo.png")
    assert prov._stored_details_differ(entry, lookup) is True


def test_stored_image_as_another_type_is_stale() -> None:
    """The stored image only counts as present when it is the thumbnail that shows."""
    prov = _make_provider()
    entry = _make_entry(image_path=STREAM_URL)
    entry.images.append(
        ImageInfo(
            type=ImageType.FANART.value, path="http://img.example.com/logo.png", provider="builtin"
        )
    )

    lookup = _stored(image_url="http://img.example.com/logo.png")
    assert prov._stored_details_differ(entry, lookup) is True


def test_stored_item_without_image_is_not_stale() -> None:
    """Cover art from the stream stays as long as the user set no image of their own."""
    prov = _make_provider()

    entry = _make_entry(image_path=STREAM_URL)
    assert prov._stored_details_differ(entry, _stored()) is False


def test_restore_writes_stored_name_and_image() -> None:
    """Restoring a radio entry puts the stored name in both #EXTMA and #EXTINF."""
    prov = _make_provider()
    entry = _make_entry(name="ICY BROADCAST NAME", image_path=STREAM_URL)
    lookup = _stored(image_url="http://img.example.com/logo.png")

    prov._restore_stored_details(entry, lookup)

    assert entry.metadata is not None
    assert entry.metadata["name"] == "My Station"
    assert entry.title == "My Station"
    assert [(x.path, x.remotely_accessible) for x in entry.images] == [
        ("http://img.example.com/logo.png", True)
    ]
    # a repaired entry must not be picked up again, or every run rewrites the file
    assert prov._stored_details_differ(entry, lookup) is False


def test_a_padded_stored_name_settles() -> None:
    """An M3U file cannot hold surrounding whitespace, so it must not count as a difference."""
    prov = _make_provider()
    entry = _make_entry(name="My Station")

    assert prov._stored_details_differ(entry, _stored(name="  My Station  ")) is False


def test_restore_keeps_non_thumb_images() -> None:
    """Restoring the artwork replaces the thumbnail but keeps other image types."""
    prov = _make_provider()
    entry = _make_entry(image_path=STREAM_URL)
    entry.images.append(
        ImageInfo(type=ImageType.FANART.value, path="http://img.example.com/art.png", provider="x")
    )

    prov._restore_stored_details(entry, _stored(image_url="http://img.example.com/logo.png"))

    assert [x.type for x in entry.images] == [ImageType.THUMB.value, ImageType.FANART.value]


def test_restore_rebuilds_a_track_title_from_its_artists() -> None:
    """A track's #EXTINF holds "<artists> - <name>", the way the entry was written."""
    prov = _make_provider()
    entry = _make_entry(name="Some Artist - Old Name", media_type=MediaType.TRACK.value)
    entry.artists = [
        ArtistInfo(
            name="Some Artist", provider_domain="builtin", item_id="x", provider_instance="builtin"
        )
    ]
    stored_item = StoredItem(item_id=STREAM_URL, name="My Song")
    lookup = {MediaType.RADIO.value: {}, MediaType.TRACK.value: {STREAM_URL: stored_item}}

    prov._restore_stored_details(entry, lookup)

    assert entry.metadata is not None
    assert entry.metadata["name"] == "My Song"
    assert entry.title == "Some Artist - My Song"
    assert prov._stored_details_differ(entry, lookup) is False


def test_restore_without_stored_item_changes_nothing() -> None:
    """An entry that was not added manually is left untouched."""
    prov = _make_provider()
    entry = _make_entry(name="Broadcast Name")
    lookup: dict[str, dict[str, StoredItem]] = {
        MediaType.RADIO.value: {},
        MediaType.TRACK.value: {},
    }

    prov._restore_stored_details(entry, lookup)

    assert entry.metadata is not None
    assert entry.metadata["name"] == "Broadcast Name"
    assert entry.title == "Broadcast Name"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # leftover text from a title/name that once contained a line break
        ("elyn Brown)", True),
        ("Some Song Title", True),
        # real references, which all carry a URI, URL or path separator
        ("spotify://track/abc123", False),
        ("spotify:track:abc123", False),
        ("builtin://radio/http://stream.example.com/live", False),
        ("https://stream.example.com/live", False),
        ("/media/music/song.mp3", False),
        ("Music/song.mp3", False),
        ("C:\\Music\\song.mp3", False),
    ],
)
def test_orphaned_entry_path_detection(path: str, expected: bool) -> None:
    """Only leftover text is treated as an orphan, never a resolvable reference."""
    assert _is_orphaned_entry_path(path) is expected
