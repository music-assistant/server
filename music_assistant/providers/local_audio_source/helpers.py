"""Helper functions for the Local Audio Source plugin."""

from __future__ import annotations

import asyncio

from music_assistant_models.config_entries import ConfigValueOption

from .pa_simple import enumerate_pa_sources


async def get_available_input_devices() -> list[ConfigValueOption]:
    """
    Scan for available PulseAudio/PipeWire capture sources via `pactl`.

    Runs pactl through the default executor since it shells out; called
    once per config-entries render, not on the audio hot path.
    """
    loop = asyncio.get_running_loop()
    try:
        sources = await loop.run_in_executor(None, enumerate_pa_sources)
    except (FileNotFoundError, RuntimeError):
        # pactl not installed, or the PulseAudio/PipeWire server isn't reachable
        sources = []

    options: list[ConfigValueOption] = []
    for src in sources:
        label = src["description"]
        if src["is_monitor"]:
            label = f"{label} (monitor)"
        label = f"{label} — {src['channels']}ch @ {src['sample_rate']}Hz"
        options.append(ConfigValueOption(src["name"], title=label))

    if not options:
        options = [ConfigValueOption("", title="Manual Entry (PA/PipeWire source name)")]
    return options
