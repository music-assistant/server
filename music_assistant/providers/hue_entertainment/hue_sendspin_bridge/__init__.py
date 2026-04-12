"""
Reusable Hue Entertainment bridge library.

Provides the core building blocks for syncing Hue lights to audio:
DTLS streaming, HueStream protocol, REST API, and audio-to-color analysis.

This package is intentionally decoupled from Music Assistant specifics so
it can later be extracted into a standalone library (e.g. on the sendspin org).
"""

from .analyzer import HueAudioAnalyzer
from .api import HueEntertainmentAPI
from .dtls import HueDtlsStreamer
from .models import EntertainmentArea, LightChannel, LightColorCommand

__all__ = [
    "EntertainmentArea",
    "HueAudioAnalyzer",
    "HueDtlsStreamer",
    "HueEntertainmentAPI",
    "LightChannel",
    "LightColorCommand",
]
