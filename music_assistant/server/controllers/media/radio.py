"""Manage MediaItems of type Radio."""

from __future__ import annotations

import asyncio

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, MediaType
from music_assistant.common.models.errors import InvalidDataError
from music_assistant.common.models.media_items import ItemMapping, Radio, Track
from music_assistant.constants import DB_TABLE_RADIOS
from music_assistant.server.helpers.compare import compare_strings, loose_compare_strings

from .base import MediaControllerBase


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = DB_TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self._db_add_lock = asyncio.Lock()
        # register api handlers
        self.mass.register_api_command("music/radio/library_items", self.library_items)
        self.mass.register_api_command("music/radio/get_radio", self.get)
        self.mass.register_api_command(
            "music/radio/update_item_in_library", self.update_item_in_library
        )
        self.mass.register_api_command(
            "music/radio/remove_item_from_library", self.remove_item_from_library
        )
        self.mass.register_api_command("music/radio/radio_versions", self.versions)

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Radio]:
        """Return all versions of a radio station we can find on all providers."""
        radio = await self.get(item_id, provider_instance_id_or_domain)
        # perform a search on all provider(types) to collect all versions/variants
        all_versions = {
            prov_item.item_id: prov_item
            for prov_items in await asyncio.gather(
                *[
                    self.search(radio.name, provider_domain)
                    for provider_domain in self.mass.music.get_unique_providers()
                ]
            )
            for prov_item in prov_items
            if loose_compare_strings(radio.name, prov_item.name)
        }
        # make sure that the 'base' version is NOT included
        for prov_version in radio.provider_mappings:
            all_versions.pop(prov_version.item_id, None)

        # return the aggregated result
        return all_versions.values()

    async def add_item_to_library(self, item: Radio, metadata_lookup: bool = True) -> Radio:
        """Add radio to library and return the new database item."""
        if isinstance(item, ItemMapping):
            metadata_lookup = False
            item = Radio.from_item_mapping(item)
        if not isinstance(item, Radio):
            msg = "Not a valid Radio object"
            raise InvalidDataError(msg)
        if not item.provider_mappings:
            msg = "Radio is missing provider mapping(s)"
            raise InvalidDataError(msg)
        if metadata_lookup:
            await self.mass.metadata.get_radio_metadata(item)
        # check for existing item first
        library_item = None
        if cur_item := await self.get_library_item_by_prov_id(item.item_id, item.provider):
            # existing item match by provider id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        elif cur_item := await self.get_library_item_by_external_ids(item.external_ids):
            # existing item match by external id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        else:
            # search by name
            async for db_item in self.iter_library_items(search=item.name):
                if compare_strings(db_item.name, item.name):
                    # existing item found: update it
                    library_item = await self.update_item_in_library(db_item.item_id, item)
                    break
        if not library_item:
            # actually add a new item in the library db
            # use the lock to prevent a race condition of the same item being added twice
            async with self._db_add_lock:
                library_item = await self._add_library_item(item)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_ADDED,
            library_item.uri,
            library_item,
        )
        return library_item

    async def update_item_in_library(
        self, item_id: str | int, update: Radio, overwrite: bool = False
    ) -> Radio:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = cur_item.metadata.update(getattr(update, "metadata", None), overwrite)
        provider_mappings = self._get_provider_mappings(cur_item, update, overwrite)
        cur_item.external_ids.update(update.external_ids)
        match = {"item_id": db_id}
        await self.mass.music.database.update(
            self.db_table,
            match,
            {
                # always prefer name from updated item here
                "name": update.name or cur_item.name,
                "sort_name": update.sort_name or cur_item.sort_name,
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", update.name, db_id)
        # get full created object
        library_item = await self.get_library_item(db_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        # return the full item we just updated
        return library_item

    async def _add_library_item(self, item: Radio) -> Radio:
        """Add a new item record to the database."""
        item.timestamp_added = int(utc_timestamp())
        item.timestamp_modified = int(utc_timestamp())
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "provider_mappings": serialize_to_json(item.provider_mappings),
                "external_ids": serialize_to_json(item.external_ids),
                "timestamp_added": int(utc_timestamp()),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database", item.name)
        # return the full item we just added
        return await self.get_library_item(db_id)

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Generate a dynamic list of tracks based on the item's content."""
        msg = "Dynamic tracks not supported for Radio MediaItem"
        raise NotImplementedError(msg)

    async def _get_dynamic_tracks(self, media_item: Radio, limit: int = 25) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        msg = "Dynamic tracks not supported for Radio MediaItem"
        raise NotImplementedError(msg)
