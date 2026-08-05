"""Minimal ctypes wrapper around libpulse-simple for direct PA sink PCM streaming."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from typing import Any, ClassVar, Final, Self

import numpy as np

from .constants import volume_pct_to_amplitude

PA_STREAM_PLAYBACK: Final = 1

PA_SAMPLE_S16LE: Final = 3
PA_SAMPLE_S32LE: Final = 7  # verified via pa_sample_format_to_string
PA_SAMPLE_S24LE: Final = 9  # packed 3-byte LE — native format of s24le PA sinks

# Map PA sample format constant -> bit depth
_PA_FORMAT_TO_BIT_DEPTH: Final[dict[int, int]] = {
    PA_SAMPLE_S16LE: 16,
    PA_SAMPLE_S24LE: 24,
    PA_SAMPLE_S32LE: 32,
}


def _pa_sample_format(bit_depth: int) -> int:
    """Return PA sample format constant for given bit depth."""
    if bit_depth == 32:
        return PA_SAMPLE_S32LE
    if bit_depth == 24:
        # MA delivers in 32-bit containers; _apply_software_volume repacks to
        # packed 3-byte before writing, so PA sees s24le here.
        return PA_SAMPLE_S24LE
    return PA_SAMPLE_S16LE


class _PASampleSpec(ctypes.Structure):
    _fields_: ClassVar = [
        ("format", ctypes.c_int),
        ("rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint8),
    ]


class _PAChannelMap(ctypes.Structure):
    # pa_channel_map: { uint8_t channels; pa_channel_position_t map[32]; }
    _fields_: ClassVar = [
        ("channels", ctypes.c_uint8),
        ("map", ctypes.c_int * 32),
    ]


_pulse_core_lib: ctypes.CDLL | None = None


def _get_pulse_core_lib() -> ctypes.CDLL:
    """libpulse.so.0 handle for channel-map helpers (distinct from libpulse-simple)."""
    global _pulse_core_lib  # noqa: PLW0603
    if _pulse_core_lib is None:
        lib = ctypes.CDLL("libpulse.so.0")
        lib.pa_channel_map_parse.restype = ctypes.c_void_p
        lib.pa_channel_map_parse.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.pa_channel_map_valid.restype = ctypes.c_int
        lib.pa_channel_map_valid.argtypes = [ctypes.c_void_p]
        _pulse_core_lib = lib
    return _pulse_core_lib


def _build_pa_channel_map(channels: int) -> _PAChannelMap | None:
    """
    Build an explicit pa_channel_map declaring MA/FFmpeg slot order.

    Declaring the data's true slot order lets the pulse server reorder by
    position name to the sink's layout, so no client-side remapping is
    needed.

    :returns: A validated map, or None when the channel count has no entry
        in _SOURCE_CHANNEL_ORDER or parsing/validation fails — callers pass
        no map in that case (previous behavior).
    """
    # Some pulse servers (observed on PipeWire) reject a multichannel stream
    # that relies on the client's defaulted channel map with PA_ERR_INVALID
    # but accept the same spec with an explicit map. Parse the map from
    # position names via libpulse's own pa_channel_map_parse rather than
    # hardcoding enum integers, so numeric positions match the linked libpulse.
    names = _SOURCE_CHANNEL_ORDER.get(channels)
    if not names:
        return None
    cmap = _PAChannelMap()
    lib = _get_pulse_core_lib()
    if not lib.pa_channel_map_parse(ctypes.byref(cmap), ",".join(names).encode()):
        return None
    if not lib.pa_channel_map_valid(ctypes.byref(cmap)) or cmap.channels != channels:
        return None
    return cmap


def _find_pulse_server() -> str:
    """Detect the PulseAudio server socket path."""
    if server := os.environ.get("PULSE_SERVER"):
        return server
    for path in (
        "/run/audio/pulse.sock",
        "/run/pulse/native",
        "/var/run/pulse/native",
    ):
        if os.path.exists(path):
            return f"unix:{path}"
    return ""


def _get_pulse_server() -> str:
    """
    Return the PulseAudio server path, checked fresh on each call.

    Intentionally not cached — the socket may not exist at import time
    but appear later once the audio addon has fully started.
    """
    return _find_pulse_server()


def _load_lib() -> ctypes.CDLL:
    lib = ctypes.CDLL("libpulse-simple.so.0")
    lib.pa_simple_new.restype = ctypes.c_void_p
    lib.pa_simple_new.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.pa_simple_write.restype = ctypes.c_int
    lib.pa_simple_write.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
    ]
    lib.pa_simple_drain.restype = ctypes.c_int
    lib.pa_simple_drain.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.pa_simple_free.restype = None
    lib.pa_simple_free.argtypes = [ctypes.c_void_p]
    return lib


_lib: ctypes.CDLL | None = None


def _get_lib() -> ctypes.CDLL:
    global _lib  # noqa: PLW0603
    if _lib is None:
        _lib = _load_lib()
    return _lib


# --- Hardware sink volume control (full libpulse, async) ----------------------

PA_VOLUME_NORM: Final = 65536
PA_CHANNELS_MAX: Final = 32


# set_sink_volume() is called frequently (on every volume/mute change, and
# at bridge start for every player). A short timeout limits how long a
# stuck/unresponsive PA call can occupy an executor thread — under normal
# conditions PA responds in single-digit milliseconds, so 0.5s is generous
# while bounding the worst case. load_module()/unload_module() (rare,
# one-time during topology setup/teardown) keep a longer 2.0s timeout since
# we'd rather wait than have sink creation/cleanup spuriously fail.
_SET_VOLUME_TIMEOUT: Final = 0.5

PA_CONTEXT_READY: Final = 4
PA_CONTEXT_FAILED: Final = 5
PA_CONTEXT_TERMINATED: Final = 6
PA_CONTEXT_NOAUTOSPAWN: Final = 1

_CONTEXT_NOTIFY_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
_CONTEXT_SUCCESS_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
_CONTEXT_INDEX_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p)

PA_INVALID_INDEX: Final = 0xFFFFFFFF


class _PACVolume(ctypes.Structure):
    _fields_: ClassVar = [
        ("channels", ctypes.c_uint8),
        ("values", ctypes.c_uint32 * PA_CHANNELS_MAX),
    ]


def _load_full_lib() -> ctypes.CDLL:
    """
    Load and configure libpulse for use by PAVolumeController.

    Called once per process; the result is cached by _get_full_lib().
    """
    lib = ctypes.CDLL("libpulse.so.0")

    lib.pa_threaded_mainloop_new.restype = ctypes.c_void_p
    lib.pa_threaded_mainloop_get_api.restype = ctypes.c_void_p
    lib.pa_threaded_mainloop_get_api.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_start.restype = ctypes.c_int
    lib.pa_threaded_mainloop_start.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_stop.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_free.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_lock.argtypes = [ctypes.c_void_p]
    lib.pa_threaded_mainloop_unlock.argtypes = [ctypes.c_void_p]

    lib.pa_context_new.restype = ctypes.c_void_p
    lib.pa_context_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.pa_context_set_state_callback.argtypes = [
        ctypes.c_void_p,
        _CONTEXT_NOTIFY_CB,
        ctypes.c_void_p,
    ]
    lib.pa_context_connect.restype = ctypes.c_int
    lib.pa_context_connect.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    lib.pa_context_get_state.restype = ctypes.c_int
    lib.pa_context_get_state.argtypes = [ctypes.c_void_p]
    lib.pa_context_disconnect.argtypes = [ctypes.c_void_p]
    lib.pa_context_unref.argtypes = [ctypes.c_void_p]

    lib.pa_cvolume_set.restype = ctypes.c_void_p
    lib.pa_cvolume_set.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint32]

    lib.pa_context_set_sink_volume_by_name.restype = ctypes.c_void_p
    lib.pa_context_set_sink_volume_by_name.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        _CONTEXT_SUCCESS_CB,
        ctypes.c_void_p,
    ]
    lib.pa_operation_unref.argtypes = [ctypes.c_void_p]

    lib.pa_context_load_module.restype = ctypes.c_void_p
    lib.pa_context_load_module.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        _CONTEXT_INDEX_CB,
        ctypes.c_void_p,
    ]
    lib.pa_context_unload_module.restype = ctypes.c_void_p
    lib.pa_context_unload_module.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        _CONTEXT_SUCCESS_CB,
        ctypes.c_void_p,
    ]
    return lib


_full_lib: ctypes.CDLL | None = None


def _get_full_lib() -> ctypes.CDLL:
    global _full_lib  # noqa: PLW0603
    if _full_lib is None:
        _full_lib = _load_full_lib()
    return _full_lib


class PAVolumeController:
    """
    Shared libpulse connection for hardware PA sink volume control.

    One instance is shared across all PA sink bridges. All calls are blocking
    and must be invoked via run_in_executor from async code.
    """

    def __init__(self) -> None:
        """Connect to PulseAudio and start the threaded mainloop."""
        self._lib = _get_full_lib()
        self._lock = threading.Lock()
        self._mainloop = self._lib.pa_threaded_mainloop_new()
        if not self._mainloop:
            raise OSError("pa_threaded_mainloop_new returned NULL")

        api = self._lib.pa_threaded_mainloop_get_api(self._mainloop)
        self._context = self._lib.pa_context_new(api, b"music-assistant-volume")
        if not self._context:
            self._lib.pa_threaded_mainloop_free(self._mainloop)
            self._mainloop = None
            raise OSError("pa_context_new returned NULL")

        self._ready = threading.Event()
        self._failed = threading.Event()

        def _state_cb_impl(_ctx: int, _userdata: int) -> None:
            state = self._lib.pa_context_get_state(self._context)
            if state == PA_CONTEXT_READY:
                self._ready.set()
            elif state in (PA_CONTEXT_FAILED, PA_CONTEXT_TERMINATED):
                self._failed.set()

        self._state_cb = _CONTEXT_NOTIFY_CB(_state_cb_impl)  # keep reference alive — GC
        self._lib.pa_context_set_state_callback(self._context, self._state_cb, None)

        pulse_server = _get_pulse_server()
        ret = self._lib.pa_context_connect(
            self._context,
            pulse_server.encode() if pulse_server else None,
            PA_CONTEXT_NOAUTOSPAWN,
            None,
        )
        if ret < 0:
            self.close()
            raise OSError(f"pa_context_connect failed (ret={ret})")

        self._lib.pa_threaded_mainloop_start(self._mainloop)

        if not self._ready.wait(timeout=5.0):
            self.close()
            raise OSError("Timed out connecting to PulseAudio for volume control")

    def set_sink_volume(self, sink_name: str, volume_pct: int, channels: int = 2) -> bool:
        """
        Set hardware volume on a named PA sink.

        :param sink_name: PA sink name as returned by ``enumerate_pa_sinks()``.
        :param volume_pct: Volume level 0-100, mapped through an exponential
            audio taper curve before being sent to PA.
        :param channels: Channel count for the PA volume structure. Should
            match the sink's actual channel count.
        :returns: True if PA reported success.
        """
        with self._lock:
            if self._failed.is_set():
                return False
            amplitude = volume_pct_to_amplitude(volume_pct)
            pa_vol = round(PA_VOLUME_NORM * amplitude ** (1.0 / 3.0))
            cvol = _PACVolume()
            self._lib.pa_cvolume_set(ctypes.byref(cvol), channels, pa_vol)

            done = threading.Event()
            result: dict[str, int] = {}

            def _success_cb_impl(_ctx: int, success: int, _userdata: int) -> None:
                result["success"] = success
                done.set()

            success_cb = _CONTEXT_SUCCESS_CB(_success_cb_impl)

            self._lib.pa_threaded_mainloop_lock(self._mainloop)
            try:
                op = self._lib.pa_context_set_sink_volume_by_name(
                    self._context,
                    sink_name.encode(),
                    ctypes.byref(cvol),
                    success_cb,
                    None,
                )
                if not op:
                    return False
            finally:
                self._lib.pa_threaded_mainloop_unlock(self._mainloop)

            if not done.wait(timeout=_SET_VOLUME_TIMEOUT):
                self._lib.pa_operation_unref(op)
                return False
            self._lib.pa_operation_unref(op)
            return bool(result.get("success", 0))

    def load_module(self, module_name: str, argument: str) -> int | None:
        """
        Load a PulseAudio module (e.g. module-remap-sink) via libpulse.

        :param module_name: PA module name, e.g. "module-remap-sink".
        :param argument: Module argument string, e.g.
            "sink_name=Foo master=bar channels=2 master_channel_map=...
            channel_map=front-left,front-right remix=no".

        Blocks (up to ~2s) for PA's response.
        :returns: The loaded module's index, or None on failure/timeout.
        """
        with self._lock:
            if self._failed.is_set():
                return None

            done = threading.Event()
            result: dict[str, int] = {}

            def _index_cb_impl(_ctx: int, idx: int, _userdata: int) -> None:
                result["index"] = idx
                done.set()

            index_cb = _CONTEXT_INDEX_CB(_index_cb_impl)

            self._lib.pa_threaded_mainloop_lock(self._mainloop)
            try:
                op = self._lib.pa_context_load_module(
                    self._context,
                    module_name.encode(),
                    argument.encode(),
                    index_cb,
                    None,
                )
                if not op:
                    return None
            finally:
                self._lib.pa_threaded_mainloop_unlock(self._mainloop)

            if not done.wait(timeout=2.0):
                self._lib.pa_operation_unref(op)
                return None
            self._lib.pa_operation_unref(op)
            idx = result.get("index", PA_INVALID_INDEX)
            return None if idx == PA_INVALID_INDEX else idx

    def unload_module(self, module_index: int) -> bool:
        """
        Unload a previously-loaded PulseAudio module by index.

        Blocks (up to ~2s) for PA's response.
        :returns: True if PA reported success.
        """
        with self._lock:
            if self._failed.is_set():
                return False

            done = threading.Event()
            result: dict[str, int] = {}

            def _success_cb_impl(_ctx: int, success: int, _userdata: int) -> None:
                result["success"] = success
                done.set()

            success_cb = _CONTEXT_SUCCESS_CB(_success_cb_impl)

            self._lib.pa_threaded_mainloop_lock(self._mainloop)
            try:
                op = self._lib.pa_context_unload_module(
                    self._context, module_index, success_cb, None
                )
                if not op:
                    return False
            finally:
                self._lib.pa_threaded_mainloop_unlock(self._mainloop)

            if not done.wait(timeout=2.0):
                self._lib.pa_operation_unref(op)
                return False
            self._lib.pa_operation_unref(op)
            return bool(result.get("success", 0))

    def close(self) -> None:
        """Disconnect and tear down the mainloop."""
        with self._lock:
            if self._context:
                self._lib.pa_context_disconnect(self._context)
                self._lib.pa_context_unref(self._context)
                self._context = None
            if self._mainloop:
                self._lib.pa_threaded_mainloop_stop(self._mainloop)
                self._lib.pa_threaded_mainloop_free(self._mainloop)
                self._mainloop = None


class PASimpleStream:
    """
    Synchronous PCM playback stream to a named PulseAudio sink.

    All libpulse calls are serialized behind a threading.Lock so that
    concurrent executor threads cannot simultaneously write/free the
    same pa_simple connection, which causes assertion failures in libpulse.
    """

    def __init__(
        self,
        sink_name: str,
        app_name: str,
        rate: int,
        channels: int,
        bit_depth: int = 16,
    ) -> None:
        """Open a synchronous PCM playback stream to the named PulseAudio sink."""
        lib = _get_lib()
        spec = _PASampleSpec(
            format=_pa_sample_format(bit_depth),
            rate=rate,
            channels=channels,
        )
        self._channel_map = _build_pa_channel_map(channels)
        error = ctypes.c_int(0)
        self._lib = lib
        self._lock = threading.Lock()
        pulse_server = _get_pulse_server()
        self._conn: int | None = lib.pa_simple_new(
            pulse_server.encode() if pulse_server else None,
            app_name.encode(),
            PA_STREAM_PLAYBACK,
            sink_name.encode(),
            b"playback",
            ctypes.byref(spec),
            ctypes.byref(self._channel_map) if self._channel_map is not None else None,
            None,
            ctypes.byref(error),
        )
        if not self._conn:
            raise OSError(
                f"pa_simple_new failed for sink '{sink_name}' "
                f"(pa_error={error.value}, server={pulse_server!r})"
            )

    def write(self, data: bytes) -> None:
        """Write a PCM chunk. Blocks until PA has buffered it."""
        with self._lock:
            if not self._conn:
                return
            error = ctypes.c_int(0)
            ret = self._lib.pa_simple_write(self._conn, data, len(data), ctypes.byref(error))
            if ret < 0:
                raise OSError(f"pa_simple_write failed (pa_error={error.value})")

    def drain(self) -> None:
        """Block until all buffered audio has played out."""
        with self._lock:
            if not self._conn:
                return
            error = ctypes.c_int(0)
            self._lib.pa_simple_drain(self._conn, ctypes.byref(error))

    def close(self) -> None:
        """
        Free the PA stream.

        Acquires the lock before zeroing _conn and calling pa_simple_free,
        ensuring no concurrent write() or drain() can touch the pointer
        between the None assignment and the free call.
        """
        with self._lock:
            conn, self._conn = self._conn, None
            if conn:
                self._lib.pa_simple_free(conn)

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(self, *_: object) -> None:
        """Exit context manager and close the stream."""
        self.close()


def enumerate_alsa_devices(*, logger: logging.Logger) -> list[dict[str, Any]]:
    """
    Enumerate stereo-capable ALSA output devices via PortAudio.

    Returns device dicts in the same shape as ``enumerate_pa_sinks()`` so
    both backends share the same bridge registration path.
    """
    import sounddevice as _sd  # noqa: PLC0415

    # Find the ALSA host API index
    alsa_hostapi_index: int | None = None
    for i, api in enumerate(_sd.query_hostapis()):
        if "alsa" in api.get("name", "").lower():
            alsa_hostapi_index = i
            break

    devices: list[dict[str, Any]] = []
    for idx, dev in enumerate(_sd.query_devices()):
        if dev.get("max_output_channels", 0) < 2:
            continue
        if alsa_hostapi_index is not None and dev.get("hostapi") != alsa_hostapi_index:
            continue
        try:
            test = _sd.RawOutputStream(
                device=idx,
                samplerate=int(dev.get("default_samplerate", 48000)),
                channels=2,
                dtype="int16",
            )
            test.close()
        except _sd.PortAudioError:
            continue
        name: str = dev.get("name", f"alsa-device-{idx}")

        # Skip virtual ALSA PCM plugins — only keep real hardware nodes.
        # PortAudio enumerates both hw: entries and virtual plugins
        # (sysdefault, front, surround*, dmix, lavrate, upmix, …).
        # Hardware entries always contain "(hw:" in their name.
        if "(hw:" not in name:
            continue

        sample_rate = int(dev.get("default_samplerate", 48000))

        # Build a clean display name: strip the " (hw:C,D)" suffix so the
        # MA player name reads e.g. "Intel Audio: ALC889A Analog" not
        # "Intel Audio: ALC889A Analog (hw:1,0)".
        import re as _re  # noqa: PLC0415

        description = _re.sub(r"\s*\(hw:\d+,\d+\)$", "", name).strip()

        # Extract "hw:C,D" to open the device for a direct chmap query.
        alsa_hw_string: str | None = None
        if hw_match := _re.search(r"\(hw:(\d+,\d+)\)$", name):
            alsa_hw_string = f"hw:{hw_match.group(1)}"

        devices.append(
            {
                "name": name,  # stable key — includes (hw:C,D) for uniqueness
                "description": description,  # human-readable MA player label
                "pa_sink_name": None,
                "max_output_channels": dev.get("max_output_channels", 2),
                "sample_rate": sample_rate,
                "bit_depth": 16,
                "is_remap": False,
                "master_device": None,
                "index": idx,
                "hostapi": dev.get("hostapi", 0),
                "alsa_hw_string": alsa_hw_string,
            }
        )
    if devices:
        for device_dict in devices:
            hw_string = device_dict.get("alsa_hw_string")
            if hw_string and device_dict.get("max_output_channels", 0) > 2:
                # Query the driver's own active channel map — the authoritative
                # source for physical channel order (same mechanism speaker-test
                # uses). If the driver reports no chmap for this channel count,
                # physical_channel_map stays unset and no remap is applied.
                resolved = query_alsa_chmap(
                    hw_string,
                    device_dict["max_output_channels"],
                    int(device_dict.get("sample_rate") or 48000),
                    logger=logger,
                )
                if resolved is not None:
                    device_dict["physical_channel_map"] = resolved
            if device_dict.get("max_output_channels", 0) > 2:
                logger.debug(
                    "ALSA device %s (%s, channels=%d): physical_channel_map=%s",
                    device_dict["name"],
                    device_dict.get("alsa_hw_string"),
                    device_dict.get("max_output_channels"),
                    device_dict.get("physical_channel_map"),
                )
    return devices


def enumerate_pa_sinks() -> list[dict[str, Any]]:
    """
    Enumerate PulseAudio output sinks via pactl.

    :raises FileNotFoundError: if pactl is not installed.
    :raises RuntimeError: if pactl returns unexpected output.
    :returns: List of sink dicts, one per sink, containing name, description,
        sample rate, bit depth, channel map, and remap-sink metadata.
    """
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    # Locate pactl — requires pulseaudio-utils to be installed
    if not (path := shutil.which("pactl")):
        raise FileNotFoundError("pactl not found — please install pulseaudio-utils")
    pactl_bin = path

    env = {**os.environ}
    pulse_server = _get_pulse_server()
    if pulse_server:
        env["PULSE_SERVER"] = pulse_server

    result = subprocess.run(  # noqa: S603
        [pactl_bin, "--format=json", "list", "sinks"],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pactl exited {result.returncode}: {result.stderr.strip()}")

    sinks = []
    for sink in json.loads(result.stdout):
        name: str = sink.get("name", "")
        desc: str = sink.get("description", name)
        spec_str: str = sink.get("sample_specification", "")
        driver: str = sink.get("driver", "")
        properties: dict[str, str] = sink.get("properties", {})
        # device.master_device is set by module-remap-sink itself on every
        # sink it creates, and only on those sinks — so it's a reliable way
        # to detect a remap-sink child regardless of what the host reports
        # in the top-level "driver" field. Real PulseAudio reports the
        # literal module name there ("module-remap-sink.c" /
        # "module-alsa-card.c"), but PipeWire's PulseAudio-compatibility
        # layer instead reports a generic "PipeWire" driver string for
        # every sink it manages, so "driver == module-remap-sink.c" alone
        # would silently misdetect every remap sink (and every ALSA-card
        # master sink) as something else on a PipeWire-only system.
        master_device: str | None = properties.get("device.master_device")
        is_remap = master_device is not None or driver == "module-remap-sink.c"
        alsa_card_name: str | None = properties.get("alsa.card_name")
        # pactl --format=json represents channel_map as a comma-separated
        # string (e.g. "front-left,front-right,rear-left,rear-right,...").
        channel_map_str: str = sink.get("channel_map", "")
        channel_map: list[str] = [c for c in channel_map_str.split(",") if c]
        try:
            parts = spec_str.split()
            fmt = parts[0]  # e.g. 's32le'
            channels = int(parts[1].replace("ch", ""))
            sample_rate = int(parts[2].replace("Hz", ""))
            # Parse bit depth from PA format string using explicit lookup.
            # Avoids s24-32le parsing as 2432 with the digit-filter approach.
            _fmt_to_depth = {
                "u8": 8,
                "s16le": 16,
                "s16be": 16,
                "s24le": 24,
                "s24be": 24,
                "s24-32le": 32,
                "s24-32be": 32,
                "s32le": 32,
                "s32be": 32,
                "float32le": 32,
                "float32be": 32,
            }
            bit_depth = _fmt_to_depth.get(fmt.lower(), 16)
        except (IndexError, ValueError):  # fmt: skip
            continue
        if channels < 2:
            continue
        sinks.append(
            {
                "name": name,  # stable PA sink name — used for UUID/player-id generation
                "description": desc,  # human-readable label — used as MA player display name
                "pa_sink_name": name,
                "max_output_channels": channels,
                "sample_rate": sample_rate,
                "bit_depth": bit_depth,
                "is_remap": is_remap,
                "master_device": master_device,
                "driver": driver,
                "channel_map": channel_map,
                "alsa_card_name": alsa_card_name,
            }
        )

    return sinks


# The channel order MA/FFmpeg actually writes PCM in for each channel count
# it produces — the PulseAudio position names for the FFmpeg layout that
# helpers/ffmpeg.py's _CHANNEL_LAYOUT selects at the same count. The two
# tables must stay in sync: one writes the order, the other interprets it.
_SOURCE_CHANNEL_ORDER: Final[dict[int, list[str]]] = {
    1: ["front-center"],
    2: ["front-left", "front-right"],
    3: ["front-left", "front-right", "lfe"],
    4: ["front-left", "front-right", "rear-left", "rear-right"],
    5: ["front-left", "front-right", "front-center", "rear-left", "rear-right"],
    6: ["front-left", "front-right", "front-center", "lfe", "rear-left", "rear-right"],
    7: [
        "front-left",
        "front-right",
        "front-center",
        "lfe",
        "rear-center",
        "side-left",
        "side-right",
    ],
    8: [
        "front-left",
        "front-right",
        "front-center",
        "lfe",
        "rear-left",
        "rear-right",
        "side-left",
        "side-right",
    ],
}

# Driver-reported positions with no counterpart in _SOURCE_CHANNEL_ORDER's
# vocabulary, aliased to the source position whose content they should carry.
# See the comment in build_channel_remap_index for the reasoning.
_PHYSICAL_POSITION_ALIASES: Final[dict[str, str]] = {
    "rear-left-of-center": "side-left",
    "rear-right-of-center": "side-right",
}


def build_channel_remap_index(
    channels: int, physical_channel_map: list[str] | None, *, logger: logging.Logger
) -> list[int] | None:
    """
    Build a channel-reorder index mapping standard PCM order to device order.

    Remaps MA's standard PCM channel order onto the device's real physical
    channel order.

    :param channels: Number of channels in the PCM stream being written.
    :param physical_channel_map: The device's real channel order (from
        query_alsa_chmap()), or None if unknown.
    :param logger: The calling provider's logger, so debug output follows the
        log level configured for the provider in Music Assistant.
    :returns: A list where result[i] = source channel index to place at
        output position i, or None if no remap is needed/possible: physical
        order unknown, channel count not in the known-source-order table,
        length mismatch, the physical map contains names outside the
        source order's set (can't identify a corresponding source channel),
        or the two orders already match (remapping would be a no-op).
    """
    if not physical_channel_map:
        logger.debug("No remap for %d-channel device: physical order unknown", channels)
        return None
    # HD Audio HDMI reports the beyond-5.1 pair as rear-left/right-of-center,
    # which FFmpeg 7.1 has no name for; alias them to the side channels.
    physical_channel_map = [
        _PHYSICAL_POSITION_ALIASES.get(name, name) for name in physical_channel_map
    ]
    source_order = _SOURCE_CHANNEL_ORDER.get(channels)
    if source_order is None:
        logger.debug(
            "No remap for %d-channel device: no known standard source order for this "
            "channel count (only %s covered)",
            channels,
            sorted(_SOURCE_CHANNEL_ORDER),
        )
        return None
    if len(physical_channel_map) != channels:
        logger.debug(
            "No remap for %d-channel device: physical map has %d entries (%s) — "
            "length mismatch, refusing to guess",
            channels,
            len(physical_channel_map),
            physical_channel_map,
        )
        return None
    if set(source_order) != set(physical_channel_map):
        logger.debug(
            "No remap for %d-channel device: physical map %s uses different channel "
            "names than the standard source order %s — can't match, refusing to guess",
            channels,
            physical_channel_map,
            source_order,
        )
        return None
    if source_order == physical_channel_map:
        logger.debug(
            "No remap needed for %d-channel device: physical order already matches "
            "standard source order %s",
            channels,
            source_order,
        )
        return None
    index = [source_order.index(name) for name in physical_channel_map]
    logger.debug(
        "Remap computed for %d-channel device: source %s -> physical %s (index %s)",
        channels,
        source_order,
        physical_channel_map,
        index,
    )
    return index


def remap_pcm_channels(
    data: bytes, channels: int, bytes_per_sample: int, index: list[int]
) -> bytes:
    """
    Reorder interleaved PCM channel data per a precomputed remap index.

    :param data: Raw interleaved PCM bytes.
    :param channels: Channel count (must match len(index)).
    :param bytes_per_sample: Bytes per sample (e.g. 2 for s16le, 4 for
        s32le/f32le). Byte-agnostic — reorders whole samples, doesn't
        interpret their value, so this works for any PCM sample format.
    :param index: Result of build_channel_remap_index() — index[i] is the
        source channel to place at output position i.
    :returns: Reordered PCM bytes, same length as input.
    """
    frame_bytes = channels * bytes_per_sample
    # Only whole frames can be reordered; any partial trailing frame is
    # passed through unchanged so the output length matches the input.
    usable_len = (len(data) // frame_bytes) * frame_bytes
    arr = np.frombuffer(data[:usable_len], dtype=np.uint8).reshape(-1, channels, bytes_per_sample)
    remapped = arr[:, index, :]
    return remapped.tobytes() + data[usable_len:]


# ALSA channel position enum values (alsa/pcm.h SND_CHMAP_*) mapped to their
# long-form position names. Limited to positions a consumer device can report.
_ALSA_CHMAP_POSITION: Final[dict[int, str]] = {
    3: "front-left",
    4: "front-right",
    5: "rear-left",
    6: "rear-right",
    7: "front-center",
    8: "lfe",
    9: "side-left",
    10: "side-right",
    11: "rear-center",
    12: "front-left-of-center",
    13: "front-right-of-center",
    14: "rear-left-of-center",
    15: "rear-right-of-center",
}
_ALSA_POSITION_MASK: Final = 0xFFFF  # low 16 bits are the position; upper bits are flags
_SND_PCM_STREAM_PLAYBACK: Final = 0

_asound_lib: ctypes.CDLL | None = None


def _get_asound_lib() -> ctypes.CDLL:
    """Load and configure libasound for chmap queries. Cached per-process."""
    global _asound_lib  # noqa: PLW0603
    if _asound_lib is None:
        lib = ctypes.CDLL("libasound.so.2")
        lib.snd_pcm_open.restype = ctypes.c_int
        lib.snd_pcm_open.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.snd_pcm_close.argtypes = [ctypes.c_void_p]
        lib.snd_pcm_query_chmaps.restype = ctypes.POINTER(ctypes.c_void_p)
        lib.snd_pcm_query_chmaps.argtypes = [ctypes.c_void_p]
        lib.snd_pcm_free_chmaps.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.snd_pcm_set_params.restype = ctypes.c_int
        lib.snd_pcm_set_params.argtypes = [
            ctypes.c_void_p,  # pcm
            ctypes.c_int,  # format
            ctypes.c_int,  # access
            ctypes.c_uint,  # channels
            ctypes.c_uint,  # rate
            ctypes.c_int,  # soft_resample
            ctypes.c_uint,  # latency (us)
        ]
        # Returns snd_pcm_chmap_t*: { uint32 channels; uint32 pos[]; } —
        # note: NO leading type field, unlike snd_pcm_query_chmaps entries.
        lib.snd_pcm_get_chmap.restype = ctypes.POINTER(ctypes.c_uint32)
        lib.snd_pcm_get_chmap.argtypes = [ctypes.c_void_p]
        _asound_lib = lib
    return _asound_lib


_libc: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL:
    """Libc handle for free() — snd_pcm_get_chmap results are malloc'd by ALSA."""
    global _libc  # noqa: PLW0603
    if _libc is None:
        _libc = ctypes.CDLL(None)
        _libc.free.argtypes = [ctypes.c_void_p]
        _libc.free.restype = None
    return _libc


