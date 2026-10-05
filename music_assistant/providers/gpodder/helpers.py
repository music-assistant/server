"""Helpers to match gPodder episode actions to the episodes of a feed."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from music_assistant.helpers.podcast_parsers import (
    get_episode_positions,
    get_stream_url_and_guid_from_episode,
)

from .client import EpisodeAction, EpisodeActionDelete, EpisodeActionNew, EpisodeActionPlay

if TYPE_CHECKING:
    from music_assistant_models.media_items import PodcastEpisode

# episode actions of one podcast by guid and episode url, each with its rank (0 is newest)
type ActionIndex = dict[str, tuple[int, EpisodeAction]]


def index_actions(actions: Iterable[EpisodeAction]) -> dict[str, ActionIndex]:
    """Index the actions per podcast by guid and episode url, keeping the newest of each."""
    index: dict[str, ActionIndex] = {}
    # newest first; of actions within the same second, the later reported one first
    for rank, action in enumerate(sorted(actions, key=action_time)[::-1]):
        podcast_actions = index.setdefault(action.podcast, {})
        for key in (action.guid, action.episode):
            if key:
                podcast_actions.setdefault(key, (rank, action))
    return index


def action_time(action: EpisodeAction) -> float:
    """Return when an action happened, an action without a readable time counts as oldest."""
    try:
        timestamp = datetime.fromisoformat(action.timestamp)
    except ValueError:
        return 0.0
    if timestamp.tzinfo is None:
        # the gpodder api sends utc without an offset
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.timestamp()


def find_action(
    actions: ActionIndex, guid: str | None, stream_url: str | None
) -> EpisodeAction | None:
    """Return the newest action of an episode, which a client may know by guid or by url."""
    found = [actions[key] for key in (guid, stream_url) if key is not None and key in actions]
    return min(found, key=lambda x: x[0])[1] if found else None


def apply_action(mass_episode: PodcastEpisode, action: EpisodeAction) -> None:
    """Set the progress an episode action holds on the episode."""
    if isinstance(action, EpisodeActionNew):
        mass_episode.resume_position_ms = 0
        mass_episode.fully_played = False
    elif isinstance(action, EpisodeActionPlay):
        mass_episode.resume_position_ms = action.position * 1000
        mass_episode.fully_played = action.position >= action.total
    elif isinstance(action, EpisodeActionDelete):
        for mapping in mass_episode.provider_mappings:
            mapping.available = False


def iter_episodes(
    parsed_podcast: dict[str, Any],
) -> Iterator[tuple[int, dict[str, Any], str, str | None]]:
    """Yield position, raw episode, stream url and guid of every playable episode of a feed."""
    episodes = parsed_podcast.get("episodes", [])
    for position, episode in zip(get_episode_positions(episodes), episodes, strict=True):
        try:
            stream_url, guid = get_stream_url_and_guid_from_episode(episode=episode)
        except ValueError:
            # episode enclosure or stream url missing
            continue
        yield position, episode, stream_url, guid
