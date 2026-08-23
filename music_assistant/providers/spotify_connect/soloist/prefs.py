"""
Audio settings for the Spotify Soloist engine.

The engine has no CLI or WebSocket control for crossfade, loudness normalization
or stream quality: it reads them from the classic desktop-client prefs stores in
its data directory, at startup only. Both the Spotify Connect backend and the
Spotify music provider's playback backend therefore write these before every
daemon spawn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from music_assistant.providers.spotify_connect.base import (
    AUDIO_QUALITY_HIGH,
    AUDIO_QUALITY_LOSSLESS,
    AUDIO_QUALITY_NORMAL,
    AUDIO_QUALITY_VERY_HIGH,
)

if TYPE_CHECKING:
    import logging
    from pathlib import Path

# The classic desktop-client prefs keys (bare key=value lines) through which the
# engine's audio behavior is controlled. The per-user prefs override the global
# prefs per key, so both stores are (re)written before every daemon spawn.
# NOTE: audio.crossfade.time_v2 is in MILLISECONDS; sub-second values silently
# disable crossfade (verified empirically), so the key is only written when
# crossfade is enabled (>= 1000 ms).
PREF_CROSSFADE: Final = "audio.crossfade_v2"
PREF_CROSSFADE_TIME: Final = "audio.crossfade.time_v2"
PREF_NORMALIZE: Final = "audio.normalize_v2"
# The engine reads the metered variant on a metered connection and the
# non-metered one otherwise; both are written so the tier holds either way.
# The "migrated" marker is what makes the engine honor the non-metered key
# instead of deriving it once from the metered one.
PREF_QUALITY: Final = "audio.play_bitrate_enumeration"
PREF_QUALITY_NON_METERED: Final = "audio.play_bitrate_non_metered_enumeration"
PREF_QUALITY_MIGRATED: Final = "audio.play_bitrate_non_metered_migrated"

# Quality tier -> the engine's bitrate enumeration value. Measured against
# build 1.3.7.349 on a 4:20 track (bytes fetched for the whole file): 2 and 3
# deliver ~96 and ~160 kbps, 4 ~320 kbps and 5 lossless FLAC (~810 kbps).
# 5 is the ceiling — values outside 1-5 are rejected and silently fall back to
# ~160 kbps, so an unknown tier must never reach the prefs file.
_QUALITY_VALUES: Final[dict[str, int]] = {
    AUDIO_QUALITY_NORMAL: 2,
    AUDIO_QUALITY_HIGH: 3,
    AUDIO_QUALITY_VERY_HIGH: 4,
    AUDIO_QUALITY_LOSSLESS: 5,
}


def write_audio_prefs(
    data_dir: Path,
    logger: logging.Logger,
    *,
    crossfade_ms: int = 0,
    loudness_normalization: bool = False,
    audio_quality: str | None = None,
) -> None:
    """
    Write the given audio behavior into the engine's prefs stores (blocking).

    Both the global prefs and every existing per-user prefs file are updated:
    per-user values override the global ones per key, and a per-user file only
    appears after an account paired — the global store covers that account's
    first session until the next daemon (re)spawn refreshes both.

    Best-effort: a write failure must never block playback, so errors are
    logged per store and the daemon spawns with the engine's previous settings.

    :param data_dir: The daemon's data directory (holds the settings stores).
    :param logger: Logger to report per-store write failures on.
    :param crossfade_ms: Crossfade duration in milliseconds (0 disables crossfade).
    :param loudness_normalization: Whether the engine normalizes loudness itself.
    :param audio_quality: One of the AUDIO_QUALITY_* tiers, or None to leave the
        quality the engine is already configured with untouched.
    """
    managed_lines = [
        f"{PREF_CROSSFADE}={'true' if crossfade_ms else 'false'}",
        f"{PREF_NORMALIZE}={'true' if loudness_normalization else 'false'}",
    ]
    if crossfade_ms:
        managed_lines.insert(1, f"{PREF_CROSSFADE_TIME}={crossfade_ms}")
    # every crossfade key is dropped even when only the boolean is rewritten:
    # leaving a stale time behind would keep a previous session's crossfade on
    managed_keys = {PREF_CROSSFADE, PREF_CROSSFADE_TIME, PREF_NORMALIZE}
    if audio_quality is not None:
        quality = _QUALITY_VALUES.get(audio_quality, _QUALITY_VALUES[AUDIO_QUALITY_LOSSLESS])
        managed_lines += [
            f"{PREF_QUALITY}={quality}",
            f"{PREF_QUALITY_NON_METERED}={quality}",
            f"{PREF_QUALITY_MIGRATED}=true",
        ]
        # a caller that does not manage the quality tier leaves the engine's own
        managed_keys |= {PREF_QUALITY, PREF_QUALITY_NON_METERED, PREF_QUALITY_MIGRATED}
    settings_dir = data_dir / "settings"
    prefs_files = [settings_dir / "prefs"]
    try:
        users_dir = settings_dir / "Users"
        if users_dir.is_dir():
            prefs_files += [
                user_dir / "prefs" for user_dir in users_dir.iterdir() if user_dir.is_dir()
            ]
    except OSError as err:
        logger.warning("Failed to list the Spotify per-user settings: %s", err)
    for prefs_file in prefs_files:
        try:
            lines = []
            if prefs_file.is_file():
                lines = [
                    line
                    for line in prefs_file.read_text(encoding="utf-8").splitlines()
                    if line.split("=", 1)[0] not in managed_keys
                ]
            prefs_file.parent.mkdir(parents=True, exist_ok=True)
            # the stores also carry engine-owned keys, so replace atomically
            # (a truncated in-place write would lose those too)
            tmp_file = prefs_file.with_suffix(".tmp")
            tmp_file.write_text("\n".join([*lines, *managed_lines]) + "\n", encoding="utf-8")
            tmp_file.replace(prefs_file)
        except (OSError, UnicodeDecodeError) as err:
            logger.warning("Failed to write the Spotify audio settings to %s: %s", prefs_file, err)
