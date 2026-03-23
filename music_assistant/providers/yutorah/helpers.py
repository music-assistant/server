"""Pure helper functions for the YuTorah music provider."""

from __future__ import annotations

from typing import Any

from music_assistant_models.enums import ContentType, ImageType, LinkType, MediaType
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    Track,
)
from music_assistant_models.unique_list import UniqueList

from .constants import YUTORAH_BASE


def _series_to_podcast(series: dict[str, Any], instance_id: str) -> Podcast:
    """Convert a YuSeriesFromBrowse dict to a MA Podcast."""
    series_id = str(series.get("ID") or series.get("seriesID") or "")
    name = series.get("name") or "Unknown Series"
    image_url = series.get("imageURL") or ""
    shiur_count = series.get("shiurCount") or series.get("numShiurim")
    images = _make_images(image_url, instance_id)

    return Podcast(
        item_id=series_id,
        provider=instance_id,
        name=name,
        publisher=series.get("middleTierName") or "",
        total_episodes=int(shiur_count) if shiur_count else None,
        provider_mappings={
            ProviderMapping(
                item_id=series_id,
                provider_domain="yutorah",
                provider_instance=instance_id,
                url=f"{YUTORAH_BASE}/series/{_slugify(name)}",
            )
        },
        metadata=MediaItemMetadata(
            images=UniqueList(images) if images else None,
            links={
                MediaItemLink(
                    type=LinkType.WEBSITE,
                    url=f"{YUTORAH_BASE}/series/{_slugify(name)}",
                )
            }
            if name
            else None,
        ),
    )


def _shiur_to_episode(
    shiur: dict[str, Any],
    position: int,
    instance_id: str,
    parent_series_id: str | None = None,
) -> PodcastEpisode | None:
    """Convert a YuShiur or YuSearchDoc dict to a MA PodcastEpisode.

    Returns None if the shiur has no playable MP3 URL.
    """
    # Field names differ between shiur/details (YuShiur) and search/get (YuSearchDoc)
    shiur_id = str(shiur.get("shiurID") or shiur.get("shiurid") or "")
    if not shiur_id:
        return None

    media_type = shiur.get("shiurMediaType") or shiur.get("mediatypename") or ""

    # Only handle audio (MP3); skip video/PDF/HTML unconditionally.
    # Empty string is for legacy content predating the shiurMediaType attribute.
    if media_type.upper() not in ("MP3", "AUDIO", ""):
        return None

    mp3_url = shiur.get("shiurFileURL") or shiur.get("shiurdownloadurl") or ""
    if not mp3_url:
        return None

    title = shiur.get("shiurTitle") or shiur.get("shiurtitle") or "Untitled Shiur"
    description = shiur.get("shiurDescription") or shiur.get("shiurdescription") or ""
    date_str = shiur.get("shiurDate") or shiur.get("shiurdate") or ""
    # search/get returns 'duration' as integer minutes; shiur/details uses 'shiurLength' string
    raw_dur = shiur.get("duration")
    if raw_dur is not None:
        duration_sec = int(raw_dur) * 60
    else:
        shiur_len = shiur.get("shiurLength") or shiur.get("durationformatted") or ""
        duration_sec = _parse_duration(shiur_len)

    teacher_id, teacher_name, image_url = _extract_teacher_info(shiur)

    series_id = parent_series_id or str(shiur.get("shiurSeries") or "")
    podcast_ref = ItemMapping(
        item_id=series_id or f"teacher_{teacher_id}",
        provider=instance_id,
        name=shiur.get("shiurSeriesName") or teacher_name or "YuTorah",
        media_type=MediaType.PODCAST,
    )

    if date_str:
        description = f"{date_str} — {description}" if description else date_str

    images = _make_images(image_url, instance_id)
    links: set[MediaItemLink] = set()
    if shiur_id:
        links.add(
            MediaItemLink(
                type=LinkType.WEBSITE,
                url=f"{YUTORAH_BASE}/lectures/{shiur_id}/",
            )
        )

    return PodcastEpisode(
        item_id=shiur_id,
        provider=instance_id,
        name=title,
        position=position,
        podcast=podcast_ref,
        duration=duration_sec,
        provider_mappings={
            ProviderMapping(
                item_id=shiur_id,
                provider_domain="yutorah",
                provider_instance=instance_id,
                audio_format=AudioFormat(content_type=ContentType.MP3),
                # Store the direct MP3 URL in details to avoid a re-fetch at play time
                details=mp3_url,
                url=f"{YUTORAH_BASE}/lectures/{shiur_id}/",
            )
        },
        metadata=MediaItemMetadata(
            description=description or None,
            images=UniqueList(images) if images else None,
            links=links or None,
        ),
    )


