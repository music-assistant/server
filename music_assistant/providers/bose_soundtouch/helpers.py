"""Helpers for Bose soundtouch."""

from defusedxml import ElementTree as DefusedET


def source_id(source: str, source_account: str | None) -> str:
    """Build a stable source id from a SoundTouch source and account."""
    return f"{source}:{source_account}" if source_account else source


def extract_preset_id(message: str) -> int | None:
    """
    Extract a Bose SoundTouch favorite/preset id from a websocket XML message.

    Detects a physical preset button press from the speaker's notification channel.
    """
    try:
        root = DefusedET.fromstring(message)
    except DefusedET.ParseError:
        return None

    preset_id = next(
        (
            element.attrib.get("id")
            for element in root.iter()
            if _local_name(element.tag) == "preset"
        ),
        None,
    )

    try:
        return int(preset_id) if preset_id else None
    except TypeError, ValueError:
        return None


def _local_name(tag: str) -> str:
    """Return an XML tag name without its namespace prefix."""
    return tag.rsplit("}", 1)[-1]
