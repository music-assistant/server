"""Tests for websocket API command authorization."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.access import PlaylistAccess
from music_assistant_models.api import CommandMessage, ErrorResultMessage
from music_assistant_models.auth import Scope, User, UserRole
from music_assistant_models.enums import EventType, FlowStepType, ProviderSharing
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import Playlist, ProviderMapping
from music_assistant_models.setup_flow import SetupFlowStep

from music_assistant.controllers.config.flows import SetupFlowAccess, SetupFlowMixin
from music_assistant.controllers.config.providers import ProviderConfigMixin
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    custom_role_scopes,
    get_current_client_id,
    get_current_token,
    get_current_user,
    set_current_client_id,
    set_current_token,
    set_current_user,
    set_custom_role_scopes,
)
from music_assistant.controllers.webserver.websocket_client import WebsocketClientHandler
from music_assistant.helpers.api import APICommandHandler


async def _noop_command() -> None:
    """Test command target."""


def _create_client(
    user_role: str | None, handler: APICommandHandler, user_id: str = "user_1"
) -> Any:
    """
    Create a minimally wired websocket client handler for dispatch tests.

    :param user_role: Role id of the connection's user, None for an unauthenticated socket.
    :param handler: The single command handler the mocked server serves.
    :param user_id: User id of the connection's user.
    """
    client: Any = WebsocketClientHandler.__new__(WebsocketClientHandler)
    client._logger = MagicMock()
    client.mass = MagicMock()
    client.mass.command_handlers = {handler.command: handler}
    # close the coroutine passed to create_task to avoid "never awaited" warnings
    client.mass.create_task = MagicMock(side_effect=lambda coro, *_: coro.close())
    client._authenticated_user = (
        User(user_id=user_id, username="tester", role=user_role) if user_role else None
    )
    client._current_token = "token" if user_role else None
    client._sendspin_player_id = None
    client.client_id = "test_client"
    client._send_message = AsyncMock()
    return client


def _command_handler(
    required_scope: Scope | tuple[Scope, ...] | None = None,
    authenticated: bool = True,
) -> APICommandHandler:
    """Create an API command handler with the given auth requirements."""
    return APICommandHandler.parse(
        "test/protected",
        _noop_command,
        authenticated=authenticated,
        required_scope=required_scope,
    )


def _sent_error_code(client: Any) -> str | None:
    """Return the error code of the first sent error message, if any."""
    for call in client._send_message.await_args_list:
        message = call.args[0]
        if isinstance(message, ErrorResultMessage):
            return str(message.error_code)
    return None


def _sent_error_details(client: Any) -> str | None:
    """Return the message text of the first sent error message, if any."""
    for call in client._send_message.await_args_list:
        message = call.args[0]
        if isinstance(message, ErrorResultMessage):
            return message.details
    return None


def _subscribed_client(
    user_role: str | None, user_id: str = "user_1", access: SetupFlowAccess | None = None
) -> Any:
    """
    Create a client that ran the real event subscription, ready to be fed events.

    :param user_role: Role id of the connection's user, None for an unauthenticated socket.
    :param user_id: User id of the connection's user.
    :param access: The access record the server resolves every setup flow to.
    """
    client = _create_client(user_role, _command_handler(), user_id=user_id)
    client._events_unsub_callback = None
    client._hidden_playlists = set()
    client._send_message_sync = MagicMock()
    client.mass.config.get_setup_flow_access = MagicMock(return_value=access)
    client._subscribe_to_events()
    return client


def _sent_events(client: Any, event: MassEvent) -> list[MassEvent]:
    """Feed the event to the handler the client subscribed with, returning what it sent for it."""
    client._send_message_sync.reset_mock()
    client.mass.subscribe.call_args.args[0](event)
    return [call.args[0] for call in client._send_message_sync.call_args_list]


def _flow_event(flow_id: str = "flow1") -> MassEvent:
    """Return a setup flow step event for the given flow."""
    return MassEvent(
        event=EventType.SETUP_FLOW_UPDATED,
        object_id=flow_id,
        data=SetupFlowStep(flow_id=flow_id, step_id="credentials", type=FlowStepType.FORM),
    )


PERMISSION_DENIED = str(InsufficientPermissions.error_code)

# the commands a member may run on the music sources it owns
SELF_SERVICE_COMMANDS = [
    pytest.param(ProviderConfigMixin.invoke_provider_config_action, id="invoke_action"),
    pytest.param(ProviderConfigMixin.save_provider_config, id="save"),
    pytest.param(ProviderConfigMixin.set_provider_access, id="set_access"),
    pytest.param(ProviderConfigMixin.remove_provider_config, id="remove"),
    pytest.param(ProviderConfigMixin._reload_provider, id="reload"),
    pytest.param(SetupFlowMixin.setup_provider, id="setup"),
    pytest.param(SetupFlowMixin.reconfigure_provider, id="reconfigure"),
]

# the owner of the flow the member-owned setup flow tests resolve
FLOW_OWNER = "user_1"

# the custom roles the scope tests sign in as, by role id
CUSTOM_ROLES = {
    "librarian": [Scope.LIBRARY_WRITE],
    "player_tinkerer": [Scope.CONFIG_PLAYERS_WRITE],
}

# who may ask which members a music source or a playlist can be shared with
SHARE_CANDIDATE_CALLERS = [
    pytest.param(UserRole.ADMIN, True, id="admin"),
    pytest.param(UserRole.USER, True, id="user"),
    pytest.param(UserRole.SERVICE, True, id="service"),
    pytest.param(UserRole.GUEST, False, id="guest"),
    pytest.param("librarian", True, id="custom_role_with_library_write"),
    pytest.param("player_tinkerer", False, id="custom_role_without_either_scope"),
]


@pytest.fixture
def custom_roles() -> Generator[None]:
    """Grant the custom roles the scope tests sign in as, dropping them again afterwards."""
    set_custom_role_scopes(
        {role_id: custom_role_scopes(scopes) for role_id, scopes in CUSTOM_ROLES.items()}
    )
    yield
    set_custom_role_scopes({})


@pytest.mark.asyncio
async def test_user_scoped_command_rejects_guest() -> None:
    """A guest must not be able to invoke commands gated on a user-only scope."""
    client = _create_client(UserRole.GUEST, _command_handler(required_scope=Scope.USERS_INVITE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) == PERMISSION_DENIED
    client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.USER, UserRole.ADMIN])
async def test_user_scoped_command_allows_user_and_admin(role: UserRole) -> None:
    """Users and admins hold the USERS_INVITE scope used by host commands."""
    client = _create_client(role, _command_handler(required_scope=Scope.USERS_INVITE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.USER, UserRole.GUEST])
async def test_admin_scoped_command_rejects_non_admin(role: UserRole) -> None:
    """Only admins hold admin-only scopes such as USERS_MANAGE."""
    client = _create_client(role, _command_handler(required_scope=Scope.USERS_MANAGE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) == PERMISSION_DENIED
    client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", SELF_SERVICE_COMMANDS)
@pytest.mark.parametrize("role", [UserRole.GUEST, UserRole.SERVICE])
async def test_self_service_command_rejects_a_guest_and_a_service_account(
    role: UserRole, command: Any
) -> None:
    """
    Adding and managing your own music sources is off limits for a guest and a service account.

    :param role: The role of the calling user.
    :param command: The command handler function to dispatch.
    """
    scope = getattr(command, "api_required_scope", None)
    assert scope is Scope.CONFIG_PROVIDERS_OWN
    client = _create_client(role, _command_handler(required_scope=scope))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) == PERMISSION_DENIED
    client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.USER])
async def test_self_service_command_allows_admin_and_user(role: str) -> None:
    """
    An admin and a regular user both reach the command.

    :param role: Role id of the calling user.
    """
    client = _create_client(role, _command_handler(required_scope=Scope.CONFIG_PROVIDERS_OWN))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("custom_roles")
@pytest.mark.parametrize(("role", "allowed"), SHARE_CANDIDATE_CALLERS)
async def test_whoever_may_share_reaches_the_share_candidates(role: str, allowed: bool) -> None:
    """
    Every caller that may share a music source or a playlist reaches the share candidates.

    :param role: Role id of the calling user.
    :param allowed: Whether the role holds one of the scopes the command lists.
    """
    scope = getattr(ProviderConfigMixin.get_share_candidates, "api_required_scope", None)
    assert scope == (Scope.CONFIG_PROVIDERS_OWN, Scope.LIBRARY_WRITE)
    client = _create_client(role, _command_handler(required_scope=scope))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    if allowed:
        assert _sent_error_code(client) is None
        client.mass.create_task.assert_called_once()
    else:
        assert _sent_error_code(client) == PERMISSION_DENIED
        client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_an_any_of_scoped_command_allows_a_holder_of_either_scope() -> None:
    """A command listing several scopes runs for a caller holding one of them, and names them."""
    scopes = (Scope.USERS_INVITE, Scope.USERS_MANAGE)
    client = _create_client(UserRole.USER, _command_handler(required_scope=scopes))
    guest = _create_client(UserRole.GUEST, _command_handler(required_scope=scopes))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))
    await guest._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()
    assert _sent_error_code(guest) == PERMISSION_DENIED
    assert _sent_error_details(guest) == (
        "This command requires the users.invite or users.manage scope"
    )
    guest.mass.create_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.USER, UserRole.GUEST])
async def test_users_read_command_rejects_non_service(role: UserRole) -> None:
    """Reading user accounts is off limits for regular users and guests."""
    client = _create_client(role, _command_handler(required_scope=Scope.USERS_READ))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) == PERMISSION_DENIED
    client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.SERVICE, UserRole.ADMIN])
async def test_users_read_command_allows_service_and_admin(role: UserRole) -> None:
    """The Home Assistant integration runs as a service account and may read user accounts."""
    client = _create_client(role, _command_handler(required_scope=Scope.USERS_READ))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_authenticated_command_without_scope_allows_guest() -> None:
    """Guests may invoke authenticated commands that require no specific scope."""
    client = _create_client(UserRole.GUEST, _command_handler(required_scope=None))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    client.mass.create_task.assert_called_once()


@pytest.mark.asyncio
async def test_scoped_command_rejects_unauthenticated_socket() -> None:
    """Without authentication a scoped command must not run at all."""
    client = _create_client(None, _command_handler(required_scope=Scope.USERS_INVITE))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is not None
    client.mass.create_task.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [True, False])
async def test_command_sets_client_id_in_context(authenticated: bool) -> None:
    """
    Every dispatched command exposes the connection's client id, authenticated or not.

    Unauthenticated handlers such as the join code exchange throttle per connection and
    would otherwise see the id of whatever ran on this connection before them.

    :param authenticated: Whether the dispatched command requires authentication.
    """
    set_current_client_id("stale_client")
    role = UserRole.GUEST if authenticated else None
    client = _create_client(role, _command_handler(authenticated=authenticated))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    assert get_current_client_id() == "test_client"


@pytest.mark.asyncio
async def test_unauthenticated_command_does_not_inherit_a_user() -> None:
    """An unauthenticated command must not see the user of a command that ran before it."""
    set_current_user(User(user_id="user_1", username="tester", role=UserRole.ADMIN))
    set_current_token("stale_token")
    client = _create_client(None, _command_handler(authenticated=False))

    await client._handle_command(CommandMessage(message_id="1", command="test/protected"))

    assert _sent_error_code(client) is None
    assert get_current_user() is None
    assert get_current_token() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "role", "delivered"),
    [
        (FLOW_OWNER, UserRole.USER, True),
        ("user_2", UserRole.USER, False),
        ("admin", UserRole.ADMIN, True),
        ("user_2", UserRole.GUEST, False),
        ("user_2", None, False),
    ],
    ids=["owner", "another_member", "admin", "guest", "unauthenticated"],
)
async def test_a_member_setup_flow_step_reaches_only_its_owner(
    user_id: str, role: str | None, delivered: bool
) -> None:
    """
    The steps of a member's setup flow go to that member and to an admin, to nobody else.

    :param user_id: User id of the connection's user.
    :param role: Role id of the connection's user, None for an unauthenticated socket.
    :param delivered: Whether the step is expected to reach this client.
    """
    access = SetupFlowAccess(Scope.CONFIG_PROVIDERS_OWN, FLOW_OWNER)
    client = _subscribed_client(role, user_id=user_id, access=access)
    event = _flow_event()

    assert _sent_events(client, event) == ([event] if delivered else [])


@pytest.mark.asyncio
async def test_a_server_started_setup_flow_step_reaches_every_member() -> None:
    """A setup flow without an owner is served to anyone holding the scope it started with."""
    access = SetupFlowAccess(Scope.CONFIG_PROVIDERS_OWN)
    client = _subscribed_client(UserRole.USER, user_id="user_2", access=access)
    event = _flow_event()

    assert _sent_events(client, event) == [event]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "delivered"),
    [(UserRole.ADMIN, True), (UserRole.USER, False)],
    ids=["admin", "member"],
)
async def test_an_unknown_setup_flow_step_reaches_only_an_admin(role: str, delivered: bool) -> None:
    """
    A step of a flow that can no longer be resolved is held back from everyone but an admin.

    :param role: Role id of the connection's user.
    :param delivered: Whether the step is expected to reach this client.
    """
    client = _subscribed_client(role, access=None)
    event = _flow_event()

    assert _sent_events(client, event) == ([event] if delivered else [])


@pytest.mark.asyncio
async def test_other_events_are_forwarded_untouched() -> None:
    """An event that is no setup flow step still reaches a member as it was signalled."""
    client = _subscribed_client(UserRole.USER)
    event = MassEvent(event=EventType.PLAYER_UPDATED, object_id="player_1", data=None)

    assert _sent_events(client, event) == [event]


def _playlist_event(
    access: PlaylistAccess | None, event: EventType = EventType.MEDIA_ITEM_UPDATED
) -> MassEvent:
    """
    Return a media item event about a Music Assistant playlist with the given access record.

    :param access: The access record of the playlist, None for a household playlist.
    :param event: The event type to signal the playlist with.
    """
    playlist = Playlist(
        item_id="1",
        provider="library",
        name="Mine",
        provider_mappings={
            ProviderMapping(item_id="mine", provider_domain="builtin", provider_instance="builtin")
        },
        access=access,
    )
    return MassEvent(event=event, object_id=playlist.uri, data=playlist)


@pytest.mark.parametrize(
    ("access", "user_role", "user_id", "delivered"),
    [
        pytest.param(None, UserRole.USER, "user_2", "event", id="household"),
        pytest.param(PlaylistAccess(owner="user_1"), UserRole.USER, "user_1", "event", id="owner"),
        pytest.param(PlaylistAccess(owner="user_1"), UserRole.USER, "user_2", "gone", id="other"),
        pytest.param(
            PlaylistAccess(owner="user_1", sharing=ProviderSharing.MEMBERS),
            UserRole.USER,
            "user_2",
            "event",
            id="shared-member",
        ),
        pytest.param(
            PlaylistAccess(owner="user_1", sharing=ProviderSharing.MEMBERS),
            None,
            "user_2",
            None,
            id="shared-unauthenticated",
        ),
    ],
)
async def test_personal_playlist_events_only_reach_who_may_see_them(
    access: PlaylistAccess | None, user_role: str | None, user_id: str, delivered: str | None
) -> None:
    """A personal playlist reaches who may see it, anyone else is at most told it is gone."""
    client = _subscribed_client(user_role, user_id=user_id)
    event = _playlist_event(access)
    gone = MassEvent(event=EventType.MEDIA_ITEM_DELETED, object_id=event.object_id)

    assert _sent_events(client, event) == {"event": [event], "gone": [gone], None: []}[delivered]


@pytest.mark.asyncio
async def test_users_who_may_not_see_a_playlist_are_told_once_it_is_gone() -> None:
    """Users who may not (or no longer) see a playlist are told once that it is gone."""
    clients = {
        user_id: _subscribed_client(UserRole.USER, user_id=user_id)
        for user_id in ("user_2", "user_3", "user_4")
    }
    shared = _playlist_event(
        PlaylistAccess(
            owner="user_1", sharing=ProviderSharing.SELECTED, shared_users=["user_2", "user_3"]
        )
    )
    gone = MassEvent(event=EventType.MEDIA_ITEM_DELETED, object_id=shared.object_id)

    assert _sent_events(clients["user_2"], shared) == [shared]
    assert _sent_events(clients["user_3"], shared) == [shared]
    assert _sent_events(clients["user_4"], shared) == [gone]

    unshared = _playlist_event(
        PlaylistAccess(owner="user_1", sharing=ProviderSharing.SELECTED, shared_users=["user_2"])
    )

    assert _sent_events(clients["user_2"], unshared) == [unshared]
    assert _sent_events(clients["user_3"], unshared) == [gone]
    assert _sent_events(clients["user_4"], unshared) == []


@pytest.mark.asyncio
async def test_a_playlist_shared_again_is_announced_gone_again() -> None:
    """A playlist that becomes visible and is hidden again is announced gone a second time."""
    client = _subscribed_client(UserRole.USER, user_id="user_2")
    private = _playlist_event(PlaylistAccess(owner="user_1"))
    shared = _playlist_event(
        PlaylistAccess(owner="user_1", sharing=ProviderSharing.SELECTED, shared_users=["user_2"])
    )
    gone = MassEvent(event=EventType.MEDIA_ITEM_DELETED, object_id=private.object_id)

    assert _sent_events(client, private) == [gone]
    assert _sent_events(client, private) == []
    assert _sent_events(client, shared) == [shared]
    assert _sent_events(client, private) == [gone]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", [EventType.MEDIA_ITEM_ADDED, EventType.MEDIA_ITEM_DELETED])
async def test_a_playlist_created_or_removed_out_of_sight_is_not_announced(
    event_type: EventType,
) -> None:
    """
    A client never held a playlist created or removed out of its sight, so it hears nothing.

    :param event_type: The media item event type the playlist is signalled with.
    """
    client = _subscribed_client(UserRole.USER, user_id="user_2")
    private = PlaylistAccess(owner="user_1")

    assert _sent_events(client, _playlist_event(private, event=event_type)) == []
    assert _sent_events(client, _playlist_event(private)) == []
