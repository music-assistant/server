"""YuTorah music provider for Music Assistant.

Streams Torah shiurim (audio lectures) from YUTorah Online (yutorah.org)
using their native mobile app JSON API (discovered via APK reverse engineering).

Data model mapping:
  Series (e.g. "Daf Yomi", "Ten Minute Halacha")  →  Podcast
  Shiur within that series                         →  PodcastEpisode (direct MP3)

API base: https://yutorah.org/api/
Login is optional but unlocks full paginated episode lists and full search.

Unauthenticated endpoints (always available):
  GET browse/series?favoritesOnly=false      → curated series list (~50)
  GET browse/categories?favoritesOnly=false  → topic category tree
  GET landingpage/landing?type=series&value=ID → recent/top shiurim per series
  GET homepage/details                       → recently uploaded shiurim
  GET shiur/details?shiurID=X               → single shiur with direct MP3 URL

Authenticated endpoints (require userToken query param, obtained via login):
  POST login/default {email, password}       → {"loginSuccess": true, "userToken": "..."}
  GET search/get?searchTerm=&seriesID=X&getFacets=true&userToken=T   → page 1 (30 results)
  GET search/get?searchTerm=&seriesID=X&getFacets=false&start=N&userToken=T → page N (N≥2)
  GET search/get?searchTerm=QUERY&getFacets=true&userToken=T          → full-text search

  IMPORTANT: searchTerm must always be present (even as empty string). Omitting it or
  sending only seriesID/teacherID/subcategoryID without searchTerm returns "". The start
  parameter is a 1-based PAGE NUMBER (not an offset), and must be omitted on the first
  request. Page size is 30 results per page.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    LinkType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemLink,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import (
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://yutorah.org/api/"
YUTORAH_BASE = "https://www.yutorah.org"

# Headers that mirror the official YuTorah Android app
API_HEADERS = {
    "Accept": "application/json",
    "os": "android",
    "app-version": "1.3.4",
    "os-version": "30",
    "User-Agent": "YuTorah/1.3.4 (Android 11)",
}


SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.ARTIST_TOPTRACKS,
}

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
# search/get returns 30 results per page; start is a 1-based page number
PAGE_SIZE = 30
MAX_EPISODES = 500


# ---------------------------------------------------------------------------
# Provider entry-point
# ---------------------------------------------------------------------------


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    :param instance_id: id of an existing provider instance (None if new instance setup).
    :param action: [optional] action key called from config entries UI.
    :param values: the (intermediate) raw values for config entries sent with the action.
    """
    return (
        ConfigEntry(
            key="label_auth",
            type=ConfigEntryType.LABEL,
            label="Login is optional. Without it, only a preview of each series is available. "
            "With a free YuTorah account the full episode list and search are unlocked.",
        ),
        ConfigEntry(
            key=CONF_EMAIL,
            type=ConfigEntryType.STRING,
            label="Email address",
            required=False,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            required=False,
        ),
    )


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> YuTorahProvider:
    """Initialize provider instance with the given configuration."""
    return YuTorahProvider(mass, manifest, config, SUPPORTED_FEATURES)


# ---------------------------------------------------------------------------
# Helper: build MA objects from raw API dicts
# ---------------------------------------------------------------------------


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

    mp3_url = shiur.get("shiurFileURL") or shiur.get("shiurdownloadurl") or ""
    media_type = shiur.get("shiurMediaType") or shiur.get("mediatypename") or ""

    # Only handle audio (MP3); skip video/PDF/HTML
    if media_type.upper() not in ("MP3", "AUDIO", "") and not mp3_url:
        return None
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

    mp3_url = shiur.get("shiurFileURL") or shiur.get("shiurdownloadurl") or ""
    media_type_str = shiur.get("shiurMediaType") or shiur.get("mediatypename") or ""
    if media_type_str.upper() not in ("MP3", "AUDIO", "") and not mp3_url:
        return None
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

    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    if teacher_id:
        artists.append(
            ItemMapping(
                item_id=teacher_id,
                provider=instance_id,
                name=teacher_name or f"Teacher {teacher_id}",
                media_type=MediaType.ARTIST,
            )
        )

    images = _make_images(image_url, instance_id)
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
    # Strip sub-second precision
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


