"""Exceptions for Soundtouch."""


class SoundtouchError(Exception):
    """Base exception."""


class ApiError(SoundtouchError):
    """ApiError."""


class NotFoundError(ApiError):
    """NotFoundError."""