def _shiur_to_track(
    shiur: dict[str, Any],
    position: int,
    instance_id: str,
) -> Track | None:
    """Convert a shiur dict to a MA Track for artist top-tracks display.

    Returns None if the shiur has no playable MP3 URL.
    """
    shiur_id = str(shiur.get("shiurID") or shiur.get("shiurid") or "")
    if not shiur_id:
        return None

    media_type_str = shiur.get("shiurMediaType") or shiur.get("mediatypename") or ""
    if media_type_str.upper() not in ("MP3", "AUDIO", ""):
        return None

    mp3_url = shiur.get("shiurFileURL") or shiur.get("shiurdownloadurl") or ""
    if not mp3_url:
        return None

    title = shiur.get("shiurTitle") or shiur.get("shiurtitle") or "Untitled Shiur"
    raw_dur = shiur.get("duration")
    if raw_dur is not None:
        duration_sec = int(raw_dur) * 60
    else:
        shiur_len = shiur.get("shiurLength") or shiur.get("durationformatted") or ""
        duration_sec = _parse_duration(shiur_len)

    teacher_id, teacher_name, image_url = _extract_teacher_info(shiur)

    images = _make_images(image_url, instance_id)
    artists: UniqueList[Artist | ItemMapping] = (
        UniqueList(
            [
                ItemMapping(
                    item_id=teacher_id,
                    provider=instance_id,
                    name=teacher_name,
                    media_type=MediaType.ARTIST,
                )
            ]
        )
        if teacher_id
        else UniqueList()
    )
    return Track(
        item_id=shiur_id,
        provider=instance_id,
        name=title,
        duration=duration_sec,
        track_number=position + 1,
        artists=artists,
        provider_mappings={
            ProviderMapping(
                item_id=shiur_id,
                provider_domain="yutorah",
                provider_instance=instance_id,
                audio_format=AudioFormat(content_type=ContentType.MP3),
                details=mp3_url,
                url=f"{YUTORAH_BASE}/lectures/{shiur_id}/",
            )
        },
        metadata=MediaItemMetadata(
            images=UniqueList(images) if images else None,
        ),
    )


def _extract_teacher_info(shiur: dict[str, Any]) -> tuple[str, str, str]:
    """Extract (teacher_id, teacher_name, image_url) from a shiur dict.

    Handles both YuShiur (shiurTeachers list) and YuSearchDoc (flat fields).
    """
    teachers = shiur.get("shiurTeachers")
    if teachers and isinstance(teachers, list):
        t = teachers[0]
        teacher_id = str(t.get("teacherID") or "")
        teacher_name = t.get("teacherName") or ""
        image_url = t.get("teacherPhotoURL") or t.get("teacherAlbumURL") or ""
    else:
        teacher_id = str(shiur.get("teacherid") or "")
        teacher_name = shiur.get("teacherfullname") or ""
        photo = shiur.get("PHOTO") or shiur.get("photo") or ""
        if photo and not photo.startswith("http"):
            photo = f"https://cdnyutorah.cachefly.net/_images/roshei_yeshiva/{photo}"
        image_url = photo
    return teacher_id, teacher_name, image_url


