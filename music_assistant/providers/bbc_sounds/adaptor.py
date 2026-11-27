"""
Adaptor for converting BBC Sounds objects to Music Assistant media items.

Many Sounds API endpoints return containers of "PlayableObjects" which can be a
range of different types. The auntie-sounds library detects these differing
types and provides a sensible set of objects to work with, e.g. RadioShow.

This adaptor maps those objects to the most sensible type for MA.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from auntie_sounds.models import (
    Category,
    Collection,
    LiveStation,
    MenuItem,
    Playlist,
    Podcast,
    PodcastEpisode,
    RadioClip,
    RadioSeries,
    RadioShow,
    RecommendedMenuItem,
    Schedule,
    ScheduleItem,
    SoundsTypes,
    Station,
    StationSearchResult,
)
from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemChapter,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Radio,
    RecommendationFolder,
    Track,
)
from music_assistant_models.media_items import Podcast as MAPodcast
from music_assistant_models.media_items import PodcastEpisode as MAPodcastEpisode
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata
from music_assistant_models.unique_list import UniqueList

from music_assistant.constants import DEFAULT_AUDIOBOOK_PODCAST_GENRE, VERBOSE_LOG_LEVEL
from music_assistant.providers.bbc_sounds.constants import Constants, ValidMenuIDs
from music_assistant.providers.bbc_sounds.live_rewind import LiveProgrammeId
from music_assistant.providers.bbc_sounds.naming import (
    TitleParts,
    full_station_name,
    programme_source,
    programme_title,
    release_date,
    station_programme_title,
)

if TYPE_CHECKING:
    from music_assistant.providers.bbc_sounds import BBCSoundsProvider


def _audio_format(stream: str) -> AudioFormat:
    return AudioFormat(content_type=ContentType.try_parse(stream))


class ConversionError(MusicAssistantError):
    """Raised when object conversion fails."""


class ImageProvider:
    """Handles image URL resolution and MediaItemImage creation."""

    ICON_BASE_URL = "https://cdn.jsdelivr.net/gh/kieranhogg/auntie-sounds@3.0.0/assets/icons/solid"

    ICON_MAPPING: ClassVar[dict[str, str]] = {
        ValidMenuIDs.LIVE: "listen_live",
        ValidMenuIDs.CATCH_UP: "latest",
        ValidMenuIDs.SCHEDULES: "episodes",
        ValidMenuIDs.MY_SOUNDS: "my_sounds",
        ValidMenuIDs.CONTINUE_LISTENING: "continue",
        ValidMenuIDs.EDITORIAL_COLLECTION: "editorial",
        ValidMenuIDs.LOCAL_RAIL: "my_location",
        ValidMenuIDs.SINGLE_ITEM_PROMO: "featured",
        ValidMenuIDs.COLLECTIONS: "collections",
        ValidMenuIDs.CATEGORIES: "categories",
        ValidMenuIDs.RECOMMENDATIONS: "my_sounds",
        ValidMenuIDs.UNMISSABLE_SPEECH: "speech",
        ValidMenuIDs.PODCASTS: "speech",
        ValidMenuIDs.UNMISSABLE_MUSIC: "music",
        ValidMenuIDs.MUSIC: "music",
        ValidMenuIDs.EXPLORE: "categories",
        ValidMenuIDs.STATIONS: "latest",
        ValidMenuIDs.NEWS: "news",
    }

    @classmethod
    def get_icon_url(cls, icon_id: str) -> str | None:
        """Get icon URL for a given icon ID."""
        if icon_id is not None:
            if icon_id in cls.ICON_MAPPING:
                return f"{cls.ICON_BASE_URL}/{cls.ICON_MAPPING[icon_id]}.png"
            if "latest_playables_for_curation" in icon_id:
                return f"{cls.ICON_BASE_URL}/news.png"
        return None

    @classmethod
    def create_image(
        cls, url: str, provider: str, image_type: ImageType = ImageType.THUMB
    ) -> MediaItemImage:
        """Create a MediaItemImage from a URL."""
        return MediaItemImage(
            path=url,
            provider=provider,
            type=image_type,
            remotely_accessible=True,
        )

    @classmethod
    def create_metadata_with_image(
        cls,
        url: str | None,
        provider: str,
        description: str | None = None,
        chapters: list[MediaItemChapter] | None = None,
    ) -> MediaItemMetadata:
        """Create metadata with optional image and description."""
        metadata = MediaItemMetadata()
        if url:
            metadata.add_image(cls.create_image(url, provider))
        if description:
            metadata.description = description
        if chapters:
            metadata.chapters = chapters
        return metadata


@dataclass
class Context:
    """Context information for object conversion."""

    provider: BBCSoundsProvider
    path_parts: list[str] | None = None
    force_type: (
        type[
            Track
            | Radio
            | MAPodcast
            | MAPodcastEpisode
            | BrowseFolder
            | RecommendationFolder
            | RecommendedMenuItem
        ]
        | None
    ) = None
    title_parts: TitleParts = TitleParts.LISTING


class BaseConverter(ABC):
    """Base model."""

    def __init__(self, context: Context):
        """Create a new instance."""
        self.context = context
        self.logger = self.context.provider.logger

    @abstractmethod
    def can_convert(self, source_obj: Any) -> bool:
        """Check if this converter can handle the source object."""

    @abstractmethod
    async def get_stream_details(self, source_obj: Any) -> StreamDetails | None:
        """Convert the source object to a stream."""

    @abstractmethod
    async def convert(
        self, source_obj: Any
    ) -> (
        Track
        | Radio
        | MAPodcast
        | MAPodcastEpisode
        | BrowseFolder
        | RecommendationFolder
        | RecommendedMenuItem
    ):
        """Convert the source object to target type."""

    def _create_provider_mapping(self, item_id: str) -> ProviderMapping:
        """Create provider mapping for the item."""
        return self.context.provider.adaptor._get_provider_mapping(item_id)

    def _get_attr(self, obj: Any, attr_path: str, default: Any = None) -> Any:
        """
        Get (optionally-nested) attribute from object.

        Supports e.g. _get_attr(object, "thing.other_thing")
        """
        current = obj
        for part in attr_path.split("."):
            if hasattr(current, part):
                current = getattr(current, part)
            elif isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return default if current is None else current

    def _require_id(self, item_id: str | None, obj: Any) -> str:
        """Return an id of an auntie-sounds object, raising ConversionError if None."""
        if not item_id:
            raise ConversionError(f"No id for {obj}")
        return item_id

    def _get_synopsis(self, obj: Any) -> str | None:
        """Return the fullest synopsis the given object carries, if any."""
        for length in ("long", "medium", "short"):
            if synopsis := self._get_attr(obj, f"synopses.{length}"):
                return str(synopsis)
        return None


class StationConverter(BaseConverter):
    """Converts Station-related objects."""

    ConvertableTypes = Station | LiveStation | StationSearchResult

    def can_convert(self, source_obj: Any) -> bool:
        """Check if this converter can convert to a Station object."""
        return isinstance(source_obj, self.ConvertableTypes)

    async def get_stream_details(self, source_obj: Any) -> StreamDetails | None:
        """Convert the source object to a stream."""
        if not isinstance(source_obj, (Station, LiveStation)):
            return None
        station = await self.convert(source_obj)
        if not station or not source_obj.stream:
            return None
        if type(source_obj) is Station and source_obj.international:
            # This is a bare-bones international stream
            sd = StreamDetails(
                media_type=MediaType.RADIO,
                stream_type=StreamType.HLS,
                path=source_obj.stream,
                item_id=station.item_id,
                provider=station.provider,
                audio_format=_audio_format(source_obj.stream),
            )
            self.logger.debug(sd)
            return sd

        return StreamDetails(
            stream_metadata=StreamMetadata(
                image_url=station.image.path if station.image else None,
                title=station_programme_title(source_obj) or station.name,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HLS,
            path=str(source_obj.stream),
            item_id=station.item_id,
            provider=station.provider,
            audio_format=_audio_format(source_obj.stream),
            data={
                "provider": self.context.provider.instance_id,
                "station": station.item_id,
            },
        )

    async def convert(self, source_obj: Any) -> Radio:
        """Convert the source object to target type."""
        if isinstance(source_obj, Station):
            return self._convert_station(source_obj)
        if isinstance(source_obj, LiveStation):
            return self._convert_live_station(source_obj)
        if isinstance(source_obj, StationSearchResult):
            return self._convert_station_search_result(source_obj)
        self.logger.error(
            "Failed to convert station with type: %s.\n%s", type(source_obj), source_obj
        )
        raise ConversionError(f"Failed to convert station {type(source_obj)}: {source_obj}")

    def _convert_station(self, station: Station) -> Radio:
        """Convert Station object."""
        image_url = self._get_attr(station, "image_url")

        return Radio(
            item_id=station.id,
            name=full_station_name(station=station),
            provider=self.context.provider.instance_id,
            metadata=ImageProvider.create_metadata_with_image(
                image_url, self.context.provider.instance_id, station.description
            ),
            provider_mappings={self._create_provider_mapping(station.id)},
        )

    def _convert_live_station(self, station: LiveStation) -> Radio:
        """Convert LiveStation object."""
        image_url = self._get_attr(station, "network.logo_url")

        return Radio(
            item_id=station.id,
            name=full_station_name(station=station),
            provider=self.context.provider.instance_id,
            metadata=ImageProvider.create_metadata_with_image(
                image_url, self.context.provider.instance_id, station.description
            ),
            provider_mappings={self._create_provider_mapping(station.id)},
        )

    def _convert_station_search_result(self, station: StationSearchResult) -> Radio:
        """Convert StationSearchResult object."""
        return Radio(
            item_id=station.service_id,
            name=full_station_name(station=station),
            provider=self.context.provider.instance_id,
            metadata=ImageProvider.create_metadata_with_image(
                station.station_image_url, self.context.provider.instance_id
            ),
            provider_mappings={self._create_provider_mapping(station.service_id)},
        )


class PodcastConverter(BaseConverter):
    """Converts podcast-related objects."""

    ConvertableTypes = Podcast | PodcastEpisode | RadioShow | RadioClip | RadioSeries
    OutputTypes = MAPodcast | MAPodcastEpisode | Track

    def can_convert(self, source_obj: Any) -> bool:
        """Check if this converter can convert to a Podcast object."""
        if self.context.force_type:
            return issubclass(self.context.force_type, self.OutputTypes)
        return isinstance(source_obj, self.ConvertableTypes)

    async def get_stream_details(self, source_obj: Any) -> StreamDetails | None:
        """Convert the source object to a stream."""
        if isinstance(source_obj, (Podcast, RadioSeries)):
            return None
        stream_details = None
        episode = await self.convert(source_obj)
        if episode and isinstance(episode, MAPodcastEpisode) and source_obj.stream:
            stream_details = StreamDetails(
                stream_metadata=StreamMetadata(
                    title=episode.name,
                    uri=source_obj.stream,
                ),
                media_type=MediaType.PODCAST_EPISODE,
                stream_type=StreamType.HLS,
                path=source_obj.stream,
                item_id=source_obj.pid,
                provider=self.context.provider.instance_id,
                audio_format=_audio_format(source_obj.stream),
                allow_seek=True,
                can_seek=True,
                duration=(episode.duration or None),
                seek_position=(int(episode.position) if episode.position else 0),
                seconds_streamed=(int(episode.position) if episode.position else 0),
            )
        elif episode and isinstance(episode, Track) and source_obj.stream:
            if source_obj.network and source_obj.network.id == "bbc_webonly":
                title = "BBC News"
            else:
                title = programme_source(source_obj) or ""

            metadata = StreamMetadata(title=title, uri=source_obj.stream)
            if episode.metadata.images:
                metadata.image_url = episode.metadata.images[0].path

            stream_details = StreamDetails(
                stream_metadata=metadata,
                media_type=MediaType.TRACK,
                stream_type=StreamType.HLS,
                path=source_obj.stream,
                item_id=episode.item_id,
                provider=self.context.provider.instance_id,
                audio_format=_audio_format(source_obj.stream),
                can_seek=True,
                duration=episode.duration,
            )
        return stream_details

    async def convert(self, source_obj: Any) -> OutputTypes:
        """Convert podcast objects."""
        if isinstance(source_obj, (Podcast, RadioSeries)) or self.context.force_type is MAPodcast:
            return await self._convert_podcast(source_obj)
        if isinstance(source_obj, PodcastEpisode):
            return await self._convert_podcast_episode(source_obj)
        if isinstance(source_obj, RadioShow):
            return await self._convert_radio_show(source_obj)
        if isinstance(source_obj, RadioClip) or self.context.force_type is Track:
            return await self._convert_radio_clip(source_obj)
        self.logger.error(
            "Failed to convert podcast object with type: %s.\n%s", type(source_obj), source_obj
        )
        raise ConversionError(f"Browse conversion failed: {source_obj}")

    async def _convert_podcast(self, podcast: Podcast | RadioSeries) -> MAPodcast:
        name = self._get_attr(podcast, "titles.primary") or self._get_attr(podcast, "title")
        description = self._get_synopsis(podcast)
        image_url = self._get_attr(podcast, "image_url") or self._get_attr(
            podcast, "sub_items.image_url"
        )

        metadata = ImageProvider.create_metadata_with_image(
            image_url, self.context.provider.instance_id, description
        )
        if isinstance(podcast, Podcast):
            metadata.genres = {DEFAULT_AUDIOBOOK_PODCAST_GENRE}
        item_id = self._require_id(podcast.item_id, podcast)
        return MAPodcast(
            item_id=item_id,
            name=name,
            provider=self.context.provider.instance_id,
            metadata=metadata,
            provider_mappings={self._create_provider_mapping(item_id)},
        )

    async def _convert_podcast_episode(self, episode: PodcastEpisode) -> MAPodcastEpisode:
        # 0 is MA's unknown duration, and None fails when the item is rebuilt from a dict
        duration = self._get_attr(episode, "duration.value", 0)
        resume_position_ms = (
            self._get_attr(episode, "progress.value") * 1000 if episode.progress else None
        )
        description = self._get_synopsis(episode)

        # Handle parent podcast
        podcast = None
        if hasattr(episode, "container") and episode.container:
            podcast = await PodcastConverter(self.context).convert(episode.container)

        if not podcast or not isinstance(podcast, MAPodcast):
            raise ConversionError(f"No podcast for episode {episode}")
        if not episode or not episode.pid:
            raise ConversionError(f"No podcast episode for {episode}")

        ma_episode = MAPodcastEpisode(
            item_id=episode.pid,
            name=programme_title(episode, self.context.title_parts),
            provider=self.context.provider.instance_id,
            duration=duration,
            position=0,
            resume_position_ms=resume_position_ms,
            metadata=ImageProvider.create_metadata_with_image(
                url=episode.image_url,
                provider=self.context.provider.instance_id,
                description=description,
            ),
            podcast=podcast,
            provider_mappings={self._create_provider_mapping(episode.pid)},
            uri=episode.stream,
        )
        ma_episode.metadata.release_date = release_date(episode)
        return ma_episode

    def _show_is_a_track(self, show: RadioShow) -> bool:
        """
        Determine if this should be a track instead of a podcast episode based on duration/context.

        A sensible, but arbitrary duration was picked for this.
        Track example: latest BBC News, PodcastEpisode: latest episode of a radio show.
        """
        duration = self._get_attr(show, "duration.value", 0)
        container = self._get_attr(show, "container")
        if self.context.force_type:
            return self.context.force_type is Track
        if duration and duration < Constants.TRACK_DURATION_THRESHOLD:
            return True
        return not container

    async def _convert_radio_show(self, show: RadioShow) -> MAPodcastEpisode | Track:
        # 0 is MA's unknown duration, and None fails when the item is rebuilt from a dict
        duration = self._get_attr(show, "duration.value", 0)
        resume_position_ms = (
            self._get_attr(show, "progress.value") * 1000 if show.progress else None
        )
        if not show or not show.pid:
            raise ConversionError(f"No radio show for {show}")

        name = programme_title(show, self.context.title_parts)
        if self._show_is_a_track(show):
            return Track(
                item_id=show.pid,
                name=name,
                provider=self.context.provider.instance_id,
                duration=duration,
                metadata=ImageProvider.create_metadata_with_image(
                    url=show.image_url,
                    provider=self.context.provider.instance_id,
                    description=self._get_synopsis(show),
                ),
                provider_mappings={self._create_provider_mapping(show.pid)},
            )

        # Handle as episode
        podcast = None
        if hasattr(show, "container") and show.container:
            podcast = await PodcastConverter(self.context).convert(cast("Podcast", show.container))

        if not podcast or not isinstance(podcast, MAPodcast):
            raise ConversionError(f"No podcast for episode for {show}")

        episode = MAPodcastEpisode(
            item_id=show.pid,
            name=name,
            provider=self.context.provider.instance_id,
            duration=duration,
            resume_position_ms=resume_position_ms,
            metadata=ImageProvider.create_metadata_with_image(
                url=show.image_url,
                provider=self.context.provider.instance_id,
                description=self._get_synopsis(show),
            ),
            podcast=podcast,
            provider_mappings={self._create_provider_mapping(show.pid)},
            position=1,
        )
        episode.metadata.release_date = release_date(show)
        return episode

    async def _convert_radio_clip(self, clip: RadioClip) -> Track | MAPodcastEpisode:
        duration = self._get_attr(clip, "duration.value", 0)
        description = self._get_synopsis(clip)

        if not clip or not clip.pid:
            raise ConversionError(f"No clip for {clip}")

        if self.context.force_type is MAPodcastEpisode:
            podcast = None
            if hasattr(clip, "container") and clip.container:
                podcast = await PodcastConverter(self.context).convert(
                    cast("Podcast", clip.container)
                )

            if not podcast or not isinstance(podcast, MAPodcast):
                raise ConversionError(f"No podcast for episode for {clip}")
            episode = MAPodcastEpisode(
                item_id=clip.pid,
                name=programme_title(clip, self.context.title_parts),
                provider=self.context.provider.instance_id,
                duration=duration,
                metadata=ImageProvider.create_metadata_with_image(
                    clip.image_url, self.context.provider.instance_id, description
                ),
                provider_mappings={self._create_provider_mapping(clip.pid)},
                podcast=podcast,
                position=0,
            )
            episode.metadata.release_date = release_date(clip)
            return episode
        return Track(
            item_id=clip.pid,
            name=programme_title(clip, self.context.title_parts),
            provider=self.context.provider.instance_id,
            duration=duration,
            metadata=ImageProvider.create_metadata_with_image(
                clip.image_url, self.context.provider.instance_id, description
            ),
            provider_mappings={self._create_provider_mapping(clip.pid)},
        )


class LiveProgrammeConverter(BaseConverter):
    """Converts a programme that is on air now into an episode played from the live stream."""

    def can_convert(self, source_obj: Any) -> bool:
        """Check if this is a scheduled programme that is on air now."""
        if self.context.force_type not in (None, MAPodcastEpisode):
            return False
        return (
            isinstance(source_obj, ScheduleItem)
            and self._programme_id(source_obj) is not None
            and source_obj.is_live
        )

    async def get_stream_details(self, source_obj: Any) -> StreamDetails | None:
        """Live programmes are streamed by the provider from the station's rewind window."""
        return None

    async def convert(self, source_obj: Any) -> MAPodcastEpisode:
        """Convert a scheduled programme to an episode played from the station's live stream."""
        programme_id = self._programme_id(source_obj)
        if programme_id is None:
            raise ConversionError(f"Not a live programme: {source_obj}")
        podcast: MAPodcast | ItemMapping
        if source_obj.container:
            podcast = cast(
                "MAPodcast",
                await PodcastConverter(self.context).convert(cast("Podcast", source_obj.container)),
            )
        else:
            podcast = ItemMapping(
                media_type=MediaType.PODCAST,
                item_id=programme_id.station_id,
                provider=self.context.provider.instance_id,
                name=self._get_attr(source_obj, "network.short_title", programme_id.station_id),
            )
        return MAPodcastEpisode(
            item_id=programme_id.item_id,
            name=programme_title(source_obj, TitleParts.LIVE),
            provider=self.context.provider.instance_id,
            duration=programme_id.duration,
            metadata=ImageProvider.create_metadata_with_image(
                url=source_obj.image_url,
                provider=self.context.provider.instance_id,
                description=self._get_synopsis(source_obj),
            ),
            podcast=podcast,
            provider_mappings={self._create_provider_mapping(programme_id.item_id)},
            position=0,
        )

    def _programme_id(self, item: ScheduleItem | RadioShow) -> LiveProgrammeId | None:
        """Return the id of a scheduled programme, if it has everything needed to play it."""
        station_id = self._get_attr(item, "network.id")
        if not (station_id and item.item_id and item.start and item.end):
            return None
        item_id = self._require_id(item.item_id, item)
        return LiveProgrammeId(
            station_id=station_id,
            pid=item_id,
            start=int(item.start.timestamp()),
            end=int(item.end.timestamp()),
        )


