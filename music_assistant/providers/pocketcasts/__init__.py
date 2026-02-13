"""Pocket Casts music provider for Music Assistant - with custom API client."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant import MusicAssistant
from music_assistant.constants import CONF_PASSWORD, CONF_USERNAME
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.models import ProviderInstanceType

# Import our custom API client
from .api_client import LoginError, PocketCastsClient

LOGGER = logging.getLogger(__name__)

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}

BROWSE_FOLDER_ICONS: dict[str, str] = {
    "up_next": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgZD0iTTMgMi45NzE1OUMzIDIuNTY0OSAzLjQ1OTY4IDIuMzI4MzQgMy43OTA2Mi"
        "AyLjU2NDcyTDkuNDMwMzkgNi41OTMxM0M5LjcwOTU2IDYuNzkyNTQgOS43MDk1NiA3LjIwNzQ1IDku"
        "NDMwMzkgNy40MDY4NkwzLjc5MDYyIDExLjQzNTNDMy40NTk2OSAxMS42NzE2IDMgMTEuNDM1MSAzID"
        "ExLjAyODRWMi45NzE1OVoiLz4KICAgIDxwYXRoIGZpbGw9IiM4Rjk3QTQiIG9wYWNpdHk9IjAuNS"
        "IgZD0iTTEyIDdDMTIgNi40NDc3MiAxMi40NDc3IDYgMTMgNkgyMUMyMS41NTIzIDYgMjIgNi40NDc3"
        "MiAyMiA3QzIyIDcuNTUyMjggMjEuNTUyMyA4IDIxIDhIMTNDMTIuNDQ3NyA4IDEyIDcuNTUyMjggMT"
        "IgN1pNOSAxMkM5IDExLjQ0NzcgOS40NDc3MiAxMSAxMCAxMUgyMUMyMS41NTIzIDExIDIyIDExLjQ0Nz"
        "cgMjIgMTJDMjIgMTIuNTUyMyAyMS41NTIzIDEzIDIxIDEzSDEwQzkuNDQ3NzIgMTMgOSAxMi41NTIz"
        "IDkgMTJaTTEwIDE2QzkuNDQ3NzIgMTYgOSAxNi40NDc3IDkgMTdDOSAxNy41NTIzIDkuNDQ3NzIgMT"
        "ggMTAgMThIMjFDMjEuNTUyMyAxOCAyMiAxNy41NTIzIDIyIDE3QzIyIDE2LjQ0NzcgMjEuNTUyMyAx"
        "NiAyMSAxNkgxMFoiLz4KPC9zdmc+Cg=="
    ),
    "new_releases": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPGcgZmlsbD0i"
        "IzhGOTdBNCIgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMCwgMSkiPgogICAgICAgIDxwYXRoIGQ9Ik0xNC"
        "43OTM1IDQuNTU3MzVMMTUuNzc0MSAwLjc4OTIwOUMxNS45Njg4IDAuMDQxMDUwOSAxNy4wMzExIDAu"
        "MDQxMDcxOSAxNy4yMjU4IDAuNzg5MjM3TDE4LjIwNjIgNC41NTczMkMxOC4yNzcgNC44MjkxNSAxOC"
        "40OTM3IDUuMDM4NjggMTguNzY3NyA1LjEwMDIzTDIxLjc0MTkgNS43NjgyM0MyMi41MjI4IDUuOTQz"
        "NjIgMjIuNTIyOCA3LjA1NjM5IDIxLjc0MTkgNy4yMzE3N0wxOC43Njc3IDcuODk5NzdDMTguNDkzNy"
        "A3Ljk2MTMyIDE4LjI3NyA4LjE3MDg1IDE4LjIwNjIgOC40NDI2OEwxNy4yMjU4IDEyLjIxMDhDMTcu"
        "MDMxMSAxMi45NTg5IDE1Ljk2ODggMTIuOTU4OSAxNS43NzQxIDEyLjIxMDhMMTQuNzkzNSA4LjQ0Mj"
        "Y1QzE0LjcyMjcgOC4xNzA4MyAxNC41MDYxIDcuOTYxMzIgMTQuMjMyIDcuODk5NzdMMTEuMjU4IDcu"
        "MjMxNzdDMTAuNDc3MSA3LjA1NjM5IDEwLjQ3NzEgNS45NDM2MiAxMS4yNTggNS43NjgyNEwxNC4yMz"
        "IgNS4xMDAyM0MxNC41MDYxIDUuMDM4NjggMTQuNzIyNyA0LjgyOTE3IDE0Ljc5MzUgNC41NTczNVoi"
        "Lz4KICAgICAgICA8cGF0aCBvcGFjaXR5PSIwLjgiIGQ9Ik01LjI5OTQgOS4yNjgzTDYuMDMwNjYgNy"
        "4yNzc2M0M2LjE5MTEyIDYuODQwODUgNi44MDg4OCA2Ljg0MDg1IDYuOTY5MzQgNy4yNzc2M0w3Ljcw"
        "MDYgOS4yNjgzQzcuNzU0MjUgOS40MTQzNSA3Ljg3Mjg0IDkuNTI3MSA4LjAyMTQxIDkuNTczMzJMOS"
        "40NjU0MSAxMC4wMjI2QzkuOTM0MDMgMTAuMTY4MyA5LjkzNDAzIDEwLjgzMTYgOS40NjU0MSAxMC45"
        "Nzc0TDguMDIxNCAxMS40MjY3QzcuODcyODMgMTEuNDcyOSA3Ljc1NDI1IDExLjU4NTYgNy43MDA2ID"
        "ExLjczMTdMNi45NjkzMyAxMy43MjI0QzYuODA4ODggMTQuMTU5MiA2LjE5MTEyIDE0LjE1OTIgNi4w"
        "MzA2NiAxMy43MjI0TDUuMjk5NCAxMS43MzE3QzUuMjQ1NzUgMTEuNTg1NiA1LjEyNzE3IDExLjQ3Mj"
        "kgNC45Nzg2IDExLjQyNjdMMy41MzQ1OSAxMC45Nzc0QzMuMDY1OTcgMTAuODMxNiAzLjA2NTk3IDEw"
        "LjE2ODMgMy41MzQ1OSAxMC4wMjI2TDQuOTc4NTkgOS41NzMzMkM1LjEyNzE2IDkuNTI3MSA1LjI0NT"
        "c1IDkuNDE0MzUgNS4yOTk0IDkuMjY4M1oiLz4KICAgICAgICA8cGF0aCBvcGFjaXR5PSIwLjYiIGQ9"
        "Ik0xMC42ODgyIDE2LjAxNEwxMS41MjQ3IDEzLjQ1NDNDMTEuNjc0OSAxMi45OTQ3IDEyLjMyNTEgMT"
        "IuOTk0NyAxMi40NzUzIDEzLjQ1NDNMMTMuMzExOCAxNi4wMTRDMTMuMzYwNCAxNi4xNjI3IDEzLjQ3"
        "NTcgMTYuMjggMTMuNjIzNSAxNi4zMzEyTDE1LjYzNSAxNy4wMjc1QzE2LjA4MzYgMTcuMTgyOCAxNi"
        "4wODM2IDE3LjgxNzIgMTUuNjM1IDE3Ljk3MjVMMTMuNjIzNSAxOC42Njg4QzEzLjQ3NTcgMTguNzIg"
        "MTMuMzYwNCAxOC44MzczIDEzLjMxMTggMTguOTg2TDEyLjQ3NTMgMjEuNTQ1N0MxMi4zMjUxIDIyLj"
        "AwNTMgMTEuNjc0OSAyMi4wMDUzIDExLjUyNDcgMjEuNTQ1N0wxMC42ODgyIDE4Ljk4NkMxMC42Mzk2"
        "IDE4LjgzNzMgMTAuNTI0MyAxOC43MiAxMC4zNzY1IDE4LjY2ODhMOC4zNjQ5OCAxNy45NzI1QzcuOT"
        "E2MzggMTcuODE3MiA3LjkxNjM5IDE3LjE4MjggOC4zNjQ5OCAxNy4wMjc1TDEwLjM3NjUgMTYuMzMx"
        "MkMxMC41MjQzIDE2LjI4IDEwLjYzOTYgMTYuMTYyNyAxMC42ODgyIDE2LjAxNFoiLz4KICAgIDwvZz"
        "4KPC9zdmc+Cg=="
    ),
    "in_progress": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgb3BhY2l0eT0iMC41IiBkPSJNNCAxMkM0IDE2LjQxODMgNy41ODE3MiAyMCAxMi"
        "AyMEMxNi40MTgzIDIwIDIwIDE2LjQxODMgMjAgMTJDMjAgNy41ODE3MiAxNi40MTgzIDQgMTIgNEM3"
        "LjU4MTcyIDQgNCA3LjU4MTcyIDQgMTJaTTE4IDEyQzE4IDE1LjMxMzcgMTUuMzEzNyAxOCAxMiAxOE"
        "M4LjY4NjI5IDE4IDYgMTUuMzEzNyA2IDEyQzYgOC42ODYyOSA4LjY4NjI5IDYgMTIgNkMxNS4zMTM3"
        "IDYgMTggOC42ODYyOSAxOCAxMloiLz4KICAgIDxwYXRoIGZpbGw9IiM4Rjk3QTQiIGQ9Ik0xNi45Mj"
        "UzIDE4LjMwNDFDMjAuNDA2OSAxNS41ODM5IDIxLjAyNDMgMTAuNTU2NCAxOC4zMDQxIDcuMDc0NzJD"
        "MTYuODI2OSA1LjE4Mzk0IDE0LjYxNjcgNC4wODMyNyAxMi4yNjUgNC4wMDQyNEMxMS43MTMxIDMuOT"
        "g1NjkgMTEuMjUwNiA0LjQxODExIDExLjIzMiA0Ljk3MDA4QzExLjIxMzUgNS41MjIwNiAxMS42NDU5"
        "IDUuOTg0NTYgMTIuMTk3OSA2LjAwMzExQzEzLjk2MzkgNi4wNjI0NiAxNS42MTkzIDYuODg2ODYgMT"
        "YuNzI4MSA4LjMwNjA1QzE4Ljc2ODIgMTAuOTE3MyAxOC4zMDUyIDE0LjY4OCAxNS42OTQgMTYuNzI4"
        "MUMxNS4yNTg4IDE3LjA2ODEgMTUuMTgxNiAxNy42OTY1IDE1LjUyMTYgMTguMTMxOEMxNS44NjE2ID"
        "E4LjU2NyAxNi40OTAxIDE4LjY0NDEgMTYuOTI1MyAxOC4zMDQxWiIvPgo8L3N2Zz4K"
    ),
    "starred": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgdHJhbnNmb3JtPSJ0cmFuc2xhdGUoMSwgLTAuNSkiIGQ9Ik0xMS41ODMxIDE3Lj"
        "Q3NzZMOC4wNzM4NCAxOS4yOTMzQzcuMDk0MTMgMTkuODAwMiA2LjQ0NjgxIDE5LjMxOTcgNi42MjQ2"
        "NiAxOC4yNDA0TDcuMjY3MDcgMTQuMzQxOEw0LjQ1NTgxIDExLjU2NTRDMy42NzA5NyAxMC43OTAzID"
        "MuOTI3OTEgMTAuMDI2MiA1LjAwOTM1IDkuODYxNzdMOC45MTU2NSA5LjI2ODAxTDEwLjY4NzUgNS43"
        "MzYzOEMxMS4xODIxIDQuNzUwNDIgMTEuOTg4MiA0Ljc1ODY3IDEyLjQ3ODggNS43MzYzOEwxNC4yNT"
        "A2IDkuMjY4MDFMMTguMTU2OSA5Ljg2MTc3QzE5LjI0NzQgMTAuMDI3NSAxOS40ODg3IDEwLjc5Njgg"
        "MTguNzEwNCAxMS41NjU0TDE1Ljg5OTIgMTQuMzQxOEwxNi41NDE2IDE4LjI0MDRDMTYuNzIwOSAxOS"
        "4zMjg4IDE2LjA2MzkgMTkuNzk2IDE1LjA5MjQgMTkuMjkzM0wxMS41ODMxIDE3LjQ3NzZaIi8+Cjwv"
        "c3ZnPgo="
    ),
    "history": (
        "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIH"
        "ZpZXdCb3g9IjAgMCAyNCAyNCIgd2lkdGg9IjI0MCIgaGVpZ2h0PSIyNDAiPgogICAgPHBhdGggZmls"
        "bD0iIzhGOTdBNCIgb3BhY2l0eT0iMC41IiBjbGlwLXJ1bGU9ImV2ZW5vZGQiIGQ9Ik0xMC41IDEzQz"
        "EwLjUgMTMuNTUyMyAxMC45NDc3IDE0IDExLjUgMTRIMTQuNUMxNS4wNTIzIDE0IDE1LjUgMTMuNTUy"
        "MyAxNS41IDEzQzE1LjUgMTIuNDQ3NyAxNS4wNTIzIDEyIDE0LjUgMTJIMTIuNUwxMi41IDEwQzEyLj"
        "UgOS40NDc3MiAxMi4wNTIzIDkgMTEuNSA5QzEwLjk0NzcgOSAxMC41IDkuNDQ3NzIgMTAuNSAxMFYx"
        "M1oiLz4KICAgIDxwYXRoIGZpbGw9IiM4Rjk3QTQiIGZpbGwtcnVsZT0iZXZlbm9kZCIgY2xpcC1y"
        "dWxlPSJldmVub2RkIiBkPSJNMTIgMThDMTUuMzEzNyAxOCAxOCAxNS4zMTM3IDE4IDEyQzE4IDguNj"
        "g2MjkgMTUuMzEzNyA2IDEyIDZDOC42ODYyOSA2IDYgOC42ODYyOSA2IDEyQzYgMTUuMzEzNyA4LjY4"
        "NjI5IDE4IDEyIDE4Wk0xMiAyMEMxNi40MTgzIDIwIDIwIDE2LjQxODMgMjAgMTJDMjAgNy41ODE3Mi"
        "AxNi40MTgzIDQgMTIgNEM3LjU4MTcyIDQgNCA3LjU4MTcyIDQgMTJDNCAxNi40MTgzIDcuNTgxNzIg"
        "MjAgMTIgMjBaIi8+Cjwvc3ZnPgo="
    ),
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PocketCastsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key=CONF_USERNAME,
            type=ConfigEntryType.STRING,
            label="Email",
            description="Your Pocket Casts email address",
            required=True,
        ),
        ConfigEntry(
            key=CONF_PASSWORD,
            type=ConfigEntryType.SECURE_STRING,
            label="Password",
            description="Your Pocket Casts password",
            required=True,
        ),
    )


class PocketCastsProvider(MusicProvider):
    """Provider for Pocket Casts podcast service."""

    _client: PocketCastsClient | None = None

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        email = self.config.get_value(CONF_USERNAME)
        password = self.config.get_value(CONF_PASSWORD)

        if not email or not password:
            msg = "Email and password are required for Pocket Casts"
            raise LoginFailed(msg)

        try:
            LOGGER.info("Initializing Pocket Casts provider")
            self._client = PocketCastsClient()
            self._duration_cache: dict[str, int] = {}
            await self._client.login(str(email), str(password))

            # Test basic functionality
            podcasts = await self._client.get_subscribed_podcasts()
            LOGGER.info("Successfully initialized with %d podcasts", len(podcasts))

        except LoginError as err:
            LOGGER.error("Failed to login: %s", err)
            raise LoginFailed(f"Failed to login to Pocket Casts: {err}") from err
        except Exception as err:
            LOGGER.exception("Failed to initialize: %s", err)
            raise LoginFailed(f"Failed to initialize Pocket Casts: {err}") from err

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._client and self._client.session:
            await self._client.session.close()

    def _convert_podcast(self, podcast_data: dict[str, Any]) -> Podcast:
        """Convert API podcast data to Podcast object."""
        # Extract podcast metadata from nested structure
        podcast_info = podcast_data.get("podcast", podcast_data)  # Handle both structures

        return Podcast(
            item_id=podcast_info["uuid"],
            provider=self.instance_id,
            name=podcast_info.get("title", ""),
            provider_mappings={
                ProviderMapping(
                    item_id=podcast_info["uuid"],
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            metadata=MediaItemMetadata(
                description=podcast_info.get("description"),
                images=UniqueList(
                    [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=f"https://static.pocketcasts.com/discover/images/280/{podcast_info['uuid']}.jpg",
                            provider=self.instance_id,
                            remotely_accessible=True,
                        )
                    ]
                ),
            ),
        )

    def _convert_episode(
        self, episode_data: dict[str, Any], podcast_uuid: str
    ) -> PodcastEpisode | None:
        """Convert Pocket Casts episode data to MA PodcastEpisode object."""
        try:
            episode_uuid = episode_data.get("uuid")
            if not episode_uuid:
                return None

            # Create composite ID: podcast_uuid:episode_uuid
            item_id = f"{podcast_uuid}:{episode_uuid}"

            episode_item = PodcastEpisode(
                item_id=item_id,
                provider=self.instance_id,
                name=episode_data.get("title", "Unknown Episode"),
                podcast=ItemMapping(
                    media_type=MediaType.PODCAST,
                    item_id=podcast_uuid,
                    provider=self.instance_id,
                    name="",
                ),
                position=episode_data.get("episode_number", 0),
                provider_mappings={
                    ProviderMapping(
                        item_id=item_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        audio_format=AudioFormat(
                            content_type=ContentType.try_parse(
                                episode_data.get("file_type", "audio/mpeg")
                            ),
                        ),
                        url=episode_data.get("url", ""),
                    )
                },
            )

            # Add duration
            if episode_data.get("duration"):
                episode_item.duration = int(episode_data["duration"])

            # Add metadata
            if episode_data.get("title"):
                episode_item.metadata.label = episode_data["title"]
            if episode_data.get("show_notes"):
                episode_item.metadata.description = episode_data["show_notes"]
            # Add thumbnail - use episode thumbnail if available, otherwise use podcast thumbnail
            thumbnail_url = episode_data.get("thumbnail_url")
            if not thumbnail_url:
                # Fallback to podcast thumbnail
                thumbnail_url = (
                    f"https://static.pocketcasts.com/discover/images/280/{podcast_uuid}.jpg"
                )

            episode_item.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumbnail_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
            return episode_item

        except Exception as err:
            LOGGER.debug("Error converting episode: %s", err)
            return None

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Get all podcasts from user's library."""
        if not self._client:
            return

        try:
            podcasts = await self._client.get_subscribed_podcasts()
            for podcast_data in podcasts:
                podcast_item = self._convert_podcast({"podcast": podcast_data})
                if podcast_item:
                    yield podcast_item
        except Exception as err:
            LOGGER.error("Error getting library podcasts: %s", err)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add podcast to library (subscribe).

        :param item: The media item to add to the library.
        """
        # Only handle podcasts
        if not isinstance(item, Podcast):
            return await super().library_add(item)

        if not self._client:
            LOGGER.error("Cannot subscribe: client not initialized")
            return False

        try:
            # Get the podcast UUID from the item
            podcast_uuid = item.item_id

            LOGGER.info("Adding podcast to library: %s (%s)", item.name, podcast_uuid)

            # Call API to subscribe
            success = await self._client.subscribe_podcast(podcast_uuid)

            if success:
                LOGGER.info("Successfully subscribed to podcast: %s", item.name)
            else:
                LOGGER.error("Failed to subscribe to podcast: %s", item.name)

            return success

        except Exception as err:
            LOGGER.exception("Error adding podcast to library: %s", err)
            return False

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove podcast from library (unsubscribe).

        :param prov_item_id: The provider item ID to remove from library.
        :param media_type: The media type of the item.
        """
        # Only handle podcasts
        if media_type != MediaType.PODCAST:
            return await super().library_remove(prov_item_id, media_type)

        if not self._client:
            LOGGER.error("Cannot unsubscribe: client not initialized")
            return False

        try:
            podcast_uuid = prov_item_id

            LOGGER.info("Removing podcast from library: %s", podcast_uuid)

            # Call API to unsubscribe
            success = await self._client.unsubscribe_podcast(podcast_uuid)

            if success:
                LOGGER.info("Successfully unsubscribed from podcast: %s", podcast_uuid)
            else:
                LOGGER.error("Failed to unsubscribe from podcast: %s", podcast_uuid)

            return success

        except Exception as err:
            LOGGER.exception("Error removing podcast from library: %s", err)
            return False

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details."""
        # First try library
        async for podcast in self.get_library_podcasts():
            if podcast.item_id == prov_podcast_id:
                return podcast

        # Not in library - fetch from podcast-api which redirects to static JSON
        LOGGER.debug("Podcast not in library, fetching from API: %s", prov_podcast_id)

        if not self._client or not self._client.session:
            msg = f"Podcast {prov_podcast_id} not found in library and client not available"
            raise MediaNotFoundError(msg)

        try:
            # This endpoint returns a 302 redirect to the static JSON with timestamp
            async with self._client.session.get(
                f"https://podcast-api.pocketcasts.com/podcast/full/{prov_podcast_id}"
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return self._convert_podcast(data)
                raise MediaNotFoundError(
                    f"Podcast {prov_podcast_id} not found (status {response.status})"
                )

        except Exception as err:
            LOGGER.error("Error fetching podcast %s: %s", prov_podcast_id, err)
            raise MediaNotFoundError(
                f"podcast://{prov_podcast_id} not found on provider {self.domain}"
            ) from err

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get all episodes for a podcast with playback status from Pocketcasts."""
        if not self._client:
            return

        try:
            # Fetch episode metadata and user status in parallel
            episodes, in_progress, history = await asyncio.gather(
                self._client.get_podcast_episodes(prov_podcast_id),
                self._client.get_in_progress_episodes(),
                self._client.get_history(),
            )

            LOGGER.debug(
                "Got %d episodes, %d in-progress, %d history for podcast %s",
                len(episodes),
                len(in_progress),
                len(history),
                prov_podcast_id,
            )

            # Create lookup maps for status
            in_progress_map = {ep.get("uuid"): ep for ep in in_progress}
            history_set = {ep.get("uuid") for ep in history}

            for episode_data in episodes:
                episode_item = self._convert_episode(episode_data, prov_podcast_id)
                if episode_item:
                    episode_uuid = episode_data.get("uuid")

                    # Check in-progress status
                    if episode_uuid in in_progress_map:
                        ip_data = in_progress_map[episode_uuid]
                        played_up_to = ip_data.get("playedUpTo", 0)
                        duration = ip_data.get("duration", episode_data.get("duration", 0))

                        episode_item.resume_position_ms = played_up_to * 1000

                        # Consider played if > 90% complete
                        if duration > 0:
                            episode_item.fully_played = (played_up_to / duration) > 0.9

                        LOGGER.debug(
                            "Episode %s in-progress: %d/%d seconds (%.1f%%)",
                            episode_uuid,
                            played_up_to,
                            duration,
                            (played_up_to / duration * 100) if duration > 0 else 0,
                        )

                    # If in history but not in-progress, likely fully played
                    elif episode_uuid in history_set:
                        episode_item.fully_played = True
                        LOGGER.debug("Episode %s in history, marking as played", episode_uuid)

                    yield episode_item
        except Exception as err:
            LOGGER.error("Error getting episodes for podcast %s: %s", prov_podcast_id, err)

    async def _get_special_folder_episodes(
        self, folder_name: str
    ) -> list[MediaItemType | BrowseFolder]:
        """Get episodes for a special browse folder.

        :param folder_name: Name of the special folder (up_next, new_releases, etc.)
        """
        if not self._client:
            return []

        try:
            # Get episodes from the appropriate API endpoint
            if folder_name == "up_next":
                episode_list = await self._client.get_up_next_episodes()
            elif folder_name == "new_releases":
                episode_list = await self._client.get_new_releases()
            elif folder_name == "in_progress":
                episode_list = await self._client.get_in_progress_episodes()
            elif folder_name == "starred":
                episode_list = await self._client.get_starred_episodes()
            else:  # history
                episode_list = await self._client.get_history()
        except Exception as err:
            LOGGER.error("Error fetching %s folder: %s", folder_name, err)
            return []

        LOGGER.debug("Got %d episodes from %s API", len(episode_list), folder_name)

        items: list[MediaItemType | BrowseFolder] = []
        # Convert episodes to PodcastEpisode objects
        # Note: up_next returns dict with UUIDs as keys, others return list
        if isinstance(episode_list, dict):
            # Up Next format: {episode_uuid: {episode_data}}
            episode_items: list[tuple[str | None, dict[str, Any]]] = list(episode_list.items())
        else:
            # Other formats: [{episode_data}, ...]
            episode_items = [(None, ep) for ep in episode_list]

        for episode_uuid_key, episode_data in episode_items:
            # For up_next, add the UUID from the dict key to the episode data
            if episode_uuid_key and "uuid" not in episode_data:
                episode_data["uuid"] = episode_uuid_key

            # Extract podcast UUID from episode data
            # Note: up_next endpoint returns podcast as string, others return object
            podcast_field = episode_data.get("podcast")
            podcast_uuid: str | None = None
            if isinstance(podcast_field, str):
                podcast_uuid = podcast_field
            elif isinstance(podcast_field, dict):
                podcast_uuid = podcast_field.get("uuid", None)
            else:
                podcast_uuid = episode_data.get("podcastUuid", None)

            if podcast_uuid:
                episode_item = self._convert_episode(episode_data, podcast_uuid)
                if episode_item:
                    items.append(episode_item)

        LOGGER.debug("Converted %d episodes successfully from %s", len(items), folder_name)
        return items

    def _create_browse_folders(self) -> list[BrowseFolder]:
        """Create special browse folders for root level."""
        folders = [
            ("up_next", "Up Next"),
            ("new_releases", "New Releases"),
            ("in_progress", "In Progress"),
            ("starred", "Starred"),
            ("history", "History"),
        ]
        return [
            BrowseFolder(
                item_id=folder_id,
                provider=self.instance_id,
                path=f"{self.instance_id}://{folder_id}",
                name=name,
                image=MediaItemImage(
                    type=ImageType.THUMB,
                    path=BROWSE_FOLDER_ICONS[folder_id],
                    provider=self.instance_id,
                    remotely_accessible=True,
                ),
            )
            for folder_id, name in folders
        ]

    async def browse(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Browse this provider's items."""
        if not self._client:
            return []

        # Parse the path
        item_path = path.split("://", 1)[1] if "://" in path else path
        LOGGER.debug("Browse called with path: %s, parsed to: %s", path, item_path)

        items: list[MediaItemType | BrowseFolder] = []

        if not item_path:
            # Root level - show special folders and subscribed podcasts
            try:
                # Add special browse folders at the top
                items.extend(self._create_browse_folders())

                # Add subscribed podcasts
                podcasts = await self._client.get_subscribed_podcasts()
                for podcast_data in podcasts:
                    podcast_item = self._convert_podcast(podcast_data)
                    if podcast_item:
                        items.append(podcast_item)
                LOGGER.debug(
                    "Returning %d items at root level (5 folders + %d podcasts)",
                    len(items),
                    len(items) - 5,
                )
                return items
            except Exception as err:
                LOGGER.exception("Error browsing podcasts: %s", err)
                return []

        elif item_path in ("up_next", "new_releases", "in_progress", "starred", "history"):
            # Special folder - show episodes from appropriate API endpoint
            LOGGER.debug("Fetching episodes for special folder: %s", item_path)
            try:
                return await self._get_special_folder_episodes(item_path)
            except Exception as err:
                LOGGER.exception("Error browsing special folder %s: %s", item_path, err)
                return []
        else:
            # Regular podcast path - show episodes for the podcast with playback status
            LOGGER.debug("Fetching episodes for podcast: %s", item_path)
            try:
                # Fetch episodes and user status in parallel
                episodes, in_progress, history = await asyncio.gather(
                    self._client.get_podcast_episodes(item_path),
                    self._client.get_in_progress_episodes(),
                    self._client.get_history(),
                )

                LOGGER.debug(
                    "Got %d episodes, %d in-progress, %d history",
                    len(episodes),
                    len(in_progress),
                    len(history),
                )

                # Create lookup maps for status
                in_progress_map = {ep.get("uuid"): ep for ep in in_progress}
                history_set = {ep.get("uuid") for ep in history}

                for episode_data in episodes:
                    episode_item = self._convert_episode(episode_data, item_path)
                    if episode_item:
                        episode_uuid = episode_data.get("uuid")

                        # Check in-progress status
                        if episode_uuid in in_progress_map:
                            ip_data = in_progress_map[episode_uuid]
                            played_up_to = ip_data.get("playedUpTo", 0)
                            duration = ip_data.get("duration", episode_data.get("duration", 0))

                            episode_item.resume_position_ms = played_up_to * 1000

                            # Consider played if > 90% complete
                            if duration > 0:
                                episode_item.fully_played = (played_up_to / duration) > 0.9

                        # If in history but not in-progress, likely fully played
                        elif episode_uuid in history_set:
                            episode_item.fully_played = True

                        items.append(episode_item)

                LOGGER.debug("Converted %d episodes successfully", len(items))
                return items
            except Exception as err:
                LOGGER.exception("Error browsing episodes for %s: %s", item_path, err)
                return []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamable URL and details for the given media item."""
        if ":" not in item_id:
            raise MediaNotFoundError(f"Invalid episode ID format: {item_id}")

        podcast_uuid, episode_uuid = item_id.split(":", 1)

        if not self._client:
            raise MediaNotFoundError("Client not initialized")

        episode_data = await self._client.get_episode_details(episode_uuid)
        if not episode_data:
            raise MediaNotFoundError(f"Episode {item_id} not found")

        url = episode_data.get("url", "")
        if not url:
            raise MediaNotFoundError(f"No URL found for episode {item_id}")

        # Add to Up Next (play now position) when starting playback
        await self._client.play_now(
            episode_uuid=episode_uuid,
            podcast_uuid=podcast_uuid,
            title=episode_data.get("title", ""),
            url=url,
            published=episode_data.get("published"),
        )

        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=ContentType.try_parse(episode_data.get("fileType", "audio/mpeg")),
            ),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search for podcasts."""
        results = SearchResults()

        if not self._client:
            return results

        if not media_types or MediaType.PODCAST in media_types:
            try:
                podcasts = await self._client.search_podcasts(search_query)

                podcast_results = []
                for podcast_data in podcasts[:limit]:
                    podcast_item = self._convert_podcast(podcast_data)
                    if podcast_item:
                        podcast_results.append(podcast_item)

                results.podcasts = podcast_results

            except Exception as err:
                LOGGER.debug("Error searching Pocket Casts: %s", err)

        return results

    async def get_podcast_episode(self, prov_item_id: str) -> PodcastEpisode:
        """Get full podcast episode details by id.

        Uses /user/episode endpoint for accurate duration and playback status.
        """
        if not self._client:
            msg = "Client not available"
            raise MediaNotFoundError(msg)

        # prov_item_id format is "podcast_uuid:episode_uuid"
        if ":" not in prov_item_id:
            msg = f"Invalid episode id format: {prov_item_id}"
            raise MediaNotFoundError(msg)
        podcast_uuid, episode_uuid = prov_item_id.split(":", 1)

        LOGGER.debug("Getting episode %s from podcast %s", episode_uuid, podcast_uuid)

        # Use /user/episode for accurate data (correct duration, playback status)
        episode_data = await self._client.get_episode_details(episode_uuid)

        if episode_data:
            # Convert using the accurate data from /user/episode
            episode_item = self._convert_episode(episode_data, podcast_uuid)
            if episode_item:
                # Set playback status from the response
                played_up_to = episode_data.get("playedUpTo", 0)
                duration = episode_data.get("duration", 0)
                playing_status = episode_data.get("playingStatus", 1)

                # Update duration with correct value
                if duration > 0:
                    episode_item.duration = duration

                # Set resume position
                if played_up_to > 0:
                    episode_item.resume_position_ms = played_up_to * 1000

                # Set fully_played based on playingStatus
                # 1=unplayed, 2=in_progress, 3=played
                if playing_status == 3:
                    episode_item.fully_played = True
                elif playing_status == 2 and duration > 0:
                    # In progress - check if > 90% complete
                    episode_item.fully_played = (played_up_to / duration) > 0.9

                LOGGER.debug(
                    "Episode %s: duration=%d, status=%d, position=%d ms",
                    episode_uuid,
                    duration,
                    playing_status,
                    episode_item.resume_position_ms,
                )

                return episode_item

        # Fallback to static API if /user/episode fails
        LOGGER.debug("Falling back to static API for episode %s", episode_uuid)
        episodes = await self._client.get_podcast_episodes(podcast_uuid)

        for ep_data in episodes:
            if ep_data["uuid"] == episode_uuid:
                episode_item = self._convert_episode(ep_data, podcast_uuid)
                if episode_item:
                    return episode_item

        msg = f"Episode {episode_uuid} not found in podcast {podcast_uuid}"
        raise MediaNotFoundError(msg)

    async def get_resume_position(self, item_id: str, media_type: MediaType) -> tuple[bool, int]:
        """Return the resume position for a podcast episode.

        :param item_id: The episode item ID (format: podcast_uuid:episode_uuid).
        :param media_type: The media type (should be PODCAST_EPISODE).

        Returns: (fully_played, position_milliseconds)
        """
        LOGGER.debug("Getting resume position for episode: %s", item_id)

        if not self._client:
            return (False, 0)

        try:
            # item_id format is "podcast_uuid:episode_uuid"
            if ":" not in item_id:
                return (False, 0)

            _, episode_uuid = item_id.split(":", 1)

            # Get in-progress episodes
            in_progress = await self._client.get_in_progress_episodes()

            for ep in in_progress:
                if ep.get("uuid") == episode_uuid:
                    played_up_to = int(ep.get("playedUpTo", 0))  # seconds from API
                    duration = int(ep.get("duration", 0))

                    # Consider fully played if > 90%
                    fully_played = duration > 0 and (played_up_to / duration) > 0.9

                    LOGGER.debug(
                        "Resume position for %s: %d seconds / %d ms (fully_played=%s)",
                        episode_uuid,
                        played_up_to,
                        played_up_to * 1000,
                        fully_played,
                    )
                    # Return position in milliseconds as expected by Music Assistant
                    return (fully_played, played_up_to * 1000)

            # Not in progress list
            return (False, 0)

        except Exception as err:
            LOGGER.error("Error getting resume position: %s", err)
            return (False, 0)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """Handle callback when a podcast episode has been played.

        Called by the Queue controller periodically while playing.
        We ignore MA's fully_played flag because MA uses the wrong (shorter) duration
        from the static API. Instead, we fetch the real duration from /user/episode
        and only mark as played when position >= real_duration - 15.

        :param media_type: The media type.
        :param prov_item_id: The provider item ID (format: podcast_uuid:episode_uuid).
        :param fully_played: Ignored - MA's duration may be wrong.
        :param position: Last known position in seconds.
        :param media_item: The full media item details.
        :param is_playing: Ignored.
        """
        # Only handle podcast episodes
        if media_type != MediaType.PODCAST_EPISODE:
            return

        if media_item is None or not isinstance(media_item, PodcastEpisode):
            return

        if not self._client:
            LOGGER.warning("Cannot sync progress: client not initialized")
            return

        # Parse the item ID
        if ":" not in prov_item_id:
            LOGGER.warning("Invalid item_id format: %s", prov_item_id)
            return

        podcast_uuid, episode_uuid = prov_item_id.split(":", 1)

        # Normalize position: treat None as 0
        if position is None:
            position = 0

        try:
            # Case 1: User explicitly marked as unplayed (not during active playback)
            if position == 0 and not is_playing:
                LOGGER.info("Marking episode %s as unplayed", episode_uuid)
                await self._client.mark_episode_unplayed(podcast_uuid, episode_uuid)
                await self._client.archive_episode(podcast_uuid, episode_uuid, archive=False)
                return

            # Use cached duration to avoid repeated API calls (~every 30s)
            real_duration = self._duration_cache.get(episode_uuid, 0)

            if not real_duration:
                # First call for this episode - fetch and cache duration
                episode_details = await self._client.get_episode_details(episode_uuid)
                if not episode_details:
                    await self._client.update_episode_progress(podcast_uuid, episode_uuid, position)
                    return
                real_duration = episode_details.get("duration", 0)
                if real_duration > 0:
                    self._duration_cache[episode_uuid] = real_duration

            # Case 2: Near or past the end (within 45 seconds of real duration)
            # Threshold must exceed MA's 30-second callback interval to guarantee
            # the last callback before episode end triggers completion.
            if real_duration > 0 and position >= (real_duration - 45):
                # Need fresh playing_status for completion logic
                episode_details = await self._client.get_episode_details(episode_uuid)
                playing_status = episode_details.get("playingStatus", 1) if episode_details else 1
                if playing_status != 3:
                    LOGGER.info(
                        "Episode %s at %ds (end=%ds), marking as played",
                        episode_uuid,
                        position,
                        real_duration,
                    )
                    await self._client.update_episode_progress(
                        podcast_uuid, episode_uuid, real_duration
                    )
                    await self._client.mark_episode_played(podcast_uuid, episode_uuid)
                    await self._client.remove_from_up_next(episode_uuid)
                    await self._client.archive_episode(podcast_uuid, episode_uuid, archive=True)
                    # Clean up cache for completed episode
                    self._duration_cache.pop(episode_uuid, None)
                else:
                    LOGGER.debug(
                        "Episode %s already completed, skipping (pos=%ds, end=%ds)",
                        episode_uuid,
                        position,
                        real_duration,
                    )
                return

            # Case 3: Regular progress update (not near end, no API call needed)
            LOGGER.debug(
                "Syncing progress for episode %s: %d/%d seconds (%.1f%%)",
                episode_uuid,
                position,
                real_duration,
                (position / real_duration * 100) if real_duration > 0 else 0,
            )
            await self._client.update_episode_progress(podcast_uuid, episode_uuid, position)

        except Exception as err:
            LOGGER.error("Error syncing playback progress: %s", err)
