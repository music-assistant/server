"""Converters for Bandcamp API models to Music Assistant models."""

from datetime import datetime

from bandcamp_async_api.models import BCAlbum as APIAlbum
from bandcamp_async_api.models import BCArtist as APIArtist
from bandcamp_async_api.models import BCTrack as APITrack
from bandcamp_async_api.models import (
    SearchResultAlbum,
    SearchResultArtist,
    SearchResultTrack,
)
from music_assistant_models.enums import ContentType, ImageType, MediaType
from music_assistant_models.media_items import Album as MAAlbum
from music_assistant_models.media_items import Artist as MAArtist
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)
from music_assistant_models.media_items import Track as MATrack


class BandcampConverters:
    """Converters for Bandcamp API models to Music Assistant models."""

    def __init__(self, domain: str, instance_id: str):
        """Initialize converters with provider information."""
        self.domain = domain
        self.instance_id = instance_id

    @staticmethod
    def streaming_url_from_api(
        streaming_info: dict[str, str],
    ) -> tuple[str | None, int | None, ContentType]:
        """Parse streaming URL info.

        :param streaming_info: Dict of format keys to URLs from the Bandcamp API.
        """
        # Extract streaming URL with priority: mp3-v0 > mp3-320 > mp3-128
        bitrate = None
        streaming_url = None
        content_type = ContentType.MP3
        if "mp3-v0" in streaming_info:
            streaming_url = streaming_info["mp3-v0"]
        elif "mp3-320" in streaming_info:
            streaming_url = streaming_info["mp3-320"]
            bitrate = 320
        elif "mp3-128" in streaming_info:
            streaming_url = streaming_info["mp3-128"]
            bitrate = 128
        elif streaming_info:
            streaming_url = next(iter(streaming_info.values()))
            content_type = ContentType.UNKNOWN
        return streaming_url, bitrate, content_type

    def track_from_search(self, item: SearchResultTrack) -> MATrack:
        """Create a Track from new API SearchResultTrack."""
        track_id = f"{item.artist_id}-{item.album_id or 0}-{item.id}"
        return MATrack(
            item_id=track_id,
            provider=self.instance_id,
            name=item.name,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=str(item.artist_id),
                        provider=self.instance_id,
                        name=item.artist_name,
                    )
                ]
            ),
            album=(
                ItemMapping(
                    media_type=MediaType.ALBUM,
                    item_id=f"{item.artist_id}-{item.album_id or 0}",
                    provider=self.instance_id,
                    name=item.album_name,
                )
                if item.album_id
                else None
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=item.url,
                )
            },
        )

    def album_from_search(self, item: SearchResultAlbum) -> MAAlbum:
        """Create an Album from new API SearchResultAlbum."""
        album_id = f"{item.artist_id}-{item.id}"
        output = MAAlbum(
            item_id=album_id,
            provider=self.instance_id,
            name=item.name,
            uri=item.url,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=str(item.artist_id),
                        provider=self.instance_id,
                        name=item.artist_name,
                        uri=item.artist_url,
                    )
                ]
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=item.url,
                )
            },
        )
        output.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=item.image_url,
                provider=self.instance_id,
                remotely_accessible=True,
            )
        )
        return output

    def artist_from_search(self, item: SearchResultArtist) -> MAArtist:
        """Create an Artist from new API SearchResultArtist."""
        output = MAArtist(
            item_id=str(item.id),
            provider=self.instance_id,
            name=item.name,
            uri=item.url,
            provider_mappings={
                ProviderMapping(
                    item_id=str(item.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=item.url,
                )
            },
        )
        output.metadata.genres = item.tags
        if item.url:
            output.metadata.description = item.url
        output.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=item.image_url,
                provider=self.instance_id,
                remotely_accessible=True,
            )
        )
        return output

    def track_from_api(
        self,
        track: APITrack,
        album_id: str | int | None = None,
        album_name: str = "",
        album_image_url: str = "",
    ) -> MATrack:
        """Convert a Track object from the API to MA Track format."""
        album_id = album_id or 0
        _, bitrate, content_type = self.streaming_url_from_api(track.streaming_url or {})
        output = MATrack(
            item_id=f"{track.artist.id}-{album_id}-{track.id}",
            provider=self.instance_id,
            name=track.title,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=str(track.artist.id),
                        provider=self.instance_id,
                        name=track.artist.name,
                    )
                ]
            ),
            disc_number=0,
            duration=track.duration,
            provider_mappings={
                ProviderMapping(
                    item_id=f"{track.artist.id}-{album_id}-{track.id}",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=track.url,
                    audio_format=AudioFormat(
                        content_type=content_type,
                        bit_rate=bitrate,
                    ),
                )
            },
        )
        if track.track_number is not None:
            output.track_number = track.track_number

        if album_id:
            output.album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=f"{track.artist.id}-{album_id}",
                provider=self.instance_id,
                name=album_name,
            )
        elif hasattr(track, "album") and track.album:
            # If the track has an album attribute, use that information
            output.album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=f"{track.artist.id}-{track.album.id}",
                provider=self.instance_id,
                name=track.album.title,
            )
        output.metadata.lyrics = track.lyrics
        if album_image_url:
            output.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=album_image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return output

    def artist_from_api(self, artist: APIArtist) -> MAArtist:
        """Convert an API Artist object to MA Artist format."""
        output = MAArtist(
            item_id=str(artist.id),
            uri=artist.url,
            provider=self.instance_id,
            name=artist.name,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist.id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=artist.url,
                )
            },
        )
        output.metadata.description = f"{artist.url}\n{artist.bio or ''}".strip()
        output.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=artist.image_url,
                provider=self.instance_id,
                remotely_accessible=True,
            )
        )
        return output

    def album_from_api(self, album: APIAlbum) -> MAAlbum:
        """Convert an API Album object to MA Album format."""
        album_id = f"{album.artist.id}-{album.id}"
        output = MAAlbum(
            item_id=album_id,
            provider=self.instance_id,
            name=album.title,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=str(album.artist.id),
                        provider=self.instance_id,
                        name=album.artist.name,
                        image=MediaItemImage(
                            path=album.art_url,
                            type=ImageType.THUMB,
                            provider=self.instance_id,
                            remotely_accessible=True,
                        ),
                    )
                ]
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=album.url,
                )
            },
            year=datetime.fromtimestamp(album.release_date).year if album.release_date else None,
        )
        output.metadata.add_image(
            MediaItemImage(
                type=ImageType.THUMB,
                path=album.art_url,
                provider=self.instance_id,
                remotely_accessible=True,
            )
        )
        output.metadata.description = f"{album.url}\n{album.about or ''}".strip()
        return output
