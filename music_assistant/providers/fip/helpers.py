"""Helper functions for the FIP provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import MediaItemImage, ProviderMapping, Radio
from music_assistant_models.streamdetails import StreamMetadata

from .constants import (
    FIP_STATIONS,
    IMAGE_URL,
    NOW_PLAYING_IMAGE_URL,
    ON_AIR_GRACE,
    STREAM_DELAY,
)


@dataclass(frozen=True)
class LiveItem:
    """One entry of the now-playing service: a song, a programme or a placeholder."""

    # A song and a programme carry start and end times; a placeholder ("Le direct",
    # which the service also puts out for a moment every minute or so) does not.

    title: str
    subtitle: str | None
    image_id: str | None
    start: int | None
    end: int | None
    song_id: str | None

    @property
    def is_timed(self) -> bool:
        """Return whether this is a song or a programme rather than a placeholder."""
        return self.start is not None


@dataclass
class LiveDisplay:
    """What a playing station shows, kept steady against the service's noise."""

    # The service is late and noisy: it names a new song 20 to 40 seconds after it
    # started, puts out its "Le direct" placeholder every minute or so, now and then
    # goes back to the song before, and its "next" turns into a copy of the current
    # song just when it would be needed. Three rules keep the display steady and on time:
    # - while the entry on display is on air, a placeholder is no news;
    # - while the entry on display is on air, an entry that started earlier is no news;
    # - when the entry on display should be over for the listener and the service has
    #   announced what follows, show that without waiting for the service.

    shown: LiveItem | None = None
    announced: LiveItem | None = None

    def update(self, now: LiveItem | None, upcoming: LiveItem | None, at: float) -> bool:
        """
        Take in an answer from the service and return whether the display changed.

        :param now: The service's current entry.
        :param upcoming: The service's next entry.
        :param at: The time of the answer (epoch seconds).
        """
        before = self.shown
        if now is not None and not self._no_news(now, at):
            self.shown = now
        self._remember(upcoming)
        self._promote(at)
        return self.shown != before

    def overdue(self, at: float) -> bool:
        """
        Return whether the entry on display should already be over for the listener.

        :param at: The current time (epoch seconds).
        """
        shown = self.shown
        return bool(shown and shown.end and at >= shown.end + STREAM_DELAY)

    def _no_news(self, item: LiveItem, at: float) -> bool:
        """Return whether an entry from the service should leave the display as it is."""
        shown = self.shown
        if shown is None or not shown.is_timed:
            return False
        on_air = not shown.end or at < shown.end + STREAM_DELAY + ON_AIR_GRACE
        if not on_air:
            return False
        return not item.is_timed or (item.start or 0) < (shown.start or 0)

    def _remember(self, upcoming: LiveItem | None) -> None:
        """Keep the last announcement of what follows the entry on display."""
        shown = self.shown

        def stale(item: LiveItem) -> bool:
            return bool(shown and shown.is_timed and (item.start or 0) <= (shown.start or 0))

        if self.announced and stale(self.announced):
            self.announced = None
        if upcoming and upcoming.is_timed and not stale(upcoming):
            self.announced = upcoming

    def _promote(self, at: float) -> None:
        """Move the display on to the announced entry once it is due."""
        upcoming = self.announced
        if upcoming is None:
            return
        # a song plays to its end, while a programme's songs start inside it
        shown = self.shown
        ends_at = shown.end if shown and shown.song_id and shown.end else 0
        if at < max(ends_at, upcoming.start or 0) + STREAM_DELAY:
            return
        self.announced = None
        self.shown = upcoming


def parse_radio(station_id: str, instance_id: str, provider_domain: str) -> Radio:
    """
    Create a Radio object for a station.

    :param station_id: A known FIP station id; callers must validate.
    :param instance_id: The provider instance id.
    :param provider_domain: The provider domain string.
    """
    station = FIP_STATIONS[station_id]
    radio = Radio(
        provider=instance_id,
        item_id=station_id,
        name=station["name"],
        provider_mappings={
            ProviderMapping(
                item_id=station_id,
                provider_domain=provider_domain,
                provider_instance=instance_id,
                available=True,
            )
        },
    )
    radio.metadata.description = station["description"]
    radio.metadata.add_image(
        MediaItemImage(
            provider=instance_id,
            type=ImageType.THUMB,
            path=IMAGE_URL.format(image_id=station["image_id"]),
            remotely_accessible=True,
        )
    )
    return radio


