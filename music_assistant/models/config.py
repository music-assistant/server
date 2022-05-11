"""Model for the Music Assisant runtime config."""

from dataclasses import dataclass
from typing import Optional

from databases import DatabaseURL

from music_assistant.helpers.util import get_ip, select_stream_port


@dataclass(frozen=True)
class MassConfig:
    """Model for the Music Assisant runtime config."""

    database_url: DatabaseURL

    spotify_enabled: bool = False
    spotify_username: Optional[str] = None
    spotify_password: Optional[str] = None

    qobuz_enabled: bool = False
    qobuz_username: Optional[str] = None
    qobuz_password: Optional[str] = None

    tunein_enabled: bool = False
    tunein_username: Optional[str] = None

    filesystem_enabled: bool = False
    filesystem_music_dir: Optional[str] = None
    filesystem_playlists_dir: Optional[str] = None

    # advanced settings
    max_simultaneous_jobs: int = 10
    stream_port: int = select_stream_port()
    stream_ip: str = get_ip()
