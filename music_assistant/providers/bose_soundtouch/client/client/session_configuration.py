"""SessionConfiguration."""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import ClientTimeout

from music_assistant.providers.bose_soundtouch.client.const import HTTP_PORT, REQUEST_TIMEOUT

if TYPE_CHECKING:
    from aiohttp.client import ClientSession


@dataclass(kw_only=True)
class SessionConfiguration:
    """Session configuration for a speaker client."""

    session: ClientSession
    ip: str
    http_port: int = HTTP_PORT
    timeout: ClientTimeout = field(default_factory=lambda: ClientTimeout(total=REQUEST_TIMEOUT))
    logger: logging.Logger | None = None
