"""Helper functions for the DR LYD provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.streamdetails import StreamMetadata

from .constants import IMAGE_URL

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from .models import Channel, DRAudioAsset, DRImageAsset, DRProgramme, DRTrack


def get_image_url(assets: Iterable[DRImageAsset], *targets: str) -> str | None:
    """
    Return the url of the square picture among the image assets, if any.

    :param assets: The image assets of an item of DR.
    :param targets: The targets of the preferred pictures, the first one is the most wanted.
    """
    # a picture whose target is named earlier wins, the other square ones follow ordered by id
    candidates = [
        (targets.index(asset.target) if asset.target in targets else len(targets), asset.id)
        for asset in assets
        if asset.ratio == "1:1"
    ]
    return IMAGE_URL.format(min(candidates)[1]) if candidates else None


def get_audio(assets: list[DRAudioAsset]) -> DRAudioAsset | None:
    """
    Return the audio asset of an episode to play, if it has one.

    :param assets: The audio assets of an episode of DR.
    """
    # the mp3 of the highest bitrate, else the HLS stream
    mp3s = [asset for asset in assets if asset.target == "Progressive" and asset.format == "mp3"]
    if mp3s:
        return max(mp3s, key=lambda asset: asset.bitrate)
    return next((asset for asset in assets if asset.format == "HLS"), None)


def get_stream(assets: list[DRAudioAsset]) -> DRAudioAsset | None:
    """
    Return the audio asset of a channel to stream, if it has one.

    :param assets: The audio assets of a channel of DR.
    """
    # the HLS stream, else the ICY stream of the highest bitrate
    if hls := next((asset for asset in assets if asset.format == "HLS"), None):
        return hls
    icy = [asset for asset in assets if asset.format == "ICY"]
    return max(icy, key=lambda asset: asset.bitrate, default=None)


def search_channels(channels: Iterable[Channel], query: str, limit: int) -> list[Channel]:
    """
    Return the channels that match every word of the query, ignoring case.

    :param channels: The channels to search.
    :param query: The words to find.
    :param limit: The maximum number of channels to return.
    """
    words = query.casefold().split()
    if not words:
        return []
    # DR titles P6 and P8 without their Beat and Jazz, which only the slug has
    matches = [
        channel
        for channel in channels
        if all(word in f"{channel.name} {channel.slug}".casefold() for word in words)
    ]
    return matches[:limit]


def find_track(tracks: Iterable[DRTrack], at: datetime) -> DRTrack | None:
    """
    Return the track playing at the time, if any.

    :param tracks: The tracks DR has played on a channel.
    :param at: The time to look at.
    """
    playing = [track for track in tracks if track.played_time <= at < track.end_time]
    # when tracks overlap, the one that started last is the one playing
    return max(playing, key=lambda track: track.played_time, default=None)


def find_programme(
    programmes: Iterable[DRProgramme], channel_slug: str, at: datetime
) -> DRProgramme | None:
    """
    Return the programme on air on the channel at the time, none when it is over.

    :param programmes: The programmes on air now, one for every channel.
    :param channel_slug: The slug of the channel.
    :param at: The time to look at.
    """
    programme = next((entry for entry in programmes if entry.channel.slug == channel_slug), None)
    return programme if programme is not None and programme.end_time > at else None


def get_stream_metadata(
    channel: Channel, tracks: Iterable[DRTrack], programmes: Iterable[DRProgramme], at: datetime
) -> StreamMetadata:
    """
    Return the stream metadata of what the channel plays at the time.

    :param channel: The channel to look at.
    :param tracks: The tracks DR has played on the channel.
    :param programmes: The programmes on air now, one for every channel.
    :param at: The time to look at.
    """
    # a track playing, else the programme on air, else the channel itself
    programme = find_programme(programmes, channel.slug, at)
    picture = get_image_url(programme.image_assets, "SquareImage") if programme else None
    if track := find_track(tracks, at):
        return StreamMetadata(title=track.title, artist=_get_artist(track), image_url=picture)
    if programme:
        return StreamMetadata(title=programme.title, image_url=picture)
    return StreamMetadata(title=channel.name)


def _get_artist(track: DRTrack) -> str | None:
    """Return the artist line of a track."""
    # the credits of classical music are a long list, so the composers are named instead
    if track.classical:
        composers = dict.fromkeys(
            role.name for role in track.roles if role.role.casefold() == "komponist"
        )
        if composers:
            return ", ".join(composers)
    return track.description or None