def _slugify(name: str) -> str:
    """Create a URL-safe slug from a display name.

    Used both for decorative external URLs and for browse path segments so that
    MA can display a human-readable label in the breadcrumb instead of a raw ID.
    """
    result = name.lower()
    for ch in ",'\"()[]{}":
        result = result.replace(ch, "")
    for ch in " ;:.!?\u2014\u2013\t":
        result = result.replace(ch, "-")
    while "--" in result:
        result = result.replace("--", "-")
    return result.strip("-")


# ---------------------------------------------------------------------------
# Provider class
# ---------------------------------------------------------------------------


class YuTorahProvider(MusicProvider):
    """Music Assistant provider for YuTorah Online.

    Uses the official mobile app JSON API — no scraping, no Cloudflare issues.
    Browse the full series directory, search by any term, and stream any shiur.
    """

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def handle_async_init(self) -> None:
        """Attempt login if credentials are configured."""
        self._user_token: str | None = None

        email = self.config.get_value(CONF_EMAIL)
        password = self.config.get_value(CONF_PASSWORD)
        if email and password:
            await self._login(str(email), str(password))
        else:
            self.logger.info(
                "YuTorah running without login — episode lists will be limited. "
                "Add credentials in provider settings to unlock full access."
            )

    async def _login(self, email: str, password: str) -> None:
        """Authenticate with YuTorah and store the user token."""
        try:
            async with self.mass.http_session.post(
                f"{API_BASE}login/default",
                json={"email": email, "password": password},
                headers=API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            if data and data.get("loginSuccess") and data.get("userToken"):
                self._user_token = data["userToken"]
                self.logger.info("YuTorah login successful — full episode access enabled.")
            else:
                self.logger.warning(
                    "YuTorah login failed (bad credentials?). Running in anonymous mode."
                )
        except aiohttp.ClientError as exc:
            self.logger.warning("YuTorah login error: %s. Running in anonymous mode.", exc)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Yield nothing — library is managed entirely by MA from Browse actions."""
        return
        yield  # type: ignore[unreachable]

    # -----------------------------------------------------------------------
    # Podcast — series
    # -----------------------------------------------------------------------

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Return a single Podcast (series or teacher) by its provider ID.

        IDs prefixed with ``st_`` identify a series+teacher virtual-podcast;
        IDs prefixed with ``t_`` identify a teacher virtual-podcast;
        plain numeric IDs identify a series.
        """
        if prov_podcast_id.startswith("st_"):
            _, series_id, teacher_id = prov_podcast_id.split("_", 2)
            teachers_map, series_list = await asyncio.gather(
                self._fetch_teachers_map(),
                self._fetch_series_list(),
            )
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
            podcast_name = f"{series_name} — {teacher_name}" if series_name else teacher_name
            return Podcast(
                item_id=prov_podcast_id,
                provider=self.instance_id,
                name=podcast_name,
                metadata=MediaItemMetadata(
                    images=UniqueList(_make_images(image_url, self.instance_id)) or None,
                ),
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_podcast_id,
                        provider_domain="yutorah",
                        provider_instance=self.instance_id,
                    )
                },
            )

        if prov_podcast_id.startswith("t_"):
            teacher_id = prov_podcast_id[2:]
            teachers_map = await self._fetch_teachers_map()
            t = teachers_map.get(teacher_id) or {}
            name = t.get("fullName") or f"Teacher {teacher_id}"
            image_url = t.get("imageURL") or ""
            return Podcast(
                item_id=prov_podcast_id,
                provider=self.instance_id,
                name=name,
                metadata=MediaItemMetadata(
                    images=UniqueList(_make_images(image_url, self.instance_id)) or None,
                ),
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_podcast_id,
                        provider_domain="yutorah",
                        provider_instance=self.instance_id,
                    )
                },
            )

        series_list = await self._fetch_series_list()
        for series in series_list:
            sid = str(series.get("ID") or series.get("seriesID") or "")
            if sid == str(prov_podcast_id):
                return _series_to_podcast(series, self.instance_id)

        self.logger.warning("Series %s not found in browse list", prov_podcast_id)
        return Podcast(
            item_id=prov_podcast_id,
            provider=self.instance_id,
            name=f"YuTorah Series {prov_podcast_id}",
            provider_mappings={
                ProviderMapping(
                    item_id=prov_podcast_id,
                    provider_domain="yutorah",
                    provider_instance=self.instance_id,
                )
            },
        )

    async def get_podcast_episodes(
        self,
        prov_podcast_id: str,
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Yield shiurim for a series.

        When authenticated, uses paginated search/get for the full episode list.
        Without a token, falls back to landingpage/landing (recent + top only).
        """
        if prov_podcast_id.startswith("st_"):
            _, series_id, teacher_id = prov_podcast_id.split("_", 2)
            for ep in await self._browse_series_teacher_episodes(series_id, teacher_id):
                yield ep
            return

        if self._user_token:
            # teacher-prefixed IDs come from search results / teacher browse
            if prov_podcast_id.startswith("t_"):
                teacher_id = prov_podcast_id[2:]
                for ep in await self._fetch_episodes_paged(teacherID=teacher_id):
                    yield ep
            else:
                for ep in await self._fetch_episodes_paged(
                    seriesID=prov_podcast_id, parent_series_id=prov_podcast_id
                ):
                    yield ep
            return

        # Unauthenticated: landingpage/landing gives recent + top shiurim
        if prov_podcast_id.startswith("t_"):
            data = await self._api_get(
                "landingpage/landing", type="speaker", value=prov_podcast_id[2:]
            )
        else:
            data = await self._api_get("landingpage/landing", type="series", value=prov_podcast_id)
        if not data or not isinstance(data, dict):
            return

        seen: set[str] = set()
        position = 0
        for key in ("recentlyAddedShiurim", "topShiurim", "featuredShiurim"):
            for raw in data.get(key) or []:
                shiur_id = str(raw.get("shiurID") or "")
                if not shiur_id or shiur_id in seen:
                    continue
                seen.add(shiur_id)
                episode = _shiur_to_episode(raw, position, self.instance_id, prov_podcast_id)
                if episode:
                    yield episode
                    position += 1

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Return a single PodcastEpisode by shiur ID via shiur/details."""
        data = await self._api_get("shiur/details", shiurID=prov_episode_id)
        if not data or not isinstance(data, dict):
            raise MediaNotFoundError(f"YuTorah shiur {prov_episode_id} not found")
        episode = _shiur_to_episode(data, 0, self.instance_id)
        if not episode:
            raise MediaNotFoundError(f"YuTorah shiur {prov_episode_id} has no playable audio")
        return episode

    # -----------------------------------------------------------------------
    # Artist — teachers
    # -----------------------------------------------------------------------

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Return a teacher as an Artist by their numeric ID."""
        teachers_map = await self._fetch_teachers_map()
        t = teachers_map.get(prov_artist_id) or {}
        name = t.get("fullName") or f"Teacher {prov_artist_id}"
        image_url = t.get("imageURL") or ""
        return Artist(
            item_id=prov_artist_id,
            provider=self.instance_id,
            name=name,
            metadata=MediaItemMetadata(
                images=UniqueList(_make_images(image_url, self.instance_id)) or None,
            ),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_artist_id,
                    provider_domain="yutorah",
                    provider_instance=self.instance_id,
                    url=f"{YUTORAH_BASE}/teachers/{_slugify(str(prov_artist_id))}/",
                )
            },
        )

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Return the most recent shiurim by a teacher as Track objects."""
        if not self._user_token:
            return []
        tracks: list[Track] = []
        page = 1
        while len(tracks) < MAX_EPISODES:
            if page == 1:
                extra: dict[str, Any] = {"getFacets": True}
            else:
                extra = {"getFacets": False, "start": page}
            data = await self._api_get(
                "search/get", searchTerm="", teacherID=prov_artist_id, **extra
            )
            docs = _extract_docs(data)
            if not docs:
                break
            for raw in docs:
                track = _shiur_to_track(raw, len(tracks), self.instance_id)
                if track:
                    tracks.append(track)
            if len(docs) < PAGE_SIZE:
                break
            page += 1
        return tracks

    async def get_track(self, prov_track_id: str) -> Track:
        """Return a single shiur as a Track by its shiurID.

        Called by MA when resolving a track URI (e.g. yutorah://track/<id>).
        """
        data = await self._api_get("shiur/details", shiurID=prov_track_id)
        if not data or not isinstance(data, dict):
            raise MediaNotFoundError(f"YuTorah: shiur {prov_track_id} not found")
        track = _shiur_to_track(data, 0, self.instance_id)
        if not track:
            raise MediaNotFoundError(f"YuTorah: shiur {prov_track_id} has no playable MP3")
        return track

    # -----------------------------------------------------------------------
    # Streaming
    # -----------------------------------------------------------------------

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return stream details for a shiur.

        We first check if the URL was cached in the ProviderMapping.details
        field (set when building the episode), and only fall back to an API
        call if it's missing.
        """
        data = await self._api_get("shiur/details", shiurID=item_id)
        mp3_url = (data.get("shiurFileURL") or "") if data and isinstance(data, dict) else ""

        if not mp3_url:
            raise MediaNotFoundError(f"YuTorah: no MP3 URL found for shiur {item_id}")

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.MP3),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=mp3_url,
            allow_seek=True,
            can_seek=True,
        )

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 25,
    ) -> SearchResults:
        """Search YuTorah for shiurim (tracks), series (podcasts) and teachers (artists).

        When authenticated, uses search/get for full-text search.
        Individual shiurim are returned as Tracks; series as Podcasts; teachers as Artists.
        Without a token, filters the cached series list by name.
        """
        results = SearchResults()

        if self._user_token:
            data = await self._api_get("search/get", searchTerm=search_query or "", getFacets=True)
            if not data:
                return results

            facet_fields = (data.get("facet_counts") or {}).get("facet_fields") or {}

            if MediaType.TRACK in media_types:
                docs = _extract_docs(data)
                tracks: list[Track] = []
                for i, raw in enumerate(docs[:limit]):
                    track = _shiur_to_track(raw, i, self.instance_id)
                    if track:
                        tracks.append(track)
                results.tracks = tracks

            if MediaType.PODCAST in media_types:
                podcasts: list[Podcast] = []
                for facet in (facet_fields.get("series") or [])[:limit]:
                    sid = str(facet.get("SeriesId") or "")
                    name = facet.get("SeriesName") or ""
                    if not sid or not name:
                        continue
                    podcasts.append(
                        Podcast(
                            item_id=sid,
                            provider=self.instance_id,
                            name=name,
                            provider_mappings={
                                ProviderMapping(
                                    item_id=sid,
                                    provider_domain="yutorah",
                                    provider_instance=self.instance_id,
                                )
                            },
                        )
                    )
                results.podcasts = podcasts

            if MediaType.ARTIST in media_types:
                teachers_map = await self._fetch_teachers_map()
                artists: list[Artist] = []
                for facet in (facet_fields.get("teachers") or [])[:limit]:
                    tid = str(facet.get("TeacherId") or "")
                    name = facet.get("TeacherName") or ""
                    if not tid or not name:
                        continue
                    teacher_data = teachers_map.get(tid) or {}
                    image_url = teacher_data.get("imageURL") or ""
                    images = _make_images(image_url, self.instance_id)
                    artists.append(
                        Artist(
                            item_id=tid,
                            provider=self.instance_id,
                            name=name,
                            metadata=MediaItemMetadata(
                                images=UniqueList(images) if images else None,
                            ),
                            provider_mappings={
                                ProviderMapping(
                                    item_id=tid,
                                    provider_domain="yutorah",
                                    provider_instance=self.instance_id,
                                )
                            },
                        )
                    )
                results.artists = artists
        # Unauthenticated: filter the cached series list by name
        elif MediaType.PODCAST in media_types:
            q = search_query.lower()
            series_list = await self._fetch_series_list()
            results.podcasts = [
                _series_to_podcast(s, self.instance_id)
                for s in series_list
                if q in (s.get("name") or "").lower()
            ][:limit]

        return results

    # -----------------------------------------------------------------------
    # Browse
    # -----------------------------------------------------------------------

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the YuTorah content tree.

        :param path: The path to browse (e.g. yutorah://series).

        Path structure:
          yutorah://                                       → root folders
          yutorah://series                                 → all series A-Z (as folders)
          yutorah://series/<name-slug>                     → teacher sub-folders within a series
          yutorah://series/<name-slug>/teacher/<t-slug>   → episodes for a teacher within a series
          yutorah://teachers                               → all teachers A-Z
          yutorah://teachers/<name-slug>                  → all episodes by a teacher
          yutorah://categories                             → topic category tree
          yutorah://categories/<name-slug>                → series in a category
          yutorah://recent                                 → 50 most recent shiurim
        """
        parts = path.split("://", 1)[1].split("/")

        section = parts[0] if parts else ""
        p1 = parts[1] if len(parts) > 1 else ""
        p2 = parts[2] if len(parts) > 2 else ""
        p3 = parts[3] if len(parts) > 3 else ""

        if section == "":
            return [
                BrowseFolder(
                    item_id="series",
                    provider=self.instance_id,
                    path=f"{self.domain}://series",
                    name="Browse by Series",
                    translation_key="podcasts",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="teachers",
                    provider=self.instance_id,
                    path=f"{self.domain}://teachers",
                    name="Browse by Teacher",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="categories",
                    provider=self.instance_id,
                    path=f"{self.domain}://categories",
                    name="Browse by Topic",
                    is_playable=False,
                ),
                BrowseFolder(
                    item_id="recent",
                    provider=self.instance_id,
                    path=f"{self.domain}://recent",
                    name="Recent Shiurim",
                    is_playable=True,
                ),
            ]

        if section == "series" and not p1:
            return await self._browse_all_series()
        if section == "series" and p1 and p2 == "teacher" and p3:
            series_id = await self._resolve_series_id(p1)
            teacher_id = await self._resolve_teacher_id(p3)
            episodes, teachers_map, series_list = await asyncio.gather(
                self._browse_series_teacher_episodes(series_id, teacher_id),
                self._fetch_teachers_map(),
                self._fetch_series_list(),
            )
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
            st_podcast = Podcast(
                item_id=podcast_id,
                provider=self.instance_id,
                name=podcast_name,
                metadata=MediaItemMetadata(
                    images=UniqueList(_make_images(image_url, self.instance_id)) or None,
                ),
                provider_mappings={
                    ProviderMapping(
                        item_id=podcast_id,
                        provider_domain="yutorah",
                        provider_instance=self.instance_id,
                    )
                },
            )
            return [st_podcast, *episodes]
        if section == "series" and p1:
            series_id = await self._resolve_series_id(p1)
            teacher_folders = await self._browse_series_teachers(series_id, series_path_slug=p1)
            series_list = await self._fetch_series_list()
            series_item: Podcast | None = next(
                (
                    _series_to_podcast(s, self.instance_id)
                    for s in series_list
                    if str(s.get("ID") or s.get("seriesID") or "") == series_id
                ),
                None,
            )
            return ([series_item] if series_item else []) + teacher_folders

        if section == "teachers" and not p1:
            return await self._browse_all_teachers()
        if section == "teachers" and p1:
            teacher_id = await self._resolve_teacher_id(p1)
            return await self._browse_teacher_episodes(teacher_id)
        # Legacy path: keep backward compat for any saved/bookmarked yutorah://teacher/{id} paths
        if section == "teacher" and p1:
            return await self._browse_teacher_episodes(p1)

        if section == "categories" and not p1:
            return await self._browse_category_list()
        if section == "categories" and p1:
            categories_map = await self._fetch_categories_map()
            category_id = categories_map.get(p1) or p1
            return await self._browse_category(category_id)
        # Legacy path: keep backward compat for yutorah://category/{id}
        if section == "category" and p1:
            return await self._browse_category(p1)

        if section == "recent":
            return await self._browse_recent()

        return []

    # -----------------------------------------------------------------------
    # Browse helpers
    # -----------------------------------------------------------------------

    async def _browse_all_series(self) -> list[BrowseFolder]:
        """Return all series as browse folders (each expands to teacher sub-folders)."""
        series_list = await self._fetch_series_list()
        folders = [
            BrowseFolder(
                item_id=str(s.get("ID") or s.get("seriesID") or ""),
                provider=self.instance_id,
                path=f"{self.domain}://series/{_slugify(s.get('name') or 'unknown')}",
                name=s.get("name") or "Unknown Series",
                is_playable=False,
            )
            for s in series_list
            if s.get("ID") or s.get("seriesID")
        ]
        folders.sort(key=lambda f: f.name.lower())
        return folders

    async def _browse_series_teachers(
        self, series_id: str, series_path_slug: str
    ) -> list[BrowseFolder]:
        """Return teacher sub-folders for a series, derived from search facets.

        :param series_id: Numeric series ID for the API query.
        :param series_path_slug: Human-readable slug already used in the parent browse path,
            kept consistent so the back-path resolves correctly.
        """
        if not self._user_token:
            return []

        data = await self._api_get("search/get", searchTerm="", seriesID=series_id, getFacets=True)
        if not data or not isinstance(data, dict):
            return []

        teachers = (data.get("facet_counts") or {}).get("facet_fields", {}).get("teachers", [])
        folders = []
        for t in teachers:
            tid = str(t.get("TeacherId") or "")
            name = t.get("TeacherName") or ""
            count = t.get("Match", 0)
            if not tid or not name or count == 0:
                continue
            teacher_slug = _slugify(name)
            folders.append(
                BrowseFolder(
                    item_id=f"st_{series_id}_{tid}",
                    provider=self.instance_id,
                    path=f"{self.domain}://series/{series_path_slug}/teacher/{teacher_slug}",
                    name=f"{name} ({count})",
                    is_playable=False,
                )
            )
        return folders

    async def _browse_series_teacher_episodes(
        self, series_id: str, teacher_id: str
    ) -> list[PodcastEpisode]:
        """Return episodes for one teacher within a series."""
        return await self._fetch_episodes_paged(
            seriesID=series_id, teacherID=teacher_id, parent_series_id=series_id
        )

    async def _browse_all_teachers(self) -> list[Podcast]:
        """Return all teachers as subscribable Podcast items (virtual podcasts with t_ prefix)."""
        teachers_map = await self._fetch_teachers_map()
        if not teachers_map:
            return []

        podcasts = []
        for tid, t in teachers_map.items():
            name = t.get("fullName") or ""
            count = t.get("shiurCount") or 0
            if not tid or not name or t.get("isHidden") or count == 0:
                continue
            image_url = t.get("imageURL") or ""
            podcasts.append(
                Podcast(
                    item_id=f"t_{tid}",
                    provider=self.instance_id,
                    name=f"{name} ({count})",
                    metadata=MediaItemMetadata(
                        images=UniqueList(_make_images(image_url, self.instance_id)) or None,
                    ),
                    provider_mappings={
                        ProviderMapping(
                            item_id=f"t_{tid}",
                            provider_domain="yutorah",
                            provider_instance=self.instance_id,
                        )
                    },
                )
            )
        return podcasts

    async def _browse_teacher_episodes(self, teacher_id: str) -> list[PodcastEpisode]:
        """Return all episodes by a teacher."""
        if self._user_token:
            return await self._fetch_episodes_paged(teacherID=teacher_id)

        data = await self._api_get("landingpage/landing", type="speaker", value=teacher_id)
        if not data or not isinstance(data, dict):
            return []

        seen: set[str] = set()
        episodes: list[PodcastEpisode] = []
        for key in ("recentlyAddedShiurim", "topShiurim", "featuredShiurim"):
            for raw in data.get(key) or []:
                shiur_id = str(raw.get("shiurID") or "")
                if not shiur_id or shiur_id in seen:
                    continue
                seen.add(shiur_id)
                episode = _shiur_to_episode(raw, len(episodes), self.instance_id)
                if episode:
                    episodes.append(episode)
        return episodes

    async def _browse_category_list(self) -> list[BrowseFolder]:
        """Return subcategories as browse folders.

        browse/categories returns top-level categories each with a subCategories list.
        Subcategory IDs are what landingpage/landing accepts as search targets.
        """
        data = await self._api_get("browse/categories", favoritesOnly=False)
        if not data or not isinstance(data, list):
            return []

        folders: list[BrowseFolder] = []
        for cat in data:
            cat_name = cat.get("name") or ""
            for sub in cat.get("subCategories") or []:
                sub_id = str(sub.get("ID") or sub.get("id") or "")
                sub_name = sub.get("name") or ""
                if not sub_id or not sub_name:
                    continue
                label = f"{cat_name} — {sub_name}" if cat_name else sub_name
                folders.append(
                    BrowseFolder(
                        item_id=f"sub_{sub_id}",
                        provider=self.instance_id,
                        path=f"{self.domain}://categories/{_slugify(label)}",
                        name=label,
                        is_playable=False,
                    )
                )
        return folders

    async def _browse_category(self, category_id: str) -> list[Podcast | BrowseFolder]:
        """Return series with shiurim in a subcategory using landingpage/landing.

        Uses type=subcategory which requires no authentication, then deduplicates
        by series so the user sees which podcasts cover that topic.
        """
        data = await self._api_get("landingpage/landing", type="subcategory", value=category_id)
        if not data or not isinstance(data, dict):
            return []

        series_list = await self._fetch_series_list()
        series_by_id = {str(s.get("ID") or s.get("seriesID") or ""): s for s in series_list}

        seen_series: set[str] = set()
        results: list[Podcast | BrowseFolder] = []
        for key in ("recentlyAddedShiurim", "topShiurim", "featuredShiurim"):
            for raw in data.get(key) or []:
                sid = str(raw.get("shiurSeries") or "")
                if sid and sid not in seen_series:
                    seen_series.add(sid)
                    if sid in series_by_id:
                        results.append(_series_to_podcast(series_by_id[sid], self.instance_id))
                    else:
                        series_name = raw.get("shiurSeriesName") or sid
                        results.append(
                            Podcast(
                                item_id=sid,
                                provider=self.instance_id,
                                name=series_name,
                                provider_mappings={
                                    ProviderMapping(
                                        item_id=sid,
                                        provider_domain="yutorah",
                                        provider_instance=self.instance_id,
                                    )
                                },
                            )
                        )
        return results

    async def _browse_recent(self) -> list[PodcastEpisode]:
        """Return recently uploaded shiurim from the homepage endpoint (no auth needed)."""
        data = await self._api_get("homepage/details")
        episodes: list[PodcastEpisode] = []
        for i, raw in enumerate((data or {}).get("recentlyUploaded") or []):
            episode = _shiur_to_episode(raw, i, self.instance_id)
            if episode:
                episodes.append(episode)
        return episodes

    # -----------------------------------------------------------------------
    # Internal — API calls
    # -----------------------------------------------------------------------

    async def _fetch_episodes_paged(
        self,
        parent_series_id: str | None = None,
        **filter_params: Any,
    ) -> list[PodcastEpisode]:
        """Fetch episodes from search/get with automatic pagination.

        Passes any keyword args as extra filter params (e.g. seriesID, teacherID).
        Requires authentication; returns empty list if no token is set.
        """
        if not self._user_token:
            return []

        episodes: list[PodcastEpisode] = []
        page = 1
        while len(episodes) < MAX_EPISODES:
            if page == 1:
                extra: dict[str, Any] = {"getFacets": True}
            else:
                extra = {"getFacets": False, "start": page}
            data = await self._api_get("search/get", searchTerm="", **filter_params, **extra)
            docs = _extract_docs(data)
            if not docs:
                break
            for raw in docs:
                episode = _shiur_to_episode(raw, len(episodes), self.instance_id, parent_series_id)
                if episode:
                    episodes.append(episode)
            if len(docs) < PAGE_SIZE:
                break
            page += 1
        return episodes

    async def _api_get(self, endpoint: str, **params: Any) -> Any:
        """Make a GET request to the YuTorah JSON API and return parsed JSON."""
        str_params = {
            k: str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in params.items()
            if v is not None
        }
        # The YuTorah API accepts the auth token as a query parameter named
        # "userToken" (confirmed by OkHttp interceptor in APK source). Sending
        # it only as an HTTP header does not work — the server ignores it.
        if self._user_token:
            str_params["userToken"] = self._user_token
        try:
            async with self.mass.http_session.get(
                f"{API_BASE}{endpoint}",
                params=str_params,
                headers=API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                raw_text = await resp.text()
                return json.loads(raw_text)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return None
            safe_params = {k: v for k, v in str_params.items() if k != "userToken"}
            self.logger.error("YuTorah API %s failed (params=%s): %s", endpoint, safe_params, exc)
            return None
        except (aiohttp.ClientError, json.JSONDecodeError) as exc:
            safe_params = {k: v for k, v in str_params.items() if k != "userToken"}
            self.logger.error("YuTorah API %s failed (params=%s): %s", endpoint, safe_params, exc)
            return None

    @use_cache(3600)
    async def _fetch_series_list(self) -> list[dict[str, Any]]:
        """Fetch the full list of curated series from browse/series, cached for 1 hour."""
        data = await self._api_get("browse/series", favoritesOnly=False)
        return data if isinstance(data, list) else []

    @use_cache(3600)
    async def _fetch_teachers_map(self) -> dict[str, dict[str, Any]]:
        """Fetch and cache all teachers as a dict keyed by teacher ID string.

        Used to look up teacher names and photo URLs without repeated API calls.
        """
        data = await self._api_get("browse/teachers", favoritesOnly=False)
        if not isinstance(data, list):
            return {}
        return {str(t.get("ID") or ""): t for t in data if t.get("ID")}

    @use_cache(3600)
    async def _fetch_categories_map(self) -> dict[str, str]:
        """Fetch and cache subcategories as a dict keyed by name slug → subcategory ID.

        Used to resolve a human-readable browse path segment back to a numeric ID.
        """
        data = await self._api_get("browse/categories", favoritesOnly=False)
        if not data or not isinstance(data, list):
            return {}
        result: dict[str, str] = {}
        for cat in data:
            cat_name = cat.get("name") or ""
            for sub in cat.get("subCategories") or []:
                sub_id = str(sub.get("ID") or sub.get("id") or "")
                sub_name = sub.get("name") or ""
                if not sub_id or not sub_name:
                    continue
                label = f"{cat_name} — {sub_name}" if cat_name else sub_name
                result[_slugify(label)] = sub_id
        return result

    async def _resolve_series_id(self, slug_or_id: str) -> str:
        """Resolve a series browse path segment (slug or numeric ID) to a numeric series ID."""
        if slug_or_id.isdigit():
            return slug_or_id
        series_list = await self._fetch_series_list()
        for s in series_list:
            if _slugify(s.get("name") or "") == slug_or_id:
                return str(s.get("ID") or s.get("seriesID") or slug_or_id)
        return slug_or_id

    async def _resolve_teacher_id(self, slug_or_id: str) -> str:
        """Resolve a teacher browse path segment (slug or numeric ID) to a numeric teacher ID."""
        if slug_or_id.isdigit():
            return slug_or_id
        teachers_map = await self._fetch_teachers_map()
        for tid, t in teachers_map.items():
            if _slugify(t.get("fullName") or "") == slug_or_id:
                return tid
        return slug_or_id


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _extract_docs(data: Any) -> list[dict[str, Any]]:
    """Extract the list of shiur documents from a search/get API response.

    The wrapper structure is not fully documented; we try common shapes.
    """
    if not data:
        return []
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        # Try common wrapper keys
        for key in ("results", "docs", "shiurim", "items", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        # Some responses nest under a "response" key
        inner = data.get("response")
        if isinstance(inner, dict):
            for key in ("results", "docs"):
                val = inner.get(key)
                if isinstance(val, list):
                    return val

    return []
