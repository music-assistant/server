"""Models specific to state synchronization."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WamSpeakerAttributes:
    """A type-safe, immutable snapshot of the speaker's raw attributes."""

    mac: str
    name: str
    model: str | None
    software_version: str | None
    source: str | None
    source_list: list[str]
    group_leader_mac: str | None
    is_group_child: bool
    volume: int | None
    muted: bool | None
    play_status: str | None
