"""Constants for the Rotation playlist provider."""

from __future__ import annotations

DOMAIN = "rotation_playlist"
HOUSEHOLD = "household"

STORAGE_FOLDER = "rotation_playlists"
ROTATIONS_FILE = "rotations.json"
PROGRESS_FILE = "progress.json"
STORAGE_VERSION = 1

PREVIEW_MAX_LIMIT = 200
BLOCKING_SORT_MAX_ITEMS = 5000
PREVIEW_COUNT_CAP = 10_000
BATCH_MAX_TRACKS = 50

MAX_STEPS = 30
MAX_CONDITIONS = 20
MAX_SORT_KEYS = 5

# the number of bits of the seeds of the random orders
SEED_BITS = 31

# the strings of this provider, for the messages and labels it sends to API clients
TRANSLATION_OWNER = f"provider.{DOMAIN}"