_SND_PCM_FORMAT_S16_LE: Final = 2
_SND_PCM_ACCESS_RW_INTERLEAVED: Final = 3


def _decode_chmap_positions(
    u32: Any, offset: int, channels: int, device: str, *, logger: logging.Logger
) -> list[str] | None:
    """Decode a run of chmap position codes into long-form names, or None on any unknown."""
    positions: list[str] = []
    for j in range(channels):
        raw_pos = u32[offset + j] & _ALSA_POSITION_MASK
        name = _ALSA_CHMAP_POSITION.get(raw_pos)
        if name is None:
            logger.debug(
                "ALSA chmap query: %s slot %d has unrecognized position code %d — "
                "refusing to guess",
                device,
                j,
                raw_pos,
            )
            return None
        positions.append(name)
    return positions


def query_alsa_chmap(
    device: str, expected_channels: int, sample_rate: int = 48000, *, logger: logging.Logger
) -> list[str] | None:
    """
    Return the active speaker layout for a raw ALSA hw: device.

    :param device: ALSA device string, e.g. "hw:0,3".
    :param expected_channels: The channel count playback will use. The map
        can differ per channel count, so the query is made at this count.
    :param sample_rate: Sample rate to query at; any supported rate works.
    :returns: Ordered list of long-form position names (index 0..N-1), or
        None if the layout could not be determined. Callers must treat None
        as "no physical order known" and fall back accordingly, rather than
        guessing.
    """
    lib = _get_asound_lib()
    pcm = ctypes.c_void_p()
    err = lib.snd_pcm_open(ctypes.byref(pcm), device.encode(), _SND_PCM_STREAM_PLAYBACK, 0)
    if err < 0:
        logger.debug("ALSA chmap query: couldn't open %s (err=%d)", device, err)
        return None
    try:
        # 1) Active map: configure exactly as playback will, then ask.
        # Retry once at 48000 if the enumerated rate is rejected — raw hw:
        # devices don't resample, and a config failure here would silently
        # drop us to the weaker available-list fallback.
        err = -1
        for rate in dict.fromkeys((sample_rate, 48000)):
            err = lib.snd_pcm_set_params(
                pcm,
                _SND_PCM_FORMAT_S16_LE,
                _SND_PCM_ACCESS_RW_INTERLEAVED,
                expected_channels,
                rate,
                1,  # soft_resample
                500_000,  # latency us — irrelevant, stream is never started
            )
            if err >= 0:
                break
        if err >= 0:
            chmap_ptr = lib.snd_pcm_get_chmap(pcm)
            if chmap_ptr:
                try:
                    channels = chmap_ptr[0]
                    if channels == expected_channels:
                        positions = _decode_chmap_positions(
                            chmap_ptr, 1, channels, device, logger=logger
                        )
                        if positions is not None:
                            logger.debug(
                                "ALSA chmap query: %s ACTIVE chmap (%d channels): %s",
                                device,
                                channels,
                                positions,
                            )
                            return positions
                    else:
                        logger.debug(
                            "ALSA chmap query: %s active chmap has %d channels, "
                            "expected %d — ignoring",
                            device,
                            channels,
                            expected_channels,
                        )
                finally:
                    _get_libc().free(chmap_ptr)
            else:
                logger.debug("ALSA chmap query: %s reports no active chmap", device)
        else:
            logger.debug(
                "ALSA chmap query: set_params failed on %s (err=%d, %dch@%dHz) — "
                "can't read active map",
                device,
                err,
                expected_channels,
                sample_rate,
            )

        # 2) Fallback: first matching entry from the available-maps list.
        # Known-weaker source (see docstring) — logged distinctly so a wrong
        # result is traceable to this path.
        maps_ptr = lib.snd_pcm_query_chmaps(pcm)
        if not maps_ptr:
            logger.debug("ALSA chmap query: %s returned no chmaps", device)
            return None
        try:
            i = 0
            while maps_ptr[i]:
                # layout: int32 type; uint32 channels; uint32 pos[channels]
                u32 = ctypes.cast(maps_ptr[i], ctypes.POINTER(ctypes.c_uint32))
                channels = u32[1]
                if channels == expected_channels:
                    positions = _decode_chmap_positions(u32, 2, channels, device, logger=logger)
                    if positions is None:
                        return None
                    logger.debug(
                        "ALSA chmap query: %s AVAILABLE-list fallback (%d channels): %s "
                        "— active map unavailable, this may not reflect actual routing",
                        device,
                        channels,
                        positions,
                    )
                    return positions
                i += 1
            logger.debug(
                "ALSA chmap query: %s had no chmap entry for %d channels",
                device,
                expected_channels,
            )
            return None
        finally:
            lib.snd_pcm_free_chmaps(maps_ptr)
    finally:
        lib.snd_pcm_close(pcm)


