"""Client library for Bose Soundtouch speakers."""

__version__ = "0.1.0"

from .client import SoundtouchDevice
from .client.session_configuration import SessionConfiguration

__all__ = ["SessionConfiguration", "SoundtouchDevice"]