def _make_images(url: str, provider_id: str) -> list[MediaItemImage]:
    """Build a list with one thumbnail MediaItemImage, or empty if url is unusable."""
    if not url or not url.startswith("http"):
        return []
    return [
        MediaItemImage(
            type=ImageType.THUMB,
            path=url,
            provider=provider_id,
            remotely_accessible=True,
        )
    ]


def _parse_duration(s: str) -> int:
    """Convert duration string (HH:MM:SS or MM:SS or seconds) to int seconds."""
    if not s:
        return 0
    s = s.split(".")[0].strip()
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return 0


def _path_segment(name: str, item_id: str) -> str:
    """Build a browse path segment encoding display name and ID as 'name|id'.

    The frontend can display the name portion; the provider parses the ID for API calls.
    Mirrors the pattern used by the Apple Music provider.
    """
    safe = name.replace("|", "-").replace("/", "-").strip()
    return f"{safe}|{item_id}"


def _segment_id(segment: str) -> str:
    """Extract the numeric ID from a 'name|id' path segment, or return segment unchanged."""
    return segment.rsplit("|", 1)[-1] if "|" in segment else segment


def _slugify(name: str) -> str:
    """Create a URL-safe slug from a display name.

    Used only for building decorative external website URLs (e.g. yutorah.org/series/daf-yomi).
    """
    result = name.lower()
    for ch in ",'\"()[]{}":
        result = result.replace(ch, "")
    for ch in " ;:.!?\u2014\u2013\t":
        result = result.replace(ch, "-")
    while "--" in result:
        result = result.replace("--", "-")
    return result.strip("-")


def _build_st_podcast(
    series_id: str,
    teacher_id: str,
    teachers_map: dict[str, dict[str, Any]],
    series_list: list[dict[str, Any]],
    instance_id: str,
) -> Podcast:
    """Build a virtual series+teacher Podcast for a combined st_ podcast ID."""
    t = teachers_map.get(teacher_id) or {}
    teacher_name = t.get("fullName") or f"Teacher {teacher_id}"
    image_url = t.get("imageURL") or ""
    series_name = next(
        (
            str(s.get("name") or "")
            for s in series_list
            if str(s.get("ID") or s.get("seriesID") or "") == series_id
        ),
        "",
    )
    podcast_id = f"st_{series_id}_{teacher_id}"
    podcast_name = f"{series_name} — {teacher_name}" if series_name else teacher_name
    return Podcast(
        item_id=podcast_id,
        provider=instance_id,
        name=podcast_name,
        metadata=MediaItemMetadata(
            images=UniqueList(_make_images(image_url, instance_id)) or None,
        ),
        provider_mappings={
            ProviderMapping(
                item_id=podcast_id,
                provider_domain="yutorah",
                provider_instance=instance_id,
            )
        },
    )


def _series_or_stub_podcast(
    sid: str,
    raw: dict[str, Any],
    series_by_id: dict[str, Any],
    instance_id: str,
) -> Podcast:
    """Return a full Podcast for a known series, or a minimal stub for an unknown one."""
    if sid in series_by_id:
        return _series_to_podcast(series_by_id[sid], instance_id)
    return Podcast(
        item_id=sid,
        provider=instance_id,
        name=raw.get("shiurSeriesName") or sid,
        provider_mappings={
            ProviderMapping(
                item_id=sid,
                provider_domain="yutorah",
                provider_instance=instance_id,
            )
        },
    )


def _extract_docs(data: Any) -> list[dict[str, Any]]:
    """Extract the list of shiur documents from a search/get API response.

    The search/get endpoint returns a Solr-style response. Documents are under
    response.docs; the facet data is under facet_counts.facet_fields.
    """
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("response")
        if isinstance(inner, dict):
            docs = inner.get("docs")
            if isinstance(docs, list):
                return docs
    return []
