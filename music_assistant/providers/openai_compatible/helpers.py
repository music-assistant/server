"""Helpers to talk to an OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import socket
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

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

# the inline image in a chat completion reply, e.g. "data:image/png;base64,iVBORw0..."
DATA_URL = re.compile(r"^data:image/[\w.+-]+;base64,(?P<data>.+)$", re.DOTALL)

# OpenAI's own image endpoint takes an explicit size; square suits every current consumer
IMAGE_SIZE = "1024x1024"

# a generated image handed out by URL is downloaded whole, so cap what is read
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_READ_CHUNK_SIZE = 64 * 1024
# redirects of an image link are followed one by one, so every hop can be checked
MAX_IMAGE_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# an upstream failure message is echoed into the raised error, so keep it bounded
MAX_ERROR_DETAIL_CHARS = 200


async def list_models(mass: MusicAssistant, base_url: str, api_key: str, timeout: int) -> list[str]:
    """
    Return the model ids the endpoint advertises, sorted.

    Raises when the endpoint cannot be asked at all, so that a wrong address is caught
    rather than mistaken for a service that simply has nothing to offer. An endpoint
    that answers without a usable list yields an empty list.

    :param mass: The Music Assistant instance, for its shared HTTP session.
    :param base_url: Base URL of the API, without a trailing slash.
    :param api_key: API key to authenticate with, empty for an endpoint without auth.
    :param timeout: Maximum request duration in seconds.
    """
    payload = await _request(mass, "get", f"{base_url}/models", api_key, timeout=timeout)
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


async def image_generation(
    mass: MusicAssistant,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int,
    *,
    use_images_endpoint: bool = False,
) -> tuple[bytes, bool]:
    """
    Return a generated image, along with whether the images endpoint produced it.

    Services disagree on where image generation lives: OpenRouter (and the services modelled
    on it) return the image inline in a chat completion, OpenAI and most others expose it on
    a dedicated images endpoint. The chat completion is tried first, falling back to the
    images endpoint when the service rejects the request or replies without an image.

    :param mass: The Music Assistant instance, for its shared HTTP session.
    :param base_url: Base URL of the API, without a trailing slash.
    :param api_key: API key to authenticate with, empty for an endpoint without auth.
    :param model: Id of the model to generate the image with.
    :param prompt: The description of the image to generate.
    :param timeout: Maximum request duration in seconds.
    :param use_images_endpoint: Go straight to the images endpoint, for a model that is
        already known to need it.
    """
    if not use_images_endpoint:
        try:
            return await _chat_completion_image(
                mass, base_url, api_key, model, prompt, timeout
            ), False
        except InvalidDataError as chat_err:
            try:
                return await _images_endpoint_image(
                    mass, base_url, api_key, model, prompt, timeout
                ), True
            except InvalidDataError as images_err:
                msg = (
                    f"Model {model} returned no image: "
                    f"chat completion: {chat_err}; images endpoint: {images_err}"
                )
                raise InvalidDataError(msg) from images_err
    return await _images_endpoint_image(mass, base_url, api_key, model, prompt, timeout), True


async def _chat_completion_image(
    mass: MusicAssistant, base_url: str, api_key: str, model: str, prompt: str, timeout: int
) -> bytes:
    """Return the first image of a chat completion asking for image output."""
    payload = await _request(
        mass,
        "post",
        f"{base_url}/chat/completions",
        api_key,
        timeout=timeout,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
        },
    )
    choices = payload.get("choices")
    message = (
        choices[0].get("message")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict)
        else None
    )
    images = message.get("images") if isinstance(message, dict) else None
    for image in images if isinstance(images, list) else []:
        image_url = image.get("image_url") if isinstance(image, dict) else None
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if isinstance(url, str) and (match := DATA_URL.match(url.strip())):
            return _decode_base64(match["data"])
    msg = "the reply carried no image"
    raise InvalidDataError(msg)


async def _images_endpoint_image(
    mass: MusicAssistant, base_url: str, api_key: str, model: str, prompt: str, timeout: int
) -> bytes:
    """Return the first image of an OpenAI style image generation request."""
    payload = await _request(
        mass,
        "post",
        f"{base_url}/images/generations",
        api_key,
        timeout=timeout,
        json={"model": model, "prompt": prompt, "n": 1, "size": IMAGE_SIZE},
    )
    data = payload.get("data")
    image = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    if isinstance(b64_json := image.get("b64_json"), str) and b64_json:
        return _decode_base64(b64_json)
    if isinstance(url := image.get("url"), str) and url.startswith(("http://", "https://")):
        return await _download(mass, base_url, url, timeout)
    msg = "the reply carried no image"
    raise InvalidDataError(msg)


async def _download(mass: MusicAssistant, base_url: str, url: str, timeout: int) -> bytes:
    """
    Download an image the service handed out by URL.

    :param mass: The Music Assistant instance, for its HTTP session.
    :param base_url: The base URL of the configured endpoint.
    :param url: The image link from the service's reply.
    :param timeout: The timeout of each request, in seconds.
    """
    for _ in range(MAX_IMAGE_REDIRECTS + 1):
        await _check_image_link(base_url, url)
        try:
            async with mass.http_session.get(
                url, timeout=ClientTimeout(total=timeout), allow_redirects=False
            ) as response:
                status = response.status
                location = response.headers.get("Location", "")
                body = b"" if status in REDIRECT_STATUSES else await _read_image(response)
        except (TimeoutError, aiohttp.ClientError) as err:
            msg = f"Could not download the generated image: {err}"
            raise ProviderUnavailableError(msg) from err
        if status in REDIRECT_STATUSES and location:
            url = urljoin(url, location)
            continue
        if status >= 400 or not body:
            msg = f"Could not download the generated image ({status})"
            raise InvalidDataError(msg)
        return body
    msg = "The generated image link redirects too often"
    raise InvalidDataError(msg)


async def _check_image_link(base_url: str, url: str) -> None:
    """
    Refuse an image link that reaches a local network, unless it is on the configured endpoint.

    The service picks the link, so it must not make Music Assistant request internal addresses.
    A local service (such as LocalAI) hands out links to itself, so its own origin is allowed.

    :param base_url: The base URL of the configured endpoint.
    :param url: The image link to check.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        msg = "the reply carried an invalid image link"
        raise InvalidDataError(msg)
    base = urlsplit(base_url)
    if (parts.scheme, parts.netloc.lower()) == (base.scheme, base.netloc.lower()):
        return
    addresses = await _host_addresses(parts.hostname, parts.port or 0)
    # a link-local IPv6 address carries its interface after a "%"
    if not addresses or not all(
        ip_address(address.split("%", 1)[0]).is_global for address in addresses
    ):
        msg = f"the reply carried an image link on a local network: {parts.hostname}"
        raise InvalidDataError(msg)


async def _host_addresses(host: str, port: int) -> list[str]:
    """Return the IP addresses a host name resolves to, none when it does not resolve."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


async def _read_image(response: aiohttp.ClientResponse) -> bytes:
    """Return the body of a downloaded image, refusing one over the size limit."""
    too_large = "The generated image is larger than the download limit"
    if (response.content_length or 0) > MAX_IMAGE_BYTES:
        raise InvalidDataError(too_large)
    # the declared length can be absent or wrong, so cap what is actually read
    body = bytearray()
    while chunk := await response.content.read(IMAGE_READ_CHUNK_SIZE):
        body.extend(chunk)
        if len(body) > MAX_IMAGE_BYTES:
            raise InvalidDataError(too_large)
    return bytes(body)


def _decode_base64(data: str) -> bytes:
    """Decode a base64 image payload."""
    try:
        return base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError) as err:
        msg = "the reply carried an image that could not be decoded"
        raise InvalidDataError(msg) from err


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
