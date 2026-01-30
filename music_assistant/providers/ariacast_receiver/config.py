"""Configuration classes for AriaCast Server."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AudioConfig:
    """Audio stream configuration parameters."""

    SAMPLE_RATE: int = 48000
    CHANNELS: int = 2
    SAMPLE_WIDTH: int = 2  # 16-bit = 2 bytes
    FRAME_DURATION_MS: int = 20
    FRAME_SIZE: int = 3840  # 48000 * 2 channels * 2 bytes * 0.020s


@dataclass
class ServerConfig:
    """Server configuration parameters."""

    SERVER_NAME: str = "AriaCast Speaker"
    VERSION: str = "1.0"
    PLATFORM: str = "MusicAssistant"
    CODECS: list[str] | None = None
    DISCOVERY_PORT: int = 12888
    STREAMING_PORT: int = 12889
    HOST: str = "0.0.0.0"
    AUDIO: AudioConfig | None = None

    def __post_init__(self):
        """Initialize default values after instantiation."""
        if self.CODECS is None:
            self.CODECS = ["PCM"]
        if self.AUDIO is None:
            self.AUDIO = AudioConfig()
