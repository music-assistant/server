"""Tests for Tag Player plugin."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping

from music_assistant.providers.tagplayer import TagPlayerProvider


def _async_iter(items: list[MagicMock]) -> Callable[..., AsyncGenerator[MagicMock]]:
    """Return a stub for iter_library_items_by_prov_id yielding the given items."""

    async def _iter(*_args: Any, **_kwargs: Any) -> AsyncGenerator[MagicMock]:
        for item in items:
            yield item

    return _iter


def _mock_library_item(
    item_id: int,
    name: str,
    media_type: MediaType,
    provider_mappings: set[ProviderMapping] | None = None,
) -> MagicMock:
    """Create a mock library item with the given properties."""
    item = MagicMock()
    item.item_id = str(item_id)
    item.name = name
    item.media_type = media_type
    item.provider_mappings = provider_mappings or set()
    return item


def _mock_controller(
    media_type: MediaType,
    items_by_prov_id: dict[str, MagicMock] | None = None,
    provider_items: list[MagicMock] | None = None,
) -> MagicMock:
    """
    Create a mock media type controller.

    :param media_type: The media type this controller handles.
    :param items_by_prov_id: Map of prov_item_id -> library item for get_library_item_by_prov_id.
    :param provider_items: Items yielded by iter_library_items_by_prov_id().
    """
    ctrl = MagicMock()
    ctrl.media_type = media_type

    prov_id_map = items_by_prov_id or {}

    async def _get_by_prov_id(
        item_id: str,
        provider_instance_id_or_domain: str,  # noqa: ARG001
    ) -> MagicMock | None:
        return prov_id_map.get(item_id)

    ctrl.get_library_item_by_prov_id = AsyncMock(side_effect=_get_by_prov_id)
    ctrl.get_library_item = AsyncMock()
    ctrl.add_provider_mapping = AsyncMock()
    ctrl.remove_provider_mapping = AsyncMock()
    ctrl.iter_library_items_by_prov_id = _async_iter(provider_items or [])
    return ctrl


@pytest.fixture
def mock_mass() -> MagicMock:
    """Create a mock MusicAssistant instance."""
    mass = MagicMock()
    mass.register_api_command = MagicMock(side_effect=lambda *_a, **_kw: MagicMock())

    # default empty controllers for all taggable types
    controllers: dict[MediaType, MagicMock] = {}
    for mt in (
        MediaType.TRACK,
        MediaType.ALBUM,
        MediaType.PLAYLIST,
        MediaType.ARTIST,
        MediaType.RADIO,
        MediaType.AUDIOBOOK,
        MediaType.PODCAST,
        MediaType.GENRE,
    ):
        controllers[mt] = _mock_controller(mt)

    mass.music = MagicMock()
    mass.music.get_controller = MagicMock(side_effect=lambda mt: controllers[mt])

    mass.player_queues = MagicMock()
    mass.player_queues.play_media = AsyncMock()

    # expose controllers dict for per-test customization
    mass._test_controllers = controllers
    return mass


@pytest.fixture
def mock_manifest() -> MagicMock:
    """Create a mock manifest."""
    manifest = MagicMock()
    manifest.domain = "tagplayer"
    manifest.name = "Tag Player"
    return manifest


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock config."""
    config = MagicMock()
    config.instance_id = "tagplayer"
    config.get_value.return_value = "GLOBAL"
    return config


@pytest.fixture
async def provider(
    mock_mass: MagicMock, mock_manifest: MagicMock, mock_config: MagicMock
) -> TagPlayerProvider:
    """Create a TagPlayerProvider instance with API commands registered."""
    prov = TagPlayerProvider(mock_mass, mock_manifest, mock_config, set())
    await prov.loaded_in_mass()
    return prov


