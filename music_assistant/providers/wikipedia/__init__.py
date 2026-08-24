"""
Wikipedia Metadata provider for Music Assistant.

Provides artist biographies in the user's preferred language, resolving the
article through MusicBrainz URL relations and (where MusicBrainz lacks coverage)
Wikidata sitelinks.
"""

from __future__ import annotations

from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlparse

import aiohttp
from music_assistant_models.enums import ProviderFeature
from music_assistant_models.errors import InvalidDataError, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import MediaItemMetadata

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.metadata_provider import MetadataProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
    from music_assistant_models.media_items import Artist
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.providers.musicbrainz import MusicbrainzProvider
    from music_assistant.providers.musicbrainz.models import MusicBrainzRelation


SUPPORTED_FEATURES: set[ProviderFeature] = {ProviderFeature.ARTIST_METADATA}

WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return WikipediaMetadataProvider(mass, manifest, config, SUPPORTED_FEATURES)


class WikipediaMetadataProvider(MetadataProvider):
    """Wikipedia Metadata provider."""

    throttler: Throttler

    @property
    def priority(self) -> int:
        """Priority for this provider (lower = more preferred)."""
        return 25

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = Throttler(rate_limit=1, period=1)

    async def get_artist_metadata(self, artist: Artist) -> MediaItemMetadata | None:
        """Retrieve metadata for an artist on Wikipedia."""
        if not artist.mbid:
            return None

        preferred_lang = self.mass.metadata.preferred_language
        languages: list[str] = [preferred_lang]
        if preferred_lang != "en":
            languages.append("en")

        relations = await self._musicbrainz_relations(artist.mbid)
        if not relations:
            return None

        titles_by_lang = _wiki_titles_by_lang(relations)

        # only query Wikidata for languages MusicBrainz didn't already provide, saving a
        # request whenever the MusicBrainz relations already cover the wanted languages
        missing = [lang for lang in languages if lang not in titles_by_lang]
        if missing and (qid := _wikidata_qid(relations)):
            sitelinks = await self._wikidata_sitelinks(qid, tuple(sorted(missing)))
            for lang, sitelink_title in sitelinks.items():
                titles_by_lang.setdefault(lang, sitelink_title)

        for lang in languages:
            if title := titles_by_lang.get(lang):
                if extract := await self._fetch_bio(lang, title):
                    self.logger.debug("Found bio for %s on Wikipedia in %s", artist.name, lang)
                    return MediaItemMetadata(description=extract, description_language=lang)
        return None

    async def _musicbrainz_relations(self, mbid: str) -> list[MusicBrainzRelation] | None:
        """Return the MusicBrainz URL relations for an artist."""
        mb_provider = cast("MusicbrainzProvider | None", self.mass.get_provider("musicbrainz"))
        if mb_provider is None:
            return None
        try:
            details = await mb_provider.get_artist_details(mbid)
        except InvalidDataError:
            return None
        return details.relations

    @use_cache(86400 * 90, persistent=True)
    async def _wikidata_sitelinks(self, qid: str, languages: tuple[str, ...]) -> dict[str, str]:
        """
        Return Wikipedia article titles for a Wikidata entity keyed by language.

        :param qid: Wikidata entity id (e.g. ``"Q12345"``).
        :param languages: ISO 639-1 codes to request sitelinks for.
        """
        sitefilter = "|".join(f"{lang}wiki" for lang in languages)
        data = await self._get_json(
            WIKIDATA_API_URL,
            params={
                "action": "wbgetentities",
                "ids": qid,
                "props": "sitelinks",
                "sitefilter": sitefilter,
                "format": "json",
            },
        )
        if not data:
            return {}
        sitelinks = data.get("entities", {}).get(qid, {}).get("sitelinks") or {}
        result: dict[str, str] = {}
        for site_key, payload in sitelinks.items():
            if not site_key.endswith("wiki"):
                continue
            lang = site_key[: -len("wiki")]
            title = payload.get("title")
            if isinstance(title, str) and title:
                result[lang] = title
        return result

    @use_cache(86400 * 90, persistent=True)
    async def _fetch_bio(self, lang: str, title: str) -> str | None:
        """
        Return the plain-text lead section of a Wikipedia article.

        :param lang: Wikipedia language edition (``"en"``, ``"de"``, ...).
        :param title: Article title as it appears in the URL.
        """
        data = await self._get_json(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "prop": "extracts",
                "exintro": "true",
                "explaintext": "true",
                "redirects": "1",
                "titles": title,
                "format": "json",
            },
        )
        if not data:
            return None
        pages = data.get("query", {}).get("pages") or {}
        if not pages:
            return None
        page = next(iter(pages.values()))
        extract = page.get("extract")
        if isinstance(extract, str) and extract.strip():
            return extract
        return None

    async def _get_json(
        self, url: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        """
        Return the parsed JSON from a GET request, or None when the resource is absent.

        Only a 404 yields None; a transient failure (network, another HTTP error or an
        unparsable response) raises ResourceTemporarilyUnavailable so callers do not cache
        it as a negative result.

        :param url: Request URL.
        :param params: Optional query parameters.
        """
        headers = {
            "User-Agent": f"Music Assistant/{self.mass.version} (https://music-assistant.io)"
        }
        try:
            async with (
                self.throttler,
                self.mass.http_session.get(url, params=params, headers=headers) as response,
            ):
                if response.status == 404:
                    return None
                response.raise_for_status()
                return cast("dict[str, Any]", await response.json())
        except (aiohttp.ClientError, TimeoutError, JSONDecodeError) as err:
            raise ResourceTemporarilyUnavailable("Wikipedia request failed") from err


def _wiki_titles_by_lang(relations: list[MusicBrainzRelation]) -> dict[str, str]:
    """Return Wikipedia article titles keyed by language from MusicBrainz URL relations."""
    result: dict[str, str] = {}
    for relation in relations:
        if relation.type != "wikipedia" or not relation.url:
            continue
        parsed = urlparse(relation.url.resource)
        host = parsed.netloc.lower()
        if not host.endswith(".wikipedia.org"):
            continue
        lang = host.split(".", 1)[0]
        if not parsed.path.startswith("/wiki/"):
            continue
        title = unquote(parsed.path[len("/wiki/") :])
        if title:
            result.setdefault(lang, title)
    return result


def _wikidata_qid(relations: list[MusicBrainzRelation]) -> str | None:
    """Return the Wikidata entity id from MusicBrainz URL relations, if present."""
    for relation in relations:
        if relation.type != "wikidata" or not relation.url:
            continue
        candidate = relation.url.resource.rstrip("/").rsplit("/", 1)[-1]
        if candidate.startswith("Q") and candidate[1:].isdigit():
            return candidate
    return None
