# Generic Provider Features

This document describes the generic features that have been extracted from party mode and made available for any provider to use. These features were originally implemented specifically for party mode but have been refactored to be reusable.

## Overview

The goal of this refactoring is to:
1. Keep provider-specific logic in the provider code
2. Make useful features available to all providers in a clean, generic way
3. Allow the party mode feature to be merged as a series of smaller, focused PRs

---

## 1. Short Code Authentication

**Status:** Complete
**Server Branch:** `feature/generic-short-code-auth`
**Frontend Branch:** `party-mode-refactor-generic`

### What It Does

Allows providers to generate short alphanumeric codes (e.g., "ABC123") that users can enter or scan via QR code to authenticate. The code is exchanged for a JWT token.

### Use Cases

- **Party Mode:** Guests scan a QR code to join and add songs to the queue
- **Device Pairing:** Pair a new device by entering a code shown on another device
- **Kiosk Mode:** Allow limited access via a simple code
- **Temporary Access:** Grant time-limited access to specific users

### API

#### Generate a Join Code

```python
from music_assistant.controllers.webserver.auth import AuthenticationManager

# Provider creates/manages its own user
user = await auth.get_user_by_username("my_provider_guest")
if not user:
    user = await auth.create_user(
        username="my_provider_guest",
        role=UserRole.GUEST,  # or USER, depending on needs
        display_name="My Provider Guest",
    )

# Generate a short code for that user
code, expires_at = await auth.generate_join_code(
    user_id=user.user_id,
    provider_name="my_provider",  # Stored in JWT for identification
    expires_in_hours=24,          # Default: 24 hours
    max_uses=0,                   # 0 = unlimited, or set a limit
    device_name="My Device",      # Shows up in token list
)

# Build a URL for the user
url = f"https://app.music-assistant.io/?join={code}"
```

#### Exchange a Code for Token (Public API)

The `auth/code` endpoint is public (no authentication required):

```javascript
// Frontend JavaScript
const result = await api.sendCommand("auth/code", {
    code: "ABC123"
});

if (result.success) {
    // result.access_token contains the JWT
    // result.user contains { user_id, username, role }
}
```

#### Revoke Join Codes

```python
# Revoke all codes for a specific user
count = await auth.revoke_join_codes(user_id=user.user_id)

# Revoke ALL join codes (use carefully)
count = await auth.revoke_join_codes()
```

### JWT Token Claims

When a code is exchanged, the resulting JWT includes:

```json
{
    "sub": "user_id",
    "username": "my_provider_guest",
    "role": "guest",
    "provider_name": "my_provider",
    "token_name": "My Device",
    "is_long_lived": false,
    "exp": 1234567890
}
```

The `provider_name` claim allows providers to identify sessions created through their join codes.

### Frontend Detection

```typescript
import { authManager } from "@/plugins/auth";

// Check if this session was created by a specific provider
if (authManager.getClaim("provider_name") === "party_mode") {
    // Show party mode UI
}

// Built-in helper for party mode
if (authManager.isPartyModeGuest()) {
    // Redirect to guest view
}
```

### Database Schema

The `join_codes` table (schema version 6):

| Column | Type | Description |
|--------|------|-------------|
| code_id | TEXT | Primary key |
| code_hash | TEXT | SHA256 hash of the code |
| user_id | TEXT | User who will own tokens from this code |
| created_at | TEXT | ISO timestamp |
| expires_at | TEXT | ISO timestamp |
| max_uses | INTEGER | 0 = unlimited |
| use_count | INTEGER | Current usage count |
| last_used_at | TEXT | ISO timestamp |
| device_name | TEXT | Name for created tokens |
| provider_name | TEXT | Provider identifier |

### Example: Party Mode Implementation

```python
# In party_mode/__init__.py

PARTY_GUEST_USERNAME = "party_guest"

async def _get_or_create_party_guest_user(self) -> str:
    auth = self.mass.webserver.auth
    user = await auth.get_user_by_username(PARTY_GUEST_USERNAME)
    if user:
        return user.user_id

    user = await auth.create_user(
        username=PARTY_GUEST_USERNAME,
        role=UserRole.GUEST,
        display_name="Party Guest",
    )
    return user.user_id

async def get_party_mode_url(self) -> dict:
    guest_user_id = await self._get_or_create_party_guest_user()

    code, expires_at = await self.mass.webserver.auth.generate_join_code(
        user_id=guest_user_id,
        provider_name="party_mode",
        expires_in_hours=24,
        max_uses=0,
        device_name="Party Mode Guest",
    )

    return {
        "url": f"https://app.music-assistant.io/?join={code}",
        "code": code,
        "expires_at": expires_at.isoformat(),
    }
```

---

## 2. Guest Role & Permissions

**Status:** Complete (in jukebox-view branch)
**Server Branch:** Part of `party-mode-refactor-generic`

### What It Does

Defines a `GUEST` user role with limited permissions. Guests can browse the library and control playback but cannot configure settings or manage users.

### Guest Permissions

```python
# From music_assistant/helpers/permissions.py

if role == UserRole.GUEST:
    return [
        Permission.LIBRARY_READ,
        Permission.PLAYERS_READ,
        Permission.PLAYERS_CONTROL,
        Permission.STREAMS_READ,
        Permission.STREAMS_CONTROL,
    ]
```

Guests **cannot**:
- Configure players or providers
- Manage users
- Delete content
- Access admin settings

### Usage

