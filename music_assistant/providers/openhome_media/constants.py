"""Constants for the Linn / OpenHome Media Provider."""

CONF_NETWORK_SCAN = "True"
CONF_USE_DEVICE_RADIO_AS_SOURCE: str = "use_source_radio" # otherwise use Playlist as source
CONF_USE_DEVICE_PLAYLIST_AS_QUEUE: str = "use_device_playlist" # will force Playlist as source

CALLBACK_URL: str = "/notify"

# The Linn/OpenHome Media provider allows you to stream music to an OpenHome Media compliant renderer as a Music Assistant player
# This allows use and control of devices such as a Linn Products Ltd streamer
# It will allow you to control transport, volume, source and see the details for the currently playing item.
