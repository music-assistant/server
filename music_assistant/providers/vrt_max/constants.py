"""Constants for the VRT MAX music provider."""

# Browse subpath prefixes (after "<instance>://").
BROWSE_RADIOS = "radios"
BROWSE_RADIO_PROGRAMS = "radio"
BROWSE_PODCASTS = "podcasts"

# Tile typenames that identify a program/podcast row on a landing page.
RADIO_ROW_TYPE = "RadioProgramTile"
PODCAST_ROW_TYPE = "PodcastProgramTile"

# Only radio-archive episodes carry a played-songs tracklist (podcasts never do).
# Their archives hold only a handful of recent broadcasts; the cap is a safety net
# against a pathologically long archive triggering a burst of tracklist fetches.
MAX_TRACKLIST_EPISODES = 50
