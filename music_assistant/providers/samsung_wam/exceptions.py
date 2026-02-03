"""Custom exceptions for the Samsung WAM provider."""


class PlayerDisabledError(Exception):
    """Signal that the player is disabled in configuration and setup should be aborted."""
