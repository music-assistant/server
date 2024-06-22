"""Test we can parse Jellyfin models into Music Assistant models."""

import logging
import pathlib
from collections.abc import AsyncGenerator

import aiofiles
import aiohttp
import pytest
from aiojellyfin import Artist, Connection, SessionConfiguration
from mashumaro.codecs.json import JSONDecoder
from syrupy.assertion import SnapshotAssertion

from music_assistant.server.providers.jellyfin.parsers import parse_album, parse_artist, parse_track

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ARTIST_FIXTURES = list(FIXTURES_DIR.glob("artists/*.json"))
ALBUM_FIXTURES = list(FIXTURES_DIR.glob("albums/*.json"))
TRACK_FIXTURES = list(FIXTURES_DIR.glob("tracks/*.json"))

ARTIST_DECODER = JSONDecoder(Artist)

_LOGGER = logging.getLogger(__name__)


@pytest.fixture
async def connection() -> AsyncGenerator[Connection, None]:
    """Spin up a dummy connection."""
    async with aiohttp.ClientSession() as session:
        session_config = SessionConfiguration(
            session=session,
            url="http://localhost:1234",
            app_name="X",
            app_version="0.0.0",
            device_id="X",
            device_name="localhost",
        )
        yield Connection(session_config, "USER_ID", "ACCESS_TOKEN")


@pytest.mark.parametrize("example", ARTIST_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_artists(
    example: pathlib.Path, connection: Connection, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse artists."""
    async with aiofiles.open(example) as fp:
        raw_data = ARTIST_DECODER.decode(await fp.read())
    parsed = parse_artist(_LOGGER, "xx-instance-id-xx", connection, raw_data)
    assert snapshot == parsed.to_dict()


@pytest.mark.parametrize("example", ALBUM_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_albums(
    example: pathlib.Path, connection: Connection, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse albums."""
    async with aiofiles.open(example) as fp:
        raw_data = ARTIST_DECODER.decode(await fp.read())
    parsed = parse_album(_LOGGER, "xx-instance-id-xx", connection, raw_data)
    assert snapshot == parsed.to_dict()


@pytest.mark.parametrize("example", TRACK_FIXTURES, ids=lambda val: str(val.stem))
async def test_parse_tracks(
    example: pathlib.Path, connection: Connection, snapshot: SnapshotAssertion
) -> None:
    """Test we can parse tracks."""
    async with aiofiles.open(example) as fp:
        raw_data = ARTIST_DECODER.decode(await fp.read())
    parsed = parse_track(_LOGGER, "xx-instance-id-xx", connection, raw_data)
    assert snapshot == parsed.to_dict()
