"""Constants for the Sendspin provider."""

from __future__ import annotations

BRIDGE_PREFIX = "spb_"
VIRTUAL_PLAYER_ID_PREFIX = "sendspin_virtual_"

CONF_CAST_AUDIO_UNSUPPORTED = "cast_audio_unsupported"
CONF_SENDSPIN_STATIC_DELAY = "sendspin_static_delay"
CONF_VIRTUAL_PLAYER_OWNER = "virtual_player_owner"
DEFAULT_SENDSPIN_STATIC_DELAY = 0

CONF_ALLOW_LEGACY_CLIENTS = "allow_legacy_clients"
# Pairing method the setup flow lets the user pick between.
CONF_PAIRING_METHOD = "pairing_method"
CONF_PAIRING_TOKEN = "pairing_token"
PAIR_METHOD_PAIRING_CODE = "pin"
PAIR_METHOD_DYNAMIC_PAIRING_CODE = "dynamic_pin"
PAIR_METHOD_STATIC_PAIRING_CODE = "static_pin"
PAIR_METHOD_TOKEN = "token"
# The consent step's opt-in checkbox for pairing a device that allows unpaired use.
CONF_PAIR_DEVICE = "pair_device"

# Consent-page note that pairing is what enables the device's audio input.
CONF_SOURCE_INPUT_NOTE = "source_input_note"

# The consent step's choice between connecting straight away and pairing first.
CONF_CONNECT_METHOD = "connect_method"
CONNECT_METHOD_UNPAIRED = "unpaired"
CONNECT_METHOD_PAIR = "pair"

# The setup flow step for a device whose audio input awaits a decision.
CONF_SOURCE_INPUT_ACTION = "source_input_action"
SOURCE_INPUT_PAIR = "pair"
SOURCE_INPUT_DISMISS = "dismiss"
# Persisted (raw player config) marker that the user declined the audio input.
CONF_SOURCE_APPROVAL_DISMISSED = "source_approval_dismissed"

CONF_PAIRING_TOKEN = "pairing_token"
CONF_PAIRING_PIN = "pairing_pin"

CONF_ACTION_UNPAIR = "unpair"
CONF_ACTION_REVOKE_UNPAIRED = "revoke_unpaired"

CONF_ACTION_MANAGEMENT_ENTER = "management_enter"
CONF_ACTION_MANAGEMENT_EXIT = "management_exit"
CONF_ACTION_MANAGEMENT_UNPAIRED_ENABLE = "management_unpaired_enable"
CONF_ACTION_MANAGEMENT_UNPAIRED_DISABLE = "management_unpaired_disable"
CONF_ACTION_MANAGEMENT_STATIC_PAIRING_CODE_ENABLE = "management_static_pin_enable"
CONF_ACTION_MANAGEMENT_STATIC_PAIRING_CODE_DISABLE = "management_static_pin_disable"
CONF_ACTION_MANAGEMENT_DYNAMIC_PAIRING_CODE_ENABLE = "management_dynamic_pin_enable"
CONF_ACTION_MANAGEMENT_DYNAMIC_PAIRING_CODE_DISABLE = "management_dynamic_pin_disable"

# Declared here because only the player provider can add player config entries.
CONF_SOURCE_AUTOSTART_TARGET = "source_autostart_target"
SOURCE_AUTOSTART_OFF = "off"
