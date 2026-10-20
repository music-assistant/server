"""Music Assistant: The music library manager in python."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .mass import MusicAssistant

__all__ = ("MusicAssistant",)


# MusicAssistant is re-exported lazily (PEP 562): importing it pulls in the full
# server (all controllers and their deps), which submodule-only consumers such as
# `import music_assistant.constants` must not pay for.
def __getattr__(name: str) -> Any:
    if name == "MusicAssistant":
        from .mass import MusicAssistant  # noqa: PLC0415

        return MusicAssistant
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
