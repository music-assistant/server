"""Configuration categories for Nicovideo provider."""

from .auth import AuthConfigCategory
from .base import ConfigCategoryBase
from .content import ContentConfigCategory
from .recommendations import RecommendationsConfigCategory

__all__ = [
    "AuthConfigCategory",
    "ConfigCategoryBase",
    "ContentConfigCategory",
    "RecommendationsConfigCategory",
]
