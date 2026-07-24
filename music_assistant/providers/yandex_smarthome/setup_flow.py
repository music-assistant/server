"""
Setup flow for the Yandex Smart Home provider.

Drives the connection-mode specific onboarding:

- ``cloud`` — registers a public yaha-cloud relay slot and shows the one-time
  code the user enters in the Yandex app to link the account.
- ``cloud_plus`` — registers a private yaha-cloud slot, provisions (or lets the
  user paste) a private Yandex Dialogs skill, collects its OAuth token and shows
  the linking code.
- ``direct`` — mints a per-install OAuth client secret, provisions the skill
  against MA's own public HTTPS endpoints and collects the skill OAuth token.

The shared skill auto-create path signs in to Yandex Passport with the native
OAuth Device Flow (``PassportClient.start_device_login`` +
``poll_device_until_confirmed``) — the code is shown to the user as a rendered
image and completion is detected by polling, not a browser callback.
"""

from __future__ import annotations

import base64
from html import escape
from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType
from ya_dialogs_api import (
    SMART_HOME_CHANNEL,
    SecretStr,
    SkillCreationArtifacts,
    SkillCreationState,
    auto_create_skill,
    dump_artifacts,
    load_artifacts,
    load_default_logo_bytes,
)
from ya_passport_auth.ma import (
    BORROW_SOURCE_OWN,
    BorrowedCredentialSource,
    list_yandex_music_instances,
)

from music_assistant.models.setup_flow import AbortFlow, SetupFlowError

from ._smarthome_auto_create import derive_smart_home_urls, resolve_base_url
from .cloud import get_cloud_otp, register_cloud_instance
from .constants import (
    CONF_AUTH_X_TOKEN,
    CONF_AUTO_CREATE_ARTIFACTS,
    CONF_CLOUD_CONNECTION_TOKEN,
    CONF_CLOUD_INSTANCE_ID,
    CONF_CLOUD_INSTANCE_PASSWORD,
    CONF_CONNECTION_TYPE,
    CONF_DIRECT_CLIENT_SECRET,
    CONF_EXTERNAL_BASE_URL,
    CONF_INSTANCE_NAME,
    CONF_SKILL_ID,
    CONF_SKILL_TOKEN,
    CONF_YM_INSTANCE,
    CONNECTION_TYPE_CLOUD,
    CONNECTION_TYPE_CLOUD_PLUS,
    CONNECTION_TYPE_DIRECT,
    YANDEX_OAUTH_URL,
)
from .ma_authenticator import make_authenticator

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

# skill-creation method choices (cloud_plus)
_METHOD_AUTO = "auto"
_METHOD_MANUAL = "manual"

# generous deadlines (seconds) for the blocking provisioning / relay steps
_SKILL_CREATE_TIMEOUT = 180.0
_CLOUD_CALL_TIMEOUT = 60.0

# device name shown on the Yandex confirmation screen during the Device Flow login
_DEVICE_NAME = "Music Assistant"


async def run_setup(session: SetupSession) -> None:
    """
    Run the Yandex Smart Home setup flow.

    Collects the connection mode, provisions the cloud slot and/or Yandex Dialogs skill
    for that mode, and persists the resulting credentials as setup data. The genuine
    playback options (instance name, exposed players/playlists) are configured via the
    options surface, not here.

    :param session: The setup session driving the flow.
    """
    collected: dict[str, ConfigValueType] = dict(session.context.setup_data)
    connection_type, instance_name, ym_instance = await _collect_user(session, collected)
    collected[CONF_CONNECTION_TYPE] = connection_type
    collected[CONF_YM_INSTANCE] = ym_instance
    if connection_type == CONNECTION_TYPE_CLOUD:
        await _run_cloud(session, collected)
    elif connection_type == CONNECTION_TYPE_CLOUD_PLUS:
        await _run_cloud_plus(session, collected, instance_name, ym_instance)
    else:
        await _run_direct(session, collected, instance_name, ym_instance)


