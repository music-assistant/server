"""Simplest client for gPodder.

Should be compatible with Nextcloud App GPodder Sync, and the original api
of gpodder.net (mygpo) or drop-in replacements like opodsync.
Gpodder Sync uses guid optionally.
"""

import datetime
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import aiohttp
from aiohttp.client_exceptions import ClientResponseError
from mashumaro.config import BaseConfig
from mashumaro.mixins.json import DataClassJSONMixin
from mashumaro.types import Discriminator


# https://gpoddernet.readthedocs.io/en/latest/api/reference/subscriptions.html#upload-subscription-changes
@dataclass(kw_only=True)
class SubscriptionsChangeRequest(DataClassJSONMixin):
    """SubscriptionChangeRequest."""

    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)


# https://gpoddernet.readthedocs.io/en/latest/api/reference/subscriptions.html#upload-subscription-changes
@dataclass(kw_only=True)
class SubscriptionsGet(SubscriptionsChangeRequest):
    """SubscriptionsGet."""

    timestamp: int


class EpisodeActionType(StrEnum):
    """EpisodeActionType."""

    DOWNLOAD = "download"
    DELETE = "delete"
    PLAY = "play"
    NEW = "new"
    FLATTR = "flattr"


@dataclass(kw_only=True)
class EpisodeAction(DataClassJSONMixin):
    """General EpisodeAction.

    See https://gpoddernet.readthedocs.io/en/latest/api/reference/events.html
    """

    class Config(BaseConfig):
        """Config."""

        discriminator = Discriminator(
            field="action",
            include_subtypes=True,
        )
        omit_none = True  # only nextcloud supports guid

    podcast: str
    episode: str
    timestamp: str = ""
    guid: str | None = None


@dataclass(kw_only=True)
class EpisodeActionDownload(EpisodeAction):
    """EpisodeActionDownload."""

    action: EpisodeActionType = EpisodeActionType.DOWNLOAD


@dataclass(kw_only=True)
class EpisodeActionDelete(EpisodeAction):
    """EpisodeActionDelete."""

    action: EpisodeActionType = EpisodeActionType.DELETE


@dataclass(kw_only=True)
class EpisodeActionNew(EpisodeAction):
    """EpisodeActionNew."""

    action: EpisodeActionType = EpisodeActionType.NEW


@dataclass(kw_only=True)
class EpisodeActionFlattr(EpisodeAction):
    """EpisodeActionFlattr."""

    action: EpisodeActionType = EpisodeActionType.FLATTR


@dataclass(kw_only=True)
class EpisodeActionPlay(EpisodeAction):
    """EpisodeActionPlay."""

    action: EpisodeActionType = EpisodeActionType.PLAY

    # all in seconds
    started: int = 0
    position: int = 0
    total: int = 0


@dataclass(kw_only=True)
class EpisodeActionGet(DataClassJSONMixin):
    """EpisodeActionGet."""

    actions: list[EpisodeAction]
    timestamp: int


