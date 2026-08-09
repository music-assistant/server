"""Converters for Bandcamp API models to Music Assistant models."""

from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

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

if TYPE_CHECKING:
    from bandcamp_async_api.models import BCAlbum as APIAlbum
    from bandcamp_async_api.models import BCArtist as APIArtist
    from bandcamp_async_api.models import BCTrack as APITrack
    from bandcamp_async_api.models import (
        FeedTrack,
        SearchResultAlbum,
        SearchResultArtist,
        SearchResultTrack,
    )

from ._ids import make_artist_id, slugify_performer


class DiscographyItem(TypedDict, total=False):
    """Raw discography item dict from the band_details API."""

    item_id: int
    item_type: str
    band_id: int
    title: str
    artist_name: str | None
    band_name: str | None
    art_id: int | None
    release_date: str | None
    is_purchasable: bool


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
        """
        Parse streaming URL info.

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

    def track_from_search(
        self, item: SearchResultTrack, *, artist_item_id: str | None = None
    ) -> MATrack:
        """
        Create a Track from new API SearchResultTrack.

        :param artist_item_id: Pre-resolved artist item_id; falls back to
            slug-based resolution if omitted.
        """
        track_id = f"{item.artist_id}-{item.album_id or 0}-{item.id}"
        artist_item_id = artist_item_id or make_artist_id(item.artist_id, item.artist_name)
        return MATrack(
            item_id=track_id,
            provider=self.instance_id,
            name=item.name,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=artist_item_id,
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

    def album_from_search(
        self, item: SearchResultAlbum, *, artist_item_id: str | None = None
    ) -> MAAlbum:
        """
        Create an Album from new API SearchResultAlbum.

        :param artist_item_id: Pre-resolved artist item_id; falls back to
            slug-based resolution if omitted.
        """
        album_id = f"{item.artist_id}-{item.id}"
        artist_item_id = artist_item_id or make_artist_id(item.artist_id, item.artist_name)
        output = MAAlbum(
            item_id=album_id,
            provider=self.instance_id,
            name=item.name,
            uri=item.url,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=artist_item_id,
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
        *,
        tralbum_artist: str | None = None,
        artist_item_id: str | None = None,
    ) -> MATrack:
        """
        Convert a Track object from the API to MA Track format.

        :param tralbum_artist: Per-album performer credit. When set and
            different from the band's own name, the displayed artist
            name is the performer rather than the band.
        :param artist_item_id: Pre-resolved artist item_id; falls back to
            slug-based resolution if omitted.
        """
        album_id = album_id or 0
        _, bitrate, content_type = self.streaming_url_from_api(track.streaming_url or {})
        band_name = track.artist.name
        display_name = tralbum_artist or band_name
        artist_item_id = artist_item_id or _resolve_artist_id(
            band_id=track.artist.id, performer=tralbum_artist, band_name=band_name
        )
        output = MATrack(
            item_id=f"{track.artist.id}-{album_id}-{track.id}",
            provider=self.instance_id,
            name=track.title,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=artist_item_id,
                        provider=self.instance_id,
                        name=display_name,
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

    def track_from_feed(self, track: FeedTrack) -> MATrack:
        """Convert a feed track_list entry to MA Track format."""
        album_id = track.album_id or 0
        item_id = f"{track.band_id}-{album_id}-{track.track_id}"
        _, bitrate, content_type = self.streaming_url_from_api(track.streaming_url or {})
        output = MATrack(
            item_id=item_id,
            provider=self.instance_id,
            name=track.title,
            duration=int(track.duration) if track.duration else 0,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=str(track.band_id),
                        provider=self.instance_id,
                        name=track.band_name,
                    )
                ]
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=track.track_url,
                    audio_format=AudioFormat(content_type=content_type, bit_rate=bitrate),
                )
            },
        )
        if track.track_num is not None:
            output.track_number = track.track_num
        if album_id:
            output.album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=f"{track.band_id}-{album_id}",
                provider=self.instance_id,
                name=track.album_title or "",
            )
        if track.art_id:
            output.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"https://f4.bcbits.com/img/a{track.art_id}_0.jpg",
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

    def album_from_discography_item(
        self, item: DiscographyItem, *, artist_item_id: str | None = None
    ) -> MAAlbum:
        """
        Convert a raw discography dict to MA Album format.

        Discography items come from the band_details API and contain summary
        data (title, art_id, release_date string) without full album details.
        Fields not available from the discography endpoint (url, description)
        are omitted and populated later when get_album fetches full details.

        :param artist_item_id: Pre-resolved artist item_id; falls back to
            slug-based resolution if omitted.
        """
        band_id = item.get("band_id", 0)
        item_id = item.get("item_id", 0)
        album_id = f"{band_id}-{item_id}"
        # `artist_name` (when set) is the per-album performer; `band_name`
        # is the page owner. They differ on label-released albums.
        performer = item.get("artist_name")
        band_name = item.get("band_name") or ""
        display_name = performer or band_name
        artist_item_id = artist_item_id or _resolve_artist_id(
            band_id=band_id, performer=performer, band_name=band_name
        )

        # Build art URL from art_id (matches _build_art_url in parsers.py)
        art_id = item.get("art_id")
        art_url = f"https://f4.bcbits.com/img/a{art_id}_0.jpg" if art_id else ""

        # Parse year from release_date string like "21 Feb 2020 00:00:00 GMT"
        year = None
        release_date = item.get("release_date")
        if release_date:
            with suppress(ValueError, TypeError, IndexError):
                # Format: "21 Feb 2020 00:00:00 GMT" — extract year directly
                year = int(release_date.split()[2])

        output = MAAlbum(
            item_id=album_id,
            provider=self.instance_id,
            name=item.get("title") or "",
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=artist_item_id,
                        provider=self.instance_id,
                        name=display_name,
                    )
                ]
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            year=year,
        )
        if art_url:
            output.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=art_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return output

    def album_from_api(self, album: APIAlbum, *, artist_item_id: str | None = None) -> MAAlbum:
        """
        Convert an API Album object to MA Album format.

        :param artist_item_id: Pre-resolved artist item_id; falls back to
            slug-based resolution if omitted.
        """
        album_id = f"{album.artist.id}-{album.id}"
        band_name = album.artist.name
        display_name = album.tralbum_artist or band_name
        artist_item_id = artist_item_id or _resolve_artist_id(
            band_id=album.artist.id, performer=album.tralbum_artist, band_name=band_name
        )
        output = MAAlbum(
            item_id=album_id,
            provider=self.instance_id,
            name=album.title,
            artists=UniqueList(
                [
                    ItemMapping(
                        media_type=MediaType.ARTIST,
                        item_id=artist_item_id,
                        provider=self.instance_id,
                        name=display_name,
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
            year=datetime.fromtimestamp(album.release_date, tz=UTC).year
            if album.release_date
            else None,
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

    def synthetic_artist(
        self,
        band_id: int,
        performer_name: str,
        *,
        url: str | None = None,
        image_url: str | None = None,
    ) -> MAArtist:
        """
        Build an Artist for a per-album performer that has no band page.

        These performers exist on Bandcamp only as a credit on a band's
        page (typically a label), so we synthesize an Artist scoped to the
        ``(band_id, performer)`` pair. See :mod:`._ids` for the ID format.

        :param band_id: The hosting Bandcamp band's id.
        :param performer_name: The performer credit (display name).
        :param url: Optional URL to attach. Usually the band page; the
            performer doesn't have their own.
        :param image_url: Optional artwork to use as the artist's thumb,
            typically borrowed from one of their albums.
        """
        item_id = make_artist_id(band_id, performer_name)
        output = MAArtist(
            item_id=item_id,
            provider=self.instance_id,
            name=performer_name,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=url,
                )
            },
        )
        if url:
            output.metadata.description = url
        if image_url:
            output.metadata.add_image(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        return output


def _resolve_artist_id(
    *,
    band_id: int | str,
    performer: str | None,
    band_name: str | None,
) -> str:
    """
    Choose between a real or synthetic artist item_id.

    Returns the synthetic ``{band_id}:{slug(performer)}`` form only when
    the performer is set AND its slug differs from the band's own slug;
    otherwise returns the plain ``{band_id}``.
    """
    if (
        not performer
        or not band_name
        or slugify_performer(performer) == slugify_performer(band_name)
    ):
        return str(band_id)
    return make_artist_id(band_id, performer)
