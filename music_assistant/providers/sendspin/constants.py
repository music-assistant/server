"""Constants for the Sendspin provider."""

from __future__ import annotations

BRIDGE_PREFIX = "spb_"
VIRTUAL_PLAYER_ID_PREFIX = "sendspin_virtual_"

CONF_CAST_AUDIO_UNSUPPORTED = "cast_audio_unsupported"
CONF_SENDSPIN_STATIC_DELAY = "sendspin_static_delay"
CONF_VIRTUAL_PLAYER_OWNER = "virtual_player_owner"
DEFAULT_SENDSPIN_STATIC_DELAY = 0

CONF_ALLOW_UNENCRYPTED = "allow_unencrypted"
CONF_MIN_PIN_LENGTH = "min_pin_length"
DEFAULT_MIN_PIN_LENGTH = 6

CONF_PAIRING_TOKEN = "pairing_token"
CONF_PAIRING_PIN = "pairing_pin"
CONF_ACTION_PAIR_TOKEN = "pair_token"
CONF_ACTION_PAIR_TOKEN_SUBMIT = "pair_token_submit"
CONF_ACTION_PAIR_TOKEN_CANCEL = "pair_token_cancel"
CONF_ACTION_PAIR_PIN_START = "pair_pin_start"
CONF_ACTION_PAIR_STATIC_PIN_START = "pair_static_pin_start"
CONF_ACTION_PAIR_PIN_SUBMIT = "pair_pin_submit"
CONF_ACTION_PAIR_PIN_RETRY = "pair_pin_retry"
CONF_ACTION_PAIR_PIN_CANCEL = "pair_pin_cancel"
CONF_ACTION_VERIFY_PIN_START = "verify_pin_start"
CONF_ACTION_UNPAIR = "unpair"
CONF_ACTION_ALLOW_UNPAIRED = "allow_unpaired"
CONF_ACTION_REVOKE_UNPAIRED = "revoke_unpaired"

CONF_ACTION_MANAGEMENT_ENTER = "management_enter"
CONF_ACTION_MANAGEMENT_EXIT = "management_exit"
CONF_ACTION_MANAGEMENT_UNPAIRED_ENABLE = "management_unpaired_enable"
CONF_ACTION_MANAGEMENT_UNPAIRED_DISABLE = "management_unpaired_disable"
CONF_ACTION_MANAGEMENT_STATIC_PIN_ENABLE = "management_static_pin_enable"
CONF_ACTION_MANAGEMENT_STATIC_PIN_DISABLE = "management_static_pin_disable"
CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_ENABLE = "management_dynamic_pin_enable"
CONF_ACTION_MANAGEMENT_DYNAMIC_PIN_DISABLE = "management_dynamic_pin_disable"
