"""Parsers translating BookOrbit JSON into Music Assistant models."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import Any

from music_assistant_models.enums import ArtistType, ImageType
from music_assistant_models.media_items import Artist as MassArtist
from music_assistant_models.media_items import Audiobook as MassAudiobook
from music_assistant_models.media_items import (
    MediaItemChapter,
    MediaItemCollection,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

from .constants import API_PREFIX, AUDIO_FORMATS, DOMAIN

AUTHOR_PREFIX = "author:"
NARRATOR_PREFIX = "narrator:"


def _build_cover_url(*, base_url: str, book_id: int, version: str | None) -> str:
    """Build the (authenticated) cover URL; the version busts caches on cover changes."""
    cover_url = f"{base_url}{API_PREFIX}/books/{book_id}/cover"
    if version:
        cover_url += f"?t={version}"
    return cover_url


def _absolute_url(base_url: str, path: str) -> str:
    """Join a BookOrbit-relative path with the instance base URL."""
    if path.startswith(("http://", "https://")):
        return path
    return f"{base_url.rstrip('/')}{path if path.startswith('/') else f'/{path}'}"


def _person_image_path(person: dict[str, Any], *, base_url: str) -> str | None:
    """Return an absolute photo URL when BookOrbit has a real person image (not a book cover)."""
    if image_url := person.get("imageUrl"):
        return _absolute_url(base_url, str(image_url))
    return None


def _audio_files(book: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the audio files of a book in BookOrbit order."""
    return [
        file
        for file in book.get("files") or []
        if (file.get("format") or "").lower() in AUDIO_FORMATS
    ]


def _parse_chapters(book: dict[str, Any], duration: float) -> list[MediaItemChapter]:
    """
    Build chapters for a book.

    Multi-file audio gets one chapter per file; a single file uses its embedded chapters.
    """
    audio_files = _audio_files(book)
    if len(audio_files) > 1:
        chapters: list[MediaItemChapter] = []
        start = 0.0
        for position, file in enumerate(audio_files, 1):
            end = start + float(file.get("durationSeconds") or 0)
            chapters.append(
                MediaItemChapter(
                    position=position,
                    name=file.get("filename") or f"Part {position}",
                    start=start,
                    end=end,
                )
            )
            start = end
        return chapters

    embedded = (book.get("audioMetadata") or {}).get("chapters") or []
    chapters = []
    for position, chapter in enumerate(embedded, 1):
        next_start_ms = embedded[position]["startMs"] if position < len(embedded) else None
        chapters.append(
            MediaItemChapter(
                position=position,
                name=chapter["title"],
                start=chapter["startMs"] / 1000,
                end=next_start_ms / 1000 if next_start_ms is not None else duration or None,
            )
        )
    return chapters


