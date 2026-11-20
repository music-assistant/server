"""Constants for the Sendspin provider."""

from __future__ import annotations

BRIDGE_PREFIX = "spb_"
VIRTUAL_PLAYER_ID_PREFIX = "sendspin_virtual_"
MULTI_CHANNEL_ID_PREFIX = "sendspin_mc_"

# The record of a multi-channel player: its group id and, per speaker number, its speaker.
CONF_MULTI_CHANNEL_GROUP_ID = "multi_channel_group_id"
CONF_SPEAKER_CLIENT_ID = "speaker_{n}_client_id"
CONF_SPEAKER_NAME = "speaker_{n}_name"
CONF_SPEAKER_CHANNEL = "speaker_{n}_channel"
# The channel the user sets for a speaker, stored until the player takes it into its record.
CONF_SPEAKER_CHANNEL_SETTING = "speaker_{n}_channel_setting"
CONF_SPEAKER_OUTPUT_DELAY = "speaker_{n}_output_delay"
CONF_SPEAKER_STATE = "speaker_{n}_state"
CONF_SPEAKER_DISPLAY_NAME = "speaker_{n}_display_name"

CONF_SENDSPIN_STATIC_DELAY = "sendspin_static_delay"
# the static delays the user set for the protocols that do not carry the output now, by their id
CONF_SENDSPIN_BRIDGE_STATIC_DELAYS = "sendspin_bridge_static_delays"
# the user switched the output on while a Cast protocol carried it
CONF_SENDSPIN_CAST_OPT_IN = "sendspin_cast_opt_in"
CONF_VIRTUAL_PLAYER_OWNER = "virtual_player_owner"
DEFAULT_SENDSPIN_STATIC_DELAY = 0

CONF_ALLOW_LEGACY_CLIENTS = "allow_legacy_clients"

DEFAULT_SERVER_PORT = 8927

# Pairing method the setup flow lets the user pick between.
CONF_PAIRING_METHOD = "pairing_method"
CONF_PAIRING_TOKEN = "pairing_token"  # noqa: S105 - config key
PAIR_METHOD_PIN = "pin"
PAIR_METHOD_DYNAMIC_PIN = "dynamic_pin"
PAIR_METHOD_STATIC_PIN = "static_pin"
PAIR_METHOD_TOKEN = "token"  # noqa: S105 - auth method name
# The consent step's choice between connecting straight away and pairing first.
CONF_CONNECT_METHOD = "connect_method"
CONNECT_METHOD_UNPAIRED = "unpaired"
CONNECT_METHOD_UNPAIRED_WITH_INPUT = "unpaired_with_input"
CONNECT_METHOD_UNPAIRED_WITHOUT_INPUT = "unpaired_without_input"
CONNECT_METHOD_PAIR = "pair"

# The setup flow step for a device whose audio input awaits a decision.
CONF_SOURCE_INPUT_ACTION = "source_input_action"
SOURCE_INPUT_PAIR = "pair"
SOURCE_INPUT_DISMISS = "dismiss"
# Persisted (raw player config) marker that the user declined the audio input.
CONF_SOURCE_INPUT_DECLINED = "source_input_declined"

CONF_PAIRING_PIN = "pairing_pin"

CONF_ACTION_UNPAIR = "unpair"
CONF_ACTION_SOURCE_INPUT_ENABLE = "source_input_enable"
CONF_ACTION_SOURCE_INPUT_DISABLE = "source_input_disable"

CONF_ACTION_MANAGEMENT_ENTER = "management_enter"
CONF_ACTION_MANAGEMENT_EXIT = "management_exit"
CONF_ACTION_MANAGEMENT_STATIC_PIN_ENABLE = "management_static_pin_enable"
CONF_ACTION_MANAGEMENT_STATIC_PIN_DISABLE = "management_static_pin_disable"
CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_ENABLE = "management_dynamic_pin_enable"
CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_DISABLE = "management_dynamic_pin_disable"

# The player a line-in starts playing on when its client senses a signal.
CONF_SOURCE_AUTOSTART_TARGET = "source_autostart_target"
SOURCE_AUTOSTART_OFF = "off"

# The name the user gives the line-in of a device that also plays.
CONF_SOURCE_NAME = "source_name"
DEFAULT_SOURCE_NAME = "Line-in"