```python
from music_assistant.helpers.permissions import has_permission, Permission

if has_permission(user, Permission.PLAYERS_CONTROL):
    # User can control playback
    pass
```

---

## 3. Queue Modifier Hooks

**Status:** Complete
**Server Branch:** `feature/queue-modifier-hooks` (builds on `feature/generic-short-code-auth`)

### What It Does

Provides a `QueueModifier` protocol that allows providers to customize queue behavior without hardcoding provider-specific logic in the core controller.

### Queue User Tracking (Built-in)

The core controller tracks which user added each item to the queue via `extra_attributes`:

- `added_by_user_id` - The user who added the item
- `added_by_user_role` - The role of that user (for priority logic)
- `queue_option` - How the item was added ("play", "next", "add")

### QueueModifier Protocol

```python
from music_assistant.controllers.player_queues import QueueModifier

class QueueModifier(Protocol):
    """Protocol for queue behavior modifiers."""

    def modify_enqueue_option(
        self,
        queue_id: str,
        option: QueueOption | None,
        user: User | None,
        queue_state: PlaybackState,
    ) -> QueueOption | None:
        """Optionally modify the queue option before processing."""
        ...

    def calculate_insert_index(
        self,
        queue_id: str,
        items: list[QueueItem],
        user: User | None,
        current_index: int | None,
        queue_length: int,
    ) -> int | None:
        """Calculate custom insert index for ADD operations."""
        ...

    def should_shuffle_items(
        self,
        queue_id: str,
        items: list[QueueItem],
        user: User | None,
    ) -> bool:
        """Determine if items should be shuffled."""
        ...

    def get_protected_item_ids(
        self,
        queue_id: str,
        items: list[QueueItem],
    ) -> set[str]:
        """Get item IDs that should be protected from shuffle."""
        ...
```

### Registration

```python
# In your provider's loaded_in_mass method
async def loaded_in_mass(self) -> None:
    self.mass.player_queues.register_queue_modifier(self.instance_id, self)

# In your provider's unload method
async def unload(self, is_removed: bool = False) -> None:
    self.mass.player_queues.unregister_queue_modifier(self.instance_id)
```

### Example: Party Mode Implementation

```python
class PartyModePlugin(PluginProvider):
    """Party Mode plugin implementing guest priority queue."""

    def modify_enqueue_option(
        self,
        queue_id: str,
        option: QueueOption | None,
        user: User | None,
        queue_state: PlaybackState,
    ) -> QueueOption | None:
        # Auto-start playback when guest adds to idle queue
        if (
            user
            and user.role == UserRole.GUEST
            and queue_state != PlaybackState.PLAYING
            and option in (QueueOption.ADD, QueueOption.NEXT)
        ):
            return QueueOption.PLAY
        return None

    def calculate_insert_index(
        self,
        queue_id: str,
        items: list[QueueItem],
        user: User | None,
        current_index: int | None,
        queue_length: int,
    ) -> int | None:
        # Guest items inserted at end of guest section (priority queue)
        if not user or user.role != UserRole.GUEST:
            return None
        return self._find_guest_section_end(queue_id, current_index, queue_length)

    def should_shuffle_items(
        self,
        queue_id: str,
        items: list[QueueItem],
        user: User | None,
    ) -> bool:
        # Don't shuffle guest items to maintain request order
        return not (user and user.role == UserRole.GUEST)

    def get_protected_item_ids(
        self,
        queue_id: str,
        items: list[QueueItem],
    ) -> set[str]:
        # Protect guest-added items from shuffle
        return {
            item.queue_item_id
            for item in items
            if item.extra_attributes.get("added_by_user_role") == UserRole.GUEST.value
        }
```

---

## 4. WebSocket Disconnection

**Status:** Complete (already in dev)

### What It Does

Allows disconnecting WebSocket connections by user or token, useful for revoking access.

### API

```python
# Disconnect all connections for a user
self.mass.webserver.disconnect_websockets_for_user(user_id)

# Disconnect connections using a specific token
self.mass.webserver.disconnect_websockets_for_token(token_id)
```

---

## Branch Structure

```
dev (upstream)
├── feature/generic-short-code-auth    # PR 1: Generic short code auth
│   └── feature/queue-modifier-hooks   # PR 2: Queue modifier system
│       └── party-mode-refactor-generic # PR 3: Party mode provider
│
└── jukebox-view (original working party mode, for reference)
```

## PR Strategy

1. **PR 1:** `feature/generic-short-code-auth` → `dev`
   - Generic short code authentication
   - No party mode specific code
   - **Status:** Ready for review

2. **PR 2:** `feature/queue-modifier-hooks` → `dev` (after PR 1 merged)
   - QueueModifier protocol for provider-driven queue behavior
   - Generic user tracking in queue items (built-in)
   - No hardcoded guest priority logic
   - **Status:** Ready for review (rebased on PR 1)

3. **PR 3:** `party-mode-refactor-generic` → `dev` (after PR 2 merged)
   - Party Mode plugin provider
   - Implements QueueModifier for guest priority queue
   - Uses generic short code auth for guest access
   - All party-mode-specific logic in the provider
   - **Status:** Ready for review (rebased on PR 2)

---

## Changelog

| Date | Change |
|------|--------|
| 2026-02-01 | Created document, documented short code auth |
| 2026-02-01 | Added Queue Modifier Hooks system (section 3) |
| 2026-02-01 | Updated branch structure and PR strategy |