class GPodderClient:
    """GPodderClient."""

    def __init__(
        self, session: aiohttp.ClientSession, logger: logging.Logger, verify_ssl: bool = True
    ) -> None:
        """Init for GPodderClient."""
        self.session = session
        self.verify_ssl = verify_ssl

        self.is_nextcloud = False
        self.base_url: str
        self.token: str | None

        self.username: str
        self.device: str
        self.auth: aiohttp.BasicAuth | None = None  # only for gpodder

        self.logger = logger

        self._nextcloud_prefix = "index.php/apps/gpoddersync/"

    def init_nc(self, base_url: str, nc_token: str | None = None) -> None:
        """Init values for a nextcloud client."""
        self.is_nextcloud = True
        self.token = nc_token
        self.base_url = base_url.rstrip("/")

    async def init_gpodder(self, username: str, password: str, device: str, base_url: str) -> None:
        """Init via basic auth."""
        self.username = username
        self.device = device
        self.base_url = base_url.rstrip("/")
        self.auth = aiohttp.BasicAuth(username, password)
        await self._post(endpoint=f"api/2/auth/{username}/login.json")

    @property
    def headers(self) -> dict[str, str]:
        """Session headers."""
        if self.token is None:
            raise RuntimeError("Token not set.")
        return {"Authorization": f"Bearer {self.token}"}

    async def _post(
        self,
        endpoint: str,
        data: dict[str, Any] | list[Any] | None = None,
    ) -> bytes:
        """POST request."""
        try:
            response = await self.session.post(
                f"{self.base_url}/{endpoint}",
                json=data,
                ssl=self.verify_ssl,
                headers=self.headers if self.is_nextcloud else None,
                raise_for_status=True,
                auth=self.auth,
            )
        except ClientResponseError as exc:
            raise RuntimeError(f"API POST call to {endpoint} failed.") from exc
        if response.status != 200:
            raise RuntimeError(f"Api post call failed to {endpoint} failed!")
        return await response.read()

    async def _get(self, endpoint: str, params: dict[str, str | int] | None = None) -> bytes:
        """GET request."""
        response = await self.session.get(
            f"{self.base_url}/{endpoint}",
            params=params,
            ssl=self.verify_ssl,
            headers=self.headers if self.is_nextcloud else None,
            auth=self.auth,
        )
        status = response.status
        if response.content_type == "application/json" and status == 200:
            return await response.read()
        if status == 404:
            return b""
        raise RuntimeError(f"API GET call to {endpoint} failed.")

    async def get_subscriptions(self, since: int = 0) -> SubscriptionsGet | None:
        """Get subscriptions.

        since is unix time epoch - this may return none if there are no
        subscriptions.
        """
        if self.is_nextcloud:
            endpoint = f"{self._nextcloud_prefix}/subscriptions"
        else:
            endpoint = f"api/2/subscriptions/{self.username}/{self.device}.json"

        response = await self._get(endpoint, params={"since": since})
        if not response:
            return None
        return SubscriptionsGet.from_json(response)

    async def get_progresses(
        self, podcast_id: str | None = None, since: int = 0
    ) -> list[EpisodeActionPlay]:
        """Get progresses.

        gpodder net may filter by podcast
        https://gpoddernet.readthedocs.io/en/latest/api/reference/events.html
        """
        params: dict[str, str | int] = {"since": since}
        if self.is_nextcloud:
            endpoint = f"{self._nextcloud_prefix}/episode_action"
        else:
            endpoint = f"api/2/episodes/{self.username}.json"
            params["device"] = self.device
        response = await self._get(endpoint, params=params)
        if not response:
            return []
        actions = EpisodeActionGet.from_json(response).actions

        # play has progress information

        if podcast_id is None:
            actions = [x for x in actions if isinstance(x, EpisodeActionPlay)]
        else:
            actions = [
                x for x in actions if isinstance(x, EpisodeActionPlay) and x.podcast == podcast_id
            ]

        with suppress(ValueError):
            actions = sorted(actions, key=lambda x: datetime.datetime.fromisoformat(x.timestamp))[
                ::-1
            ]

        return actions

    async def update_subscriptions(
        self, add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        """Update subscriptions."""
        if add is None:
            add = []
        if remove is None:
            remove = []
        request = SubscriptionsChangeRequest(add=add, remove=remove)
        if self.is_nextcloud:
            endpoint = "{self._nextcloud_prefix}/subscription_change/create"
        else:
            endpoint = f"api/2/subscriptions/{self.username}/{self.device}.json"

        await self._post(endpoint=endpoint, data=request.to_dict())

    async def update_progress(
        self,
        *,
        podcast_id: str,
        episode_id: str,
        guid: str | None,
        position_s: float,
        duration_s: float,
    ) -> None:
        """Update progress."""
        timestamp = datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
        if position_s == 0:
            # mark unplayed
            episode_action = EpisodeActionNew(
                podcast=podcast_id, episode=episode_id, timestamp=timestamp
            )
            if self.is_nextcloud:
                episode_action.guid = guid
        else:
            episode_action = EpisodeActionPlay(
                podcast=podcast_id,
                episode=episode_id,
                timestamp=timestamp,
                position=int(position_s),
                started=0,
                total=int(duration_s),
            )
        if self.is_nextcloud:
            episode_action.guid = guid
            endpoint = f"{self._nextcloud_prefix}/episode_action/create"
        else:
            endpoint = f"api/2/episodes/{self.username}.json"
        await self._post(endpoint=endpoint, data=[episode_action.to_dict()])