def parse_audiobook(
    book: dict[str, Any], *, provider_instance_id: str, base_url: str
) -> MassAudiobook:
    """Translate a BookOrbit book (BookDetail) to a MassAudiobook."""
    book_id = book["id"]
    item_id = str(book_id)
    audio_metadata = book.get("audioMetadata") or {}
    audio_files = _audio_files(book)

    file_durations = [file.get("durationSeconds") for file in audio_files]
    if len(audio_files) > 1 and all(d is not None for d in file_durations):
        duration = float(sum(file_durations))  # type: ignore[arg-type]
    else:
        duration = float(
            audio_metadata.get("durationSeconds") or sum(d or 0 for d in file_durations)
        )

    name = book.get("title") or "UNKNOWN TITLE"
    if subtitle := book.get("subtitle"):
        name += f" | {subtitle}"

    mass_audiobook = MassAudiobook(
        item_id=item_id,
        provider=provider_instance_id,
        name=name,
        duration=int(duration),
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=DOMAIN,
                provider_instance=provider_instance_id,
                # MA only re-imports when mappings/date_added change; updatedAt forces a refresh
                details=str(book.get("updatedAt") or ""),
            )
        },
        publisher=book.get("publisher"),
    )
    metadata = mass_audiobook.metadata
    metadata.description = book.get("description")
    metadata.languages = UniqueList([book["language"]] if book.get("language") else [])
    metadata.genres = set(book.get("genres") or [])

    if published_date := book.get("publishedDate"):
        with suppress(ValueError):
            metadata.release_date = datetime.fromisoformat(published_date)
    if metadata.release_date is None and book.get("publishedYear"):
        with suppress(ValueError):
            # ruff: noqa: DTZ001 # ignore tzinfo, this is a fallback attempt
            metadata.release_date = datetime(year=int(book["publishedYear"]), month=1, day=1)

    memberships = book.get("seriesMemberships") or []
    if memberships:
        series = [
            (m["seriesName"], m.get("seriesIndex"))
            for m in sorted(memberships, key=lambda m: m.get("displayOrder", 0))
        ]
    elif book.get("seriesName"):
        series = [(book["seriesName"], book.get("seriesIndex"))]
    else:
        series = []
    metadata.collections = UniqueList(
        [MediaItemCollection(title=title, sequence=sequence) for title, sequence in series]
    )

    if book.get("coverSource") or book.get("hasCover"):
        cover_url = _build_cover_url(
            base_url=base_url, book_id=book_id, version=book.get("updatedAt")
        )
        metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=cover_url,
                    provider=provider_instance_id,
                    remotely_accessible=False,
                )
            ]
        )
    else:
        metadata.images = UniqueList([])

    # BookCard payloads carry plain name strings without ids; only detail objects are linked
    mass_audiobook.authors.set(
        [
            parse_author(author, provider_instance_id=provider_instance_id, base_url=base_url)
            for author in book.get("authors") or []
            if isinstance(author, dict)
        ]
    )
    narrators = sorted(
        (n for n in audio_metadata.get("narrators") or [] if isinstance(n, dict)),
        key=lambda n: n.get("displayOrder", 0),
    )
    mass_audiobook.narrators.set(
        [
            parse_narrator(n, provider_instance_id=provider_instance_id, base_url=base_url)
            for n in narrators
        ]
    )

    metadata.chapters = _parse_chapters(book, duration)

    if added_at := book.get("addedAt"):
        with suppress(ValueError):
            mass_audiobook.date_added = datetime.fromisoformat(added_at)

    return mass_audiobook


def _parse_person(
    person: dict[str, Any],
    *,
    prefix: str,
    artist_type: ArtistType,
    provider_instance_id: str,
    base_url: str,
) -> MassArtist:
    item_id = f"{prefix}{person['id']}"
    image_path = _person_image_path(person, base_url=base_url)
    mass_artist = MassArtist(
        item_id=item_id,
        provider=provider_instance_id,
        name=person["name"],
        sort_name=person.get("sortName") or person["name"],
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=DOMAIN,
                provider_instance=provider_instance_id,
                # Image fingerprint forces a library refresh when photos change.
                details=image_path or "",
            )
        },
        artist_type=artist_type,
    )
    if description := person.get("description"):
        mass_artist.metadata.description = description
    if image_path:
        mass_artist.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_path,
                    provider=provider_instance_id,
                    remotely_accessible=False,
                )
            ]
        )
    else:
        mass_artist.metadata.images = UniqueList([])
    return mass_artist


def parse_author(author: dict[str, Any], *, provider_instance_id: str, base_url: str) -> MassArtist:
    """Translate a BookOrbit author to a MassArtist."""
    return _parse_person(
        author,
        prefix=AUTHOR_PREFIX,
        artist_type=ArtistType.AUTHOR,
        provider_instance_id=provider_instance_id,
        base_url=base_url,
    )


def parse_narrator(
    narrator: dict[str, Any], *, provider_instance_id: str, base_url: str
) -> MassArtist:
    """Translate a BookOrbit narrator to a MassArtist."""
    return _parse_person(
        narrator,
        prefix=NARRATOR_PREFIX,
        artist_type=ArtistType.NARRATOR,
        provider_instance_id=provider_instance_id,
        base_url=base_url,
    )
