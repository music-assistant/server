"""Pairing profiles for the fake Sendspin devices, one per scenario worth testing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiosendspin.noise.pin import DEFAULT_MIN_PIN_DIGITS


class PinChannel(StrEnum):
    """Out-channel a device uses to convey a derived dynamic PIN to the operator."""

    NONE = "none"
    DISPLAY = "display"
    SPEAKER = "speaker"
    BOTH = "both"

    @property
    def has_display(self) -> bool:
        """Whether this channel shows the PIN on a screen."""
        return self in (PinChannel.DISPLAY, PinChannel.BOTH)

    @property
    def has_speaker(self) -> bool:
        """Whether this channel speaks the PIN out loud."""
        return self in (PinChannel.SPEAKER, PinChannel.BOTH)


@dataclass(frozen=True, kw_only=True, slots=True)
class Scenario:
    """
    One fake device's pairing profile.

    :param scenario_id: Stable id; seeds the device identity, so the Music Assistant
        player config survives a restart.
    :param name: Friendly name the device reports to the server.
    :param product_name: Model name shown on the player's device info.
    :param description: What this device is meant to demonstrate, shown in the settings.
    :param pairing_psk: Offer the token method, as every real speaker does. Setup only
        surfaces it for a device with no PIN method of its own.
    :param static_pin: Offer the fixed PIN. Always gesture-gated by the spec.
    :param dynamic_pin: Offer a per-attempt derived PIN.
    :param unpaired_access: Admit a server without pairing. The device is then approved on
        connect and needs no setup at all, unless it also has an audio input.
    :param min_pin_length: Shortest dynamic PIN the device accepts. The server negotiates
        ``max(this, its own minimum)``, and anything under 6 digits is gesture-gated.
    :param pin_channel: How the derived dynamic PIN reaches the operator.
    :param secret_locations: Where the operator finds a static secret ("device", "leaflet"
        or "operator"), which picks the instruction text shown during setup.
    :param source_role: Also act as an audio input, which adds the line-in decision step.
    """

    scenario_id: str
    name: str
    product_name: str
    description: str
    pairing_psk: bool = False
    static_pin: bool = False
    dynamic_pin: bool = False
    unpaired_access: bool = False
    min_pin_length: int = DEFAULT_MIN_PIN_DIGITS
    pin_channel: PinChannel = PinChannel.NONE
    secret_locations: tuple[str, ...] = ()
    source_role: bool = False

    @property
    def offers_pairing(self) -> bool:
        """Whether the device offers any pairing method at all."""
        return self.pairing_psk or self.static_pin or self.dynamic_pin

    @property
    def gesture_gated(self) -> bool:
        """Whether a first pairing needs the device's pairing button pressed."""
        return self.static_pin or (self.dynamic_pin and self.min_pin_length < 6)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        scenario_id="open",
        name="Demo Open Speaker",
        product_name="Open Speaker",
        description="Guest access only. No pairing method offered, so setup is a single consent step.",
        unpaired_access=True,
    ),
    Scenario(
        scenario_id="guest_or_pair",
        name="Demo Guest Speaker",
        product_name="Guest Speaker",
        description="Guest access, with a dynamic PIN offered as the optional secure alternative.",
        unpaired_access=True,
        dynamic_pin=True,
        pin_channel=PinChannel.DISPLAY,
        pairing_psk=True,
    ),
    Scenario(
        scenario_id="dynamic_pin",
        name="Demo PIN Speaker",
        product_name="PIN Speaker",
        description="Dynamic PIN only, shown on a display. Six digits, no button press needed.",
        dynamic_pin=True,
        pin_channel=PinChannel.DISPLAY,
        pairing_psk=True,
    ),
    Scenario(
        scenario_id="dynamic_pin_spoken",
        name="Demo Spoken PIN Speaker",
        product_name="Spoken PIN Speaker",
        description="Dynamic PIN only, spoken out loud instead of displayed (a device with no screen).",
        dynamic_pin=True,
        pin_channel=PinChannel.SPEAKER,
        pairing_psk=True,
    ),
    Scenario(
        scenario_id="dynamic_pin_long",
        name="Demo Long PIN Speaker",
        product_name="Long PIN Speaker",
        description="Dynamic PIN of eight digits, which the entry field renders as two groups of four.",
        dynamic_pin=True,
        pin_channel=PinChannel.DISPLAY,
        min_pin_length=8,
        pairing_psk=True,
    ),
    Scenario(
        scenario_id="dynamic_pin_short",
        name="Demo Short PIN Speaker",
        product_name="Short PIN Speaker",
        description="Four-digit dynamic PIN. Short PINs are gesture-gated, so press the button first.",
        dynamic_pin=True,
        pin_channel=PinChannel.DISPLAY,
        min_pin_length=4,
        pairing_psk=True,
    ),
    Scenario(
        scenario_id="static_pin",
        name="Demo Static PIN Speaker",
        product_name="Static PIN Speaker",
        description="Fixed eight-digit PIN printed on the device. Always needs the button pressed first.",
        static_pin=True,
        secret_locations=("device",),
        pairing_psk=True,
    ),
    Scenario(
        scenario_id="both_pins",
        name="Demo Dual PIN Speaker",
        product_name="Dual PIN Speaker",
        description="Static and dynamic PIN both offered, so setup first asks which one to use.",
        static_pin=True,
        dynamic_pin=True,
        pin_channel=PinChannel.DISPLAY,
        secret_locations=("leaflet",),
        pairing_psk=True,
    ),
    Scenario(
        scenario_id="token",
        name="Demo Token Speaker",
        product_name="Token Speaker",
        description=(
            "No PIN support, so setup falls back to the pairing token printed on the "
            "device. Copy the token below into setup."
        ),
        pairing_psk=True,
        secret_locations=("device",),
    ),
    Scenario(
        scenario_id="token_operator",
        name="Demo Managed Speaker",
        product_name="Managed Speaker",
        description=(
            "No PIN support either, with its token handed out by whoever administers "
            "the device rather than printed on it."
        ),
        pairing_psk=True,
        secret_locations=("operator",),
    ),
    Scenario(
        scenario_id="locked",
        name="Demo Locked Speaker",
        product_name="Locked Speaker",
        description="Nothing on offer: no guest access and no pairing method. Setup can only abort.",
    ),
    Scenario(
        scenario_id="everything",
        name="Demo Everything Speaker",
        product_name="Everything Speaker",
        description="Guest access plus every pairing method, with the PIN on both out-channels.",
        unpaired_access=True,
        pairing_psk=True,
        static_pin=True,
        dynamic_pin=True,
        pin_channel=PinChannel.BOTH,
        secret_locations=("device", "leaflet"),
    ),
    Scenario(
        scenario_id="line_in",
        name="Demo Line-In Speaker",
        product_name="Line-In Speaker",
        description="Guest access plus an audio input, which adds the line-in decision to setup.",
        unpaired_access=True,
        dynamic_pin=True,
        pin_channel=PinChannel.DISPLAY,
        source_role=True,
        pairing_psk=True,
    ),
)

SCENARIOS_BY_ID: dict[str, Scenario] = {scenario.scenario_id: scenario for scenario in SCENARIOS}
