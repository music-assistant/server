# Webserver and Authentication Architecture

This document provides a comprehensive overview of the Music Assistant webserver architecture, authentication system, and remote access capabilities.

## Table of Contents

- [Overview](#overview)
- [Core Components](#core-components)
- [Authentication System](#authentication-system)
- [Remote Access (WebRTC)](#remote-access-webrtc)
- [Request Flow](#request-flow)
- [Security Considerations](#security-considerations)
- [Development Guide](#development-guide)

## Overview

The Music Assistant webserver is a core controller that provides:
- WebSocket-based real-time API for bidirectional communication
- HTTP/JSON-RPC API for simple request-response interactions
- User authentication and authorization system
- Frontend hosting (Vue-based PWA)
- Remote access via WebRTC for external connectivity
- Home Assistant integration via Ingress

The webserver runs on port `8095` by default and can be configured via the webserver controller settings.

## Core Components

### 1. WebserverController ([controller.py](controller.py))

The main orchestrator that manages:
- HTTP server setup and lifecycle
- Route registration (static files, API endpoints, auth endpoints)
- WebSocket client management
- Authentication manager initialization
- Remote access manager initialization
- Home Assistant Supervisor announcement (when running as add-on)

**Key responsibilities:**
- Serves the frontend application (PWA)
- Hosts the WebSocket API endpoint (`/ws`)
- Provides HTTP/JSON-RPC API endpoint (`/api`)
- Manages authentication routes (`/login`, `/auth/*`, `/setup`)
- Serves API documentation (`/api-docs`)
- Handles image proxy and audio preview endpoints

### 2. AuthenticationManager ([auth.py](auth.py))

Handles all authentication and user management:

**Database Schema:**
- `users` - User accounts with roles (admin/user)
- `user_auth_providers` - Links users to authentication providers (many-to-many)
- `auth_tokens` - Access tokens with expiration tracking
- `settings` - Schema version and configuration

**Authentication Providers:**
- **Built-in Provider** - Username/password authentication with bcrypt hashing
- **Home Assistant OAuth** - OAuth2 flow for Home Assistant users (auto-enabled when HA provider is configured)

**Token Types:**
- **Short-lived tokens**: Auto-renewing on use, 30-day sliding expiration window (for user sessions)
- **Long-lived tokens**: No auto-renewal, 10-year expiration (for integrations/API access)

**Security Features:**
- Rate limiting on login attempts (progressive delays)
- Password hashing with bcrypt and user- and server specific salts
- Secure token generation with secrets.token_urlsafe()
- WebSocket disconnect on token revocation
- Session management and cleanup

**User Roles:**
- `ADMIN` - Full access to all commands and settings
- `USER` - Standard access (configurable via player/provider filters)

### 3. RemoteAccessManager ([remote_access/](remote_access/))

Manages WebRTC-based remote access for external connectivity:

**Architecture:**
- **Signaling Server**: Cloud-based WebSocket server for WebRTC signaling (hosted at `wss://signaling.music-assistant.io/ws`)
- **WebRTC Gateway**: Local component that bridges WebRTC data channels to the WebSocket API
- **Remote ID**: Unique identifier (format: `MA-XXXX-XXXX`) for connecting to specific instances

**How it works:**
1. Remote access can be enabled regardless of Home Assistant Cloud subscription
2. A unique Remote ID is generated and stored in config
3. The gateway connects to the signaling server and registers with the Remote ID
4. Remote clients (PWA or mobile apps) connect via WebRTC using the Remote ID
5. Data channel messages are bridged to/from the local WebSocket API

**Connection Modes:**

- **Basic Mode** (default, no HA Cloud required):
  - Uses public STUN servers (Home Assistant, Google, Cloudflare)
  - Works in most network configurations
  - May not work behind complex NAT setups or corporate firewalls
  - Free for all users

- **Optimized Mode** (with HA Cloud subscription):
  - Uses Home Assistant Cloud STUN/TURN servers
  - Reliable connections in all network configurations
  - TURN relay servers ensure connectivity even in restrictive networks
  - Requires active Home Assistant Cloud subscription

**Key features:**
- Automatic reconnection on signaling server disconnect
- Multiple concurrent WebRTC sessions supported
- No port forwarding required
- End-to-end encryption via WebRTC (DTLS-SRTP)
- Automatic mode switching when HA Cloud status changes

### 4. WebSocket Client Handler ([websocket_client.py](websocket_client.py))

Manages individual WebSocket connections:
- Authentication enforcement (auth or login command must be first)
- Command routing and response handling
- Event subscription and broadcasting
- Connection lifecycle management
- Token validation and user context

### 5. Authentication Helpers

**Middleware ([helpers/auth_middleware.py](helpers/auth_middleware.py)):**
- Request authentication for HTTP endpoints
- User context management (thread-local storage)
- Ingress detection (Home Assistant add-on)
- Token extraction from Authorization header

**Providers ([helpers/auth_providers.py](helpers/auth_providers.py)):**
- Base classes for authentication providers
- Built-in username/password provider
- Home Assistant OAuth provider
- Rate limiting implementation

## Authentication System

### First-Time Setup Flow

1. **Initial State**: No users exist
2. **Setup Required**: User is redirected to `/setup`
3. **Admin Creation**: User creates the first admin account with username/password
4. **Setup completes** User gets redirected to the frontend
5. **Onboarding wizard** The frontend shows the onboarding wizard if it detects 'onboard_done' is False
4. **Onboarding Complete**: User completes onboarding and the `onboard_done` flag is set to `true`

### First-Time Setup Flow when HA Ingress is used

1. **Initial State**: No users exist
2. **Auto user creation**: User is auto created based on HA user
4. **Setup completes** User gets redirected to the frontend
5. **Onboarding wizard** The frontend shows the onboarding wizard if it detects 'onboard_done' is False
4. **Onboarding Complete**: User completes onboarding and the `onboard_done` flag is set to `true`

### Login Flow (Standard)

1. **Client Request**: POST to `/auth/login` with credentials
2. **Provider Authentication**: Credentials validated by authentication provider
3. **Token Generation**: Short-lived token created for the user
4. **Response**: Token and user info returned to frontend
5. **Subsequent Requests**: Token included in Authorization header or WebSocket auth command

### Login Flow (Home Assistant OAuth)

1. **Initiate OAuth**: GET `/auth/authorize?provider_id=homeassistant&return_url=...`
2. **Redirect to HA**: User is redirected to Home Assistant OAuth consent page
3. **OAuth Callback**: HA redirects back to `/auth/callback` with code and state
4. **Token Exchange**: Code exchanged for HA access token
5. **User Lookup/Creation**: User found or created with HA provider link
6. **Token Generation**: MA token created and returned via redirect with `code` parameter
7. **Client Handling**: Client extracts token from URL and stores it

### Remote Client OAuth Flow

For remote clients (PWA over WebRTC), OAuth requires special handling since redirect URLs can't point to localhost:

1. **Request Session**: Remote client calls `auth/authorization_url` with `for_remote_client=true`
2. **Session Created**: Server creates a pending OAuth session and returns session_id and auth URL
3. **User Opens Browser**: Client opens auth URL in system browser
4. **OAuth Flow**: User completes OAuth in browser
5. **Token Stored**: Server stores token in pending session (using special return URL format)
6. **Polling**: Client polls `auth/oauth_status` with session_id
7. **Token Retrieved**: Once complete, client receives token and can authenticate

### Ingress Authentication (Home Assistant Add-on)

When running as a Home Assistant add-on:
- A dedicated webserver TCP site is hosted (on port 8094) bound to the internal HA docker network only
- Ingress requests include HA user headers (`X-Remote-User-ID`, `X-Remote-User-Name`)
- Users are auto-created on first access
- No password required (authentication handled by HA)
- System user created for HA integration communication

### WebSocket Authentication

1. **Connection Established**: Client connects to `/ws`
2. **Auth Command Required**: First command must be `auth` with token
3. **Token Validation**: Token validated and user context set
4. **Authenticated Session**: All subsequent commands executed in user context
5. **Auto-Disconnect**: Connection closed on token revocation or user disable

## Remote Access (WebRTC)

### Architecture Overview

Remote access enables users to connect to their Music Assistant instance from anywhere without port forwarding or VPN:

```
[Remote Client (PWA or app)]
       |
       | WebRTC Data Channel
       v
[Signaling Server] ←→ [WebRTC Gateway]
                              |
                              | WebSocket
                              v
                      [Local WebSocket API]
```

### Components

**Signaling Server** (`wss://signaling.music-assistant.io/ws`):
- Cloud-based WebSocket server for WebRTC signaling
- Handles SDP offer/answer exchange
- Routes ICE candidates between peers
- Maintains Remote ID registry

**WebRTC Gateway** ([remote_access/gateway.py](remote_access/gateway.py)):
- Runs locally as part of the webserver controller
- Connects to signaling server and registers Remote ID
- Accepts incoming WebRTC connections from remote clients
- Bridges WebRTC data channel messages to local WebSocket API
- Handles multiple concurrent sessions

**Remote ID**:
- Format: `MA-XXXX-XXXX` (e.g., `MA-K7G3-P2M4`)
- Uniquely identifies a Music Assistant instance
- Generated once and stored in controller config
- Used by remote clients to connect to specific instance

### Connection Flow

1. **Initialization**:
   - Remote access is enabled by user in settings
   - Remote ID generated/retrieved from config
   - HA Cloud status checked (determines mode)
   - Gateway connects to signaling server with appropriate ICE servers
   - Remote ID registered with signaling server

2. **Remote Client Connection**:
   - User opens PWA (https://app.music-assistant.io) and enters Remote ID
   - PWA creates WebRTC peer connection
   - PWA sends SDP offer via signaling server
   - Gateway receives offer and creates peer connection
   - Gateway sends SDP answer via signaling server
   - ICE candidates exchanged for NAT traversal
   - WebRTC data channel established

3. **Message Bridging**:
   - Remote client sends WebSocket-format messages over data channel
   - Gateway forwards messages to local WebSocket API
   - Responses and events sent back through data channel
   - Authentication and authorization work identically to local WebSocket

### ICE Servers (STUN/TURN)

NAT traversal is critical for WebRTC connections. Music Assistant uses:

- **STUN servers**: Servers for discovering public IP addresses and port mappings
- **TURN servers**: Relay servers for cases where direct peer-to-peer connection fails

**Basic Mode (Public STUN):**
- `stun:stun.home-assistant.io:3478` (Home Assistant public STUN)
- `stun:stun.l.google.com:19302` (Google public STUN)
- `stun:stun1.l.google.com:19302` (Google public STUN backup)
- `stun:stun.cloudflare.com:3478` (Cloudflare public STUN)

Most connections succeed with public STUN servers alone, but they may fail in:
- Symmetric NAT configurations
- Corporate firewalls that block UDP
- Networks with restrictive firewall policies

**Optimized Mode (HA Cloud):**
- STUN/TURN servers provided by Home Assistant Cloud
- Includes TURN relay servers for guaranteed connectivity

### Availability

Remote access is available to all users:
- **Basic Mode**: Always available, no subscription required
- **Optimized Mode**: Requires active Home Assistant Cloud subscription

### API Endpoints

**`remote_access/info`** (WebSocket command):
Returns remote access status:
```json
{
  "enabled": true,
  "running": true,
  "connected": true,
  "remote_id": "MA-K7G3-P2M4",
  "using_ha_cloud": false,
  "signaling_url": "wss://signaling.music-assistant.io/ws"
}
```

**`remote_access/configure`** (WebSocket command, admin only):
Enable or disable remote access:
```json
{
  "enabled": true
}
```

## Request Flow

### HTTP Request Flow

```
HTTP Request → Webserver → Auth Middleware → Command Handler → Response
                                |
                                ├─ Ingress? → Auto-authenticate with HA headers
                                └─ Regular? → Validate Bearer token
```

### WebSocket Request Flow

```
WebSocket Connect → WebsocketClientHandler
                           |
                           ├─ First command: auth → Validate token → Set user context
                           └─ Subsequent commands → Check auth/role → Execute → Respond
```

### Remote WebRTC Request Flow

```
Remote Client → WebRTC Data Channel → Gateway → Local WebSocket API
                                         |
                                         └─ Message forwarding (bidirectional)
```

## Security Considerations

### Authentication

- **Mandatory authentication**: All API access requires authentication (except Ingress)
- **Secure token generation**: Uses `secrets.token_urlsafe(48)` for cryptographically secure tokens
- **Password hashing**: bcrypt with user-specific salts
- **Rate limiting**: Progressive delays on failed login attempts
- **Token expiration**: Both short-lived (30 days sliding) and long-lived (10 years) tokens supported

### Authorization

- **Role-based access**: Admin vs User roles
- **Command-level enforcement**: API commands can require specific roles
- **Player/Provider filtering**: Users can be restricted to specific players/providers
- **Token revocation**: Immediate WebSocket disconnect on token revocation

### Network Security

**Local Network:**
- Webserver is unencrypted (HTTP) by design (runs on local network)
- Users should use reverse proxy or VPN for external access
- Never expose webserver directly to internet

**Remote Access:**
- End-to-end encryption via WebRTC (DTLS/SRTP)
- Authentication required (same as local access)
- Signaling server only routes encrypted signaling messages
- Cannot decrypt or inspect user data

### Data Protection

- **Token storage**: Only hashed tokens stored in database
- **Password storage**: bcrypt with user-specific salts
- **Session cleanup**: Expired tokens automatically deleted
- **User disable**: Immediate disconnect of all user sessions

## Development Guide

### Adding New Authentication Providers

1. Create provider class inheriting from `LoginProvider` in [helpers/auth_providers.py](helpers/auth_providers.py)
2. Implement required methods: `authenticate()`, `get_authorization_url()` (if OAuth), `handle_oauth_callback()` (if OAuth)
3. Register provider in `AuthenticationManager._setup_login_providers()`
4. Add provider configuration to webserver config entries if needed

### Adding New API Endpoints

1. Define route handler in [controller.py](controller.py) (for HTTP endpoints)
2. Use `@api_command()` decorator for WebSocket commands (in respective controllers)
3. Specify authentication requirements: `authenticated=True` or `required_role="admin"`

### Testing Authentication

1. **Local Testing**: Use `/setup` to create admin user, then `/auth/login` to get token
2. **HTTP API Testing**: Use curl with `Authorization: Bearer <token>` header
3. **WebSocket Testing**: Connect to `/ws` and send auth command with token
4. **Role Testing**: Create users with different roles and test access restrictions

### Common Patterns

**Getting current user in command handler:**
```python
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user

@api_command("my_command")
async def my_command():
    user = get_current_user()
    if not user:
        raise AuthenticationRequired("Not authenticated")
    # ... use user ...
```

**Getting current token (for revocation):**
```python
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_token

@api_command("my_command")
async def my_command():
    token = get_current_token()
    # ... use token ...
```

**Requiring admin role:**
```python
@api_command("admin_only_command", required_role="admin")
async def admin_command():
    # Only admins can call this
    pass
```

### Database Migrations

When modifying the auth database schema:
1. Increment `DB_SCHEMA_VERSION` in [auth.py](auth.py)
2. Add migration logic to `_migrate_database()` method
3. Test migration from previous version
4. Consider backwards compatibility

### Testing Remote Access

1. **Enable Remote Access**: Toggle remote access in settings UI or via API
2. **Verify Remote ID**: Check webserver config for generated Remote ID
3. **Test Gateway**: Check logs for "Starting remote access in basic/optimized mode" message
4. **Test Connection**: Use PWA with Remote ID to connect externally
5. **Monitor Sessions**: Check `remote_access/info` command for status and mode
6. **Test Mode Switching**: Enable/disable HA Cloud and verify automatic mode switching

## File Structure

```
webserver/
├── __init__.py                         # Module exports
├── controller.py                       # Main webserver controller
├── auth.py                             # Authentication manager
├── websocket_client.py                 # WebSocket client handler
├── api_docs.py                         # API documentation generator
├── README.md                           # This file
├── helpers/
│   ├── auth_middleware.py              # HTTP auth middleware
│   └── auth_providers.py               # Authentication providers
└── remote_access/
    ├── __init__.py                     # Remote access manager
    └── gateway.py                      # WebRTC gateway implementation
```

## Additional Resources

- [API Documentation](http://localhost:8095/api-docs) - Auto-generated API docs
- [Commands Reference](http://localhost:8095/api-docs/commands) - List of all API commands
- [Schemas Reference](http://localhost:8095/api-docs/schemas) - Data model documentation
- [Swagger UI](http://localhost:8095/api-docs/swagger) - Interactive API explorer

## Contributing

When contributing to the webserver/auth system:
1. Follow the existing patterns for consistency
2. Add comprehensive docstrings with Sphinx-style parameter documentation
3. Update this README if adding significant new features
4. Test authentication flows thoroughly
5. Consider security implications of all changes
6. Update API documentation if adding new commands
