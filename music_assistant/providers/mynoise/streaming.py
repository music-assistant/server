"""
Custom (looping + mixing) audio stream for the myNoise provider.

myNoise machines are built from a set of short, seamlessly loopable audio
files (one per frequency band) that the website plays through the WebAudio
API with `source.loop = true`. We recreate that behaviour server-side with a
single ffmpeg process: every layer file is looped endlessly
(`-stream_loop -1`), attenuated to its band level and all layers are mixed
together into one endless PCM stream, which is handed to Music Assistant.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat

from music_assistant.helpers.process import AsyncProcess

from .constants import (
    OUTPUT_BIT_DEPTH,
    OUTPUT_CHANNELS,
    OUTPUT_SAMPLE_RATE,
    STREAM_CHUNK_SIZE,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import AsyncGenerator

    from .catalog import Machine


def pcm_output_format() -> AudioFormat:
    """Return the audio format of the mixed output stream."""
    return AudioFormat(
        content_type=ContentType.PCM_S16LE,
        sample_rate=OUTPUT_SAMPLE_RATE,
        bit_depth=OUTPUT_BIT_DEPTH,
        channels=OUTPUT_CHANNELS,
    )


def build_ffmpeg_args(
    machine: Machine,
    layer_gains: list[float],
    master_gain_db: float = 0.0,
    use_variant_b: bool = False,
) -> list[str]:
    """
    Build the ffmpeg args that loop and mix all layers of a machine.

    :param machine: the machine to stream.
    :param layer_gains: linear gain (0..1) per layer.
    :param master_gain_db: extra gain applied to the final mix.
    :param use_variant_b: use the B-variant files (the alternate takes myNoise
        uses to randomize playback) instead of the A-variants.
    """
    args: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    for index, layer in enumerate(machine.layers):
        if layer_gains[index] <= 0:
            continue
        url = layer.url_b if use_variant_b else layer.url_a
        # -stream_loop -1 loops the (seamless) file forever
        args += ["-stream_loop", "-1"]
        if urlparse(url).scheme.startswith("http"):
            # -re paces remote sources to realtime so we don't buffer the whole
            # internet. Local cached files must NOT be paced: -re drifts below
            # 1x (decode/mix overhead is not subtracted from its sleep budget
            # and the clock re-anchors every loop pass), which slowly drains
            # the player's rolling buffer and causes growing playback gaps.
            # Unpaced local files are instead paced by the downstream
            # backpressure of the radio buffer.
            args += ["-re"]
        args += ["-i", url]
    if not any(gain > 0 for gain in layer_gains):
        msg = f"No active layers to mix for {machine.name}"
        raise AudioError(msg)

    filter_parts: list[str] = []
    mix_labels: list[str] = []
    input_index = 0
    for index, _layer in enumerate(machine.layers):
        gain = layer_gains[index]
        if gain <= 0:
            continue
        filter_parts.append(f"[{input_index}:a]volume={gain:.3f}[a{index}]")
        mix_labels.append(f"[a{index}]")
        input_index += 1
    active = len(mix_labels)
    if active == 1:
        # single layer: no mixing needed, just normalize the label
        filter_parts.append(f"{mix_labels[0]}anull[mix]")
    else:
        # normalize=0 keeps the myNoise band mix as designed by the author
        filter_parts.append(
            "".join(mix_labels) + f"amix=inputs={active}:normalize=0:duration=longest[mix]"
        )
    filter_parts.append(
        f"[mix]volume={master_gain_db:.1f}dB,"
        f"aformat=sample_fmts=s16:channel_layouts=stereo:"
        f"sample_rates={OUTPUT_SAMPLE_RATE}[out]"
    )

    args += [
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[out]",
        "-f",
        "s16le",
        "-",
    ]
    return args


async def stream_machine(
    machine: Machine,
    layer_gains: list[float],
    master_gain_db: float = 0.0,
    use_variant_b: bool = False,
    logger: logging.Logger | None = None,
) -> AsyncGenerator[bytes]:
    """Yield raw s16le stereo PCM for an endlessly looping machine mix."""
    attempt = 0
    while True:
        attempt += 1
        args = build_ffmpeg_args(machine, layer_gains, master_gain_db, use_variant_b=use_variant_b)
        async with AsyncProcess(args, stdout=True, name="mynoise-stream") as proc:
            assert proc.proc is not None
            assert proc.proc.stdout is not None
            while True:
                chunk = await proc.proc.stdout.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        # process ended: this should not happen for an endless mix (the layers are
        # cached local files) unless a remote fallback source dropped. Retry once,
        # flipping to the alternate file variants for any remote fallback sources.
        if attempt >= 2:
            return
        use_variant_b = not use_variant_b
        if logger:
            logger.warning("myNoise stream for %s ended unexpectedly, retrying", machine.name)
        await asyncio.sleep(1)


def resolve_layer_gains(
    machine: Machine,
    band_offset_db: float = 0.0,
    overrides: dict[int, float] | None = None,
) -> list[float]:
    """
    Determine per-layer linear gains for playback of the given machine.

    The machine's default curve (0..1, where 0.5 is the website's unity
    position) is converted to a linear gain, optionally offset by a uniform
    dB amount and/or per-band overrides (e.g. to mute a band).
    """
    overrides = overrides or {}
    gains: list[float] = []
    for index, layer in enumerate(machine.layers):
        if index in overrides:
            gains.append(overrides[index])
            continue
        base = layer.default_level / 0.5
        gains.append(base * (10 ** (band_offset_db / 20)))
    return gains