class BrowseConverter(BaseConverter):
    """Converts browsable objects like menus, categories, collections."""

    ConvertableTypes = MenuItem | Category | Collection | Schedule | RecommendedMenuItem | Playlist
    OutputTypes = BrowseFolder | RecommendationFolder

    def can_convert(self, source_obj: Any) -> bool:
        """Check if this converter can convert to a Browsable object."""
        can_convert = False
        if self.context.force_type:
            can_convert = issubclass(self.context.force_type, self.OutputTypes)
        else:
            can_convert = isinstance(source_obj, self.ConvertableTypes)
        return can_convert

    async def get_stream_details(self, source_obj: Any) -> StreamDetails | None:
        """Convert the source object to a stream."""
        return None

    async def convert(self, source_obj: Any) -> OutputTypes:
        """Convert browsable objects."""
        if isinstance(source_obj, MenuItem) and self.context.force_type is not RecommendationFolder:
            return self._convert_menu_item(source_obj)
        if isinstance(source_obj, (Category, Collection, Playlist)):
            return self._convert_category_or_collection(source_obj)
        if isinstance(source_obj, Schedule):
            return self._convert_schedule(source_obj)
        if isinstance(source_obj, RecommendedMenuItem):
            return await self._convert_recommended_item(source_obj)
        self.logger.error(
            "Failed to convert browse object with type: %s.\n%s", type(source_obj), source_obj
        )
        raise ConversionError(f"Browse conversion failed: {source_obj}")

    def _convert_menu_item(self, item: MenuItem) -> BrowseFolder | RecommendationFolder:
        """Convert MenuItem to BrowseFolder or RecommendationFolder."""
        if not item or not item.title:
            raise ConversionError(f"No menu item {item}")
        item_id = self._require_id(item.item_id, item)
        if not item.image_url:
            image_url = ImageProvider.get_icon_url(item_id)
            image = (
                ImageProvider.create_image(image_url, self.context.provider.instance_id)
                if image_url
                else None
            )
        else:
            image = ImageProvider.create_image(item.image_url, self.context.provider.instance_id)

        path = self._build_path(item_id)

        return_type = BrowseFolder

        if self.context.force_type is RecommendationFolder:
            return_type = RecommendationFolder
        return return_type(
            item_id=self._require_id(item.item_id, item),
            name=item.title,
            provider=self.context.provider.instance_id,
            path=path,
            image=image,
        )

    def _convert_category_or_collection(
        self, item: Category | Collection | Playlist
    ) -> BrowseFolder:
        """Convert Category, Collection or Playlist to BrowseFolder."""
        item_id = self._require_id(item.item_id, item)
        if isinstance(item, Playlist):
            if not isinstance(self.context.path_parts, list):
                path = f"{self.context.provider.instance_id}://playlists/{item_id}"
            else:
                path = "/".join([*self.context.path_parts, item_id])
        else:
            path_prefix = "categories" if isinstance(item, Category) else "collections"
            path = f"{self.context.provider.instance_id}://{path_prefix}/{item_id}"

        return BrowseFolder(
            item_id=item_id,
            name=self._get_attr(item, "titles.primary", "Untitled folder"),
            provider=self.context.provider.instance_id,
            path=path,
            image=(
                ImageProvider.create_image(item.image_url, self.context.provider.instance_id)
                if item.image_url
                else None
            ),
        )

    def _convert_schedule(self, schedule: Schedule) -> BrowseFolder:
        """Convert Schedule to BrowseFolder."""
        return BrowseFolder(
            item_id="schedule",
            name="Schedule",
            translation_key="schedule",
            provider=self.context.provider.instance_id,
            path=self._build_path("schedule"),
        )

    async def _convert_recommended_item(self, item: RecommendedMenuItem) -> RecommendationFolder:
        """Convert RecommendedMenuItem to RecommendationFolder."""
        if not item or not item.sub_items or not item.title:
            raise ConversionError(f"Incorrect format for item {item}")

        items: list[Track | Radio | MAPodcast | MAPodcastEpisode | BrowseFolder] = []
        for sub_item in item.sub_items:
            new_item = await self.context.provider.adaptor.new_object(sub_item)
            if (
                new_item is not None
                and not isinstance(new_item, RecommendationFolder)
                and not isinstance(new_item, RecommendedMenuItem)
            ):
                items.append(new_item)

        return RecommendationFolder(
            item_id=self._require_id(item.item_id, item),
            name=item.title,
            provider=self.context.provider.instance_id,
            items=UniqueList(items),
        )

    def _build_path(self, item_id: str) -> str:
        """Build path for browse items."""
        if self.context.path_parts:
            return "/".join([*self.context.path_parts, item_id])
        return f"{self.context.provider.instance_id}://{item_id}"


