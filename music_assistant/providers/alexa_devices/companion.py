"""
Client for the Music Assistant companion Alexa skill API.

Amazon offers no API to play an arbitrary stream URL on an Echo device. The companion
skill closes that gap: Music Assistant pushes the stream URL (plus metadata) to the
skill's API, then asks the device by voice command to open the skill, which plays it.
This is the same contract the ``alexa`` provider uses, so both work with one skill.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.errors import ActionUnavailable

if TYPE_CHECKING:
    from logging import Logger


def parse_json_object(text: str) -> dict[str, Any]:
    """Return text parsed as a JSON object, or an empty dict if it isn't one."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class CompanionSkillApi:
    """Talk to the companion skill's API (push stream urls, control its screen, intents)."""

    def __init__(
        self,
        http_session: aiohttp.ClientSession,
        api_url: str,
        username: str | None,
        password: str | None,
        logger: Logger,
    ) -> None:
        """
        Initialize the companion skill API client.

        :param http_session: The (shared) aiohttp session to use.
        :param api_url: Base URL of the companion skill API.
        :param username: Optional basic auth username.
        :param password: Optional basic auth password.
        :param logger: Logger of the owning provider.
        """
        self._http_session = http_session
        self._api_url = api_url.rstrip("/")
        self._headers = (
            {"Authorization": aiohttp.encode_basic_auth(username, password)}
            if username and password
            else None
        )
        self._logger = logger
        self._intents: list[dict[str, Any]] | None = None
        self._invocation_name: str | None = None
        self._intents_lock = asyncio.Lock()

    async def request(
        self,
        endpoint: str,
        method: str = "POST",
        json_data: dict[str, Any] | None = None,
        timeout: int = 10,
    ) -> str:
        """
        Send a request to the companion skill API and return the response text.

        :param endpoint: Endpoint path, relative to the API url.
        :param method: HTTP method; GET sends json_data as query params.
        :param json_data: Optional JSON payload (or query params for GET).
        :param timeout: Timeout in seconds for the request.
        """
        url = f"{self._api_url}/{endpoint.lstrip('/')}"
        is_get = method.upper() == "GET"
        async with self._http_session.request(
            method.upper(),
            url,
            params=json_data if is_get else None,
            json=None if is_get else json_data,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers=self._headers,
        ) as resp:
            resp_text = await resp.text()
            if not 200 <= resp.status < 300:
                msg = f"Failed API request to {url}: Status code: {resp.status}, Response: {resp_text}"
                self._logger.error(msg)
                raise ActionUnavailable(msg)
            return resp_text

    async def push_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Push a stream URL and its metadata to the skill.

        :param payload: streamUrl/title/artist/album/imageUrl, optionally playerId and
            canSkipSpeech.
        """
        return parse_json_object(await self.request("/ma/push-url", json_data=payload))

    async def control(self, player_id: str, command: str) -> dict[str, Any]:
        """
        Send a pause/resume command to the skill's open screen.

        :param player_id: The player the command is meant for.
        :param command: "pause" or "resume".
        """
        return parse_json_object(
            await self.request(
                "/ma/control",
                json_data={"playerId": player_id, "command": command, "canSkipSpeech": True},
                timeout=5,
            )
        )

    async def get_intent_utterance(self, intent_name: str, default: str) -> str:
        """
        Return the voice command for the skill intent (cached), or the default.

        :param intent_name: The Alexa intent, e.g. AMAZON.PauseIntent.
        :param default: Command to use when the skill doesn't define one.
        """
        if self._intents is None:
            try:
                await self.load_intents()
            except (ActionUnavailable, aiohttp.ClientError, TimeoutError) as err:
                # the default command still works, retry loading on the next command
                self._logger.debug("Unable to load skill intents: %s", err)
                return default
        for intent in self._intents or []:
            if intent.get("intent") != intent_name:
                continue
            if utterances := cast("list[str]", intent.get("utterances") or []):
                if self._invocation_name:
                    return f"{self._invocation_name.strip()} {utterances[0]}".strip()
                return utterances[0]
        return default

    async def load_intents(self) -> None:
        """Load and cache the skill's intents and invocation name."""
        async with self._intents_lock:
            if self._intents is not None:
                return
            data = parse_json_object(await self.request("/alexa/intents", "GET", timeout=5))
            self._invocation_name = data.get("invocationName")
            self._intents = data.get("intents") or []
