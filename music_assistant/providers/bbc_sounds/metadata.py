"""Metadata-related functions for BBC Sounds provider."""

from auntie_sounds.models import LiveStation, Segment
from music_assistant_models.streamdetails import StreamMetadata

from .constants import Constants


def _find_segment(segments: list[Segment], elapsed_time: int) -> Segment | None:
    segment = None
    if segments:
        segment = next(
            (
                s
                for s in segments
                if isinstance(s, Segment)
                and s.offset is not None
                and s.offset.end is not None
                and s.offset.start <= elapsed_time < s.offset.end
            ),
            None,
        )
    return segment


def _segment_to_metadata(now_playing: Segment) -> StreamMetadata | None:
    """Convert a now-playing segment to StreamMetadata."""
    title = now_playing.titles.secondary or ""
    artist = now_playing.titles.primary or ""
    image_url = now_playing.image_url
    if image_url and Constants.BLANK_IMAGE_NAME in image_url:
        image_url = None
    return StreamMetadata(title=title, artist=artist, image_url=image_url)


def _station_programme_display(station: LiveStation) -> StreamMetadata | None:
    if station and station.titles and station.titles.primary:
        title = f"{station.titles.secondary} • {station.titles.primary}"
        return StreamMetadata(title=title, artist=None, image_url=station.image_url)
    return None
