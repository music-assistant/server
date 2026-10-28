"""Fixtures for BookOrbit provider tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.bookorbit import SUPPORTED_FEATURES, BookOrbitProvider
from music_assistant.providers.bookorbit.parsers import _audio_files

FIXTURES = Path(__file__).parent / "fixtures"


def wire_mass_create_task(mass: Any) -> None:
    """Make a Mock mass.create_task return real asyncio tasks (for catalog gathers)."""

    def create_task(
        target: Any,
        *args: Any,
        task_name: str | None = None,
        **_kwargs: Any,
    ) -> asyncio.Task[Any]:
        coro = target if asyncio.iscoroutine(target) else target(*args)
        return asyncio.create_task(coro, name=task_name)

    mass.create_task = create_task


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())  # type: ignore[no-any-return]


def manifest_from_book(book: dict[str, Any]) -> dict[str, Any]:
    """Build a BookOrbit audiobook manifest from a BookDetail fixture."""
    assets: list[dict[str, Any]] = []
    total_ms = 0
    for index, file in enumerate(_audio_files(book)):
        duration_s = file.get("durationSeconds")
        duration_ms = int(float(duration_s) * 1000) if duration_s is not None else None
        if duration_ms is not None:
            total_ms += duration_ms
        assets.append(
            {
                "assetId": f"aud_00000000-0000-4000-8000-{int(file['id']):012d}",
                "fileId": file["id"],
                "sequence": index,
                "format": file["format"],
                "durationMs": duration_ms,
                "sizeBytes": file.get("sizeBytes"),
                "etag": "test",
            }
        )
    return {
        "schema": "bookorbit.audiobook-manifest",
        "schemaVersion": 2,
        "revision": "test-revision",
        "book": {"id": book["id"], "title": book.get("title"), "authors": [], "narrators": []},
        "assets": assets,
        "chapters": [],
        "totalDurationMs": total_ms,
    }


@pytest.fixture
def book_audiobook() -> dict[str, Any]:
    """Return a single-file m4b BookDetail with embedded chapters."""
    return _load("book_audiobook.json")


@pytest.fixture
def book_multifile() -> dict[str, Any]:
    """Return a multi-file mp3 BookDetail (plus an epub) without embedded chapters."""
    return _load("book_multifile.json")


@pytest.fixture
def provider(book_audiobook: dict[str, Any], book_multifile: dict[str, Any]) -> BookOrbitProvider:
    """Create a provider with a mocked client."""
    mass = Mock()
    wire_mass_create_task(mass)
    mass.streams.base_url = "http://ma:8097"
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    mass.music.get_playlog_provider_item_ids = AsyncMock(return_value=[])
    manifest = Mock()
    manifest.domain = "bookorbit"
    config = Mock()
    config.instance_id = "bookorbit--test"
    config.name = "BookOrbit Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    prov = BookOrbitProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov.base_url = "http://bo"
    prov._client = Mock()
    details = {book_audiobook["id"]: book_audiobook, book_multifile["id"]: book_multifile}
    manifests = {book_id: manifest_from_book(book) for book_id, book in details.items()}
    prov._client.get_book = AsyncMock(side_effect=lambda book_id: details[book_id])
    prov._client.get_audiobook_manifest = AsyncMock(side_effect=lambda book_id: manifests[book_id])
    prov._client.get_audio_progress = AsyncMock(return_value=None)
    prov._client.upsert_audio_progress = AsyncMock()
    prov._client.get_playback_state = AsyncMock(return_value=None)
    prov._client.put_playback_state = AsyncMock(return_value={"revision": 1})
    prov._client.get_continue_listening_book_ids = AsyncMock(return_value=[])
    return prov
