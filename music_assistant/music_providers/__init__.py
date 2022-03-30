"""Package with Music Providers (and connected logic and models)."""

from .model import MusicProvider  # noqa
from .spotify import SpotifyProvider  # noqa
from .filesystem import FileProvider  # noqa
from .qobuz import QobuzProvider  # noqa
from .tunein import TuneInProvider  # noqa
