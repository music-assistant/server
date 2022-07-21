"""Manage MediaItems of type Radio."""
from __future__ import annotations

from time import time

from music_assistant.helpers.database import TABLE_RADIOS
from music_assistant.helpers.json import json_serializer
from music_assistant.models.enums import EventType, MediaType
from music_assistant.models.event import MassEvent
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import Radio


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio

    async def get_radio_by_name(self, name: str) -> Radio | None:
        """Get in-library radio by name."""
        return await self.mass.database.get_row(self.db_table, {"name": name})

    async def add(self, item: Radio, overwrite_existing: bool = False) -> Radio:
        """Add radio to local db and return the new database item."""
        item.metadata.last_refresh = int(time())
        await self.mass.metadata.get_radio_metadata(item)
        return await self.add_db_item(item, overwrite_existing)

    async def add_db_item(self, item: Radio, overwrite_existing: bool = False) -> Radio:
        """Add a new item record to the database."""
        assert item.provider_ids
        async with self._db_add_lock:
            match = {"name": item.name}
            if cur_item := await self.mass.database.get_row(self.db_table, match):
                # update existing
                return await self.update_db_item(
                    cur_item["item_id"], item, overwrite=overwrite_existing
                )

            # insert new item
            new_item = await self.mass.database.insert(self.db_table, item.to_db_row())
            item_id = new_item["item_id"]
            self.logger.debug("added %s to database", item.name)
            # return created object
            db_item = await self.get_db_item(item_id)
            self.mass.signal_event(
                MassEvent(EventType.MEDIA_ITEM_ADDED, db_item.uri, db_item)
            )
            return db_item

    async def update_db_item(
        self,
        item_id: int,
        item: Radio,
        overwrite: bool = False,
    ) -> Radio:
        """Update Radio record in the database."""
        cur_item = await self.get_db_item(item_id)
        if overwrite:
            metadata = item.metadata
            provider_ids = item.provider_ids
        else:
            metadata = cur_item.metadata.update(item.metadata)
            provider_ids = {*cur_item.provider_ids, *item.provider_ids}

        match = {"item_id": item_id}
        await self.mass.database.update(
            self.db_table,
            match,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
            },
        )
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        db_item = await self.get_db_item(item_id)
        self.mass.signal_event(
            MassEvent(EventType.MEDIA_ITEM_UPDATED, db_item.uri, db_item)
        )
        return db_item
