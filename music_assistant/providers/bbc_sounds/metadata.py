"""Metadata-related functions for BBC Sounds provider."""

from music_assistant_models.streamdetails import StreamMetadata
from sounds.models import LiveStation, Segment

from .constants import _Constants


def _find_segment(segments: list[Segment], elapsed_time: int) -> Segment | None:
    segment = None
    if segments:
        segment = next(
            (
                s
                for s in segments
                if isinstance(s, Segment)
                and s.offset is not None
                and (start := s.offset.get("start")) is not None
                and (end := s.offset.get("end")) is not None
                and int(start) <= elapsed_time < int(end)
            ),
            None,
        )
    return segment


def _segment_to_metadata(now_playing: Segment) -> StreamMetadata | None:
    """Convert a now-playing segment to StreamMetadata."""
    if isinstance(now_playing, Segment):
        title = now_playing.titles.get("secondary", "")
        artist = now_playing.titles.get("primary", "")
        image_url = now_playing.image_url
        if image_url and _Constants.BLANK_IMAGE_NAME in image_url:
            image_url = None
        return StreamMetadata(title=title, artist=artist, image_url=image_url)
    return None


def _station_programme_display(station: LiveStation) -> StreamMetadata | None:
    if station and station.titles:
        title = f"{station.titles.get('secondary')} • {station.titles.get('primary')}"
        return StreamMetadata(title=title, artist=None, image_url=station.image_url)
    return None
