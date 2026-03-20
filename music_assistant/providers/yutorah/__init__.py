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

  search/get response shape (Solr-style):
    {
      "response": {"docs": [...shiur objects...], "numFound": N},
      "facet_counts": {"facet_fields": {"teachers": [...], "series": [...]}}
    }

Browse path structure:
  yutorah://                                         → root folders
  yutorah://series                                   → all series (as folders)
  yutorah://series/<series_id>                       → teacher sub-folders within a series
  yutorah://series/<series_id>/teacher/<teacher_id>  → episodes for a teacher within a series
  yutorah://teachers                                 → all teachers
  yutorah://teachers/<teacher_id>                    → all episodes by a teacher
  yutorah://categories                               → topic category tree
  yutorah://categories/<subcategory_id>              → series in a category
  yutorah://recent                                   → 50 most recent shiurim
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
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, ProviderUnavailableError
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

from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
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
            key=CONF_USERNAME,
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

    Used only for building decorative external website URLs (e.g. yutorah.org/series/daf-yomi).
    Browse paths use numeric IDs instead.
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

        username = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)
        if username and password:
            await self._login(str(username), str(password))
        else:
            self.logger.info(
                "YuTorah running without login — episode lists will be limited. "
                "Add credentials in provider settings to unlock full access."
            )

    async def _login(self, email: str, password: str) -> None:
        """Authenticate with YuTorah and store the user token.

        :raises LoginFailed: if credentials are rejected by the API.
        """
        try:
            async with self.mass.http_session.post(
                f"{API_BASE}login/default",
                json={"email": email, "password": password},
                headers=API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise LoginFailed(f"YuTorah login error: {exc}") from exc

        if not (data and data.get("loginSuccess") and data.get("userToken")):
            raise LoginFailed("YuTorah login failed — check your email and password.")

        self._user_token = data["userToken"]
        self.logger.info("YuTorah login successful — full episode access enabled.")

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

        raise MediaNotFoundError(f"YuTorah series {prov_podcast_id} not found")

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
                    url=f"{YUTORAH_BASE}/teachers/{_slugify(name)}/",
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
        """Return a single shiur as a Track by its shiurID."""
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

        Calls shiur/details to retrieve the direct MP3 download URL.
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
                    translation_key="artists",
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
            episodes, teachers_map, series_list = await asyncio.gather(
                self._browse_series_teacher_episodes(p1, p3),
                self._fetch_teachers_map(),
                self._fetch_series_list(),
            )
            t = teachers_map.get(p3) or {}
            teacher_name = t.get("fullName") or f"Teacher {p3}"
            image_url = t.get("imageURL") or ""
            series_name = next(
                (
                    str(s.get("name") or "")
                    for s in series_list
                    if str(s.get("ID") or s.get("seriesID") or "") == p1
                ),
                "",
            )
            podcast_id = f"st_{p1}_{p3}"
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
            teacher_folders, series_list = await asyncio.gather(
                self._browse_series_teachers(p1),
                self._fetch_series_list(),
            )
            series_item: Podcast | None = next(
                (
                    _series_to_podcast(s, self.instance_id)
                    for s in series_list
                    if str(s.get("ID") or s.get("seriesID") or "") == p1
                ),
                None,
            )
            items: list[Podcast | BrowseFolder] = []
            if series_item:
                items.append(series_item)
            items.extend(teacher_folders)
            return items

        if section == "teachers" and not p1:
            return await self._browse_all_teachers()
        if section == "teachers" and p1:
            return await self._browse_teacher_episodes(p1)

        if section == "categories" and not p1:
            return await self._browse_category_list()
        if section == "categories" and p1:
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
                path=f"{self.domain}://series/{s.get('ID') or s.get('seriesID')}",
                name=s.get("name") or "Unknown Series",
                is_playable=False,
            )
            for s in series_list
            if s.get("ID") or s.get("seriesID")
        ]
        folders.sort(key=lambda f: f.name.lower())
        return folders

    async def _browse_series_teachers(self, series_id: str) -> list[BrowseFolder]:
        """Return teacher sub-folders for a series, derived from search facets.

        :param series_id: Numeric series ID for the API query.
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
            folders.append(
                BrowseFolder(
                    item_id=f"st_{series_id}_{tid}",
                    provider=self.instance_id,
                    path=f"{self.domain}://series/{series_id}/teacher/{tid}",
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
                        path=f"{self.domain}://categories/{sub_id}",
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
        """Make a GET request to the YuTorah JSON API and return parsed JSON.

        Returns None for 404 responses. Raises ProviderUnavailableError for other errors.
        """
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
        safe_params = {k: v for k, v in str_params.items() if k != "userToken"}
        try:
            async with self.mass.http_session.get(
                f"{API_BASE}{endpoint}",
                params=str_params,
                headers=API_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                raw_text = await resp.text()
                return json.loads(raw_text)
        except aiohttp.ClientResponseError as exc:
            raise ProviderUnavailableError(
                f"YuTorah API {endpoint} failed (params={safe_params}): {exc}"
            ) from exc
        except aiohttp.ClientError as exc:
            raise ProviderUnavailableError(
                f"YuTorah API {endpoint} network error (params={safe_params}): {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderUnavailableError(
                f"YuTorah API {endpoint} returned invalid JSON: {exc}"
            ) from exc

    @use_cache(3600)
    async def _fetch_series_list(self) -> list[dict[str, Any]]:
        """Fetch the full list of curated series from browse/series, cached for 1 hour."""
        data = await self._api_get("browse/series", favoritesOnly=False)
        return data if isinstance(data, list) else []

    @use_cache(3600)
    async def _fetch_teachers_map(self) -> dict[str, dict[str, Any]]:
        """Fetch and cache all teachers as a dict keyed by teacher ID string."""
        data = await self._api_get("browse/teachers", favoritesOnly=False)
        if not isinstance(data, list):
            return {}
        return {str(t.get("ID") or ""): t for t in data if t.get("ID")}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


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
