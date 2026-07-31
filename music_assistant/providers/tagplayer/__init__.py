"""
Tag Player plugin for Music Assistant.

Links arbitrary identifiers (NFC tags, QR codes, etc.) to media items for quick playback.
Uses provider mappings on existing library items — no custom database tables.

Usage:
- Link tags via API: tagplayer/link with tag_id and target (e.g., "playlist/42")
- Play via API: tagplayer/play with tag_id and player_id
- Play via URI: tagplayer://<media_type>/<tag_id> (media type must match the linked item)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ProviderMapping

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType, ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Media types that support tag mappings
TAGGABLE_MEDIA_TYPES = (
    MediaType.TRACK,
    MediaType.ALBUM,
    MediaType.PLAYLIST,
    MediaType.ARTIST,
    MediaType.RADIO,
    MediaType.AUDIOBOOK,
    MediaType.PODCAST,
    MediaType.GENRE,
)


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return TagPlayerProvider(mass, manifest, config, set())


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return ()


class TagPlayerProvider(PluginProvider):
    """Tag Player plugin that links identifiers to existing library items."""

    _unregister_commands: list[Callable[[], None]]

    async def loaded_in_mass(self) -> None:
        """Register API commands after the provider is loaded."""
        self._unregister_commands = [
            self.mass.register_api_command(
                "tagplayer/link", self.link_tag, required_scope=Scope.LIBRARY_WRITE
            ),
            self.mass.register_api_command(
                "tagplayer/unlink", self.unlink_tag, required_scope=Scope.LIBRARY_WRITE
            ),
            self.mass.register_api_command(
                "tagplayer/get", self.get_tag, required_scope=Scope.LIBRARY_READ
            ),
            self.mass.register_api_command(
                "tagplayer/list", self.list_tags, required_scope=Scope.LIBRARY_READ
            ),
            self.mass.register_api_command(
                "tagplayer/play", self.play_tag, required_scope=Scope.QUEUES_CONTROL
            ),
        ]

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands and clean up."""
        for unregister in self._unregister_commands:
            unregister()
        self._unregister_commands.clear()

    async def link_tag(self, tag_id: str, target: str) -> dict[str, Any]:
        """
        Link a tag identifier to a library media item.

        Adds a provider mapping with available=False on the target item so the tag
        resolves to it via URI (tagplayer://<media_type>/<tag_id>) without interfering
        with streaming through the item's real providers.

        :param tag_id: The tag identifier (NFC serial, QR code content, etc.).
        :param target: Media path like "track/42" or "playlist/5".
        """
        self._validate_tag_id(tag_id)

        media_type, library_id = self.(target)
        controller = self.mass.music.get_controller(media_type)

        # verify the library item exists
        await controller.get_library_item(library_id)

        # check if this tag is already linked to a different item and unlink it
        existing = await self._find_tagged_item(tag_id)
        if existing is not None:
            old_type, old_item = existing
            old_ctrl = self.mass.music.get_controller(old_type)
            await old_ctrl.remove_provider_mapping(int(old_item.item_id), self.instance_id, tag_id)

        mapping = ProviderMapping(
            item_id=tag_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            available=False,
        )
        await controller.add_provider_mapping(library_id, mapping)

        uri = f"{self.instance_id}://{media_type.value}/{tag_id}"
        self.logger.debug(
            "Linked tag '%s' to %s/%d (URI: %s)", tag_id, media_type.value, library_id, uri
        )
        return {"tag_id": tag_id, "target": target, "uri": uri}

    async def unlink_tag(self, tag_id: str) -> dict[str, str]:
        """
        Remove a tag mapping.

        :param tag_id: The tag identifier to unlink.
        """
        result = await self._find_tagged_item(tag_id)
        if result is None:
            raise MediaNotFoundError(f"Unknown tag: {tag_id}")

        media_type, library_item = result
        controller = self.mass.music.get_controller(media_type)
        await controller.remove_provider_mapping(
            int(library_item.item_id), self.instance_id, tag_id
        )

        self.logger.debug("Unlinked tag '%s'", tag_id)
        return {"tag_id": tag_id}

    async def get_tag(self, tag_id: str) -> dict[str, Any]:
        """
        Get the mapping for a single tag.

        :param tag_id: The tag identifier to look up.
        """
        result = await self._find_tagged_item(tag_id)
        if result is None:
            raise MediaNotFoundError(f"Unknown tag: {tag_id}")

        media_type, library_item = result
        uri = f"{self.instance_id}://{media_type.value}/{tag_id}"
        return {
            "tag_id": tag_id,
            "media_type": media_type.value,
            "item_id": int(library_item.item_id),
            "name": library_item.name,
            "uri": uri,
        }

    async def list_tags(self) -> list[dict[str, Any]]:
        """List all tag mappings."""
        tags: list[dict[str, Any]] = []
        for media_type in TAGGABLE_MEDIA_TYPES:
            controller = self.mass.music.get_controller(media_type)
            async for item in controller.iter_library_items_by_prov_id(self.instance_id):
                for mapping in item.provider_mappings:
                    if mapping.provider_instance == self.instance_id:
                        tags.append(
                            {
                                "tag_id": mapping.item_id,
                                "media_type": media_type.value,
                                "item_id": int(item.item_id),
                                "name": item.name,
                                "uri": f"{self.instance_id}://{media_type.value}/{mapping.item_id}",
                            }
                        )
        return tags

    async def play_tag(
        self,
        tag_id: str,
        player_id: str,
        queue_option: QueueOption = QueueOption.PLAY,
    ) -> None:
        """
        Resolve a tag and play the linked media on a player.

        :param tag_id: The tag identifier to play.
        :param player_id: The player to play on.
        :param queue_option: How to add to queue (default: PLAY).
        """
        result = await self._find_tagged_item(tag_id)
        if result is None:
            raise MediaNotFoundError(f"Unknown tag: {tag_id}")

        media_type, library_item = result
        self.logger.debug(
            "Playing tag '%s' -> %s/%s on %s",
            tag_id,
            media_type.value,
            library_item.item_id,
            player_id,
        )
        await self.mass.player_queues.play_media(player_id, library_item, queue_option)

    async def _find_tagged_item(self, tag_id: str) -> tuple[MediaType, MediaItemType] | None:
        """
        Search all media types for a library item with the given tag mapping.

        :param tag_id: The tag identifier to search for.
        """
        self._validate_tag_id(tag_id)
        for media_type in TAGGABLE_MEDIA_TYPES:
            controller = self.mass.music.get_controller(media_type)
            if item := await controller.get_library_item_by_prov_id(tag_id, self.instance_id):
                return (media_type, item)
        return None

    @staticmethod
    def _validate_tag_id(tag_id: str) -> None:
        """
        Validate that a tag identifier is non-empty.

        :param tag_id: The tag identifier to validate.
        """
        if not tag_id or not tag_id.strip():
            raise ValueError("tag_id cannot be empty")

    @staticmethod
    def _parse_target(target: str) -> tuple[MediaType, int]:
        """
        Parse a target string like 'track/42' into (MediaType, library_id).

        :param target: Target string in the format "media_type/library_id".
        """
        msg = f"Invalid target format: {target}. Expected: type/id (e.g., track/42)"
        media_type_str, _, item_id_str = target.strip("/").partition("/")
        if not item_id_str.isdigit():
            raise InvalidDataError(msg)
        try:
            media_type = MediaType(media_type_str)
        except ValueError as err:
            raise InvalidDataError(msg) from err
        if media_type not in TAGGABLE_MEDIA_TYPES:
            raise InvalidDataError(msg)
        return (media_type, int(item_id_str))
