"""Recommendation logic for Tidal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderType
from music_assistant_models.media_items import (
    Album,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Playlist,
    RecommendationFolder,
    Track,
    UniqueList,
)

from .constants import CACHE_CATEGORY_RECOMMENDATIONS
from .tidal_page_parser import TidalPageParser

if TYPE_CHECKING:
    from .provider import TidalProvider


class TidalRecommendationManager:
    """Manages Tidal recommendations."""

    def __init__(self, provider: TidalProvider):
        """Initialize recommendation manager."""
        self.provider = provider
        self.api = provider.api
        self.auth = provider.auth
        self.logger = provider.logger
        self.mass = provider.mass
        self.page_cache_ttl = 3 * 3600

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get this provider's recommendations organized into folders."""
        results: list[RecommendationFolder] = []
        pages = [
            "pages/home",
            "pages/for_you",
            "pages/hi_res",
            "pages/explore_new_music",
            "pages/explore_top_music",
        ]
        combined_modules: dict[str, list[Playlist | Album | Track | Artist]] = {}
        module_content_types: dict[str, MediaType] = {}
        module_page_names: dict[str, str] = {}

        try:
            all_tidal_configs = await self.mass.config.get_provider_configs(ProviderType.MUSIC)
            tidal_configs = [
                config for config in all_tidal_configs if config.domain == self.provider.domain
            ]
            sorted_instances = sorted(tidal_configs, key=lambda x: x.instance_id)
            show_user_identifier = len(sorted_instances) > 1

            for page_path in pages:
                parser = await self.get_page_content(page_path)
                page_name = page_path.split("/")[-1].replace("_", " ").title()

                if page_path in ("pages/home", "pages/explore_top_music") and show_user_identifier:
                    if (
                        sorted_instances
                        and self.provider.instance_id != sorted_instances[0].instance_id
                    ):
                        continue

                for module_info in parser._module_map:
                    title = module_info.get("title", "Unknown")
                    if not title or title == "Unknown" or "Videos" in title:
                        continue

                    items, content_type = parser.get_module_items(module_info)
                    if not items:
                        continue

                    key = f"{self.auth.user_id}_{title}"
                    if key not in combined_modules:
                        combined_modules[key] = []
                        module_content_types[key] = content_type
                        module_page_names[key] = page_name

                    combined_modules[key].extend(items)

            for key, items in combined_modules.items():
                user_id_prefix = f"{self.auth.user_id}_"
                title = key.removeprefix(user_id_prefix)

                unique_items = UniqueList(items)
                item_id = "".join(
                    c for c in key.lower().replace(" ", "_") if c.isalnum() or c == "_"
                )
                content_type = module_content_types.get(key, MediaType.PLAYLIST)
                page_name = module_page_names.get(key, "Tidal")

                folder_name = title
                if show_user_identifier and page_name not in ("Home", "Explore Top Music"):
                    user_name = (
                        self.auth.user.profile_name
                        or self.auth.user.user_name
                        or str(self.auth.user_id)
                    )
                    folder_name = f"{title} ({user_name})"

                results.append(
                    RecommendationFolder(
                        item_id=item_id,
                        name=folder_name,
                        provider=self.provider.instance_id,
                        items=UniqueList[MediaItemType | ItemMapping | BrowseFolder](unique_items),
                        subtitle=f"From {page_name} • {len(unique_items)} items",
                        translation_key=item_id,
                        icon="mdi-playlist-music"
                        if content_type == MediaType.PLAYLIST
                        else "mdi-album",
                    )
                )

        except Exception as err:
            self.logger.warning("Error fetching recommendations: %s", err)

        return results

    async def get_page_content(self, page_path: str = "pages/home") -> TidalPageParser:
        """Get a lazy page parser for a Tidal page."""
        if cached := await TidalPageParser.from_cache(self.provider, page_path):
            return cached

        try:
            locale = self.mass.metadata.locale.replace("_", "-")
            api_result = await self.api.get(
                page_path,
                base_url="https://listen.tidal.com/v1",
                params={
                    "locale": locale,
                    "deviceType": "BROWSER",
                    "countryCode": self.auth.country_code or "US",
                },
            )

            data = api_result[0] if isinstance(api_result, tuple) else api_result
            parser = TidalPageParser(self.provider)
            parser.parse_page_structure(data or {}, page_path)

            await self.mass.cache.set(
                key=page_path,
                data={
                    "module_map": parser._module_map,
                    "content_map": parser._content_map,
                    "parsed_at": parser._parsed_at,
                },
                provider=self.provider.instance_id,
                category=CACHE_CATEGORY_RECOMMENDATIONS,
                expiration=self.page_cache_ttl,
            )
            return parser
        except Exception:
            return TidalPageParser(self.provider)
