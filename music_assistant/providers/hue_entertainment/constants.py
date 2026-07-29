"""Config constants for the Hue Lights Sync provider."""

from __future__ import annotations

from typing import Final

CONF_BRIDGE_HOST: Final[str] = "bridge_host"
CONF_BRIDGE_ID: Final[str] = "bridge_id"
CONF_ACTION_PAIR: Final[str] = "pair"
CONF_USERNAME: Final[str] = "hue_username"
CONF_CLIENTKEY: Final[str] = "hue_clientkey"
CONF_BRIGHTNESS: Final[str] = "brightness"
CONF_COLOR_MODE: Final[str] = "color_mode"
CONF_HUE_LATENCY_MS: Final[str] = "hue_latency_ms"

# ---- Distributed strobe overlay ----
# Which lights take part in the strobe, stored as "<area_id>:<channel_id>"
# entries (a multi-select across every channel on the bridge).
CONF_STROBE_LIGHTS: Final[str] = "strobe_lights"
# Hard cap on how many of the selected lights may flash at once, as a percentage.
CONF_STROBE_COVERAGE: Final[str] = "strobe_coverage"
# 0-100; 100 = strobe runs continuously, lower needs a bigger energy jump.
CONF_STROBE_SENSITIVITY: Final[str] = "strobe_sensitivity"
# Master on/off for the strobe overlay (lets you keep the light selection but
# silence the strobe without clearing it).
CONF_STROBE_ENABLED: Final[str] = "strobe_enabled"
# True = selected lights go black between flashes (max contrast); False = they
# fall back to the base effect so the preset shows through between flashes.
CONF_STROBE_BLACKOUT: Final[str] = "strobe_blackout"
# Strobe flash colour (hex). Independent of the palette / base effects.
CONF_STROBE_COLOR: Final[str] = "strobe_color"
# Strobe brightness cap (0-100). Independent of the base brightness.
CONF_STROBE_BRIGHTNESS: Final[str] = "strobe_brightness"
# Flash rate in Hz and the on-duty as a percentage of the flash period.
CONF_STROBE_FLASH_HZ: Final[str] = "strobe_flash_hz"
CONF_STROBE_DUTY: Final[str] = "strobe_duty"
# Hysteresis: minimum time the burst stays engaged, and the release delay.
CONF_STROBE_MIN_HOLD_MS: Final[str] = "strobe_min_hold_ms"
CONF_STROBE_RELEASE_MS: Final[str] = "strobe_release_ms"
# Align flashes to the detected beat instead of free-running at flash_hz.
CONF_STROBE_BEAT_SYNC: Final[str] = "strobe_beat_sync"
# Auto strobe: the song-structure detector drives engagement + flash rate
# (off in verses/breaks, accelerating through builds, hard on drops), so the
# sensitivity / flash-Hz / coverage sliders are ignored while it is on.
CONF_STROBE_AUTO: Final[str] = "strobe_auto"

DEFAULT_STROBE_COVERAGE: Final[int] = 50
DEFAULT_STROBE_SENSITIVITY: Final[int] = 70
DEFAULT_STROBE_ENABLED: Final[bool] = True
DEFAULT_STROBE_BLACKOUT: Final[bool] = True
DEFAULT_STROBE_COLOR: Final[str] = "#FFFFFF"
DEFAULT_STROBE_BRIGHTNESS: Final[int] = 100
DEFAULT_STROBE_FLASH_HZ: Final[int] = 11
DEFAULT_STROBE_DUTY: Final[int] = 30
DEFAULT_STROBE_MIN_HOLD_MS: Final[int] = 300
DEFAULT_STROBE_RELEASE_MS: Final[int] = 250
DEFAULT_STROBE_BEAT_SYNC: Final[bool] = False
DEFAULT_STROBE_AUTO: Final[bool] = False

# Selectable strobe colours for the config dropdown (value = hex, title = name).
STROBE_COLOR_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("#FFFFFF", "White"),
    ("#FFD9A0", "Warm white"),
    ("#FF0000", "Red"),
    ("#FF6A00", "Amber"),
    ("#FFD000", "Yellow"),
    ("#00FF66", "Green"),
    ("#00E5FF", "Cyan"),
    ("#2563EB", "Blue"),
    ("#8A2BE2", "Purple"),
    ("#FF00AA", "Magenta"),
)

