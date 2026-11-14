"""Tests for the index of the community catalog and the copy of it that the server keeps."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from aiohttp import ClientSession, ClientTimeout

from music_assistant.controllers.community_catalog import index as index_module
from music_assistant.controllers.community_catalog.index import IndexClient, server_line
from tests.controllers.community_catalog.conftest import catalog_index

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from tests.controllers.community_catalog.conftest import Registry

LOGGER = logging.getLogger(__name__)


@pytest.mark.parametrize(
    ("version", "line"),
    [
        ("0.0.0", "nightly"),
        ("2.12.0.dev2026111403", "nightly"),
        ("2.12.0b4", "beta"),
        ("2.12.0rc1", "beta"),
        ("2.11.3", "2.11"),
    ],
)
def test_server_line(version: str, line: str) -> None:
    """A server reads the index of its own line."""
    assert server_line(version) == line


@pytest.fixture
async def client(tmp_path: Path) -> AsyncGenerator[IndexClient]:
    """Return an index client of a nightly server that keeps its copy in a temporary folder."""
    async with ClientSession() as session:
        mass = SimpleNamespace(version="0.0.0", http_session=session)
        yield IndexClient(mass, str(tmp_path / "community_providers"), LOGGER)  # type: ignore[arg-type]


async def test_fetch_keeps_the_copy_and_its_etag(
    tmp_path: Path, client: IndexClient, registry: Registry
) -> None:
    """A fetched index is kept with its ETag, which the next fetch sends to learn of a change."""
    folder = tmp_path / "community_providers"

    assert await client.fetch() is True

    assert registry.requests == [("nightly", None)]
    assert json.loads((folder / "index.json").read_bytes()) == registry.document
    assert json.loads((folder / "state.json").read_bytes()) == {
        "schema": 1,
        "index_etag": registry.etag,
    }
    assert client.index is not None
    assert set(client.index.providers) == set(catalog_index()["providers"])

    kept = (folder / "index.json").read_bytes()
    assert await client.fetch() is False

    assert registry.requests[1] == ("nightly", registry.etag)
    assert (folder / "index.json").read_bytes() == kept


async def test_unchanged_index_is_no_change(client: IndexClient, registry: Registry) -> None:
    """An index fetched again with the same content does not count as a change."""
    assert await client.fetch() is True
    registry.etag = '"two"'

    assert await client.fetch() is False

    assert client.state.index_etag == '"two"'


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("not_found", "404"),
        ("timeout", "no answer"),
        ("not_json", "Could not fetch"),
        ("other_schema", "the document has schema 2, this server reads 1"),
        ("other_line", "the index is for line 2.11, this server reads nightly"),
    ],
)
async def test_failed_fetch_keeps_the_copy(
    tmp_path: Path,
    client: IndexClient,
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    message: str,
) -> None:
    """Whatever goes wrong, the kept copy stays and one warning tells why."""
    folder = tmp_path / "community_providers"
    assert await client.fetch() is True
    kept, kept_index = (folder / "index.json").read_bytes(), client.index
    registry.etag = '"two"'
    match failure:
        case "not_found":
            registry.status = 404
        case "timeout":
            monkeypatch.setattr(index_module, "_TIMEOUT", ClientTimeout(total=0.05))
            registry.delay = 0.5
        case "not_json":
            registry.document = b"<html>maintenance</html>"
        case "other_schema":
            registry.document = catalog_index(schema=2)
        case "other_line":
            registry.document = catalog_index(line="2.11")

    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        assert await client.fetch() is False

    assert len(registry.requests) == 2
    assert (folder / "index.json").read_bytes() == kept
    assert client.index == kept_index
    assert client.state.index_etag == '"one"'
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert message in warnings[0]


async def test_index_that_cannot_be_kept_is_still_used(
    tmp_path: Path, client: IndexClient, registry: Registry, caplog: pytest.LogCaptureFixture
) -> None:
    """An index that cannot be written is used all the same, and the next fetch keeps it."""
    folder = tmp_path / "community_providers"
    with (
        patch.object(IndexClient, "_write", side_effect=OSError("No space left on device")),
        caplog.at_level(logging.WARNING, logger=LOGGER.name),
    ):
        assert await client.fetch() is True

    assert client.index is not None
    assert client.state.index_etag is None
    assert not folder.exists()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "No space left on device" in warnings[0]

    assert await client.fetch() is False

    assert registry.requests[1] == ("nightly", None)
    assert (folder / "index.json").exists()
    assert client.state.index_etag == registry.etag


async def test_load_reads_the_kept_copy(
    tmp_path: Path, client: IndexClient, registry: Registry
) -> None:
    """The kept copy and its ETag are read back; a state of another format reads as empty."""
    folder = tmp_path / "community_providers"
    assert await client.fetch() is True
    fetched = client.index
    async with ClientSession() as session:
        mass = SimpleNamespace(version="0.0.0", http_session=session)
        restarted = IndexClient(mass, str(folder), LOGGER)  # type: ignore[arg-type]
        await restarted.load()
        assert restarted.index == fetched
        assert restarted.state.index_etag == registry.etag

        (folder / "state.json").write_text(json.dumps({"schema": 2, "index_etag": '"one"'}))
        await restarted.load()
        assert restarted.index == fetched
        assert restarted.state.index_etag is None


async def test_no_etag_without_a_copy(
    tmp_path: Path, client: IndexClient, registry: Registry
) -> None:
    """Without a kept copy the ETag is not sent, so the registry answers with the index."""
    folder = tmp_path / "community_providers"
    assert await client.fetch() is True
    (folder / "index.json").unlink()
    await client.load()

    assert await client.fetch() is True

    assert registry.requests[1] == ("nightly", None)
    assert (folder / "index.json").exists()
