"""Host (personality) storage/normalization mixin for AI Radio."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any

import aiofiles
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.json import async_json_loads

from .constants import DEFAULT_LLM_INSTRUCTIONS
from .helpers import slugify

if TYPE_CHECKING:
    from pathlib import Path

    from music_assistant.mass import MusicAssistant


class AIRadioHostsMixin:
    """Mixin with host persistence and normalization helpers."""

    if TYPE_CHECKING:
        mass: MusicAssistant
        logger: logging.Logger
        _hosts_file: Path
        _hosts: dict[str, dict[str, Any]]

    async def _load_hosts(self) -> None:
        """Load host profiles from disk."""
        hosts_file_exists = await asyncio.to_thread(self._hosts_file.exists)
        if not hosts_file_exists:
            self._hosts = {}
            return
        async with aiofiles.open(self._hosts_file) as file_handle:
            content = await file_handle.read()
        try:
            payload = await async_json_loads(content)
        except ValueError as err:
            # keep the corrupt file on disk for inspection; it is only
            # overwritten again once a host is saved
            self.logger.error("Hosts file is corrupt, starting without hosts: %s", err)
            payload = {}
        items = payload.get("hosts", []) if isinstance(payload, dict) else []
        parsed: dict[str, dict[str, Any]] = {}
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    normalized = self._normalize_host(item)
                except Exception as err:
                    self.logger.warning("Skipping invalid host profile: %s", err)
                    continue
                parsed[normalized["id"]] = normalized
        self._hosts = parsed

    async def _write_hosts(self) -> None:
        """Persist host profiles to disk."""
        payload = {
            "version": 1,
            "hosts": sorted(self._hosts.values(), key=lambda item: item["name"]),
        }
        await self._write_json_file(self._hosts_file, payload)

    def _normalize_host(
        self,
        host: dict[str, Any],
        sections_map: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Validate and normalize a host profile."""
        known_sections = self._sections if sections_map is None else sections_map
        host_id = slugify(str(host.get("id", "")).strip() or str(host.get("name", "")).strip())
        name = str(host.get("name", "")).strip()
        if not name:
            raise InvalidDataError("Host name is required")
        if not host_id:
            raise InvalidDataError("Host id is required")

        instructions = str(host.get("instructions") or "").strip() or DEFAULT_LLM_INSTRUCTIONS
        tts_engine = str(host.get("tts_engine") or "").strip()

        section_ids = [
            str(item).strip()
            for item in host.get("section_ids", [])
            if isinstance(item, (str, int)) and str(item).strip()
        ]
        if not section_ids:
            raise InvalidDataError("Host requires at least one section id")
        _sections, missing = self._materialize_sections(section_ids, known_sections)
        if missing:
            raise InvalidDataError(
                f"Host references unknown sections: {', '.join(sorted(set(missing)))}"
            )

        raw_section_order = host.get("section_order")
        if not isinstance(raw_section_order, list) or not raw_section_order:
            raise InvalidDataError("Host requires a non-empty 'section_order' list")
        self._validate_section_order(raw_section_order, set(section_ids))

        merge_section_id = str(host.get("merge_section_id", "")).strip()
        if merge_section_id:
            if merge_section_id not in section_ids:
                raise InvalidDataError("merge_section_id must be selected in host section_ids")
            merge_section = known_sections.get(merge_section_id)
            if not merge_section or str(merge_section.get("type", "")).strip().lower() != "ai_meta":
                raise InvalidDataError("merge_section_id must reference an ai_meta section")

        return {
            "id": host_id,
            "name": name,
            "instructions": instructions,
            "tts_engine": tts_engine,
            "section_ids": section_ids,
            "section_order": deepcopy(raw_section_order),
            "merge_section_id": merge_section_id,
        }

    def _default_host_template(self) -> dict[str, Any]:
        """Return the built-in host template."""
        default_sections = self._default_sections_template()
        default_section_ids = [item["id"] for item in default_sections]
        station_template = self._default_station_template()
        return {
            "id": "default_host",
            "name": "Default Host",
            "instructions": DEFAULT_LLM_INSTRUCTIONS,
            "tts_engine": "",
            "section_ids": default_section_ids,
            "section_order": deepcopy(station_template["section_order"]),
            "merge_section_id": "Between_Songs_Smoother",
        }