async def _collect_user(
    session: SetupSession, collected: dict[str, ConfigValueType]
) -> tuple[str, str, str]:
    """
    Show the first form and return the (connection_type, instance_name, ym_instance) choice.

    For direct mode the external base URL is validated to be HTTPS here so the user can
    correct it before any provisioning starts.
    """
    ym_instances = list_yandex_music_instances(session.mass)
    valid_sources = {inst_id for inst_id, _ in ym_instances}
    ym_default = str(collected.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
    if ym_default != BORROW_SOURCE_OWN and ym_default not in valid_sources:
        ym_default = BORROW_SOURCE_OWN
    conn_default = str(collected.get(CONF_CONNECTION_TYPE) or CONNECTION_TYPE_CLOUD)
    base_url_default = str(collected.get(CONF_EXTERNAL_BASE_URL) or "")

    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            _user_entries(conn_default, ym_default, base_url_default, ym_instances),
            step_id="user",
            errors=errors,
        )
        connection_type = str(values[CONF_CONNECTION_TYPE])
        instance_name = str(values.get(CONF_INSTANCE_NAME) or "Music Assistant")
        ym_instance = str(values.get(CONF_YM_INSTANCE) or BORROW_SOURCE_OWN)
        if connection_type != CONNECTION_TYPE_DIRECT:
            return connection_type, instance_name, ym_instance
        external = str(values.get(CONF_EXTERNAL_BASE_URL) or "")
        base_url = resolve_base_url(session.mass, external or None)
        try:
            # placeholder secret keeps derive_smart_home_urls from failing on the
            # missing-secret precondition, so only the HTTPS check can fail here
            derive_smart_home_urls(
                connection_type=CONNECTION_TYPE_DIRECT,
                base_url=base_url,
                cloud_instance_id="",
                direct_client_secret="__validate__",
            )
        except ValueError:
            errors = {"base": "direct_requires_https"}
            conn_default, ym_default, base_url_default = connection_type, ym_instance, external
            continue
        collected[CONF_EXTERNAL_BASE_URL] = external
        return connection_type, instance_name, ym_instance


async def _run_cloud(session: SetupSession, collected: dict[str, ConfigValueType]) -> None:
    """Register a public yaha-cloud slot, show the linking code and finish."""
    data = await session.progress_until(
        register_cloud_instance(session.mass.http_session, platform=None),
        step_id="registering",
        text="registering_cloud",
        expires_in=_CLOUD_CALL_TIMEOUT,
    )
    collected[CONF_CLOUD_INSTANCE_ID] = data["id"]
    collected[CONF_CLOUD_INSTANCE_PASSWORD] = data["password"]
    collected[CONF_CLOUD_CONNECTION_TOKEN] = data["connection_token"]
    errors: dict[str, str] | None = None
    while True:
        await _show_linking_code(session, collected, errors=errors)
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _run_cloud_plus(
    session: SetupSession,
    collected: dict[str, ConfigValueType],
    instance_name: str,
    ym_instance: str,
) -> None:
    """Register a private yaha-cloud slot, provision/enter the skill, then link and finish."""
    data = await session.progress_until(
        register_cloud_instance(session.mass.http_session, platform="yandex"),
        step_id="registering",
        text="registering_cloud",
        expires_in=_CLOUD_CALL_TIMEOUT,
    )
    collected[CONF_CLOUD_INSTANCE_ID] = data["id"]
    collected[CONF_CLOUD_INSTANCE_PASSWORD] = data["password"]
    collected[CONF_CLOUD_CONNECTION_TOKEN] = data["connection_token"]

    method = str(
        (
            await session.form(
                [
                    ConfigEntry(
                        key="skill_method",
                        type=ConfigEntryType.STRING,
                        required=True,
                        default_value=_METHOD_AUTO,
                        value=_METHOD_AUTO,
                        options=[
                            ConfigValueOption(value=_METHOD_AUTO),
                            ConfigValueOption(value=_METHOD_MANUAL),
                        ],
                    )
                ],
                step_id="skill_method",
            )
        )["skill_method"]
    )
    if method == _METHOD_AUTO:
        collected[CONF_SKILL_ID] = await _provision_skill(
            session,
            collected,
            connection_type=CONNECTION_TYPE_CLOUD_PLUS,
            skill_name=instance_name,
            ym_instance=ym_instance,
        )
    else:
        skill_values = await session.form(
            [
                ConfigEntry(
                    key=CONF_SKILL_ID,
                    type=ConfigEntryType.STRING,
                    required=True,
                    value=collected.get(CONF_SKILL_ID),
                )
            ],
            step_id="skill_id",
        )
        collected[CONF_SKILL_ID] = str(skill_values[CONF_SKILL_ID])

    errors: dict[str, str] | None = None
    while True:
        errors = await _collect_skill_token(session, collected, errors)
        if errors is not None:
            continue
        await _show_linking_code(session, collected, errors=None)
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            # a finish failure here is most likely a rejected skill token; re-prompt it
            errors = {"base": err.translation_key or str(err)}


