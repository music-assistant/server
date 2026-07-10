"""Guess the song quiz type: guess the currently playing track."""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Playlist, Track

from music_assistant.models.plugin import PluginProvider
from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.models import (
    MultipleChoiceRoundState,
    MusicQuizAnswerType,
    MusicQuizDifficulty,
    MusicQuizRound,
)
from music_assistant.providers.music_quiz.quiz_types.base import QuizType
from music_assistant.providers.music_quiz.suggestions import (
    SuggestionCandidate,
    build_answer_label,
    build_suggestions,
    parse_ai_distractors,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Artist, ItemMapping

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.music_quiz.models import MusicQuizConfig

LOGGER = logging.getLogger(__name__)


class GuessTheSongQuizType(QuizType):
    """Quiz type where players guess the currently playing track."""

    answer_type = MusicQuizAnswerType.MULTIPLE_CHOICE

    def __init__(self, mass: MusicAssistant, config: MusicQuizConfig) -> None:
        """
        Initialize the guess-the-song quiz type for a single game.

        :param mass: MusicAssistant instance.
        :param config: Config of the game this quiz type generates rounds for.
        """
        super().__init__(mass, config)
        self._source_track_pool: dict[str, Track] | None = None

    async def prepare_round(
        self, round_index: int, previous_rounds: list[MusicQuizRound]
    ) -> MusicQuizRound:
        """
        Prepare a guess-the-song round from the configured sources.

        :param round_index: Index of the round to prepare.
        :param previous_rounds: Rounds already prepared in earlier iterations.
        :raises InvalidDataError: If no unused source track or not enough
            distractors are available.
        :return: The prepared (not yet started) round.
        """
        used_track_uris = {
            previous_round.track_uri
            for previous_round in previous_rounds
            if previous_round.track_uri
        }
        track = await self._get_next_source_track(used_track_uris)
        correct = _track_to_candidate(track)
        distractors = await self._gather_distractors(track, correct)
        try:
            suggestions = build_suggestions(
                correct,
                distractors,
                self.config.suggestion_count,
            )
        except ValueError as err:
            raise InvalidDataError(
                str(err),
                translation_key="music_quiz_not_enough_distractors",
                translation_owner=TRANSLATION_OWNER,
            ) from err
        assert track.uri is not None  # guaranteed by _get_next_source_track
        return MusicQuizRound(
            round_index=round_index,
            track_uri=track.uri,
            answer_label=correct.label,
            answer_state=MultipleChoiceRoundState(suggestions=suggestions),
            image_url=await self.mass.metadata.get_image_url_for_item(track),
            duration=track.duration,
        )

    async def _get_next_source_track(self, used_track_uris: set[str]) -> Track:
        """Return a random unused track from the configured sources."""
        pool = await self._get_source_track_pool()
        available = [track for uri, track in pool.items() if uri not in used_track_uris]
        if not available:
            raise InvalidDataError(
                "No unused source tracks are available",
                translation_key="music_quiz_no_unused_source_tracks",
                translation_owner=TRANSLATION_OWNER,
            )
        return secrets.choice(available)

    async def _get_source_track_pool(self) -> dict[str, Track]:
        """Return all configured source tracks keyed by URI, fetched once per game."""
        if self._source_track_pool is not None:
            return self._source_track_pool
        if not self.config.source_uris:
            raise InvalidDataError(
                "At least one source URI is required",
                translation_key="music_quiz_source_required",
                translation_owner=TRANSLATION_OWNER,
            )
        pool: dict[str, Track] = {}
        for source_uri in self.config.source_uris:
            # skip individual unavailable sources so one bad source does not
            # abort a round that other sources can still populate
            try:
                media_item = await self.mass.music.get_item_by_uri(source_uri)
                if isinstance(media_item, Track):
                    if media_item.uri:
                        pool[media_item.uri] = media_item
                    continue
                if isinstance(media_item, Playlist):
                    async for track in self.mass.music.playlists.tracks(
                        item_id=media_item.item_id,
                        provider_instance_id_or_domain=media_item.provider,
                    ):
                        if isinstance(track, Track) and track.uri:
                            pool[track.uri] = track
            except Exception as err:
                LOGGER.warning("Could not load Music Quiz source %s: %s", source_uri, err)
        if not pool:
            raise InvalidDataError(
                "None of the configured sources could be loaded",
                translation_key="music_quiz_sources_unavailable",
                translation_owner=TRANSLATION_OWNER,
            )
        self._source_track_pool = pool
        return pool

    async def _gather_distractors(
        self, track: Track, correct: SuggestionCandidate
    ) -> list[SuggestionCandidate]:
        """
        Collect distractor candidates ordered by preference for the game settings.

        :param track: The source track that is the correct answer this round.
        :param correct: The correct answer candidate.
        """
        candidates: list[SuggestionCandidate] = []
        if self.config.use_ai_distractors:
            candidates.extend(await self._get_ai_distractors(correct))
        if self.config.difficulty == MusicQuizDifficulty.HARD:
            candidates.extend(await self._get_similar_distractors(track))
        elif self.config.difficulty == MusicQuizDifficulty.EASY:
            candidates.extend(await self._get_easy_distractors(track))
        # the label search doubles as the normal-difficulty source and as the
        # universal fallback, so a round never fails when preferred sources are sparse
        candidates.extend(await self._get_search_distractors(correct))
        return candidates

    async def _get_search_distractors(
        self, correct: SuggestionCandidate
    ) -> list[SuggestionCandidate]:
        """Return distractors from a catalog search on the correct answer's label."""
        search_results = await self.mass.music.search(
            search_query=correct.label,
            media_types=[MediaType.TRACK],
            limit=max(self.config.suggestion_count * 8, 24),
            library_only=False,
        )
        return [
            _track_to_candidate(item) for item in search_results.tracks if isinstance(item, Track)
        ]

    async def _get_similar_distractors(self, track: Track) -> list[SuggestionCandidate]:
        """Return plausible distractors from tracks and artists similar to the source track."""
        limit = max(self.config.suggestion_count * 4, 12)
        candidates: list[SuggestionCandidate] = []
        try:
            similar = await self.mass.music.tracks.similar_tracks(
                item_id=track.item_id,
                provider_instance_id_or_domain=track.provider,
                limit=limit,
            )
            candidates.extend(_track_to_candidate(item) for item in similar)
        except Exception as err:
            LOGGER.debug("Could not fetch similar tracks for %s: %s", track.uri, err)
        if len(candidates) < self.config.suggestion_count - 1 and track.artists:
            candidates.extend(await self._get_similar_artist_distractors(track.artists[0], limit))
        return candidates

    async def _get_similar_artist_distractors(
        self, artist: Artist | ItemMapping, limit: int
    ) -> list[SuggestionCandidate]:
        """Return distractors from the top track of each artist similar to the given artist."""
        candidates: list[SuggestionCandidate] = []
        try:
            similar_artists = await self.mass.music.artists.similar_artists(
                item_id=artist.item_id,
                provider_instance_id_or_domain=artist.provider,
                limit=self.config.suggestion_count,
            )
        except Exception as err:
            LOGGER.debug("Could not fetch similar artists for %s: %s", artist.name, err)
            return candidates
        for similar_artist in similar_artists:
            if len(candidates) >= limit:
                break
            try:
                top_tracks = await self.mass.music.artists.top_tracks(
                    item_id=similar_artist.item_id,
                    provider_instance_id_or_domain=similar_artist.provider,
                )
            except Exception as err:
                LOGGER.debug("Could not fetch top tracks for %s: %s", similar_artist.name, err)
                continue
            if top_tracks:
                candidates.append(_track_to_candidate(top_tracks[0]))
        return candidates

    async def _get_easy_distractors(self, track: Track) -> list[SuggestionCandidate]:
        """Return obviously-different distractors sampled from the configured source pool."""
        pool = await self._get_source_track_pool()
        others = [item for uri, item in pool.items() if uri != track.uri]
        sample_size = min(len(others), max(self.config.suggestion_count * 4, 12))
        sampled = secrets.SystemRandom().sample(others, sample_size)
        return [_track_to_candidate(item) for item in sampled]

    async def _get_ai_distractors(self, correct: SuggestionCandidate) -> list[SuggestionCandidate]:
        """Return AI-generated distractors, or an empty list when unavailable or unusable."""
        prompt = self._build_ai_prompt(correct)
        for provider in self.mass.get_providers_supporting_feature(ProviderFeature.AI_QUERY):
            if not isinstance(provider, PluginProvider):
                continue
            try:
                response = await provider.ai_query(prompt)
            except Exception as err:
                LOGGER.debug("AI distractor query failed via %s: %s", provider.instance_id, err)
                continue
            if candidates := parse_ai_distractors(response):
                return candidates
        return []

    def _build_ai_prompt(self, correct: SuggestionCandidate) -> str:
        """Build the prompt asking an AI provider for plausible wrong answers."""
        wanted = self.config.suggestion_count - 1
        return (
            "You are helping build a 'guess the song' music quiz. "
            f"Suggest {wanted} plausible but INCORRECT song choices that could fool a player "
            "who knows the correct song. Each must be a real, well-known song by a similar or "
            "related artist and in a similar style, but must not be the correct song or a "
            "different version or remix of it. Reply with only the wrong choices, one per line, "
            "each formatted exactly as 'Artist - Title', with no numbering, quotes or extra text.\n"
            f"Correct answer: {correct.label}"
        )


def _track_to_candidate(track: Track) -> SuggestionCandidate:
    """Convert a track to an answer suggestion candidate."""
    return SuggestionCandidate(
        label=build_answer_label(track.artist_str or None, track.name),
        uri=track.uri,
        title=track.name,
    )
