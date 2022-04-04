"""All logic for metadata retrieval."""

from music_assistant.helpers.cache import cached
from music_assistant.helpers.images import create_thumbnail
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import merge_dict

from .fanarttv import FanartTv
from .musicbrainz import MusicBrainz

# TODO: add more metadata providers such as theaudiodb
# TODO: add metadata support for albums and other media types

TABLE_THUMBS = "thumbnails"


class MetaDataController:
    """Several helpers to search and store metadata for mediaitems."""

    # TODO: create periodic task to search for missing metadata
    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.cache = mass.cache
        self.logger = mass.logger.getChild("metadata")
        self.fanarttv = FanartTv(mass)
        self.musicbrainz = MusicBrainz(mass)

    async def setup(self):
        """Async initialize of module."""
        async with self.mass.database.get_db() as _db:
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE_THUMBS}(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    size INTEGER,
                    img BLOB,
                    UNIQUE(url, size));"""
            )

    async def get_artist_metadata(self, mb_artist_id: str, cur_metadata: dict) -> dict:
        """Get/update rich metadata for an artist by providing the musicbrainz artist id."""
        metadata = cur_metadata
        if "fanart" in metadata:
            # no need to query (other) metadata providers if we already have a result
            return metadata
        self.logger.info(
            "Fetching metadata for MusicBrainz Artist %s on Fanrt.tv", mb_artist_id
        )
        cache_key = f"fanarttv.artist_metadata.{mb_artist_id}"
        res = await cached(
            self.cache, cache_key, self.fanarttv.get_artist_images, mb_artist_id
        )
        if res:
            metadata = merge_dict(metadata, res)
            self.logger.debug(
                "Found metadata for MusicBrainz Artist %s on Fanart.tv: %s",
                mb_artist_id,
                ", ".join(res.keys()),
            )
        return metadata

    async def get_thumbnail(self, url, size) -> bytes:
        """Get/create thumbnail image for url."""
        match = {"url": url, "size": size}
        if result := await self.mass.database.get_row(TABLE_THUMBS, match):
            return result["img"]
        # create thumbnail if it doesn't exist
        thumbnail = await create_thumbnail(self.mass, url, size)
        await self.mass.database.insert_or_replace(
            TABLE_THUMBS, {**match, "img": thumbnail}
        )
        return thumbnail