# Visualization modes; first entry is the default. Used to build the config
# options and to migrate away orphaned stored values from older versions.
COLOR_MODES: Final[tuple[str, ...]] = (
    "smooth",
    "ambient",
    "flashing",
    "energetic",
    "club",
    "pulse",
)
DEFAULT_COLOR_MODE: Final[str] = COLOR_MODES[0]

# Pulse / Club-groove fire engine knobs (apply to the "pulse" mode and the club
# groove). floor/decay are stored as whole percents; select picks which light fires.
CONF_PULSE_FLOOR: Final[str] = "pulse_floor"
CONF_PULSE_DECAY: Final[str] = "pulse_decay"
CONF_PULSE_SELECT: Final[str] = "pulse_select"
CONF_PULSE_DOWNBEAT: Final[str] = "pulse_downbeat"
DEFAULT_PULSE_FLOOR: Final[int] = 0  # % brightness held between beats (0 = full black)
DEFAULT_PULSE_DECAY: Final[int] = 90  # % of the beat gap the fire envelope spans
DEFAULT_PULSE_SELECT: Final[str] = "chase"
DEFAULT_PULSE_DOWNBEAT: Final[bool] = True
PULSE_SELECT_OPTIONS: Final[tuple[tuple[str, str], ...]] = (
    ("chase", "Chase (travels light to light)"),
    ("scatter", "Scatter (random light each beat)"),
    ("spectrum", "Spectrum (loudest band's light)"),
)

# Selected colour palette name. The album-art choice needs a real value: an empty string
# flattens the translation key to "...palette.options." and the frontend cannot select it.
# Older configs stored "" for the same choice, so both still resolve to album colours.
CONF_PALETTE: Final[str] = "palette"
PALETTE_ALBUM_COLORS: Final[str] = "album_colors"
DEFAULT_PALETTE: Final[str] = PALETTE_ALBUM_COLORS

# Bar-aligned palette rotation: cycle through a list of palettes, advancing every
# N detected beats (16 = 4 bars of 4/4), snapped to bar starts (downbeats).
CONF_PALETTE_ROTATE: Final[str] = "palette_rotate"
CONF_PALETTE_ROTATE_LIST: Final[str] = "palette_rotate_list"
CONF_PALETTE_ROTATE_BEATS: Final[str] = "palette_rotate_beats"
# Short, tempo-locked crossfade between palettes when rotating (~1 beat). Off =
# a hard cut exactly on the bar (punchier for clubbing).
CONF_PALETTE_ROTATE_SMOOTH: Final[str] = "palette_rotate_smooth"
DEFAULT_PALETTE_ROTATE: Final[bool] = False
DEFAULT_PALETTE_ROTATE_BEATS: Final[int] = 16
DEFAULT_PALETTE_ROTATE_SMOOTH: Final[bool] = True

# Per-light base brightness, stored as a JSON object {"<area_id>:<channel_id>": pct}.
# Scales the BASE effects on individual lights only; never limits the strobe.
# Edited from the live preview (a slider under each light), persisted here.
CONF_PERLIGHT_BRIGHTNESS_DATA: Final[str] = "perlight_brightness"
DEFAULT_PERLIGHT_BRIGHTNESS_DATA: Final[str] = ""

DEFAULT_HUE_LATENCY_MS: Final[int] = 20

HUE_MDNS_TYPE: Final[str] = "_hue._tcp.local."

# The devicetype registered with the Hue bridge during pairing. Shown in the
# Hue app's list of linked apps; keeps Music Assistant's identity on (re)pair.
HUE_DEVICE_TYPE: Final[str] = "music_assistant#hue_entertainment"

# ---- Spectrum config requested from the Sendspin visualizer ----

# 17 mel bins over 20-20kHz: enough resolution to map distinct bands to lights
# while keeping the per-frame payload small (bin ~10 ≈ 3.5kHz is the musical
# ceiling, see _CHANNEL_BIN_MAX in the analyzer).
SPECTRUM_BINS: Final[int] = 17
SPECTRUM_SCALE: Final = "mel"
SPECTRUM_F_MIN: Final[int] = 20
SPECTRUM_F_MAX: Final[int] = 20000
