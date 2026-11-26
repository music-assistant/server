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
from .helpers import collection_sequence_key

AUTHOR_PREFIX = "author:"
NARRATOR_PREFIX = "narrator:"


def _build_cover_url(*, base_url: str, book_id: int, version: str | None) -> str:
    """Build an authenticated cover URL, optionally versioned for cache busting."""
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
    """Return an absolute person photo URL when ``imageUrl`` is present."""
    if image_url := person.get("imageUrl"):
        return _absolute_url(base_url, str(image_url))
    return None


def _audio_files(book: dict[str, Any]) -> list[dict[str, Any]]:
    """Return audio files in BookOrbit playback order (``sortOrder``, then id)."""
    files = [
        file
        for file in book.get("files") or []
        if (file.get("format") or "").lower() in AUDIO_FORMATS
    ]
    # Match BookOrbit: ORDER BY sortOrder ASC NULLS LAST, id ASC.
    return sorted(
        files,
        key=lambda file: (
            file.get("sortOrder") is None,
            file.get("sortOrder") if file.get("sortOrder") is not None else 0,
            int(file["id"]) if file.get("id") is not None else 0,
        ),
    )


def _chapters_from_embedded(
    embedded: list[dict[str, Any]], duration: float
) -> list[MediaItemChapter]:
    """Build chapters from BookOrbit ``audioMetadata.chapters`` start offsets."""
    # BookOrbit may return stored chapters out of order; sort before end offsets.
    ordered = sorted(embedded, key=lambda chapter: chapter["startMs"])
    chapters: list[MediaItemChapter] = []
    for position, chapter in enumerate(ordered, 1):
        next_start_ms = ordered[position]["startMs"] if position < len(ordered) else None
        chapters.append(
            MediaItemChapter(
                position=position,
                name=chapter["title"],
                start=chapter["startMs"] / 1000,
                end=next_start_ms / 1000 if next_start_ms is not None else duration or None,
            )
        )
    return chapters


def _chapters_from_playback_parts(
    audio_files: list[dict[str, Any]],
) -> list[MediaItemChapter]:
    """One synthetic chapter per audio part when every part duration is known."""
    if len(audio_files) <= 1:
        return []
    if any(file.get("durationSeconds") is None for file in audio_files):
        # Unknown durations would invent a broken seek timeline (treat-as-zero).
        return []
    chapters: list[MediaItemChapter] = []
    start = 0.0
    for position, file in enumerate(audio_files, 1):
        end = start + float(file["durationSeconds"])
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


def _parse_chapters(
    book: dict[str, Any],
    duration: float,
    *,
    playback_files: list[dict[str, Any]] | None = None,
) -> list[MediaItemChapter]:
    """
    Build chapters from authoritative metadata, else one chapter per playback part.

    Prefer ``audioMetadata.chapters`` for any book. Synthetic multi-part chapters use
    playback order (``sortOrder`` / explicit ``playback_files``) and are omitted when
    any part duration is unknown.
    """
    embedded = (book.get("audioMetadata") or {}).get("chapters") or []
    if embedded:
        return _chapters_from_embedded(embedded, duration)
    parts = playback_files if playback_files is not None else _audio_files(book)
    return _chapters_from_playback_parts(parts)


def parse_audiobook(
    book: dict[str, Any],
    *,
    provider_instance_id: str,
    base_url: str,
    playback_files: list[dict[str, Any]] | None = None,
) -> MassAudiobook:
    """
    Translate a BookOrbit book detail payload to a Music Assistant audiobook.

    :param book: BookOrbit ``BookDetail`` JSON.
    :param provider_instance_id: Provider instance id for mappings and images.
    :param base_url: Normalized BookOrbit base URL for cover/image paths.
    :param playback_files: Optional manifest-ordered audio parts for chapters/duration.
    """
    book_id = book["id"]
    item_id = str(book_id)
    audio_metadata = book.get("audioMetadata") or {}
    audio_files = playback_files if playback_files is not None else _audio_files(book)

    file_durations = [file.get("durationSeconds") for file in audio_files]
    meta_duration = audio_metadata.get("durationSeconds")
    if len(audio_files) > 1 and all(d is not None for d in file_durations):
        duration = float(sum(file_durations))  # type: ignore[arg-type]
    elif meta_duration is not None:
        # Prefer an authoritative book-level total over a partial sum of measured parts.
        duration = float(meta_duration)
    elif len(audio_files) == 1 and file_durations and file_durations[0] is not None:
        duration = float(file_durations[0])
    else:
        # Unknown: do not invent a short total from incomplete part measurements.
        duration = 0.0

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
                # Store updatedAt so library sync can detect remote metadata changes.
                details=str(book.get("updatedAt") or ""),
                is_unique=True,
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
        [
            MediaItemCollection(title=title, sequence=collection_sequence_key(sequence))
            for title, sequence in series
        ]
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

    # BookCard may only have name strings; link authors only when detail objects include ids.
    mass_audiobook.authors.set(
        [
            parse_author(author, provider_instance_id=provider_instance_id, base_url=base_url)
            for author in book.get("authors") or []
            if isinstance(author, dict) and "id" in author
        ]
    )
    narrators = sorted(
        (n for n in audio_metadata.get("narrators") or [] if isinstance(n, dict) and "id" in n),
        key=lambda n: n.get("displayOrder", 0),
    )
    mass_audiobook.narrators.set(
        [
            parse_narrator(n, provider_instance_id=provider_instance_id, base_url=base_url)
            for n in narrators
        ]
    )

    metadata.chapters = _parse_chapters(book, duration, playback_files=playback_files)

    if added_at := book.get("addedAt"):
        with suppress(ValueError):
            mass_audiobook.date_added = datetime.fromisoformat(added_at).replace(microsecond=0)

    return mass_audiobook


def _parse_person(
    person: dict[str, Any],
    *,
    prefix: str,
    artist_type: ArtistType,
    provider_instance_id: str,
    base_url: str,
) -> MassArtist:
    """Translate a BookOrbit person object to a Music Assistant artist."""
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
                # Name/sort/bio/image so renames and metadata-only edits trigger a refresh.
                details=(
                    f"{person['name']}|{person.get('sortName') or person['name']}|"
                    f"{person.get('description') or ''}|{image_path or ''}"
                ),
                is_unique=True,
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
    """
    Translate a BookOrbit author to a Music Assistant artist.

    :param author: Author JSON object.
    :param provider_instance_id: Provider instance id for mappings and images.
    :param base_url: Normalized BookOrbit base URL for image paths.
    """
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
    """
    Translate a BookOrbit narrator to a Music Assistant artist.

    :param narrator: Narrator JSON object.
    :param provider_instance_id: Provider instance id for mappings and images.
    :param base_url: Normalized BookOrbit base URL for image paths.
    """
    return _parse_person(
        narrator,
        prefix=NARRATOR_PREFIX,
        artist_type=ArtistType.NARRATOR,
        provider_instance_id=provider_instance_id,
        base_url=base_url,
    )
