from typing import Any, cast

from music_assistant_models.streamdetails import StreamMetadata
from sounds import Segment

from .constants import _Constants


def _find_segment(segments: list[Segment], elapsed_time: int) -> Segment | None:
    segment = None
    if segments:
        segment = next(
            (
                s
                for s in segments
                if isinstance(s, Segment)
                and s.offset
                and int(cast("int", s.offset.get("start")))
                <= elapsed_time
                < int(cast("int", s.offset.get("end")))
            ),
            None,
        )
    return segment


def _segment_to_metadata(now_playing: Segment | dict[str, Any]) -> StreamMetadata:
    """Convert a now-playing segment to StreamMetadata."""
    if isinstance(now_playing, Segment):
        title = now_playing.titles.get("secondary", "")
        artist = now_playing.titles.get("primary", "")
        image_url = now_playing.image_url
        if image_url and _Constants.BLANK_IMAGE_NAME in image_url:
            image_url = None
    elif isinstance(now_playing, dict):
        title = now_playing.get("titles").get("secondary", "") if now_playing else "Unknown title"
        artist = now_playing.get("titles").get("primary", "") if now_playing else "Unknown artist"
        image_url = now_playing.get("image_url")
        if image_url and _Constants.BLANK_IMAGE_NAME in image_url:
            image_url = None
    return StreamMetadata(title=title, artist=artist, image_url=image_url)
