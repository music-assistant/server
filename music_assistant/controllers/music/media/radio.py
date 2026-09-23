"""Manage MediaItems of type Radio."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import (
    InvalidDataError,
    MusicAssistantError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.helpers import create_safe_string
from music_assistant_models.media_items import ProviderMapping, Radio, RadioSummary, Track

from music_assistant.constants import DB_TABLE_RADIOS
from music_assistant.controllers.tasks.context import (
    report_current_task_failure,
    update_current_task_progress_from_index,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.compare import (
    compare_media_item,
    compare_radio,
    loose_compare_strings,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json
from music_assistant.helpers.playlists import (
    PlaylistItem,
    ProviderMappingInfo,
    construct_media_item_from_playlist_item,
    generate_m3u,
    media_item_to_playlist_item,
    parse_m3u,
)
from music_assistant.helpers.uri import parse_uri
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase

if TYPE_CHECKING:
    from collections.abc import Mapping

    from music_assistant_models.background_task import BackgroundTask

    from music_assistant import MusicAssistant
    from music_assistant.models.plugin import PluginProvider


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = DB_TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio
    summary_item_cls = RadioSummary

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(
            f"music/{api_base}/radio_versions", self.versions, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/radio_tracks",
            self.radio_tracks,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/export_radios", self.export_radios, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/import_radios",
            self.import_radios,
            required_scope=Scope.LIBRARY_WRITE,
        )

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """Return the slim SELECT query used for radio summary listings."""
        query = f"""
        SELECT
            {self._summary_base_columns()},
            {self.db_table}.is_dynamic,
            json_extract({self.db_table}.metadata, '$.description') AS description,
            {self._provider_mappings_query()} AS provider_mappings
            FROM {self.db_table}"""
        return query, {}

    async def radio_tracks(self, item_id: str, provider_instance_id_or_domain: str) -> list[Track]:
        """
        Return a fresh batch of tracks for a dynamic radio station.

        :param item_id: The provider (or library) item id of the station.
        :param provider_instance_id_or_domain: The provider instance id or domain the
            item id belongs to ("library" for a library item).
        """
        radio = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        return await self.dynamic_tracks(radio)

    async def dynamic_tracks(self, radio: Radio) -> list[Track]:
        """
        Return a fresh batch of tracks for an already resolved dynamic radio station.

        :param radio: The dynamic station to fetch the next batch for.
        """
        if not radio.is_dynamic:
            raise UnsupportedFeaturedException(f"{radio.name} is not a dynamic radio station")
        provider_instance_id_or_domain, item_id = (
            self._select_provider_id(radio)
            if radio.provider == "library"
            else (radio.provider, radio.item_id)
        )
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            raise ProviderUnavailableError(f"{provider_instance_id_or_domain} is not available")
        return await cast("MusicProvider | PluginProvider", provider).get_dynamic_radio_tracks(
            item_id
        )

    async def export_radios(self) -> str:
        """Export all library radio stations to M3U8 format."""
        items: list[PlaylistItem] = []
        async for radio in self.iter_library_items():
            entry = media_item_to_playlist_item(radio)
            if radio.favorite:
                # favorite is library-level user state, so only a library export carries it
                entry.metadata = {**(entry.metadata or {}), "favorite": "true"}
            items.append(entry)
        return generate_m3u("Radio Stations", items)

    async def import_radios(self, m3u_data: str) -> BackgroundTask:
        """
        Queue importing radio stations from M3U8 format.

        Any station that can not be imported is reported as a failure on the returned
        task, and the remaining stations are still imported.

        :param m3u_data: The M3U8 data as a string.
        :return: Managed background task performing the import.
        :raises InvalidDataError: The M3U data holds no entries.
        """
        parsed_items = parse_m3u(m3u_data)
        if not parsed_items:
            msg = "No items found in M3U data"
            raise InvalidDataError(msg)
        user = get_current_user()
        return self.mass.tasks.run_background_task(
            name=f"Import {len(parsed_items)} radio stations",
            handler=lambda: self._handle_import_radios(parsed_items),
            translation_key="import_radios",
            translation_owner=self.translation_owner,
            translation_args=[len(parsed_items)],
            user_id=user.user_id if user else None,
            metadata={
                "task_domain": "radio_import",
                "item_count": len(parsed_items),
            },
            allow_retry=True,
            priority=True,
        )

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Radio]:
        """Return all versions of a radio station we can find on all providers."""
        radio = await self.get(item_id, provider_instance_id_or_domain)
        if radio.is_dynamic:
            # a dynamic station is its provider's own, so a same-named station is a different one
            return []
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
        return list(all_versions.values())

    async def match_provider(
        self, db_radio: Radio, provider: MusicProvider, strict: bool = True
    ) -> list[ProviderMapping]:
        """
        Try to find match on (streaming) provider for the provided (database) radio.

        This is used to link objects of different providers/qualities together.
        """
        self.logger.debug(
            "Trying to match radio %s on provider %s",
            db_radio.name,
            provider.name,
        )
        matches: list[ProviderMapping] = []
        search_str = db_radio.name
        search_result = await self.search(search_str, provider.instance_id)
        for search_result_item in search_result:
            if not search_result_item.available:
                continue
            if not compare_media_item(db_radio, search_result_item, strict=strict):
                continue
            # we must fetch the full radio version, search results can be simplified objects
            prov_radio = await self.get_provider_item(
                search_result_item.item_id,
                search_result_item.provider,
                fallback=search_result_item,
            )
            if compare_radio(db_radio, prov_radio, strict=strict):
                # 100% match
                matches.extend(prov_radio.provider_mappings)
        if not matches:
            self.logger.debug(
                "Could not find match for Radio %s on provider %s",
                db_radio.name,
                provider.name,
            )
        return matches

    async def match_providers(self, db_radio: Radio) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) radio.

        This is used to link objects of different providers/qualities together.
        """
        if db_radio.provider != "library":
            return  # Matching only supported for database items
        if db_radio.is_dynamic:
            # matching a dynamic station by name would link an unrelated radio stream to it
            return

        # try to find match on all providers
        cur_provider_domains = {x.provider_domain for x in db_radio.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if MediaType.RADIO not in provider.supported_media_types:
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self.match_provider(db_radio, provider):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_radio.item_id, match)
                cur_provider_domains.add(provider.domain)

    async def _add_library_item(self, item: Radio, overwrite_existing: bool = False) -> int:
        """Add a new item record to the database."""
        assert self.mass.music.database is not None  # For type checking
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(
                    item.sort_name if item.sort_name is not None else "", True, True
                ),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
                "is_dynamic": item.is_dynamic,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(db_id, item.external_ids)
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Radio, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        match = {"item_id": db_id}
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        assert self.mass.music.database is not None  # For type checking
        await self.mass.music.database.update(
            self.db_table,
            match,
            {
                # always prefer name from updated item here
                "name": name,
                "sort_name": sort_name,
                "metadata": serialize_to_json(metadata),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
                "is_dynamic": update.is_dynamic,
            },
        )
        # update/set external id lookup table
        await self.set_external_ids(
            db_id, update.external_ids if overwrite else cur_item.external_ids
        )
        # update/set provider_mappings table
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*update.provider_mappings, *cur_item.provider_mappings}
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> RadioSummary:
        """Parse a raw summary db row into a RadioSummary object."""
        item = cast("RadioSummary", super()._parse_summary_row(db_row))
        item.is_dynamic = bool(db_row["is_dynamic"])
        item.metadata.description = db_row["description"]
        return item

    async def _handle_import_radios(self, parsed_items: list[PlaylistItem]) -> None:
        """Add the parsed M3U entries to the library, one station at a time."""
        total = len(parsed_items)
        for index, item in enumerate(parsed_items):
            update_current_task_progress_from_index(
                index, total, f"Importing station {index + 1}/{total}"
            )
            label = (item.metadata or {}).get("name") or item.title or item.path
            try:
                await self._import_radio_item(item)
            except MusicAssistantError as err:
                report_current_task_failure(f"{label}: {err}")
            except Exception as err:
                # a transient fault such as a provider timeout must not abandon the
                # stations queued behind it, but it is a real fault worth a log line
                self.logger.warning(
                    "Error importing radio station %s: %s",
                    label,
                    str(err),
                    exc_info=err if self.logger.isEnabledFor(logging.DEBUG) else None,
                )
                report_current_task_failure(f"{label}: {err}")
        update_current_task_progress_from_index(total, total, "Import complete")

    async def _import_radio_item(self, item: PlaylistItem) -> None:
        """Resolve a single parsed M3U entry against its provider and add it to the library."""
        if not item.providers:
            # a plain third-party M3U carries no #EXTPROV, so recover the mapping from the
            # path, which parse_uri normalises into a provider and its native item_id
            if not item.is_url:
                msg = f"{item.path} is not a stream URL"
                raise InvalidDataError(msg)
            media_type, prov_lookup, prov_item_id = await parse_uri(item.path)
            if media_type not in (MediaType.RADIO, MediaType.UNKNOWN):
                msg = f"{item.path} is a {media_type.value}, not a radio station"
                raise InvalidDataError(msg)
            provider = self.mass.get_provider(prov_lookup, provider_type=MusicProvider)
            if not provider:
                msg = f"Provider {prov_lookup} is not available"
                raise ProviderUnavailableError(msg)
            # a retry re-runs this handler over the same parsed items, so build a new
            # entry rather than writing the resolved mapping back into the shared one
            item = replace(
                item,
                providers=[
                    ProviderMappingInfo(
                        domain=provider.domain,
                        item_id=prov_item_id,
                        instance_id=provider.instance_id,
                    )
                ],
            )
        radio = construct_media_item_from_playlist_item(item, self.mass, MediaType.RADIO)
        if not isinstance(radio, Radio):
            msg = f"{item.path} is not a radio station"
            raise InvalidDataError(msg)
        if not any(mapping.available for mapping in radio.provider_mappings):
            msg = f"No available provider for radio station {radio.name}"
            raise ProviderUnavailableError(msg)
        library_item = await self.mass.music.add_item_to_library(radio)
        # a station owned by another provider is refetched from it, so the library item comes
        # back with only that provider's mapping; reattach the ones the file recorded for the
        # other providers it is loaded on, which the refetch has no way to know about
        await self.add_provider_mappings(
            library_item.item_id, [pm for pm in radio.provider_mappings if pm.available]
        )
        # the refetch also discards anything set on the object passed in, so the exported
        # favorite goes onto the library item
        if (item.metadata or {}).get("favorite") == "true":
            await self.set_favorite(library_item.item_id, True)
