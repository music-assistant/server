"""Helpers for parsing Overcast's extended OPML export."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit
from xml.parsers.expat import ExpatError

import xmltodict
from music_assistant_models.errors import InvalidDataError


@dataclass
class OvercastEpisodeState:
    """Playback state of a single episode as exported by Overcast."""

    overcast_id: str | None
    title: str | None
    enclosure_url: str | None
    pub_date: datetime | None
    progress_s: int | None
    played: bool
    user_updated_at: datetime | None


@dataclass
class OvercastSubscription:
    """A single subscribed podcast feed from the Overcast export."""

    xml_url: str
    title: str | None
    overcast_id: str | None
    episodes: list[OvercastEpisodeState] = field(default_factory=list)
    # lazily built enclosure url -> state index, see match_episode_state
    _state_index: dict[str, OvercastEpisodeState] | None = field(
        default=None, repr=False, compare=False
    )


def parse_extended_opml(xml_text: str) -> dict[str, OvercastSubscription]:
    """
    Parse Overcast's extended OPML export into the subscribed feeds, keyed by feed url.

    :param xml_text: The raw OPML document as returned by the export endpoint.
    """
    try:
        document = xmltodict.parse(xml_text, force_list=("outline",))
    except ExpatError as err:
        raise InvalidDataError("Invalid Overcast OPML document") from err

    body = (document.get("opml") or {}).get("body") or {}
    subscriptions: dict[str, OvercastSubscription] = {}
    for group in body.get("outline", []):
        # the export also contains a "playlists" group, only "feeds" is relevant
        if group.get("@text") != "feeds":
            continue
        for feed in group.get("outline", []):
            if feed.get("@type") != "rss":
                continue
            xml_url = feed.get("@xmlUrl")
            if not xml_url or feed.get("@subscribed") != "1":
                continue
            episodes = [
                _parse_episode_outline(episode)
                for episode in feed.get("outline", [])
                if episode.get("@type") == "podcast-episode"
            ]
            subscriptions[xml_url] = OvercastSubscription(
                xml_url=xml_url,
                title=feed.get("@title"),
                overcast_id=feed.get("@overcastId"),
                episodes=episodes,
            )
    return subscriptions


def match_episode_state(
    subscription: OvercastSubscription, stream_url: str
) -> OvercastEpisodeState | None:
    """
    Find the Overcast playback state matching an episode's stream url.

    :param subscription: The subscription holding the episode states.
    :param stream_url: The episode's enclosure/stream url from the RSS feed.
    """
    if subscription._state_index is None:
        subscription._state_index = _build_state_index(subscription.episodes)
    if (state := subscription._state_index.get(stream_url)) is not None:
        return state
    # signed enclosure urls may rotate their query part between exports,
    # so look up again with query and fragment stripped
    return subscription._state_index.get(_strip_url(stream_url))


def _build_state_index(
    episodes: list[OvercastEpisodeState],
) -> dict[str, OvercastEpisodeState]:
    # exact urls are indexed first so they win from a stripped url of another state,
    # and the first state wins per url to keep the export's order authoritative
    index: dict[str, OvercastEpisodeState] = {}
    for state in episodes:
        if state.enclosure_url:
            index.setdefault(state.enclosure_url, state)
    for state in episodes:
        if state.enclosure_url:
            index.setdefault(_strip_url(state.enclosure_url), state)
    return index


def _parse_episode_outline(episode: dict[str, str]) -> OvercastEpisodeState:
    return OvercastEpisodeState(
        overcast_id=episode.get("@overcastId"),
        title=episode.get("@title"),
        enclosure_url=episode.get("@enclosureUrl"),
        pub_date=_parse_datetime(episode.get("@pubDate")),
        progress_s=_parse_int(episode.get("@progress")),
        played=episode.get("@played") == "1",
        user_updated_at=_parse_datetime(episode.get("@userUpdatedDate")),
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # normalize to an aware datetime so values can safely be compared
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _strip_url(url: str) -> str:
    return urlsplit(url)._replace(query="", fragment="").geturl()
