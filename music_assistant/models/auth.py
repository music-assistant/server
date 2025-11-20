"""Authentication models for Music Assistant."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class UserRole(StrEnum):
    """User role enum."""

    ADMIN = "admin"
    USER = "user"


class AuthProviderType(StrEnum):
    """Authentication provider type enum."""

    BUILTIN = "builtin"
    HOME_ASSISTANT = "homeassistant"


@dataclass
class User:
    """User model - decoupled from auth providers."""

    user_id: str
    username: str
    role: UserRole
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    display_name: str | None = None
    avatar_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert User to dictionary."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role.value,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> User:
        """Create User from dictionary."""
        return cls(
            user_id=data["user_id"],
            username=data["username"],
            role=UserRole(data["role"]),
            enabled=data.get("enabled", True),
            created_at=datetime.fromisoformat(data["created_at"]),
            display_name=data.get("display_name"),
            avatar_url=data.get("avatar_url"),
        )


@dataclass
class UserAuthProvider:
    """Link between a User and an Authentication Provider."""

    link_id: str
    user_id: str
    provider_type: AuthProviderType
    provider_user_id: str  # The user ID from the provider (e.g., Google user ID)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        """Convert UserAuthProvider to dictionary."""
        return {
            "link_id": self.link_id,
            "user_id": self.user_id,
            "provider_type": self.provider_type.value,
            "provider_user_id": self.provider_user_id,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserAuthProvider:
        """Create UserAuthProvider from dictionary."""
        return cls(
            link_id=data["link_id"],
            user_id=data["user_id"],
            provider_type=AuthProviderType(data["provider_type"]),
            provider_user_id=data["provider_user_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )


@dataclass
class AuthToken:
    """Authentication token model."""

    token_id: str
    user_id: str
    token_hash: str
    name: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    last_used_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert AuthToken to dictionary."""
        return {
            "token_id": self.token_id,
            "user_id": self.user_id,
            "token_hash": self.token_hash,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthToken:
        """Create AuthToken from dictionary."""
        return cls(
            token_id=data["token_id"],
            user_id=data["user_id"],
            token_hash=data["token_hash"],
            name=data["name"],
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=(
                datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
            ),
            last_used_at=(
                datetime.fromisoformat(data["last_used_at"]) if data.get("last_used_at") else None
            ),
        )


@dataclass
class LoginProviderConfig:
    """Login provider configuration model."""

    provider_id: str
    provider_type: AuthProviderType
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert LoginProviderConfig to dictionary."""
        return {
            "provider_id": self.provider_id,
            "provider_type": self.provider_type.value,
            "enabled": self.enabled,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoginProviderConfig:
        """Create LoginProviderConfig from dictionary."""
        return cls(
            provider_id=data["provider_id"],
            provider_type=AuthProviderType(data["provider_type"]),
            enabled=data.get("enabled", True),
            config=data.get("config", {}),
        )
