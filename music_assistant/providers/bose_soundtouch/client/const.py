"""Constants for the Bose Soundtouch client."""

# the speakers do not use the default utf8 encoding
STRING_ENCODING = "latin-1"

# a speaker serves its local HTTP API on port 8090 and a websocket
# notification channel on port 8080 (the "gabbo" subprotocol)
HTTP_PORT = 8090
NOTIFICATION_PORT = 8080
WS_SUBPROTOCOLS = ("gabbo",)
WS_HEARTBEAT = 30

REQUEST_TIMEOUT = 10
RECONNECT_DELAY = 10
