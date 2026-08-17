"""SessionConfiguration."""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import ClientTimeout

if TYPE_CHECKING:
    from aiohttp.client import ClientSession


@dataclass(kw_only=True)
class SessionConfiguration:
    """Session configuration for a speaker client."""

    session: ClientSession
    ip: str
    http_port: int = 8090
    timeout: ClientTimeout = field(default_factory=lambda: ClientTimeout(total=10))
    logger: logging.Logger | None = None
