"""Ready-made Rotation playlists that show what the steps can do."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mashumaro import DataClassDictMixin
from music_assistant_models.translations import resolve_translation

from .constants import TRANSLATION_OWNER
from .models import (
    Condition,
    ExpandStep,
    FilterStep,
    FlattenStep,
    ItemKind,
    SortKey,
    SortStep,
    SourceStep,
    Step,
)


@dataclass
class Recipe:
    """A complete steps document with a short goal."""

    id: str
    name: str
    goal: str
    steps: list[Step]


@dataclass
class RecipeDescription(DataClassDictMixin):
    """A recipe as the API sends it: complete steps with a name and a short goal."""

    id: str
    # in English, as the goal; an API result carries both in the language of the client
    name: str
    goal: str
    steps: list[dict[str, Any]]

    def __post_serialize__(self, d: dict[str, Any]) -> dict[str, Any]:
        """Localize the name and the goal when a resolver for the client language is active."""
        for attr in ("name", "goal"):
            text = resolve_translation(f"recipes.{self.id}.{attr}", owner=TRANSLATION_OWNER)
            if text is not None:
                d[attr] = text
        return d


def track_order() -> SortStep:
    """Return the sort step that plays the tracks of an album in order."""
    return SortStep([SortKey("track_number")])


RECIPES: list[Recipe] = [
    Recipe(
        id="random_tracks",
        name="Random tracks",
        goal="Play all tracks of your library in random order.",
        steps=[SourceStep(ItemKind.TRACK), SortStep([SortKey("random")])],
    ),
    Recipe(
        id="albums_random_complete",
        name="Albums in random order, each complete",
        goal=(
            "Play the albums of one artist in random order, each album complete in track order. "
            "Pick the artist, and use the last filter to leave out tracks such as intros."
        ),
        steps=[
            SourceStep(ItemKind.ALBUM),
            FilterStep("keep", "all", [Condition("artist", "is", None)]),
            SortStep([SortKey("random")]),
            ExpandStep(ItemKind.TRACK),
            FilterStep("remove", "any", []),
            track_order(),
        ],
    ),
    Recipe(
        id="favorite_artists_albums",
        name="Albums of my favorite artists",
        goal="Play the albums of your favorite artists in random order, each album complete.",
        steps=[
            SourceStep(ItemKind.ARTIST),
            FilterStep("keep", "all", [Condition("favorite", "is", True)]),
            ExpandStep(ItemKind.ALBUM),
            FlattenStep(),
            SortStep([SortKey("random")]),
            ExpandStep(ItemKind.TRACK),
            track_order(),
        ],
    ),
    Recipe(
        id="forgotten_favorites",
        name="Forgotten favorites",
        goal="Play your favorite tracks that did not play in the last 90 days, in random order.",
        steps=[
            SourceStep(ItemKind.TRACK),
            FilterStep("keep", "all", [Condition("favorite", "is", True)]),
            FilterStep("remove", "all", [Condition("last_played", "in_last_days", 90)]),
            SortStep([SortKey("random")]),
        ],
    ),
    Recipe(
        id="newest_albums",
        name="Newest albums first",
        goal="Play the albums added in the last 30 days, newest first, each album complete.",
        steps=[
            SourceStep(ItemKind.ALBUM),
            FilterStep("keep", "all", [Condition("date_added", "in_last_days", 30)]),
            SortStep([SortKey("date_added", descending=True)]),
            ExpandStep(ItemKind.TRACK),
            track_order(),
        ],
    ),
]
