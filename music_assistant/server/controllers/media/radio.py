"""Manage MediaItems of type Radio."""
from __future__ import annotations

import asyncio

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, MediaType
from music_assistant.common.models.media_items import Radio, Track
from music_assistant.constants import DB_TABLE_RADIOS
from music_assistant.server.helpers.compare import loose_compare_strings

from .base import MediaControllerBase


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = DB_TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio
    _db_add_lock = asyncio.Lock()

    def __init__(self, *args, **kwargs):
        """Initialize class."""
        super().__init__(*args, **kwargs)
        # register api handlers
        self.mass.register_api_command("music/radios", self.db_items)
        self.mass.register_api_command("music/radio", self.get)
        self.mass.register_api_command("music/radio/versions", self.versions)
        self.mass.register_api_command("music/radio/update", self._update_db_item)
        self.mass.register_api_command("music/radio/delete", self.delete)

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

    async def add(self, item: Radio, skip_metadata_lookup: bool = False) -> Radio:
        """Add radio to local db and return the new database item."""
        if not skip_metadata_lookup:
            await self.mass.metadata.get_radio_metadata(item)
        if item.provider == "database":
            db_item = await self._update_db_item(item.item_id, item)
        else:
            # use the lock to prevent a race condition of the same item being added twice
            async with self._db_add_lock:
                db_item = await self._add_db_item(item)
        return db_item

    async def update(self, item_id: str | int, update: Radio, overwrite: bool = False) -> Radio:
        """Update existing record in the database."""
        return await self._update_db_item(item_id=item_id, item=update, overwrite=overwrite)

    async def _add_db_item(self, item: Radio) -> Radio:
        """Add a new item record to the database."""
        assert item.provider_mappings, "Item is missing provider mapping(s)"
        cur_item = None
        # safety guard: check for existing item first
        if cur_item := await self.get_db_item_by_prov_id(item.item_id, item.provider):
            # existing item found: update it
            return await self._update_db_item(cur_item.item_id, item)
        # try name matching
        match = {"name": item.name}
        if db_row := await self.mass.music.database.get_row(self.db_table, match):
            cur_item = Radio.from_db_row(db_row)
            # existing item found: update it
            return await self._update_db_item(cur_item.item_id, item)
        # insert new item
        item.timestamp_added = int(utc_timestamp())
        item.timestamp_modified = int(utc_timestamp())
        new_item = await self.mass.music.database.insert(self.db_table, item.to_db_row())
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database", item.name)
        # get full created object
        db_item = await self.get_db_item(db_id)
        # only signal event if we're not running a sync (to prevent a floodstorm of events)
        if not self.mass.music.get_running_sync_tasks():
            self.mass.signal_event(
                EventType.MEDIA_ITEM_ADDED,
                db_item.uri,
                db_item,
            )
        # return the full item we just added
        return db_item

    async def _update_db_item(
        self, item_id: str | int, item: Radio, overwrite: bool = False
    ) -> Radio:
        """Update Radio record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_db_item(db_id)
        metadata = cur_item.metadata.update(getattr(item, "metadata", None), overwrite)
        provider_mappings = self._get_provider_mappings(cur_item, item, overwrite)
        match = {"item_id": db_id}
        await self.mass.music.database.update(
            self.db_table,
            match,
            {
                # always prefer name from updated item here
                "name": item.name or cur_item.name,
                "sort_name": item.sort_name or cur_item.sort_name,
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", item.name, db_id)
        # get full created object
        db_item = await self.get_db_item(db_id)
        # only signal event if we're not running a sync (to prevent a floodstorm of events)
        if not self.mass.music.get_running_sync_tasks():
            self.mass.signal_event(
                EventType.MEDIA_ITEM_UPDATED,
                db_item.uri,
                db_item,
            )
        # return the full item we just updated
        return db_item

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Generate a dynamic list of tracks based on the item's content."""
        raise NotImplementedError("Dynamic tracks not supported for Radio MediaItem")

    async def _get_dynamic_tracks(self, media_item: Radio, limit: int = 25) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        raise NotImplementedError("Dynamic tracks not supported for Radio MediaItem")