def parse_live_item(raw: dict[str, Any] | None) -> LiveItem | None:
    """
    Parse one entry of a now-playing answer, or return None for an empty one.

    :param raw: An entry ("now", or one of "next") of the service's answer.
    """
    if not raw or not (title := (raw.get("firstLine") or "").strip()):
        return None
    song_id = raw.get("songUuid")
    return LiveItem(
        title=title,
        subtitle=(raw.get("secondLine") or "").strip() or None,
        image_id=raw.get("cover") or None,
        start=_as_int(raw.get("startTime")),
        end=_as_int(raw.get("endTime")),
        song_id=song_id if song_id and song_id != "None" else None,
    )


def parse_live(data: dict[str, Any]) -> tuple[LiveItem | None, LiveItem | None]:
    """
    Parse a now-playing answer into its current and its next entry.

    :param data: JSON answer of the now-playing service.
    """
    upcoming: list[dict[str, Any]] = data.get("next") or []
    return parse_live_item(data.get("now")), parse_live_item(upcoming[0] if upcoming else None)


def build_stream_metadata(item: LiveItem | None, at: float) -> StreamMetadata | None:
    """
    Build the stream metadata for the entry on display, or None to show the station.

    :param item: The entry on display.
    :param at: The current time (epoch seconds), for the elapsed time.
    """
    # No album, so players show the station there; a placeholder shows the station itself.
    if item is None or not item.is_timed or item.start is None:
        return None
    return StreamMetadata(
        title=item.title,
        artist=item.subtitle,
        image_url=NOW_PLAYING_IMAGE_URL.format(image_id=item.image_id) if item.image_id else None,
        duration=item.end - item.start if item.end else None,
        elapsed_time=max(0, int(at - item.start - STREAM_DELAY)),
        elapsed_time_last_updated=at,
    )


def parse_pull(data: dict[str, Any], at: float) -> tuple[LiveItem | None, LiveItem | None]:
    """
    Parse an answer of the older now-playing service into the song heard and the one after it.

    :param data: JSON answer of the older service.
    :param at: The current time (epoch seconds).
    """
    # the service times songs by the studio, which the listener hears STREAM_DELAY later
    heard_at = at - STREAM_DELAY
    songs = sorted(
        (
            step
            for step in (data.get("steps") or {}).values()
            if step.get("embedType") == "song"
            and _as_int(step.get("start")) is not None
            and _as_int(step.get("end")) is not None
        ),
        key=lambda step: int(step["start"]),
    )
    current = next(
        (step for step in reversed(songs) if int(step["start"]) <= heard_at < int(step["end"])),
        None,
    )
    # songs can overlap by a second or two, so the next one is the next to start
    if current is not None:
        upcoming = next(
            (step for step in songs if int(step["start"]) > int(current["start"])), None
        )
    else:
        upcoming = next((step for step in songs if int(step["start"]) >= heard_at), None)
    return _parse_pull_song(current), _parse_pull_song(upcoming)


def _parse_pull_song(step: dict[str, Any] | None) -> LiveItem | None:
    """Return a song of the older service's answer as an entry, or None for none."""
    if not step or not (title := (step.get("title") or "").strip()):
        return None
    artists = ", ".join(step.get("highlightedArtists") or []) or (step.get("authors") or "").strip()
    return LiveItem(
        title=title,
        subtitle=artists or None,
        image_id=step.get("coverUuid") or None,
        start=_as_int(step.get("start")),
        end=_as_int(step.get("end")),
        song_id=step.get("songId") or None,
    )


def _as_int(value: Any) -> int | None:
    """Return a timestamp from the service as int, or None when absent."""
    try:
        return int(value) if value is not None else None
    except TypeError, ValueError:
        return None
