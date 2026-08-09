"""Setup flow for the Plex music provider."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from plexapi.myplex import MyPlexPinLogin

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError

from .constants import (
    AUTH_TOKEN_UNAUTH,
    CONF_AUTH_TOKEN,
    CONF_LIBRARY_ID,
    CONF_LOCAL_SERVER_IP,
    CONF_LOCAL_SERVER_PORT,
    CONF_LOCAL_SERVER_SSL,
    CONF_LOCAL_SERVER_VERIFY_CERT,
)
from .helpers import (
    CONF_LIBRARY_TYPE,
    LIBRARY_TYPE_AUDIOBOOKS,
    LIBRARY_TYPE_MUSIC,
    LIBRARY_TYPE_PODCASTS,
    discover_local_servers,
    get_section_info,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

    from .helpers import PlexSectionInfo

AUTH_METHOD_MYPLEX = "myplex"
AUTH_METHOD_LOCAL = "local"


async def run_setup(session: SetupSession) -> None:
    """
    Run the Plex setup flow.

    Collects the local server details, authenticates (MyPlex OAuth or a tokenless local
    connection), lets the user pick the music/audiobook/podcast library, and persists the
    connection + selection as setup data.

    :param session: The setup session driving the flow.
    """
    setup_data = dict(session.context.setup_data)
    discovered = await _discover_servers(session)
    # server details first (matches the pre-flow UI order)
    server = await _collect_server(session, setup_data, discovered)
    # authenticate once: MyPlex OAuth is expensive to repeat on a retry
    token = await _authenticate(session)
    errors: dict[str, str] | None = None
    while True:
        if errors is not None:
            # a previous attempt failed: let the user correct the server details
            server = await _collect_server(session, setup_data, discovered, errors=errors)
        sections = await session.progress_until(
            get_section_info(
                session.mass,
                token,
                bool(server[CONF_LOCAL_SERVER_SSL]),
                str(server[CONF_LOCAL_SERVER_IP]),
                str(server[CONF_LOCAL_SERVER_PORT]),
                bool(server[CONF_LOCAL_SERVER_VERIFY_CERT]),
                session.context.instance_id,
            ),
            step_id="loading_libraries",
            text="loading_libraries",
            expires_in=60,
        )
        if not sections:
            errors = {"base": "no_libraries"}
            continue
        library_id, library_type = await _collect_library(
            session, sections, setup_data, session.context.instance_id
        )
        try:
            await session.finish(
                {
                    CONF_AUTH_TOKEN: token,
                    **server,
                    CONF_LIBRARY_ID: library_id,
                    CONF_LIBRARY_TYPE: library_type,
                }
            )
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _discover_servers(session: SetupSession) -> tuple[str | None, int | None]:
    """Run a best-effort GDM discovery to prefill the server details."""
    try:
        return await session.progress_until(
            discover_local_servers(),
            step_id="discovering",
            text="discovering_servers",
            expires_in=15,
        )
    except Exception:
        # discovery is only a convenience prefill; never let it break the flow
        return None, None


async def _collect_server(
    session: SetupSession,
    setup_data: dict[str, ConfigValueType],
    discovered: tuple[str | None, int | None],
    errors: dict[str, str] | None = None,
) -> dict[str, ConfigValueType]:
    """Show the server-details form and return the collected connection settings."""
    ip_default = setup_data.get(CONF_LOCAL_SERVER_IP) or discovered[0]
    port_default = setup_data.get(CONF_LOCAL_SERVER_PORT) or discovered[1] or 32400
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_LOCAL_SERVER_IP,
                type=ConfigEntryType.STRING,
                required=True,
                value=ip_default,
            ),
            ConfigEntry(
                key=CONF_LOCAL_SERVER_PORT,
                type=ConfigEntryType.INTEGER,
                required=True,
                default_value=32400,
                value=port_default,
            ),
            ConfigEntry(
                key=CONF_LOCAL_SERVER_SSL,
                type=ConfigEntryType.BOOLEAN,
                required=True,
                default_value=False,
                value=setup_data.get(CONF_LOCAL_SERVER_SSL),
            ),
            ConfigEntry(
                key=CONF_LOCAL_SERVER_VERIFY_CERT,
                type=ConfigEntryType.BOOLEAN,
                required=True,
                default_value=True,
                depends_on=CONF_LOCAL_SERVER_SSL,
                advanced=True,
                value=setup_data.get(CONF_LOCAL_SERVER_VERIFY_CERT),
            ),
        ],
        step_id="server",
        errors=errors,
    )
    return {
        CONF_LOCAL_SERVER_IP: str(values[CONF_LOCAL_SERVER_IP]),
        CONF_LOCAL_SERVER_PORT: int(values[CONF_LOCAL_SERVER_PORT]),  # type: ignore[arg-type]
        CONF_LOCAL_SERVER_SSL: bool(values[CONF_LOCAL_SERVER_SSL]),
        CONF_LOCAL_SERVER_VERIFY_CERT: bool(values[CONF_LOCAL_SERVER_VERIFY_CERT]),
    }


async def _authenticate(session: SetupSession) -> str:
    """Pick an auth method and return the Plex auth token (or the local sentinel)."""
    method = (
        await session.form(
            [
                ConfigEntry(
                    key="auth_method",
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=AUTH_METHOD_MYPLEX,
                    options=[
                        ConfigValueOption(value=AUTH_METHOD_MYPLEX),
                        ConfigValueOption(value=AUTH_METHOD_LOCAL),
                    ],
                )
            ],
            step_id="auth_method",
        )
    )["auth_method"]
    if method == AUTH_METHOD_LOCAL:
        return AUTH_TOKEN_UNAUTH
    plex_auth = MyPlexPinLogin(headers={"X-Plex-Product": "Music Assistant"}, oauth=True)
    await asyncio.to_thread(plex_auth._getCode)
    auth_url = plex_auth.oauthUrl(session.callback_url)
    # the callback is only a "user came back" signal; the token is fetched by polling
    await session.external(auth_url, step_id="myplex_auth", expires_in=600)
    token = await session.progress_until(
        _poll_myplex_token(plex_auth),
        step_id="finalizing_auth",
        text="finalizing_auth",
        expires_in=45,
    )
    if not token:
        raise AbortFlow("myplex_auth_failed")
    return token


async def _poll_myplex_token(plex_auth: MyPlexPinLogin) -> str | None:
    """Poll plex.tv until the PIN is linked and the auth token becomes available."""
    for attempt in range(8):
        if await asyncio.to_thread(plex_auth.checkLogin):
            break
        await asyncio.sleep(min(0.5 * (2**attempt), 5))
    return str(plex_auth.token) if plex_auth.token else None


async def _collect_library(
    session: SetupSession,
    sections: list[PlexSectionInfo],
    setup_data: dict[str, ConfigValueType],
    instance_id: str | None,
) -> tuple[str, str]:
    """Show the library-selection form (with smart defaults) and return (library_id, type)."""
    used_libraries, has_audiobook_provider = _claimed_libraries(session.mass, instance_id)
    prefilled_library = setup_data.get(CONF_LIBRARY_ID)
    default_library, suggested_type = _default_library_selection(
        sections,
        used_libraries,
        has_audiobook_provider,
        str(prefilled_library) if prefilled_library else None,
    )
    # a stored library type (reconfigure) wins over the freshly derived suggestion
    default_type = str(setup_data.get(CONF_LIBRARY_TYPE) or suggested_type)
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_LIBRARY_ID,
                type=ConfigEntryType.STRING,
                required=True,
                options=[
                    ConfigValueOption(title=s.display_name, value=s.display_name) for s in sections
                ],
                default_value=default_library,
                value=default_library,
            ),
            ConfigEntry(
                key=CONF_LIBRARY_TYPE,
                type=ConfigEntryType.STRING,
                required=True,
                options=[
                    ConfigValueOption(value=LIBRARY_TYPE_MUSIC),
                    ConfigValueOption(value=LIBRARY_TYPE_AUDIOBOOKS),
                    ConfigValueOption(value=LIBRARY_TYPE_PODCASTS),
                ],
                default_value=default_type,
                value=default_type,
            ),
        ],
        step_id="library",
        last_step=True,
    )
    return str(values[CONF_LIBRARY_ID]), str(values[CONF_LIBRARY_TYPE])


def _claimed_libraries(mass: MusicAssistant, instance_id: str | None) -> tuple[set[str], bool]:
    """
    Return the libraries already claimed by other plex instances (and if an audiobook one exists).

    Reads the raw stored provider config directly to avoid recursively triggering
    get_config_entries for every plex instance.

    :param mass: The MusicAssistant instance.
    :param instance_id: The instance being configured (excluded from the claim scan).
    """
    used_libraries: set[str] = set()
    has_audiobook_provider = False
    for prov_id, prov_conf in mass.config.get("providers", {}).items():
        if prov_conf.get("domain") != "plex" or prov_id == instance_id:
            continue
        prov_setup = prov_conf.get("setup_data", {})
        prov_values = prov_conf.get("values", {})
        if lib_val := (prov_setup.get(CONF_LIBRARY_ID) or prov_values.get(CONF_LIBRARY_ID)):
            used_libraries.add(str(lib_val))
        if LIBRARY_TYPE_AUDIOBOOKS in (
            prov_setup.get(CONF_LIBRARY_TYPE),
            prov_values.get(CONF_LIBRARY_TYPE),
        ):
            has_audiobook_provider = True
    return used_libraries, has_audiobook_provider


def _default_library_selection(
    sections: list[PlexSectionInfo],
    used_libraries: set[str],
    has_audiobook_provider: bool,
    prefilled_library: str | None,
) -> tuple[str, str]:
    """
    Derive the default (library display name, library type) for the selection form.

    :param sections: All music sections discovered on the server.
    :param used_libraries: Libraries already claimed by other plex instances.
    :param has_audiobook_provider: Whether another plex instance already serves audiobooks.
    :param prefilled_library: A previously selected library (reconfigure) to keep, if any.
    """
    available = [s for s in sections if s.display_name not in used_libraries] or sections
    if prefilled_library:
        default_library = prefilled_library
    else:
        # non-tracking libraries first (music), then alphabetically
        default_library = sorted(available, key=lambda s: (s.is_tracking_progress, s.display_name))[
            0
        ].display_name
    selected = next((s for s in sections if s.display_name == default_library), None)
    if selected and selected.is_tracking_progress:
        default_type = LIBRARY_TYPE_PODCASTS if has_audiobook_provider else LIBRARY_TYPE_AUDIOBOOKS
    else:
        default_type = LIBRARY_TYPE_MUSIC
    return default_library, default_type
