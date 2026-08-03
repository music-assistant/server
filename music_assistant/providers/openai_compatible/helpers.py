"""Helpers to talk to an OpenAI-compatible API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import ClientTimeout
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    ProviderUnavailableError,
    RateLimited,
)

from music_assistant.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

# a reasoning model may narrate its thinking inline ahead of the answer, while the
# consumers want the answer alone - some of them parse it as strict JSON
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)

# an upstream failure message is echoed into the raised error, so keep it bounded
MAX_ERROR_DETAIL_CHARS = 200


async def list_models(mass: MusicAssistant, base_url: str, api_key: str, timeout: int) -> list[str]:
    """
    Return the model ids the endpoint advertises, sorted.

    Returns an empty list when the endpoint has no usable listing: it is optional in
    the OpenAI API and several compatible servers do not implement it. Bad credentials
    and an unreachable host are raised instead, so a caller can tell them apart.

    :param mass: The Music Assistant instance, for its shared HTTP session.
    :param base_url: Base URL of the API, without a trailing slash.
    :param api_key: API key to authenticate with, empty for an endpoint without auth.
    :param timeout: Maximum request duration in seconds.
    """
    try:
        payload = await _request(mass, "get", f"{base_url}/models", api_key, timeout=timeout)
    except InvalidDataError:
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    models = {
        model["id"].strip()
        for model in data
        if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"].strip()
    }
    return sorted(models)


async def chat_completion(
    mass: MusicAssistant,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int,
) -> str:
    """
    Return the reply of a single-turn chat completion.

    :param mass: The Music Assistant instance, for its shared HTTP session.
    :param base_url: Base URL of the API, without a trailing slash.
    :param api_key: API key to authenticate with, empty for an endpoint without auth.
    :param model: Id of the model to answer the prompt.
    :param prompt: The prompt to send.
    :param timeout: Maximum request duration in seconds.
    """
    payload = await _request(
        mass,
        "post",
        f"{base_url}/chat/completions",
        api_key,
        timeout=timeout,
        # deliberately no token limit: reasoning models reject max_tokens outright and
        # a truncated answer is worthless to every consumer anyway
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
    )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        msg = f"Model {model} returned no choices"
        raise InvalidDataError(msg)
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        msg = f"Model {model} returned no message content"
        raise InvalidDataError(msg)
    if not (answer := THINK_BLOCK.sub("", content).strip()):
        msg = f"Model {model} returned an empty answer"
        raise InvalidDataError(msg)
    return answer


async def _request(
    mass: MusicAssistant,
    method: str,
    url: str,
    api_key: str,
    *,
    timeout: int,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform one API request and return its parsed JSON object."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with mass.http_session.request(
            method, url, headers=headers, json=json, timeout=ClientTimeout(total=timeout)
        ) as response:
            status = response.status
            retry_after = response.headers.get("Retry-After", "")
            body = await response.read()
    except TimeoutError as err:
        msg = f"Request to {url} timed out after {timeout} seconds"
        raise ProviderUnavailableError(msg) from err
    except aiohttp.ClientError as err:
        msg = f"Could not reach {url}: {err}"
        raise ProviderUnavailableError(msg) from err
    if status in (401, 403):
        msg = f"The endpoint rejected the API key: {_error_detail(body)}"
        raise LoginFailed(msg)
    if status == 429:
        msg = f"The endpoint is rate limiting us: {_error_detail(body)}"
        raise RateLimited(msg, backoff_time=int(retry_after) if retry_after.isdigit() else 0)
    if status >= 400:
        msg = f"The endpoint returned an error ({status}): {_error_detail(body)}"
        raise InvalidDataError(msg)
    try:
        payload = json_loads(body)
    except JSON_DECODE_EXCEPTIONS as err:
        msg = f"The endpoint returned a malformed response: {_error_detail(body)}"
        raise InvalidDataError(msg) from err
    if not isinstance(payload, dict):
        msg = "The endpoint returned an unexpected response"
        raise InvalidDataError(msg)
    return payload


def _error_detail(body: bytes) -> str:
    """Return a short, readable description of a failed request's response body."""
    text = body.decode(errors="ignore").strip()
    try:
        payload = json_loads(body)
    except JSON_DECODE_EXCEPTIONS:
        payload = None
    if isinstance(payload, dict) and isinstance(error := payload.get("error"), dict):
        if isinstance(message := error.get("message"), str) and message.strip():
            text = message.strip()
    return text[:MAX_ERROR_DETAIL_CHARS] or "no details provided"