class Adaptor:
    """An adaptor object to convert Sounds API objects into MA ones."""

    def __init__(self, provider: BBCSoundsProvider):
        """Create new adaptor."""
        self.provider = provider
        self.logger = self.provider.logger

    async def new_live_programme(self, programme: ScheduleItem | RadioShow) -> MAPodcastEpisode:
        """
        Convert a scheduled programme to an episode played from the station's live stream.

        :param programme: The programme from a station schedule.
        """
        context = self._create_context(force_type=MAPodcastEpisode)
        return await LiveProgrammeConverter(context).convert(programme)

    def _get_provider_mapping(self, item_id: str) -> ProviderMapping:
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.provider.domain,
            provider_instance=self.provider.instance_id,
        )

    def _create_context(
        self,
        path_parts: list[str] | None = None,
        force_type: (
            type[
                Track
                | Radio
                | MAPodcast
                | MAPodcastEpisode
                | BrowseFolder
                | RecommendationFolder
                | RecommendedMenuItem
            ]
            | None
        ) = None,
        title_parts: TitleParts = TitleParts.LISTING,
    ) -> Context:
        return Context(
            provider=self.provider,
            path_parts=path_parts,
            force_type=force_type,
            title_parts=title_parts,
        )

    async def new_streamable_object(
        self,
        source_obj: SoundsTypes | None,
        force_type: type[Track | Radio | MAPodcastEpisode] | None = None,
        path_parts: list[str] | None = None,
    ) -> StreamDetails | None:
        """
        Convert an auntie-sounds object to appropriate Music Assistant object.

        Args:
            source_obj: The source object from Sounds API via auntie-sounds
            force_type: Force conversion to specific type if the expected target type is known
            path_parts: Path parts for browse items to construct the object's path

        Returns:
            Converted Music Assistant media item or None if no converter found
        """
        if source_obj is None:
            return None
        context = self._create_context(path_parts, force_type)

        converters: list[BaseConverter] = [
            StationConverter(context),
            PodcastConverter(context),
            BrowseConverter(context),
        ]

        for converter in converters:
            if converter.can_convert(source_obj):
                try:
                    stream_details = await converter.get_stream_details(source_obj)
                except AttributeError:
                    self.logger.exception("Error converting object %s", source_obj)
                    return None
                self.provider.logger.log(VERBOSE_LOG_LEVEL, source_obj)
                self.provider.logger.debug(
                    "Successfully converted %s to %s",
                    type(source_obj).__name__,
                    type(stream_details).__name__,
                )
                self.provider.logger.log(VERBOSE_LOG_LEVEL, stream_details)
                return stream_details
        self.provider.logger.warning(
            "No stream converter found for type %s", type(source_obj).__name__
        )
        return None

    async def new_object(
        self,
        source_obj: SoundsTypes | None,
        force_type: (
            type[
                Track
                | Radio
                | MAPodcast
                | MAPodcastEpisode
                | BrowseFolder
                | RecommendationFolder
                | RecommendedMenuItem
            ]
            | None
        ) = None,
        path_parts: list[str] | None = None,
        title_parts: TitleParts = TitleParts.LISTING,
    ) -> (
        Track
        | Radio
        | MAPodcast
        | MAPodcastEpisode
        | BrowseFolder
        | RecommendationFolder
        | RecommendedMenuItem
        | None
    ):
        """
        Convert an auntie-sounds object to appropriate Music Assistant object.

        Args:
            source_obj: The source object from Sounds API via auntie-sounds
            force_type: Force conversion to specific type if the expected target type is known
            path_parts: Path parts for browse items to construct the object's path
            title_parts: The parts of a programme's title to show where it is listed

        Returns:
            Converted Music Assistant media item or None if no converter found
        """
        if source_obj is None:
            return None
        context = self._create_context(path_parts, force_type, title_parts)

        converters: list[BaseConverter] = [
            StationConverter(context),
            LiveProgrammeConverter(context),
            PodcastConverter(context),
            BrowseConverter(context),
        ]
        for converter in converters:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "Checking if converter %s can convert %s",
                converter,
                type(source_obj),
            )
            if converter.can_convert(source_obj):
                try:
                    result = await converter.convert(source_obj)
                except AttributeError:
                    self.logger.exception("Error converting object %s", source_obj)
                    return None
                if context.force_type and type(result) is not context.force_type:
                    msg = (
                        f"Expected forced type of {context.force_type} but received "
                        f"{type(result)} using {type(converter)}"
                    )
                    raise ConversionError(msg)

                self.provider.logger.log(VERBOSE_LOG_LEVEL, source_obj)

                msg = "Successfully converted %s to %s"
                args: list[Any] = [type(source_obj).__name__, type(result).__name__]
                if hasattr(result, "item_id") and result.item_id is not None:
                    msg += " item_id: %s"
                    args.append(result.item_id)
                if hasattr(result, "urn") and result.urn is not None:
                    msg += " urn: %s"
                    args.append(result.urn)
                self.logger.debug(msg, *args)
                self.provider.logger.log(VERBOSE_LOG_LEVEL, result)
                return result
            self.logger.log(
                VERBOSE_LOG_LEVEL, "Converter %s could not convert %s", converter, type(source_obj)
            )

        self.logger.warning("No converter found for type %s", type(source_obj).__name__)
        self.logger.debug(source_obj)
        return None
