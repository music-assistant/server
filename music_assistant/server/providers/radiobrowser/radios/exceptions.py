"""Exceptions for the Radio Browser API."""


class RadioBrowserError(Exception):
    """Generic Radio Browser exception."""


class RadioBrowserConnectionError(RadioBrowserError):
    """Radio Browser connection exception."""


class RadioBrowserConnectionTimeoutError(RadioBrowserConnectionError):
    """Radio Browser connection Timeout exception."""
