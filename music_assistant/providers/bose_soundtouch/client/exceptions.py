"""Exceptions for Soundtouch."""

from aiohttp import ClientError


class SoundtouchError(ClientError):
    """Base exception."""


class ApiError(SoundtouchError):
    """ApiError."""


class NotFoundError(ApiError):
    """NotFoundError."""
