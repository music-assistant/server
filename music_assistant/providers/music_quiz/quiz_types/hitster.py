"""Hitster quiz type: place the currently playing track on a shared timeline."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import replace
from itertools import batched
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Album, ItemMapping, Track

from music_assistant.helpers.datetime import utc
from music_assistant.providers.music_quiz.errors import TRANSLATION_OWNER
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswerType,
    MusicQuizDifficulty,
    MusicQuizRound,
    TimelineBonusDefinition,
    TimelineBonusMode,
    TimelineBonusOption,
    TimelineBonusType,
    TimelineCandidate,
    TimelineEntry,
    TimelineFreeTextBonusDefinition,
    TimelineMultipleChoiceBonusDefinition,
    TimelineRoundState,
)
from music_assistant.providers.music_quiz.quiz_types.base import QuizType
from music_assistant.providers.music_quiz.suggestions import (
    SuggestionCandidate,
    build_answer_label,
    build_opaque_options,
)

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.music_quiz.models import MusicQuizConfig

LOGGER = logging.getLogger(__name__)

DEFAULT_BONUS_OPTION_COUNT = 4
MIN_RELEASE_YEAR = 1000
TRACK_ENRICHMENT_CONCURRENCY = 10


class HitsterQuizType(QuizType):
    """Quiz type where players place songs on a shared chronological timeline."""

    answer_type = MusicQuizAnswerType.TIMELINE

    def __init__(self, mass: MusicAssistant, config: MusicQuizConfig) -> None:
        """
        Initialize the Hitster quiz type for a single game.

        :param mass: MusicAssistant instance.
        :param config: Config of the game this quiz type generates rounds for.
        """
        super().__init__(mass, config)
        self._eligible_tracks: list[Track] | None = None

    @classmethod
    def normalize_config(cls, config: MusicQuizConfig) -> MusicQuizConfig:
        """
        Set fixed internal defaults for configuration not exposed by Hitster.

        :param config: Raw typed game configuration.
        :return: Configuration to persist for this quiz type.
        """
        return replace(
            config,
            suggestion_count=DEFAULT_BONUS_OPTION_COUNT,
            difficulty=MusicQuizDifficulty.NORMAL.value,
            use_ai_distractors=False,
        )

    @classmethod
    def validate_config(cls, config: MusicQuizConfig) -> None:
        """
        Validate Hitster source and bonus-mode requirements.

        :param config: Configuration to validate.
        """
        super().validate_config(config)
        if not config.source_uris:
            raise InvalidDataError(
                "At least one source URI is required",
                translation_key="music_quiz_source_required",
                translation_owner=TRANSLATION_OWNER,
            )
        for mode in (config.artist_bonus_mode, config.title_bonus_mode):
            try:
                TimelineBonusMode(mode)
            except ValueError as err:
                raise InvalidDataError(
                    f"Unknown timeline bonus mode: {mode}",
                    translation_key="music_quiz_invalid_bonus_mode",
                    translation_owner=TRANSLATION_OWNER,
                ) from err

    async def initialize(self) -> None:
        """Validate enough unique dated content exists for the complete game."""
        eligible_tracks = await self._get_eligible_tracks()
        required_track_count = self.config.round_count + 1
        if len(eligible_tracks) < required_track_count:
            raise InvalidDataError(
                f"Hitster requires at least {required_track_count} unique tracks with release years",
                translation_key="music_quiz_not_enough_dated_tracks",
                translation_owner=TRANSLATION_OWNER,
                translation_args=[required_track_count],
            )

    async def prepare_round(
        self, round_index: int, previous_rounds: list[MusicQuizRound]
    ) -> MusicQuizRound:
        """
        Prepare a Hitster round against the timeline guaranteed at reveal.

        :param round_index: Index of the round to prepare.
        :param previous_rounds: Rounds already prepared in earlier iterations.
        :raises InvalidDataError: If the selected content or round history is inconsistent.
        :return: The prepared (not yet started) round.
        """
        eligible_tracks = await self._get_eligible_tracks()
        if round_index < 0 or round_index >= self.config.round_count:
            raise InvalidDataError("Hitster round index is outside the configured game")
        if len(previous_rounds) != round_index:
            raise InvalidDataError("Hitster round history does not match the requested round")

        if round_index == 0:
            anchor_track = self._select_unused_track(eligible_tracks, set())
            placement_snapshot = [await self._create_entry(anchor_track, is_anchor=True)]
        else:
            placement_snapshot = self._timeline_from_previous_rounds(previous_rounds)
        used_track_uris = {entry.track_uri for entry in placement_snapshot}
        current_track = self._select_unused_track(eligible_tracks, used_track_uris)
        candidate = await self._create_candidate(
            current_track,
            existing_ids={entry.entry_id for entry in placement_snapshot},
        )
        bonus_definitions = await self._create_bonus_definitions(current_track)
        assert current_track.uri is not None
        return MusicQuizRound(
            round_index=round_index,
            track_uri=current_track.uri,
            answer_label=build_answer_label(current_track.artist_str or None, current_track.name),
            answer_state=TimelineRoundState(
                placement_snapshot=placement_snapshot,
                candidate=candidate,
                bonus_definitions=bonus_definitions,
            ),
            image_url=candidate.entry.image_url,
            duration=current_track.duration,
        )

    async def _get_eligible_tracks(self) -> list[Track]:
        """Return unique source tracks with usable release metadata."""
        if self._eligible_tracks is not None:
            return self._eligible_tracks
        source_tracks = list((await self._get_source_track_pool()).values())

        async def _resolve_track(track: Track) -> Track | None:
            if self._track_is_eligible(track):
                return track
            try:
                enriched = await self.mass.music.tracks.get(track.item_id, track.provider)
            except Exception as err:
                LOGGER.debug("Could not enrich Music Quiz track %s: %s", track.uri, err)
                return None
            return enriched if self._track_is_eligible(enriched) else None

        resolved_tracks: list[Track | None] = []
        for track_batch in batched(
            source_tracks,
            TRACK_ENRICHMENT_CONCURRENCY,
            strict=False,
        ):
            resolved_tracks.extend(
                await asyncio.gather(*(_resolve_track(track) for track in track_batch))
            )
        self._eligible_tracks = list(
            {
                track.uri: track
                for track in resolved_tracks
                if track is not None and track.uri is not None
            }.values()
        )
        if not self._eligible_tracks:
            raise InvalidDataError(
                "None of the configured tracks have usable release years",
                translation_key="music_quiz_no_dated_tracks",
                translation_owner=TRANSLATION_OWNER,
            )
        return self._eligible_tracks

    async def _create_entry(
        self,
        track: Track,
        *,
        is_anchor: bool = False,
        existing_ids: set[str] | None = None,
    ) -> TimelineEntry:
        """Create a stable timeline entry from an eligible track."""
        release_year = self._release_year(track)
        if release_year is None or not track.uri or not track.artist_str:
            raise InvalidDataError("Hitster track is missing required timeline metadata")
        existing_ids = existing_ids or set()
        while (entry_id := secrets.token_hex(8)) in existing_ids:
            continue
        return TimelineEntry(
            entry_id=entry_id,
            release_year=release_year,
            title=track.name,
            artist=track.artist_str,
            track_uri=track.uri,
            image_url=await self.mass.metadata.get_image_url_for_item(track),
            is_anchor=is_anchor,
        )

    async def _create_candidate(
        self,
        track: Track,
        *,
        existing_ids: set[str],
    ) -> TimelineCandidate:
        """Create the protected timeline candidate and its accepted truths."""
        entry = await self._create_entry(track, existing_ids=existing_ids)
        return TimelineCandidate(
            entry=entry,
            artist_answers=self._artist_answers(track),
            title_answers=[track.name],
        )

    async def _create_bonus_definitions(
        self,
        track: Track,
    ) -> list[TimelineBonusDefinition]:
        """Create protected definitions for the enabled artist and title bonuses."""
        definitions: list[TimelineBonusDefinition] = []
        values = {
            TimelineBonusType.ARTIST: track.artist_str,
            TimelineBonusType.TITLE: track.name,
        }
        for bonus_type in TimelineBonusType:
            mode = self._bonus_mode(bonus_type)
            if mode == TimelineBonusMode.OFF:
                continue
            correct_value = values[bonus_type]
            if not correct_value:
                raise InvalidDataError("Hitster track is missing required bonus metadata")
            if mode == TimelineBonusMode.FREE_TEXT:
                definitions.append(TimelineFreeTextBonusDefinition(bonus_type=bonus_type))
                continue
            definitions.append(
                TimelineMultipleChoiceBonusDefinition(
                    bonus_type=bonus_type,
                    options=await self._create_bonus_options(track, bonus_type, correct_value),
                )
            )
        return definitions

    async def _create_bonus_options(
        self,
        track: Track,
        bonus_type: TimelineBonusType,
        correct_value: str,
    ) -> list[TimelineBonusOption]:
        """Create four opaque and distinct options for a multiple-choice bonus."""
        assert self._eligible_tracks is not None
        correct = self._bonus_candidate(track, bonus_type)
        distractors = [
            self._bonus_candidate(candidate, bonus_type)
            for candidate in self._eligible_tracks
            if candidate.uri != track.uri
        ]
        try:
            options = build_opaque_options(
                correct,
                distractors,
                DEFAULT_BONUS_OPTION_COUNT,
            )
        except ValueError:
            distractors.extend(await self._search_bonus_distractors(correct_value, bonus_type))
            try:
                options = build_opaque_options(
                    correct,
                    distractors,
                    DEFAULT_BONUS_OPTION_COUNT,
                )
            except ValueError as err:
                raise InvalidDataError(
                    "Not enough distinct tracks are available to build bonus options",
                    translation_key="music_quiz_not_enough_distractors",
                    translation_owner=TRANSLATION_OWNER,
                ) from err
        return [
            TimelineBonusOption(
                option_id=option.option_id,
                label=option.label,
                is_correct=option.is_correct,
            )
            for option in options
        ]

    async def _search_bonus_distractors(
        self,
        correct_value: str,
        bonus_type: TimelineBonusType,
    ) -> list[SuggestionCandidate]:
        """Return bonus distractors from a catalog track search."""
        try:
            search_results = await self.mass.music.search(
                search_query=correct_value,
                media_types=[MediaType.TRACK],
                limit=max(DEFAULT_BONUS_OPTION_COUNT * 8, 24),
                library_only=False,
            )
        except Exception as err:
            LOGGER.debug("Could not search for Music Quiz bonus options: %s", err)
            return []
        return [
            self._bonus_candidate(item, bonus_type)
            for item in search_results.tracks
            if isinstance(item, Track)
            and (
                (bonus_type == TimelineBonusType.ARTIST and item.artist_str)
                or (bonus_type == TimelineBonusType.TITLE and item.name)
            )
        ]

    def _timeline_from_previous_rounds(
        self,
        previous_rounds: list[MusicQuizRound],
    ) -> list[TimelineEntry]:
        """Derive the guaranteed shared timeline from persisted round history."""
        timeline: list[TimelineEntry] = []
        for previous_round in previous_rounds:
            if not isinstance(previous_round.answer_state, TimelineRoundState):
                raise InvalidDataError("Hitster round history contains a different answer type")
            state = previous_round.answer_state
            expected_snapshot = sorted(
                timeline or state.placement_snapshot,
                key=lambda entry: (entry.release_year, entry.entry_id),
            )
            if state.placement_snapshot != expected_snapshot:
                raise InvalidDataError("Hitster round history contains a stale timeline snapshot")
            timeline = sorted(
                [*state.placement_snapshot, state.candidate.entry],
                key=lambda entry: (entry.release_year, entry.entry_id),
            )
        track_uris = [entry.track_uri for entry in timeline]
        if len(track_uris) != len(set(track_uris)):
            raise InvalidDataError("Hitster round history contains duplicate tracks")
        return timeline

    def _bonus_mode(self, bonus_type: TimelineBonusType) -> TimelineBonusMode:
        """Return the configured mode for a bonus type."""
        return (
            self.config.artist_bonus_mode
            if bonus_type == TimelineBonusType.ARTIST
            else self.config.title_bonus_mode
        )

    @staticmethod
    def _select_unused_track(
        tracks: list[Track],
        used_track_uris: set[str],
    ) -> Track:
        """Return one random track that is absent from persisted round history."""
        available_tracks = [
            track for track in tracks if track.uri and track.uri not in used_track_uris
        ]
        if not available_tracks:
            raise InvalidDataError(
                "No unused dated tracks are available",
                translation_key="music_quiz_no_unused_source_tracks",
                translation_owner=TRANSLATION_OWNER,
            )
        return secrets.choice(available_tracks)

    @staticmethod
    def _artist_answers(track: Track) -> list[str]:
        """Return deterministic accepted artist names available on the track."""
        answers: list[str] = []
        seen_answers: set[str] = set()
        for answer in (
            track.artist_str,
            *(artist.name for artist in track.artists),
            *(artist.sort_name for artist in track.artists),
        ):
            if answer and (normalized_answer := answer.casefold()) not in seen_answers:
                answers.append(answer)
                seen_answers.add(normalized_answer)
        return answers

    @staticmethod
    def _bonus_candidate(track: Track, bonus_type: TimelineBonusType) -> SuggestionCandidate:
        """Convert a track to an artist or title bonus candidate."""
        if bonus_type == TimelineBonusType.ARTIST:
            return SuggestionCandidate(label=track.artist_str, uri=track.uri)
        return SuggestionCandidate(label=track.name, uri=track.uri, title=track.name)

    @classmethod
    def _track_is_eligible(cls, track: Track) -> bool:
        """Return whether a track has all metadata required by Hitster."""
        return bool(track.uri and track.name and track.artist_str and cls._release_year(track))

    @staticmethod
    def _release_year(track: Track) -> int | None:
        """Return a usable release year from the track's album or metadata."""
        album = track.album
        year = album.year if isinstance(album, Album | ItemMapping) else None
        current_year = utc().year
        if year is not None and MIN_RELEASE_YEAR <= year <= current_year:
            return year
        if track.metadata.release_date is not None:
            year = track.metadata.release_date.year
            if MIN_RELEASE_YEAR <= year <= current_year:
                return year
        return None