class TestLifecycle:
    """Tests for provider lifecycle."""

    async def test_registers_five_commands(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """All five API commands should be registered on load."""
        assert mock_mass.register_api_command.call_count == 5
        commands = [c[0][0] for c in mock_mass.register_api_command.call_args_list]
        assert sorted(commands) == [
            "tagplayer/get",
            "tagplayer/link",
            "tagplayer/list",
            "tagplayer/play",
            "tagplayer/unlink",
        ]

    async def test_unload_deregisters_commands(self, provider: TagPlayerProvider) -> None:
        """Unload should call every unregister handle and clear the list."""
        handles = [cast("MagicMock", h) for h in provider._unregister_commands]
        assert len(handles) == 5
        await provider.unload()
        for handle in handles:
            handle.assert_called_once()
        assert len(provider._unregister_commands) == 0


class TestLinkTag:
    """Tests for the tagplayer/link command."""

    async def test_link_creates_provider_mapping(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Linking a tag should add a provider mapping with available=False."""
        ctrl = mock_mass._test_controllers[MediaType.TRACK]
        mock_item = _mock_library_item(42, "Test Track", MediaType.TRACK)
        ctrl.get_library_item.return_value = mock_item

        result = await provider.link_tag("nfc-001", "track/42")

        assert result["tag_id"] == "nfc-001"
        assert result["uri"] == "tagplayer://track/nfc-001"

        ctrl.add_provider_mapping.assert_called_once()
        call_args = ctrl.add_provider_mapping.call_args
        assert call_args[0][0] == 42  # library_id
        mapping: ProviderMapping = call_args[0][1]
        assert mapping.item_id == "nfc-001"
        assert mapping.provider_domain == "tagplayer"
        assert mapping.provider_instance == "tagplayer"
        assert mapping.available is False

    async def test_link_replaces_existing_mapping(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Re-linking a tag should remove the old mapping first."""
        # existing tag is on a playlist
        old_item = _mock_library_item(10, "Old Playlist", MediaType.PLAYLIST)
        playlist_ctrl = mock_mass._test_controllers[MediaType.PLAYLIST]
        playlist_ctrl.get_library_item_by_prov_id = AsyncMock(return_value=old_item)

        # new target is a track
        track_ctrl = mock_mass._test_controllers[MediaType.TRACK]
        new_item = _mock_library_item(42, "New Track", MediaType.TRACK)
        track_ctrl.get_library_item.return_value = new_item

        await provider.link_tag("nfc-001", "track/42")

        # old mapping should be removed
        playlist_ctrl.remove_provider_mapping.assert_called_once_with(10, "tagplayer", "nfc-001")
        # new mapping should be added
        track_ctrl.add_provider_mapping.assert_called_once()

    async def test_link_empty_tag_id_raises(self, provider: TagPlayerProvider) -> None:
        """Empty or whitespace-only tag_id should raise InvalidDataError."""
        with pytest.raises(InvalidDataError, match="tag_id cannot be empty"):
            await provider.link_tag("", "track/42")
        with pytest.raises(InvalidDataError, match="tag_id cannot be empty"):
            await provider.link_tag("   ", "track/42")

    async def test_link_invalid_target_raises(self, provider: TagPlayerProvider) -> None:
        """Invalid target format should raise InvalidDataError."""
        with pytest.raises(InvalidDataError, match="Invalid target format"):
            await provider.link_tag("nfc-001", "invalid")
        with pytest.raises(InvalidDataError, match="Invalid target format"):
            await provider.link_tag("nfc-001", "track/not-a-number")


class TestUnlinkTag:
    """Tests for the tagplayer/unlink command."""

    async def test_unlink_removes_mapping(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Unlinking should remove the provider mapping from the library item."""
        mock_item = _mock_library_item(42, "Test Track", MediaType.TRACK)
        ctrl = mock_mass._test_controllers[MediaType.TRACK]
        ctrl.get_library_item_by_prov_id = AsyncMock(return_value=mock_item)

        result = await provider.unlink_tag("nfc-001")

        assert result["tag_id"] == "nfc-001"
        ctrl.remove_provider_mapping.assert_called_once_with(42, "tagplayer", "nfc-001")

    async def test_unlink_unknown_tag_raises(self, provider: TagPlayerProvider) -> None:
        """Unlinking a tag that doesn't exist should raise MediaNotFoundError."""
        with pytest.raises(MediaNotFoundError, match="Unknown tag"):
            await provider.unlink_tag("nonexistent")


class TestGetTag:
    """Tests for the tagplayer/get command."""

    async def test_get_returns_tag_info(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Getting a tag should return its mapping details."""
        mock_item = _mock_library_item(5, "Party Mix", MediaType.PLAYLIST)
        ctrl = mock_mass._test_controllers[MediaType.PLAYLIST]
        ctrl.get_library_item_by_prov_id = AsyncMock(return_value=mock_item)

        result = await provider.get_tag("party-mix")

        assert result["tag_id"] == "party-mix"
        assert result["media_type"] == "playlist"
        assert result["item_id"] == 5
        assert result["name"] == "Party Mix"
        assert result["uri"] == "tagplayer://playlist/party-mix"

    async def test_get_unknown_tag_raises(self, provider: TagPlayerProvider) -> None:
        """Getting a tag that doesn't exist should raise MediaNotFoundError."""
        with pytest.raises(MediaNotFoundError, match="Unknown tag"):
            await provider.get_tag("nonexistent")


class TestListTags:
    """Tests for the tagplayer/list command."""

    async def test_list_returns_all_tags(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Listing should return tags across all media types."""
        tag_mapping = ProviderMapping(
            item_id="nfc-001",
            provider_domain="tagplayer",
            provider_instance="tagplayer",
            available=False,
        )
        other_mapping = ProviderMapping(
            item_id="spotify:abc",
            provider_domain="spotify",
            provider_instance="spotify--xyz",
            available=True,
        )
        track = _mock_library_item(
            42,
            "Tagged Track",
            MediaType.TRACK,
            provider_mappings={tag_mapping, other_mapping},
        )
        mock_mass._test_controllers[MediaType.TRACK].iter_library_items_by_prov_id = _async_iter(
            [track]
        )

        result = await provider.list_tags()

        assert len(result) == 1
        assert result[0]["tag_id"] == "nfc-001"
        assert result[0]["media_type"] == "track"
        assert result[0]["item_id"] == 42
        assert result[0]["name"] == "Tagged Track"

    async def test_list_empty(self, provider: TagPlayerProvider) -> None:
        """Listing with no tags should return an empty list."""
        result = await provider.list_tags()
        assert result == []

    async def test_list_across_media_types(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Tags from different media types should all appear."""
        track_tag = ProviderMapping(
            item_id="tag-a",
            provider_domain="tagplayer",
            provider_instance="tagplayer",
            available=False,
        )
        playlist_tag = ProviderMapping(
            item_id="tag-b",
            provider_domain="tagplayer",
            provider_instance="tagplayer",
            available=False,
        )
        track = _mock_library_item(1, "Track", MediaType.TRACK, {track_tag})
        playlist = _mock_library_item(2, "Playlist", MediaType.PLAYLIST, {playlist_tag})

        mock_mass._test_controllers[MediaType.TRACK].iter_library_items_by_prov_id = _async_iter(
            [track]
        )
        mock_mass._test_controllers[MediaType.PLAYLIST].iter_library_items_by_prov_id = _async_iter(
            [playlist]
        )

        result = await provider.list_tags()

        assert len(result) == 2
        tag_ids = {t["tag_id"] for t in result}
        assert tag_ids == {"tag-a", "tag-b"}


class TestPlayTag:
    """Tests for the tagplayer/play command."""

    async def test_play_resolves_and_plays(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Playing a tag should resolve it and call play_media."""
        mock_item = _mock_library_item(42, "Test Track", MediaType.TRACK)
        ctrl = mock_mass._test_controllers[MediaType.TRACK]
        ctrl.get_library_item_by_prov_id = AsyncMock(return_value=mock_item)

        await provider.play_tag("nfc-001", "living_room")

        mock_mass.player_queues.play_media.assert_called_once_with(
            "living_room", mock_item, QueueOption.PLAY
        )

    async def test_play_with_queue_option(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Playing with a custom queue option should pass it through."""
        mock_item = _mock_library_item(42, "Test Track", MediaType.TRACK)
        ctrl = mock_mass._test_controllers[MediaType.TRACK]
        ctrl.get_library_item_by_prov_id = AsyncMock(return_value=mock_item)

        await provider.play_tag("nfc-001", "living_room", QueueOption.ADD)

        mock_mass.player_queues.play_media.assert_called_once_with(
            "living_room", mock_item, QueueOption.ADD
        )

    async def test_play_unknown_tag_raises(self, provider: TagPlayerProvider) -> None:
        """Playing a tag that doesn't exist should raise MediaNotFoundError."""
        with pytest.raises(MediaNotFoundError, match="Unknown tag"):
            await provider.play_tag("nonexistent", "living_room")


class TestFindTaggedItem:
    """Tests for the _find_tagged_item helper."""

    async def test_finds_item_in_first_matching_type(
        self, provider: TagPlayerProvider, mock_mass: MagicMock
    ) -> None:
        """Should return the first matching item across media types."""
        mock_item = _mock_library_item(7, "An Album", MediaType.ALBUM)
        mock_mass._test_controllers[MediaType.ALBUM].get_library_item_by_prov_id = AsyncMock(
            return_value=mock_item
        )

        result = await provider._find_tagged_item("album-tag")

        assert result is not None
        media_type, item = result
        assert media_type == MediaType.ALBUM
        assert item.name == "An Album"

    async def test_returns_none_when_not_found(self, provider: TagPlayerProvider) -> None:
        """Should return None when no media type has the tag."""
        result = await provider._find_tagged_item("nonexistent")
        assert result is None

    @pytest.mark.parametrize("tag_id", ["", "   "])
    async def test_empty_tag_id_raises(self, provider: TagPlayerProvider, tag_id: str) -> None:
        """Empty or whitespace-only tag_id should raise InvalidDataError for all tag commands."""
        with pytest.raises(InvalidDataError, match="tag_id cannot be empty"):
            await provider.unlink_tag(tag_id)
        with pytest.raises(InvalidDataError, match="tag_id cannot be empty"):
            await provider.get_tag(tag_id)
        with pytest.raises(InvalidDataError, match="tag_id cannot be empty"):
            await provider.play_tag(tag_id, "player1")


class TestParseTarget:
    """Tests for the _parse_target static method."""

    def test_track_target(self) -> None:
        """Should parse track targets."""
        media_type, item_id = TagPlayerProvider._parse_target("track/42")
        assert media_type == MediaType.TRACK
        assert item_id == 42

    def test_playlist_target(self) -> None:
        """Should parse playlist targets."""
        media_type, item_id = TagPlayerProvider._parse_target("playlist/5")
        assert media_type == MediaType.PLAYLIST
        assert item_id == 5

    def test_strips_leading_slash(self) -> None:
        """Should handle leading slashes."""
        media_type, item_id = TagPlayerProvider._parse_target("/album/10")
        assert media_type == MediaType.ALBUM
        assert item_id == 10

    def test_invalid_format_raises(self) -> None:
        """Should raise InvalidDataError for invalid formats."""
        with pytest.raises(InvalidDataError, match="Invalid target format"):
            TagPlayerProvider._parse_target("invalid")

    def test_non_integer_id_raises(self) -> None:
        """Should raise InvalidDataError for non-integer IDs."""
        with pytest.raises(InvalidDataError, match="Invalid target format"):
            TagPlayerProvider._parse_target("track/abc")

    def test_invalid_media_type_raises(self) -> None:
        """Should raise InvalidDataError for unknown media types."""
        with pytest.raises(InvalidDataError, match="Invalid target format"):
            TagPlayerProvider._parse_target("widget/42")