def suspend_resume_sink(sink_name: str) -> None:
    """
    Suspend then resume a PA sink to reset its underlying ALSA driver state.

    Works around the snd_ctxfi mmap bug (kernel 6.12.x, commit 391e69143d0a)
    where the X-Fi card's DMA transfer stalls after driver init, causing
    pa_simple_write to timeout. A suspend/resume cycle re-initialises the
    ALSA PCM device and clears the stall without requiring a PA or system
    restart.

    Called on ALSA-card master sinks after remap-sink topology creation at
    provider load/reload time. No-op if pactl is not available.

    :param sink_name: PA sink name to suspend and resume.
    """
    import shutil  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    if not (pactl_bin := shutil.which("pactl")):
        return

    env = {**os.environ}
    if pulse_server := _get_pulse_server():
        env["PULSE_SERVER"] = pulse_server

    try:
        subprocess.run(  # noqa: S603
            [pactl_bin, "suspend-sink", sink_name, "1"],
            check=True,
            capture_output=True,
            timeout=3,
            env=env,
        )
        time.sleep(0.5)
        subprocess.run(  # noqa: S603
            [pactl_bin, "suspend-sink", sink_name, "0"],
            check=True,
            capture_output=True,
            timeout=3,
            env=env,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):  # fmt: skip
        pass