async def _run_direct(
    session: SetupSession,
    collected: dict[str, ConfigValueType],
    instance_name: str,
    ym_instance: str,
) -> None:
    """Mint the per-install client secret, provision the skill and finish (no relay OTP)."""
    if not collected.get(CONF_DIRECT_CLIENT_SECRET):
        collected[CONF_DIRECT_CLIENT_SECRET] = uuid4().hex
    collected[CONF_SKILL_ID] = await _provision_skill(
        session,
        collected,
        connection_type=CONNECTION_TYPE_DIRECT,
        skill_name=instance_name,
        ym_instance=ym_instance,
    )
    errors: dict[str, str] | None = None
    while True:
        errors = await _collect_skill_token(session, collected, errors)
        if errors is not None:
            continue
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": err.translation_key or str(err)}


async def _provision_skill(
    session: SetupSession,
    collected: dict[str, ConfigValueType],
    *,
    connection_type: str,
    skill_name: str,
    ym_instance: str,
) -> str:
    """
    Run the shared skill auto-create pipeline and return the created skill id.

    Signs in to Yandex Passport (borrowed token, cached token, or a fresh native Device
    Flow), then provisions the Yandex Dialogs skill. Raises SetupFlowError when the
    account source is unusable or the pipeline does not reach the DONE state.
    """
    if ym_instance != BORROW_SOURCE_OWN:
        # borrow the linked yandex_music instance's x_token read-only — never persisted
        # or rotated by this provider (the owner is the single writer)
        x_token = _borrowed_x_token(session.mass, ym_instance)
    else:
        cached = collected.get(CONF_AUTH_X_TOKEN)
        if cached:
            x_token = str(cached)
        else:
            x_token = await _device_login(session)
            collected[CONF_AUTH_X_TOKEN] = x_token

    authenticator = make_authenticator(
        mass=session.mass,
        session_id=session.flow_id,
        cached_x_token=x_token,
        on_token_obtained=None,
        allow_device_flow=False,
    )
    base_url = resolve_base_url(
        session.mass, str(collected.get(CONF_EXTERNAL_BASE_URL) or "") or None
    )
    try:
        urls = derive_smart_home_urls(
            connection_type=connection_type,
            base_url=base_url,
            cloud_instance_id=str(collected.get(CONF_CLOUD_INSTANCE_ID) or ""),
            direct_client_secret=str(collected.get(CONF_DIRECT_CLIENT_SECRET) or ""),
        )
    except ValueError as err:
        raise SetupFlowError(str(err)) from err

    async def _track(updated: SkillCreationArtifacts) -> None:
        # persist intermediate state so a reconfigure can resume the pipeline
        collected[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(updated)

    artifacts_raw = collected.get(CONF_AUTO_CREATE_ARTIFACTS)
    artifacts = await session.progress_until(
        auto_create_skill(
            authenticator=authenticator,
            skill_name=skill_name,
            artifacts=load_artifacts(str(artifacts_raw) if artifacts_raw else None),
            backend_uri=urls.backend_uri,
            oauth_authorize_url=urls.oauth_authorize_url,
            oauth_token_url=urls.oauth_token_url,
            oauth_client_id=urls.oauth_client_id,
            oauth_client_secret=urls.oauth_client_secret,
            logo_bytes=load_default_logo_bytes(),
            channel=SMART_HOME_CHANNEL,
            progress_cb=_track,
        ),
        step_id="creating_skill",
        text="creating_skill",
        expires_in=_SKILL_CREATE_TIMEOUT,
    )
    collected[CONF_AUTO_CREATE_ARTIFACTS] = dump_artifacts(artifacts)
    if artifacts.state != SkillCreationState.DONE or not artifacts.skill_id:
        raise SetupFlowError(artifacts.last_error or "skill creation failed")
    return artifacts.skill_id


async def _device_login(session: SetupSession) -> str:
    """
    Run the native Yandex Passport OAuth Device Flow and return the resulting x_token.

    Shows the ``user_code`` + verification URL as a rendered image and detects completion
    by polling; the browser-callback based flow the frontend can no longer drive is not
    used here.
    """
    # imported lazily so the module can be imported in minimal test envs
    from ya_passport_auth import (  # noqa: PLC0415
        ClientConfig,
        InvalidCredentialsError,
        PassportClient,
    )

    async with PassportClient.create(config=ClientConfig()) as client:
        device = await client.start_device_login(device_name=_DEVICE_NAME)
        try:
            credentials = await session.progress_until(
                # give the poll a longer deadline than the step so the step's countdown
                # (StepExpiredError -> a clean timed_out abort) always wins the race
                client.poll_device_until_confirmed(
                    device, total_timeout=float(device.expires_in) + 60
                ),
                step_id="device_login",
                text="device_login",
                image=_device_image(device.user_code, device.verification_url),
                expires_in=float(device.expires_in),
            )
        except InvalidCredentialsError as err:
            raise AbortFlow("device_login_denied") from err
    return credentials.x_token.get_secret()


async def _show_linking_code(
    session: SetupSession,
    collected: dict[str, ConfigValueType],
    *,
    errors: dict[str, str] | None,
) -> None:
    """Fetch + show the relay one-time linking code and wait for the user to confirm."""
    code = await session.progress_until(
        get_cloud_otp(
            session.mass.http_session,
            str(collected[CONF_CLOUD_INSTANCE_ID]),
            SecretStr(str(collected[CONF_CLOUD_CONNECTION_TOKEN])),
        ),
        step_id="fetching_otp",
        text="fetching_otp",
        expires_in=_CLOUD_CALL_TIMEOUT,
    )
    # the code is shown as an image (dynamic value, i18n-safe) and also passed as a
    # translation param on the confirm form's label so it stays visible while the user
    # acts on it in the Yandex app (the static wording lives in the owner's strings.json)
    session.progress(step_id="cloud_otp", text="enter_otp_in_yandex_app", image=_code_image(code))
    await session.form(
        [
            ConfigEntry(
                key="label_otp_confirm",
                type=ConfigEntryType.LABEL,
                translation_params=[code],
            )
        ],
        step_id="cloud_confirm",
        errors=errors,
        last_step=True,
    )


async def _collect_skill_token(
    session: SetupSession,
    collected: dict[str, ConfigValueType],
    errors: dict[str, str] | None,
) -> dict[str, str] | None:
    """
    Show the skill-OAuth-token form and store a pasted token in ``collected``.

    Returns None when a usable token is present (pasted now or kept from a prior run),
    or a form-errors dict to re-render when the field was left empty with nothing stored.
    """
    existing = collected.get(CONF_SKILL_TOKEN)
    values = await session.form(
        [
            ConfigEntry(
                key=CONF_SKILL_TOKEN,
                type=ConfigEntryType.SECURE_STRING,
                required=not existing,
                help_link=YANDEX_OAUTH_URL,
            )
        ],
        step_id="skill_token",
        errors=errors,
    )
    pasted = str(values.get(CONF_SKILL_TOKEN) or "")
    if pasted:
        collected[CONF_SKILL_TOKEN] = pasted
        return None
    if existing:
        # reconfigure: keep the previously stored token when the field is left blank
        return None
    return {"base": "skill_token_required"}


def _user_entries(
    conn_default: str,
    ym_default: str,
    base_url_default: str,
    ym_instances: list[tuple[str, str]],
) -> list[ConfigEntry]:
    """Build the first-form entries (connection mode + account source + optional base URL)."""
    return [
        ConfigEntry(
            key=CONF_CONNECTION_TYPE,
            type=ConfigEntryType.STRING,
            required=True,
            default_value=CONNECTION_TYPE_CLOUD,
            value=conn_default,
            options=[
                ConfigValueOption(value=CONNECTION_TYPE_CLOUD),
                ConfigValueOption(value=CONNECTION_TYPE_CLOUD_PLUS),
                ConfigValueOption(value=CONNECTION_TYPE_DIRECT),
            ],
        ),
        ConfigEntry(
            key=CONF_INSTANCE_NAME,
            type=ConfigEntryType.STRING,
            required=False,
            default_value="Music Assistant",
        ),
        ConfigEntry(
            key=CONF_YM_INSTANCE,
            type=ConfigEntryType.STRING,
            required=False,
            default_value=BORROW_SOURCE_OWN,
            value=ym_default,
            options=[
                *(
                    ConfigValueOption(value=inst_id, title=f"Yandex Music: {name}")
                    for inst_id, name in ym_instances
                ),
                ConfigValueOption(value=BORROW_SOURCE_OWN),
            ],
        ),
        ConfigEntry(
            key=CONF_EXTERNAL_BASE_URL,
            type=ConfigEntryType.STRING,
            required=False,
            default_value="",
            value=base_url_default,
            depends_on=CONF_CONNECTION_TYPE,
            depends_on_value=CONNECTION_TYPE_DIRECT,
        ),
    ]


def _borrowed_x_token(mass: MusicAssistant, ym_instance: str) -> str:
    """Read the linked yandex_music instance's x_token, or raise SetupFlowError."""
    try:
        _, x_token = BorrowedCredentialSource(mass, ym_instance).read_tokens()
    except Exception as err:
        raise SetupFlowError(
            str(err), translation_key=getattr(err, "translation_key", None)
        ) from err
    if x_token is None:
        raise SetupFlowError(
            "The linked Yandex Music account has no session token. Authenticate the "
            "Yandex Music provider (with Remember session enabled) and retry.",
            translation_key="no_borrowed_token",
        )
    return x_token.get_secret()


def _device_image(user_code: str, verification_url: str) -> str:
    """Render the device ``user_code`` + verification URL as an SVG data URI."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="460" height="180" '
        'viewBox="0 0 460 180" role="img">'
        '<rect width="460" height="180" rx="16" fill="#ffdb4d"/>'
        '<text x="230" y="82" font-family="monospace" font-size="46" font-weight="700" '
        f'text-anchor="middle" fill="#1a1a1a">{escape(user_code)}</text>'
        '<text x="230" y="130" font-family="sans-serif" font-size="16" '
        f'text-anchor="middle" fill="#5a4a00">{escape(verification_url)}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _code_image(code: str) -> str:
    """Render a one-time code as an SVG data URI."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="460" height="140" '
        'viewBox="0 0 460 140" role="img">'
        '<rect width="460" height="140" rx="16" fill="#ffdb4d"/>'
        '<text x="230" y="88" font-family="monospace" font-size="52" font-weight="700" '
        f'text-anchor="middle" fill="#1a1a1a">{escape(code)}</text>'
        "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
