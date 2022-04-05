"""Manage MediaItems of type Radio."""
from __future__ import annotations

from music_assistant.constants import EventType
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.util import create_sort_name, merge_dict, merge_list
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import MediaType, Radio


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = "radios"
    media_type = MediaType.RADIO
    item_cls = Radio

    async def setup(self):
        """Async initialize of module."""
        # prepare database
        async with self.mass.database.get_db() as _db:
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.db_table}(
                        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        sort_name TEXT NOT NULL,
                        in_library BOOLEAN DEFAULT 0,
                        metadata json,
                        provider_ids json
                    );"""
            )

    async def get_radio_by_name(self, name: str) -> Radio | None:
        """Get in-library radio by name."""
        return await self.mass.database.get_row(self.db_table, {"name": name})

    async def add(self, item: Radio) -> Radio:
        """Add radio to local db and return the new database item."""
        db_item = await self.add_db_item(item)
        self.mass.signal_event(EventType.RADIO_ADDED, db_item)
        return db_item

    async def add_db_item(self, radio: Radio) -> Radio:
        """Add a new radio record to the database."""
        if not radio.sort_name:
            radio.sort_name = create_sort_name(radio.name)
        match = {"sort_name": radio.sort_name}
        if cur_item := await self.mass.database.get_row(self.db_table, match):
            # update existing
            return await self.update_db_radio(cur_item["item_id"], radio)

        # insert new radio
        new_item = await self.mass.database.insert_or_replace(
            self.db_table, radio.to_db_row()
        )
        item_id = new_item["item_id"]
        # store provider mappings
        await self.mass.music.add_provider_mappings(
            item_id, MediaType.RADIO, radio.provider_ids
        )
        self.logger.debug("added %s to database", radio.name)
        # return created object
        return await self.get_db_item(item_id)

    async def update_db_radio(self, item_id: int, radio: Radio) -> Radio:
        """Update Radio record in the database."""
        cur_item = await self.get_db_item(item_id)
        metadata = merge_dict(cur_item.metadata, radio.metadata)
        provider_ids = merge_list(cur_item.provider_ids, radio.provider_ids)
        if not radio.sort_name:
            radio.sort_name = create_sort_name(radio.name)

        match = {"item_id": item_id}
        await self.mass.database.update(
            self.db_table,
            match,
            {
                "name": radio.name,
                "sort_name": radio.sort_name,
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
            },
        )
        await self.mass.music.add_provider_mappings(
            item_id, MediaType.RADIO, radio.provider_ids
        )
        self.logger.debug("updated %s in database: %s", radio.name, item_id)
        return await self.get_db_item(item_id)
