"""SessionConfiguration."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp.client import DEFAULT_TIMEOUT, ClientSession

if TYPE_CHECKING:
    from aiohttp import ClientTimeout


@dataclass(kw_only=True)
class SessionConfiguration:
    """Session configuration for a speaker client."""

    session: ClientSession
    ip: str
    http_port: int = 8090
    timeout: ClientTimeout = DEFAULT_TIMEOUT
    logger: logging.Logger | None = None
