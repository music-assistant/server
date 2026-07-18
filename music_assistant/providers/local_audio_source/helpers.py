"""Helper functions for the Local Audio Source plugin."""

from __future__ import annotations

import re

from music_assistant_models.config_entries import ConfigValueOption

from music_assistant.helpers.process import check_output

# Matches e.g. "card 1: USB [USB Audio], device 0: USB Audio [USB Audio]"
_ARECORD_DEVICE_RE = re.compile(
    r"^card\s+(?P<card>\d+):.*device\s+(?P<dev>\d+):.*\[(?P<desc>[^\]]+)\]\s*$"
)


async def get_available_input_devices() -> list[ConfigValueOption]:
    """
    Scan for available ALSA capture devices using `arecord -l`.

    Labels are formatted as: 'hw X,Y - <last [] desc>'.
    """
    devices: list[ConfigValueOption] = []
    try:
        rc, out = await check_output("arecord", "-l")
        if rc == 0:
            for line in out.decode("utf-8", "ignore").strip().splitlines():
                match = _ARECORD_DEVICE_RE.match(line)
                if not match:
                    continue
                card, dev, desc = match["card"], match["dev"], match["desc"]
                label = f"hw {card},{dev} - {desc}"
                devices.append(ConfigValueOption(label, f"alsa:hw:{card},{dev}"))
    except OSError:
        # arecord not available on this host
        pass

    if not devices:
        devices = [ConfigValueOption("Manual Entry (alsa:hw:X,Y)", "alsa:")]
    return devices


def parse_alsa_device_string(device: str) -> str:
    """Normalize a configured device string into an ALSA device name for arecord."""
    if device.startswith("alsa:"):
        return device[5:] or "hw:1,0"
    if device in ("default", ""):
        return "hw:1,0"
    return device
