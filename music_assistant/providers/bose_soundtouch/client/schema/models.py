"""Models for Bose Soundtouch."""

from dataclasses import dataclass, field
from typing import Annotated

from mashumaro.mixins.json import DataClassJSONMixin
from mashumaro.types import Alias

from music_assistant.providers.bose_soundtouch.client.schema.enums import (
    ArtStatus,
    PlayStatus,
    SourceStatus,
)


@dataclass
class Source(DataClassJSONMixin):
    """Source."""

    source: str | None = None
    # source account is unique, but not always present
    source_account: Annotated[str | None, Alias("sourceAccount")] = None
    source_name: str | None = None
    status: SourceStatus | None = None
    is_local: Annotated[bool | None, Alias("isLocal")] = None
    multiroom_allowed: Annotated[bool | None, Alias("multiroomallowed")] = None


@dataclass
class Sources(DataClassJSONMixin):
    """Sources."""

    sources: list[Source] = field(default_factory=list)


@dataclass
class BassCapabilities(DataClassJSONMixin):
    """BassCapabilities."""

    available: Annotated[bool | None, Alias("bassAvailable")] = None
    minimum: Annotated[int | None, Alias("bassMin")] = None
    maximum: Annotated[int | None, Alias("bassMax")] = None
    default: Annotated[int | None, Alias("bassDefault")] = None


@dataclass
class Bass(DataClassJSONMixin):
    """Bass."""

    target_bass: Annotated[int | None, Alias("targetbass")] = None
    actual_bass: Annotated[int | None, Alias("actualbass")] = None


@dataclass
class ZoneMember(DataClassJSONMixin):
    """ZoneMember."""

    ip: Annotated[str | None, Alias("ipaddress")] = None
    mac: str | None = None


@dataclass
class Zone(DataClassJSONMixin):
    """Zone."""

    leader: ZoneMember | None = None
    members: list[ZoneMember] = field(default_factory=list)


@dataclass
class Art(DataClassJSONMixin):
    """Art."""

    status: ArtStatus | None = None
    url: str | None = None


@dataclass
class TimeInformation(DataClassJSONMixin):
    """TimeInformation."""

    total: int | None = None
    position: int | None = None


@dataclass
class NowPlayingContentItem(DataClassJSONMixin):
    """NowPlayingContentItem."""

    source: str | None = None
    source_account: Annotated[str | None, Alias("sourceAccount")] = None
    is_presetable: Annotated[bool | None, Alias("isPresetable")] = None
    item_name: Annotated[str | None, Alias("itemName")] = None


@dataclass
class NowPlaying(DataClassJSONMixin):
    """NowPlaying."""

    track: str | None = None
    artist: str | None = None
    album: str | None = None
    station_name: Annotated[str | None, Alias("stationName")] = None
    description: str | None = None
    station_location: Annotated[str | None, Alias("stationLocation")] = None
    play_status: Annotated[PlayStatus | None, Alias("playStatus")] = None
    genre: str | None = None
    art: Art | None = None
    time_information: TimeInformation | None = None
    content_item: NowPlayingContentItem | None = None


@dataclass
class Volume(DataClassJSONMixin):
    """Volume."""

    target_volume: Annotated[int | None, Alias("targetvolume")] = None
    actual_volume: Annotated[int | None, Alias("actualvolume")] = None
    mute_enabled: Annotated[bool | None, Alias("muteenabled")] = None


@dataclass
class Preset(DataClassJSONMixin):
    """Preset."""


@dataclass
class Presets(DataClassJSONMixin):
    """Presets."""

    presets: list[Preset] = field(default_factory=list)


@dataclass
class Info(DataClassJSONMixin):
    """Info."""

    device_id: str
    name: str
    model: str | None = None
    mac_addresses: set[str] | None = None
    ip_addresses: set[str] | None = None
    software_version: str | None = None
