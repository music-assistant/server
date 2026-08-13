"""Stable, URL-free Yoto catalogue records."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

STORY_CATEGORIES = frozenset({"sleep", "stories", "story"})


@dataclass(frozen=True, slots=True)
class CatalogueTrack:
    """Represent a playable track without its ephemeral stream URL."""

    item_id: str
    card_id: str
    chapter_key: str
    track_key: str
    title: str
    chapter_title: str | None
    duration: int
    chapter_number: int
    track_number: int
    format: str | None = None
    channels: str | None = None
    artwork: str | None = None


@dataclass(frozen=True, slots=True)
class CatalogueCard:
    """Represent a Yoto card and its ordered playable tracks."""

    item_id: str
    title: str
    description: str | None = None
    author: str | None = None
    category: str | None = None
    artwork: str | None = None
    series_title: str | None = None
    series_order: int | None = None
    tracks: tuple[CatalogueTrack, ...] = ()

    @property
    def is_audiobook(self) -> bool:
        """Return whether Yoto classifies this card as story content."""
        return bool(self.category and self.category.strip().casefold() in STORY_CATEGORIES)


@dataclass(frozen=True, slots=True)
class CatalogueGroup:
    """Represent an ordered Yoto library group."""

    item_id: str
    name: str
    card_ids: tuple[str, ...] = ()
    artwork: str | None = None


@dataclass(slots=True)
class Catalogue:
    """Represent a snapshot of cards and groups from the Yoto family library."""

    cards: dict[str, CatalogueCard] = field(default_factory=dict)
    groups: dict[str, CatalogueGroup] = field(default_factory=dict)

    @classmethod
    def from_responses(
        cls,
        library: Mapping[str, Any],
        details: Mapping[str, Mapping[str, Any]],
        groups: list[Mapping[str, Any]] | None = None,
    ) -> Catalogue:
        """
        Parse API responses into a stable catalogue snapshot.

        :param library: Yoto library response.
        :param details: Card detail responses keyed by card ID.
        :param groups: Optional Yoto group responses.
        :return: Parsed stable catalogue.
        """
        raw_cards = library.get("cards")
        if not isinstance(raw_cards, list):
            raise ValueError("Yoto library response has no cards list")  # noqa: TRY004
        cards: dict[str, CatalogueCard] = {}
        for raw in raw_cards:
            if not isinstance(raw, Mapping) or not (card_id := _text(_child(raw, "cardId"))):
                continue
            card = _mapping(_child(raw, "card"))
            metadata = _mapping(_child(card, "metadata"))
            cover = _mapping(_child(metadata, "cover"))
            cards[card_id] = CatalogueCard(
                item_id=card_id,
                title=_text(_child(card, "title")) or card_id,
                description=_optional_text(_child(metadata, "description")),
                author=_optional_text(_child(metadata, "author")),
                category=_optional_text(_child(metadata, "category")),
                artwork=_optional_text(_child(cover, "imageL")),
                series_title=_optional_text(_child(cover, "seriestitle")),
                series_order=_optional_int(_child(cover, "seriesorder")),
                tracks=_parse_tracks(card_id, details.get(card_id, {})),
            )
        parsed_groups: dict[str, CatalogueGroup] = {}
        for raw in groups or []:
            if not (group_id := _text(_child(raw, "id"))):
                continue
            card_ids = tuple(
                card_id
                for item in raw.get("items", [])
                if isinstance(item, Mapping) and (card_id := _text(_child(item, "contentId")))
            )
            parsed_groups[group_id] = CatalogueGroup(
                item_id=group_id,
                name=_text(_child(raw, "name")) or group_id,
                card_ids=card_ids,
                artwork=_optional_text(_child(raw, "imageUrl")),
            )
        return cls(cards, parsed_groups)

    @classmethod
    def from_yoto_models(cls, library: Mapping[str, Any], groups: Mapping[str, Any]) -> Catalogue:
        """
        Build a URL-free snapshot from yoto-api model objects.

        :param library: Card models keyed by card ID.
        :param groups: Group models keyed by group ID.
        :return: Stable catalogue snapshot.
        """
        cards: dict[str, CatalogueCard] = {}
        for card_id, card in library.items():
            tracks: list[CatalogueTrack] = []
            for chapter_number, chapter in enumerate(card.chapters.values(), 1):
                for track in chapter.tracks.values():
                    if getattr(track, "type", None) not in (None, "audio"):
                        continue
                    tracks.append(
                        CatalogueTrack(
                            item_id=encode_track_id(card_id, chapter.key, track.key),
                            card_id=card_id,
                            chapter_key=chapter.key,
                            track_key=track.key,
                            title=track.title or track.key,
                            chapter_title=chapter.title,
                            duration=track.duration or 0,
                            chapter_number=chapter_number,
                            track_number=len(tracks) + 1,
                            format=track.format,
                            channels=track.channels,
                            artwork=track.icon or chapter.icon,
                        )
                    )
            cards[card_id] = CatalogueCard(
                item_id=card_id,
                title=card.title or card_id,
                description=card.description,
                author=card.author,
                category=card.category,
                artwork=card.cover_image_large,
                series_title=card.series_title,
                series_order=card.series_order,
                tracks=tuple(tracks),
            )
        parsed_groups = {
            group_id: CatalogueGroup(
                item_id=group_id,
                name=group.name or group_id,
                card_ids=tuple(group.card_ids),
                artwork=group.image_url,
            )
            for group_id, group in groups.items()
        }
        return cls(cards, parsed_groups)

    def find_track(self, item_id: str) -> CatalogueTrack | None:
        """Find one track by stable provider ID."""
        try:
            card_id, _, _ = decode_track_id(item_id)
        except ValueError:
            return None
        card = self.cards.get(card_id)
        return (
            next((track for track in card.tracks if track.item_id == item_id), None)
            if card
            else None
        )


def encode_track_id(card_id: str, chapter_key: str, track_key: str) -> str:
    """Encode Yoto's three-part track identity as a URL-safe provider ID."""
    payload = json.dumps([card_id, chapter_key, track_key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_track_id(item_id: str) -> tuple[str, str, str]:
    """Decode a provider track ID into its Yoto identity."""
    try:
        payload = base64.b64decode(
            item_id + "=" * (-len(item_id) % 4), altchars=b"-_", validate=True
        )
        values = json.loads(payload)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError("Invalid Yoto track ID") from err
    if (
        not isinstance(values, list)
        or len(values) != 3
        or not all(isinstance(value, str) and value for value in values)
    ):
        raise ValueError("Invalid Yoto track ID")
    return values[0], values[1], values[2]


def _parse_tracks(card_id: str, detail: Mapping[str, Any]) -> tuple[CatalogueTrack, ...]:
    chapters = _mapping(_child(_mapping(_child(detail, "card")), "content")).get("chapters", [])
    if not isinstance(chapters, list):
        return ()
    result: list[CatalogueTrack] = []
    for chapter_number, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, Mapping) or not (chapter_key := _text(_child(chapter, "key"))):
            continue
        chapter_title = _optional_text(_child(chapter, "title"))
        chapter_artwork = _optional_text(_child(_mapping(_child(chapter, "display")), "icon16x16"))
        tracks = chapter.get("tracks", [])
        if not isinstance(tracks, list):
            continue
        for track in tracks:
            if not isinstance(track, Mapping) or not (track_key := _text(_child(track, "key"))):
                continue
            if _optional_text(_child(track, "type")) not in (None, "audio"):
                continue
            result.append(
                CatalogueTrack(
                    item_id=encode_track_id(card_id, chapter_key, track_key),
                    card_id=card_id,
                    chapter_key=chapter_key,
                    track_key=track_key,
                    title=_text(_child(track, "title")) or track_key,
                    chapter_title=chapter_title,
                    duration=_optional_int(_child(track, "duration")) or 0,
                    chapter_number=chapter_number,
                    track_number=len(result) + 1,
                    format=_optional_text(_child(track, "format")),
                    channels=_optional_text(_child(track, "channels")),
                    artwork=_optional_text(_child(_mapping(_child(track, "display")), "icon16x16"))
                    or chapter_artwork,
                )
            )
    return tuple(result)


def _child(value: Mapping[str, Any], key: str) -> Any:
    child = value.get(key)
    return child["value"] if isinstance(child, Mapping) and set(child) == {"value"} else child


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    return text if (text := _text(value)) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
