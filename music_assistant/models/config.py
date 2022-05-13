"""Model for the Music Assisant runtime config."""

from dataclasses import dataclass, field
from typing import List, Optional

from databases import DatabaseURL

from music_assistant.helpers.util import get_ip, select_stream_port
from music_assistant.models.enums import ProviderType


@dataclass(frozen=True)
class MusicProviderConfig:
    """Base Model for a MusicProvider config."""

    type: ProviderType
    enabled: bool = True
    username: Optional[str] = None
    password: Optional[str] = None
    path: Optional[str] = None


@dataclass(frozen=True)
class MassConfig:
    """Model for the Music Assisant runtime config."""

    database_url: DatabaseURL

    providers: List[MusicProviderConfig] = field(default_factory=list)

    # advanced settings
    max_simultaneous_jobs: int = 10
    stream_port: int = select_stream_port()
    stream_ip: str = get_ip()
