"""
CoreAudio hardware volume control for macOS.

Uses ctypes to call CoreAudio's AudioObject API directly,
controlling the actual device hardware volume rather than the system mixer.
"""

from __future__ import annotations

import ctypes
import ctypes.util

# CoreAudio property selectors (FourCC encoded as big-endian uint32)
_PROP_DEVICES = int.from_bytes(b"dev#", "big")
_PROP_NAME = int.from_bytes(b"lnam", "big")
_SCOPE_GLOBAL = int.from_bytes(b"glob", "big")
_SCOPE_OUTPUT = int.from_bytes(b"outp", "big")
_PROP_VOLUME_SCALAR = int.from_bytes(b"volm", "big")
_PROP_MUTE = int.from_bytes(b"mute", "big")
_SYSTEM_OBJECT = 1

# CoreFoundation UTF-8 encoding
_CF_ENCODING_UTF8 = 0x08000100


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


def _load_frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    """Load CoreAudio and CoreFoundation frameworks."""
    ca_path = ctypes.util.find_library("CoreAudio")
    cf_path = ctypes.util.find_library("CoreFoundation")
    if not ca_path or not cf_path:
        msg = "CoreAudio or CoreFoundation not found"
        raise OSError(msg)
    ca = ctypes.cdll.LoadLibrary(ca_path)
    cf = ctypes.cdll.LoadLibrary(cf_path)
    # Set up CFString helper signatures
    cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
    cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    return ca, cf


def _cfstring_to_str(cf: ctypes.CDLL, cfstr: ctypes.c_void_p) -> str:
    """Convert a CFStringRef to a Python string."""
    ptr = cf.CFStringGetCStringPtr(cfstr, _CF_ENCODING_UTF8)
    if ptr:
        return str(ptr.decode("utf-8"))
    buf = ctypes.create_string_buffer(512)
    if cf.CFStringGetCString(cfstr, buf, 512, _CF_ENCODING_UTF8):
        return buf.value.decode("utf-8")
    return ""


def _find_device_id(ca: ctypes.CDLL, cf: ctypes.CDLL, device_name: str) -> int | None:
    """Find a CoreAudio device ID by name."""
    prop = _AudioObjectPropertyAddress(_PROP_DEVICES, _SCOPE_GLOBAL, 0)
    size = ctypes.c_uint32(0)
    if (
        ca.AudioObjectGetPropertyDataSize(
            _SYSTEM_OBJECT, ctypes.byref(prop), 0, None, ctypes.byref(size)
        )
        != 0
    ):
        return None

    n_devices = size.value // 4
    device_ids = (ctypes.c_uint32 * n_devices)()
    if (
        ca.AudioObjectGetPropertyData(
            _SYSTEM_OBJECT, ctypes.byref(prop), 0, None, ctypes.byref(size), device_ids
        )
        != 0
    ):
        return None

    for did in device_ids:
        name_prop = _AudioObjectPropertyAddress(_PROP_NAME, _SCOPE_GLOBAL, 0)
        cfstr = ctypes.c_void_p()
        sz = ctypes.c_uint32(ctypes.sizeof(cfstr))
        if (
            ca.AudioObjectGetPropertyData(
                did, ctypes.byref(name_prop), 0, None, ctypes.byref(sz), ctypes.byref(cfstr)
            )
            == 0
        ):
            name = _cfstring_to_str(cf, cfstr)
            if name == device_name:
                return int(did)
    return None


def set_device_volume(device_name: str, volume: int) -> bool:
    """
    Set the hardware volume for a named CoreAudio device.

    :param device_name: The device name (must match CoreAudio device name).
    :param volume: Volume level 0-100.
    :return: True if successful.
    """
    try:
        ca, cf = _load_frameworks()
    except OSError:
        return False

    device_id = _find_device_id(ca, cf, device_name)
    if device_id is None:
        return False

    vol_prop = _AudioObjectPropertyAddress(_PROP_VOLUME_SCALAR, _SCOPE_OUTPUT, 0)
    if not ca.AudioObjectHasProperty(device_id, ctypes.byref(vol_prop)):
        return False

    scalar = ctypes.c_float(volume / 100.0)
    size = ctypes.c_uint32(4)
    result: int = ca.AudioObjectSetPropertyData(
        device_id, ctypes.byref(vol_prop), 0, None, size, ctypes.byref(scalar)
    )
    return result == 0


def set_device_mute(device_name: str, muted: bool) -> bool:
    """
    Set the hardware mute state for a named CoreAudio device.

    :param device_name: The device name (must match CoreAudio device name).
    :param muted: Whether to mute or unmute.
    :return: True if successful.
    """
    try:
        ca, cf = _load_frameworks()
    except OSError:
        return False

    device_id = _find_device_id(ca, cf, device_name)
    if device_id is None:
        return False

    mute_prop = _AudioObjectPropertyAddress(_PROP_MUTE, _SCOPE_OUTPUT, 0)
    if not ca.AudioObjectHasProperty(device_id, ctypes.byref(mute_prop)):
        return False

    mute_val = ctypes.c_uint32(1 if muted else 0)
    size = ctypes.c_uint32(4)
    result: int = ca.AudioObjectSetPropertyData(
        device_id, ctypes.byref(mute_prop), 0, None, size, ctypes.byref(mute_val)
    )
    return result == 0
