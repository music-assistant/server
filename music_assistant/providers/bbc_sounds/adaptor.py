"""Adaptor for converting BBC Sounds objects to Music Assistant media items.

Many Sounds API endpoints return containers of "PlayableObjects" which can be a
range of different types. The auntie-sounds library detects these differing
types and provides a sensible set of objects to work with, e.g. RadioShow.

This adaptor maps those objects to the most sensible type for MA.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, ImageType, MediaType, StreamType
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
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
from sounds.models import (
    Category,
    Collection,
    LiveStation,
    MenuItem,
    Podcast,
    PodcastEpisode,
    RadioClip,
    RadioSeries,
    RadioShow,
    RecommendedMenuItem,
    Schedule,
    SoundsTypes,
    Station,
    StationSearchResult,
)

import music_assistant.helpers.datetime as dt
from music_assistant.helpers.datetime import LOCAL_TIMEZONE

if TYPE_CHECKING:
    from music_assistant.providers.bbc_sounds import BBCSoundsProvider


def _date_convertor(
    timestamp: str | datetime,
    date_format: str,
    timezone: tzinfo | None = LOCAL_TIMEZONE,
) -> str:
    if isinstance(timestamp, str):
        timestamp = dt.from_iso_string(timestamp)
    else:
        timestamp = timestamp.astimezone(timezone)
    return timestamp.strftime(date_format)


def _to_time(timestamp: str | datetime) -> str:
    return _date_convertor(timestamp, "%H:%M")


def _to_date_and_time(timestamp: str | datetime) -> str:
    return _date_convertor(timestamp, "%a %d %B %H:%M")


def _to_date(timestamp: str | datetime) -> str:
    return _date_convertor(timestamp, "%d/%m/%y")


class ConversionError(Exception):
    """Raised when object conversion fails."""


class ImageProvider:
    """Handles image URL resolution and MediaItemImage creation."""

    # TODO: keeping this in for demo purposes
    ICON_BASE_URL = (
        "https://cdn.jsdelivr.net/gh/kieranhogg/auntie-sounds@main/src/sounds/icons/solid"
    )

    ICON_MAPPING = {
        "listen_live": "listen_live",
        "continue_listening": "continue",
        "editorial_collection": "editorial",
        "local_rail": "my_location",
        "single_item_promo": "featured",
        "collections": "collections",
        "categories": "categories",
        "recommendations": "my_sounds",
        "unmissable_speech": "speech",
        "unmissable_music": "music",
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

    provider: "BBCSoundsProvider"
    provider_domain: str
    path_parts: list[str] | None = None
    force_type: (
        type[Track]
        | type[LiveStation]
        | type[Radio]
        | type[MAPodcast]
        | type[MAPodcastEpisode]
        | type[BrowseFolder]
        | type[RecommendationFolder]
        | type[RecommendedMenuItem]
        | None
    ) = None


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
        | LiveStation
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
        return self.context.provider._get_provider_mapping(item_id)

    def _get_attr(self, obj: Any, attr_path: str, default: Any = None) -> Any:
        """Get (optionally-nested) attribute from object.

        Supports e.g. _get_attr(object, "thing.other_thing")
        """
        # TODO: I'm fairly sure there is existing code/libs for this?
        try:
            current = obj
            for part in attr_path.split("."):
                if hasattr(current, part):
                    current = getattr(current, part)
                elif isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    return default
            return current
        except (AttributeError, KeyError, TypeError):
            return default


class StationConverter(BaseConverter):
    """Converts Station-related objects."""

    type ConvertableTypes = Station | LiveStation | StationSearchResult
    convertable_types = (Station, LiveStation, StationSearchResult)

    def can_convert(self, source_obj: ConvertableTypes) -> bool:
        """Check if this converter can convert to a Station object."""
        return isinstance(source_obj, self.convertable_types)

    async def get_stream_details(self, source_obj: Station | LiveStation) -> StreamDetails | None:
        """Convert the source object to a stream."""
        from music_assistant.providers.bbc_sounds import FEATURES, _Constants  # noqa: PLC0415

        # TODO: can't seek this stream
        station = await self.convert(source_obj)
        if not station or not source_obj.stream:
            return None
        show_time = self._get_attr(source_obj, "titles.secondary")
        show_title = self._get_attr(source_obj, "titles.primary")
        programme_name = f"{show_time} • {show_title}"
        stream_details = None
        if station and source_obj.stream:
            if FEATURES["now_playing"]:
                stream_metadata = StreamMetadata(
                    title=programme_name,
                )

                if station.image is not None:
                    stream_metadata.image_url = station.image.path
            else:
                stream_metadata = None

            stream_details = StreamDetails(
                stream_metadata=stream_metadata,
                media_type=MediaType.RADIO,
                stream_type=StreamType.HLS
                if self.context.provider.stream_format == _Constants.HLS
                else StreamType.HTTP,
                path=str(source_obj.stream),
                item_id=station.item_id,
                provider=station.provider,
                audio_format=AudioFormat(
                    content_type=ContentType.try_parse(str(source_obj.stream))
                ),
                data={
                    "provider": self.context.provider_domain,
                    "station": station.item_id,
                },
            )
        return stream_details

    async def convert(self, source_obj: ConvertableTypes) -> Radio:
        """Convert the source object to target type."""
        if isinstance(source_obj, Station):
            return self._convert_station(source_obj)
        elif isinstance(source_obj, LiveStation):
            return self._convert_live_station(source_obj)
        elif isinstance(source_obj, StationSearchResult):
            return self._convert_station_search_result(source_obj)
        self.logger.error(f"Failed to convert station {type(source_obj)}: {source_obj}")
        raise ConversionError(f"Failed to convert station {type(source_obj)}: {source_obj}")

    def _convert_station(self, station: Station) -> Radio:
        """Convert Station object."""
        image_url = self._get_attr(station, "image_url")

        radio = Radio(
            item_id=station.id,
            # Add BBC prefix back to station to help identify station within MA
            name=f"BBC {self._get_attr(station, 'title', 'Unknown')}",
            provider=self.context.provider_domain,
            metadata=ImageProvider.create_metadata_with_image(
                image_url, self.context.provider_domain
            ),
            provider_mappings={self._create_provider_mapping(station.id)},
        )
        if station.stream:
            radio.uri = station.stream.uri
        return radio

    def _convert_live_station(self, station: LiveStation) -> Radio:
        """Convert LiveStation object."""
        name = self._get_attr(station, "network.short_title", "Unknown")
        image_url = self._get_attr(station, "network.logo_url")

        return Radio(
            item_id=station.id,
            name=f"BBC {name}",
            provider=self.context.provider_domain,
            metadata=ImageProvider.create_metadata_with_image(
                image_url, self.context.provider_domain
            ),
            provider_mappings={self._create_provider_mapping(station.id)},
        )

    def _convert_station_search_result(self, station: StationSearchResult) -> Radio:
        """Convert StationSearchResult object."""
        return Radio(
            item_id=station.service_id,
            name=f"BBC {station.station_name}",
            provider=self.context.provider_domain,
            metadata=ImageProvider.create_metadata_with_image(
                station.station_image_url, self.context.provider_domain
            ),
            provider_mappings={self._create_provider_mapping(station.service_id)},
        )


class PodcastConverter(BaseConverter):
    """Converts podcast-related objects."""

    type ConvertableTypes = Podcast | PodcastEpisode | RadioShow | RadioClip | RadioSeries
    convertable_types = (Podcast, PodcastEpisode, RadioShow, RadioClip, RadioSeries)
    type OutputTypes = MAPodcast | MAPodcastEpisode | Track
    output_types = MAPodcast | MAPodcastEpisode | Track
    SCHEDULE_ITEM_FORMAT = "{start} {show_name} • {show_title} ({date})"
    SCHEDULE_ITEM_DEFAULT_FORMAT = "{show_name} • {show_title}"
    PODCAST_EPISODE_DEFAULT_FORMAT = "{episode_title} ({date})"
    PODCAST_EPISODE_DETAILED_FORMAT = "{episode_title} • {detail} ({date})"

    def _format_show_title(self, show: RadioShow) -> str:
        if show is None:
            return "Unknown show"
        if show.start and show.titles:
            return self.SCHEDULE_ITEM_FORMAT.format(
                start=_to_time(show.start),
                show_name=show.titles["primary"],
                show_title=show.titles["secondary"],
                date=_to_date(show.start),
            )
        elif show.titles:
            # TODO: when getting a schedule listing, we have a broadcast time
            # when we fetch the streaming details later we lose that from the new API call
            title = self.SCHEDULE_ITEM_DEFAULT_FORMAT.format(
                show_name=show.titles["primary"],
                show_title=show.titles["secondary"],
            )
            date = show.release.get("date") if show.release else None
            if date and isinstance(date, (str, datetime)):
                title += f" ({_to_date(date)})"
            return title
        return "Unknown"

    def _format_podcast_episode_title(self, episode: PodcastEpisode) -> str:
        # Similar to show, but not quite: we expect to see this in the context of a podcast detail
        # page
        if episode is None:
            return "Unknown episode"

        if episode.release:
            date = episode.release.get("date")
        elif episode.availability:
            date = episode.availability.get("from")
        else:
            date = None
        if isinstance(date, (str, datetime)) and episode.titles:
            datestamp = _to_date(date)
            title = self.PODCAST_EPISODE_DEFAULT_FORMAT.format(
                episode_title=episode.titles.get("secondary"),
                date=datestamp,
            )
        else:
            title = str(episode.titles.get("secondary")) if episode.titles else "Unknown episode"
        return title

    def can_convert(self, source_obj: ConvertableTypes) -> bool:
        """Check if this converter can convert to a Podcast object."""
        # Can't use type alias here https://github.com/python/mypy/issues/11673
        if self.context.force_type:
            return issubclass(self.context.force_type, self.output_types)
        return isinstance(source_obj, self.convertable_types)

    async def get_stream_details(self, source_obj: ConvertableTypes) -> StreamDetails | None:
        """Convert the source object to a stream."""
        from music_assistant.providers.bbc_sounds import _Constants  # noqa: PLC0415

        if isinstance(source_obj, (Podcast, RadioSeries)):
            return None
        stream_details = None
        episode = await self.convert(source_obj)
        if (
            episode
            and isinstance(episode, MAPodcastEpisode)
            and (episode.metadata.description or episode.name)
            and source_obj.stream
        ):
            stream_details = StreamDetails(
                stream_metadata=StreamMetadata(
                    title=episode.metadata.description or episode.name,
                    uri=source_obj.stream,
                ),
                media_type=MediaType.PODCAST_EPISODE,
                stream_type=StreamType.HLS
                if self.context.provider.stream_format == _Constants.HLS
                else StreamType.HTTP,
                path=source_obj.stream,
                item_id=source_obj.id,
                provider=self.context.provider_domain,
                audio_format=AudioFormat(content_type=ContentType.try_parse(source_obj.stream)),
                allow_seek=True,
                can_seek=True,
                duration=(episode.duration if episode.duration else None),
                seek_position=(int(episode.position) if episode.position else 0),
                seconds_streamed=(int(episode.position) if episode.position else 0),
            )
        elif episode and isinstance(episode, Track) and source_obj.stream:
            # Try to work out the best network/series name to display
            if source_obj.network and source_obj.network.id == "bbc_webonly":
                title = "BBC News"
            elif source_obj.network:
                title = f"BBC {source_obj.network.short_title}"
            elif source_obj.container:
                title = source_obj.container.title
            elif episode.metadata and episode.metadata.description:
                title = episode.metadata.description
            elif source_obj.titles:
                title = source_obj.titles["primary"]
            else:
                title = ""

            metadata = StreamMetadata(title=title, uri=source_obj.stream)
            if episode.metadata.images:
                metadata.image_url = episode.metadata.images[0].path

            stream_details = StreamDetails(
                stream_metadata=metadata,
                media_type=MediaType.TRACK,
                stream_type=StreamType.HLS
                if self.context.provider.stream_format == _Constants.HLS
                else StreamType.HTTP,
                path=source_obj.stream,
                item_id=episode.item_id,
                provider=self.context.provider_domain,
                audio_format=AudioFormat(content_type=ContentType.try_parse(source_obj.stream)),
                can_seek=True,
                duration=episode.duration,
            )
        return stream_details

    async def convert(self, source_obj: ConvertableTypes) -> OutputTypes:
        """Convert podcast objects."""
        if isinstance(source_obj, (Podcast, RadioSeries)) or self.context.force_type is Podcast:
            return await self._convert_podcast(source_obj)
        elif isinstance(source_obj, PodcastEpisode):
            return await self._convert_podcast_episode(source_obj)
        elif isinstance(source_obj, RadioShow):
            return await self._convert_radio_show(source_obj)
        elif isinstance(source_obj, RadioClip) or self.context.force_type is Track:
            return await self._convert_radio_clip(source_obj)
        return source_obj

    async def _convert_podcast(self, podcast: Podcast | RadioSeries) -> MAPodcast:
        name = self._get_attr(podcast, "titles.primary") or self._get_attr(podcast, "title")
        description = self._get_attr(podcast, "synopses.long") or self._get_attr(
            podcast, "synopses.short"
        )
        image_url = self._get_attr(podcast, "image_url") or self._get_attr(
            podcast, "sub_items.image_url"
        )

        return MAPodcast(
            item_id=podcast.id,
            name=name,
            provider=self.context.provider_domain,
            metadata=ImageProvider.create_metadata_with_image(
                image_url, self.context.provider_domain, description
            ),
            provider_mappings={self._create_provider_mapping(podcast.item_id)},
        )

    async def _convert_podcast_episode(self, episode: PodcastEpisode) -> MAPodcastEpisode:
        duration = self._get_attr(episode, "duration.value")
        progress_ms = self._get_attr(episode, "progress.value")
        resume_position = (progress_ms * 1000) if progress_ms else None
        description = self._get_attr(episode, "synopses.short")

        # Handle parent podcast
        podcast = None
        if hasattr(episode, "container") and episode.container:
            podcast = await PodcastConverter(self.context).convert(episode.container)

        if not podcast or not isinstance(podcast, MAPodcast):
            raise ConversionError(f"No podcast for episode {episode}")
        if not episode or not episode.pid:
            raise ConversionError(f"No podcast episode for {episode}")

        return MAPodcastEpisode(
            item_id=episode.pid,
            name=self._format_podcast_episode_title(episode),
            provider=self.context.provider_domain,
            duration=duration,
            position=0,
            resume_position_ms=resume_position,
            metadata=ImageProvider.create_metadata_with_image(
                episode.image_url,
                self.context.provider_domain,
                description,
            ),
            podcast=podcast,
            provider_mappings={self._create_provider_mapping(episode.pid)},
            uri=episode.stream,
        )

    async def _convert_radio_show(self, show: RadioShow) -> MAPodcastEpisode | Track:
        from music_assistant.providers.bbc_sounds import _Constants  # noqa: PLC0415

        duration = self._get_attr(show, "duration.value")
        progress_ms = self._get_attr(show, "progress.value")
        resume_position = (progress_ms * 1000) if progress_ms else None

        if not show or not show.pid:
            raise ConversionError(f"No radio show for {show}")

        # Determine if this should be an episode or track based on duration/context
        # TODO: picked a sensible default but need to investigate if this makes sense
        # Track example: latest BBC News, PodcastEpisode: latest episode of a radio show
        if (
            self.context.force_type == Track
            or (
                not self.context.force_type
                and duration
                and duration < _Constants.TRACK_DURATION_THRESHOLD
            )
            or (not hasattr(show, "container") or not show.container)
        ):
            return Track(
                item_id=show.pid,
                name=self._format_show_title(show),
                provider=self.context.provider_domain,
                duration=duration,
                metadata=ImageProvider.create_metadata_with_image(
                    url=show.image_url,
                    provider=self.context.provider_domain,
                    description=show.synopses.get("long") if show.synopses else None,
                ),
                provider_mappings={self._create_provider_mapping(show.pid)},
            )
        else:
            # Handle as episode
            podcast = None
            if hasattr(show, "container") and show.container:
                podcast = await PodcastConverter(self.context).convert(show.container)

            if not podcast or not isinstance(podcast, MAPodcast):
                raise ConversionError(f"No podcast for episode for {show}")

            return MAPodcastEpisode(
                item_id=show.pid,
                name=self._format_show_title(show),
                provider=self.context.provider_domain,
                duration=duration,
                resume_position_ms=resume_position,
                metadata=ImageProvider.create_metadata_with_image(
                    show.image_url, self.context.provider_domain
                ),
                podcast=podcast,
                provider_mappings={self._create_provider_mapping(show.pid)},
                position=1,
            )

    async def _convert_radio_clip(self, clip: RadioClip) -> Track | MAPodcastEpisode:
        duration = self._get_attr(clip, "duration.value")
        description = self._get_attr(clip, "network.short_title")

        if not clip or not clip.pid:
            raise ConversionError(f"No clip for {clip}")

        if self.context.force_type is MAPodcastEpisode:
            podcast = None
            if hasattr(clip, "container") and clip.container:
                podcast = await PodcastConverter(self.context).convert(clip.container)

            if not podcast or not isinstance(podcast, MAPodcast):
                raise ConversionError(f"No podcast for episode for {clip}")
            return MAPodcastEpisode(
                item_id=clip.pid,
                name=self._get_attr(clip, "titles.entity_title", "Unknown title"),
                provider=self.context.provider_domain,
                duration=duration,
                metadata=ImageProvider.create_metadata_with_image(
                    clip.image_url, self.context.provider_domain, description
                ),
                provider_mappings={self._create_provider_mapping(clip.pid)},
                podcast=podcast,
                position=0,
            )
        else:
            return Track(
                item_id=clip.pid,
                name=self._get_attr(clip, "titles.entity_title", "Unknown Track"),
                provider=self.context.provider_domain,
                duration=duration,
                metadata=ImageProvider.create_metadata_with_image(
                    clip.image_url, self.context.provider_domain, description
                ),
                provider_mappings={self._create_provider_mapping(clip.pid)},
            )


class BrowseConverter(BaseConverter):
    """Converts browsable objects like menus, categories, collections."""

    type ConvertableTypes = MenuItem | Category | Collection | Schedule | RecommendedMenuItem
    convertable_types = (MenuItem, Category, Collection, Schedule, RecommendedMenuItem)
    type OutputTypes = BrowseFolder | RecommendationFolder
    output_types = (BrowseFolder, RecommendationFolder)

    def can_convert(self, source_obj: ConvertableTypes) -> bool:
        """Check if this converter can convert to a Browsable object."""
        can_convert = False
        if self.context.force_type:
            can_convert = issubclass(self.context.force_type, self.output_types)
        else:
            can_convert = isinstance(source_obj, self.convertable_types)
        return can_convert

    async def get_stream_details(self, source_obj: ConvertableTypes) -> StreamDetails | None:
        """Convert the source object to a stream."""
        return None

    async def convert(self, source_obj: ConvertableTypes) -> OutputTypes:
        """Convert browsable objects."""
        if isinstance(source_obj, MenuItem) and self.context.force_type is not RecommendationFolder:
            return self._convert_menu_item(source_obj)
        elif isinstance(source_obj, (Category, Collection)):
            return self._convert_category_or_collection(source_obj)
        elif isinstance(source_obj, Schedule):
            return self._convert_schedule(source_obj)
        elif isinstance(source_obj, RecommendedMenuItem):
            return await self._convert_recommended_item(source_obj)
        self.logger.error(f"Failed to convert browse object {type(source_obj)}: {source_obj}")
        raise ConversionError(f"Browse conversion failed: {source_obj}")

    def _convert_menu_item(self, item: MenuItem) -> BrowseFolder | RecommendationFolder:
        """Convert MenuItem to BrowseFolder or RecommendationFolder."""
        image_url = ImageProvider.get_icon_url(item.item_id)
        image = (
            ImageProvider.create_image(image_url, self.context.provider_domain)
            if image_url
            else None
        )
        if not item or not item.title:
            raise ConversionError(f"No menu item {item}")
        path = self._build_path(item.item_id)

        return_type = BrowseFolder

        if self.context.force_type is RecommendationFolder:
            return_type = RecommendationFolder

        return return_type(
            item_id=item.item_id,
            name=item.title,
            provider=self.context.provider_domain,
            path=path,
            image=image,
        )

    def _convert_category_or_collection(self, item: Category | Collection) -> BrowseFolder:
        """Convert Category or Collection to BrowseFolder."""
        path_prefix = "categories" if isinstance(item, Category) else "collections"
        path = f"{self.context.provider_domain}://{path_prefix}/{item.item_id}"

        return BrowseFolder(
            item_id=item.item_id,
            name=self._get_attr(item, "titles.primary", "Untitled folder"),
            provider=self.context.provider_domain,
            path=path,
            image=(
                ImageProvider.create_image(item.image_url, self.context.provider_domain)
                if item.image_url
                else None
            ),
        )

    def _convert_schedule(self, schedule: Schedule) -> BrowseFolder:
        """Convert Schedule to BrowseFolder."""
        return BrowseFolder(
            item_id="schedule",
            name="Schedule",
            provider=self.context.provider_domain,
            path=self._build_path("schedule"),
        )

    async def _convert_recommended_item(self, item: RecommendedMenuItem) -> RecommendationFolder:
        """Convert RecommendedMenuItem to RecommendationFolder."""
        if not item or not item.sub_items or not item.title:
            raise ConversionError(f"Incorrect format for item {item}")

        # TODO this is messy
        new_adaptor = Adaptor(provider=self.context.provider)
        items: list[Track | Radio | MAPodcast | MAPodcastEpisode | BrowseFolder] = []
        for sub_item in item.sub_items:
            new_item = await new_adaptor.new_object(sub_item)
            if (
                new_item is not None
                and not isinstance(new_item, RecommendationFolder)
                and not isinstance(new_item, RecommendedMenuItem)
            ):
                items.append(new_item)

        return RecommendationFolder(
            item_id=item.item_id,
            name=item.title,
            provider=self.context.provider_domain,
            items=UniqueList(items),
        )

    def _build_path(self, item_id: str) -> str:
        """Build path for browse items."""
        if self.context.path_parts:
            return "/".join([*self.context.path_parts, item_id])
        return f"{self.context.provider_domain}://{item_id}"


class Adaptor:
    """An adaptor object to convert Sounds API objects into MA ones."""

    def __init__(self, provider: "BBCSoundsProvider"):
        """Create new adaptor."""
        self.provider = provider
        self.logger = self.provider.logger
        self._converters: list[BaseConverter] = []

    def _create_context(
        self,
        path_parts: list[str] | None = None,
        force_type: (
            type[Track]
            | type[Any]
            | type[Radio]
            | type[Podcast]
            | type[PodcastEpisode]
            | type[BrowseFolder]
            | type[RecommendationFolder]
            | None
        ) = None,
    ) -> Context:
        return Context(
            provider=self.provider,
            provider_domain=self.provider.domain,
            path_parts=path_parts,
            force_type=force_type,
        )

    async def new_streamable_object(
        self,
        source_obj: SoundsTypes,
        force_type: type[Track] | type[Radio] | type[MAPodcastEpisode] | None = None,
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

        converters = [
            StationConverter(context),
            PodcastConverter(context),
            BrowseConverter(context),
        ]

        for converter in converters:
            if converter.can_convert(source_obj):
                try:
                    stream_details = await converter.get_stream_details(source_obj)
                    self.provider.logger.debug(
                        f"Successfully converted {type(source_obj).__name__}"
                        f" to {type(stream_details).__name__}"
                    )
                    return stream_details
                except Exception as e:
                    self.provider.logger.error(
                        f"Unexpected error in converter {type(converter).__name__}: {e}"
                    )
                    raise
        self.provider.logger.warning(
            f"No stream converter found for type {type(source_obj).__name__}"
        )
        return None

    async def new_object(
        self,
        source_obj: SoundsTypes,
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

        Returns:
            Converted Music Assistant media item or None if no converter found
        """
        if source_obj is None:
            return None

        context = self._create_context(path_parts, force_type)

        converters = [
            StationConverter(context),
            PodcastConverter(context),
            BrowseConverter(context),
        ]
        for converter in converters:
            self.logger.debug(f"Checking if converter {converter} can convert {type(source_obj)}")
            if converter.can_convert(source_obj):
                try:
                    result = await converter.convert(source_obj)
                    if context.force_type:
                        assert type(result) is context.force_type, (
                            f"Forced type to {context.force_type} but received {type(result)} "
                            f"using {type(converter)}"
                        )
                    self.provider.logger.debug(
                        f"Successfully converted {type(source_obj).__name__}"
                        f" to {type(result).__name__} {result}"
                    )
                    return result
                except Exception as e:
                    self.provider.logger.error(
                        f"Unexpected error in converter {type(converter).__name__}: {e}"
                    )
                    raise
            self.logger.debug(f"Converter {converter} could not convert {type(source_obj)}")

        self.logger.warning(f"No converter found for type {type(source_obj).__name__}")
        return None
