"""Models specific to discovery."""

from dataclasses import dataclass
from enum import StrEnum


class DiscoveryEventType(StrEnum):
    """Enumeration for the type of discovery event."""

    PRESENCE = "presence"
    OFFLINE = "offline"


class DiscoverySource(StrEnum):
    """Enumeration for the source of a discovery event."""

    SSDP = "ssdp"
    MANUAL = "manual"


@dataclass
class DiscoveryInfo:
    """Holds information about a network discovery event."""

    udn: str
    ip_address: str
    event_type: DiscoveryEventType
    discovery_source: DiscoverySource


@dataclass
class ProbeResult:
    """Holds the result of a successful device probe."""

    udn: str
    model_name: str
