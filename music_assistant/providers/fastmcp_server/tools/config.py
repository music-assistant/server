"""
FastMCP sub-server for configuration view/edit tools.

Spec: ``specs/inprogress/0006-config-read-write.md``.

Read tools proxy ``mass.config.get_*`` (secrets masked by MA's
``__post_serialize__``). Write tools (later registration functions)
delegate to MA's atomic ``save_*_config``. All tools are gated by
off-by-default ConfigEntries; with no tag enabled the namespace is
invisible via ``TagFilterMiddleware``.
"""
# ruff: noqa: TID252  -- relative imports are the canonical MA-provider pattern.

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from music_assistant_models.config_entries import ConfigActionResult
from music_assistant_models.constants import SECURE_STRING_SUBSTITUTE
from music_assistant_models.enums import ConfigEntryType

from ..config_io.differ import compute_diff
from ..config_io.secret_handler import gate_secret_writes
from ..config_io.validator import coerce
from ..models import (
    ActionResult,
    ConfigEntryDump,
    ConfigEntryList,
    ConfigTarget,
    ConfigTargetList,
    ConfigValueDump,
    CoreConfigDump,
    DSPConfigDump,
    PlayerConfigDump,
    ProviderConfigDump,
    SaveResult,
    SetValueResult,
)
from ..tags import Tag
from ._common import (
    TIMEOUT_FAST,
    TIMEOUT_INTERACTIVE,
    confirm_or_raise,
    lean_schema_view,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import (
        ConfigEntry,
        CoreConfig,
        PlayerConfig,
        ProviderConfig,
    )

    from music_assistant.mass import MusicAssistant


LOGGER = logging.getLogger("music_assistant.providers.fastmcp_server.config")

_PAYLOAD_CAP_BYTES = 256 * 1024
_SAVE_PAYLOAD_CAP_BYTES = 64 * 1024


def _resolve_secret_enabled(flag: bool | Callable[[], bool]) -> bool:
    """
    Resolve the secret-write gate at call time.

    The runtime passes a callable that reads the current
    ``CONF_CONFIG_WRITE_SECRET`` value so a hot-swapped permission toggle
    takes effect on the next request (mirrors the TagFilterMiddleware
    closure). Tests pass a plain bool.

    :param flag: Either a plain bool or a zero-arg callable returning one.
    """
    return flag() if callable(flag) else flag


def _readonly(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _values_from_raw(raw: dict[str, Any]) -> tuple[list[ConfigValueDump], bool]:
    """Build ConfigValueDump list from a Config.to_dict() raw dict (secrets pre-masked)."""
    out: list[ConfigValueDump] = []
    truncated = False
    running = 0
    for key, entry in raw.get("values", {}).items():
        value = entry.get("value")
        etype = entry.get("type", "unknown")
        size = len(str(value)) + len(str(key)) + len(str(etype)) + 16
        if running + size > _PAYLOAD_CAP_BYTES:
            truncated = True
            break
        running += size
        out.append(ConfigValueDump(key=str(key), type=str(etype), value=value))
    return out, truncated


def _entry_dump(entry: ConfigEntry, current: Any) -> ConfigEntryDump:
    """Map a ConfigEntry + current value to ConfigEntryDump."""
    opts = [o.value for o in entry.options] if entry.options else None
    # Label/description are resolved from the translations at serialization
    # (server-category entries leave the raw attributes None), so read the
    # localized values via to_dict() instead of the bare attributes.
    localized = entry.to_dict()
    # Mask secrets: _resolve_entries reads raw ConfigEntry.value, bypassing
    # the to_dict()/__post_serialize__ hook the sibling read tools use.
    current_value = (
        SECURE_STRING_SUBSTITUTE
        if entry.type == ConfigEntryType.SECURE_STRING and current is not None
        else current
    )
    return ConfigEntryDump(
        key=entry.key,
        type=entry.type.value,
        label=localized.get("label"),
        default_value=entry.default_value,
        required=entry.required,
        description=localized.get("description"),
        options=opts,
        range=entry.range,
        advanced=getattr(entry, "advanced", False),
        hidden=entry.hidden,
        requires_reload=entry.requires_reload,
        depends_on=entry.depends_on,
        action=entry.action,
        current_value=current_value,
    )


async def _resolve_entries(
    mass: MusicAssistant, target_type: str, target_id: str
) -> tuple[list[ConfigEntry], dict[str, Any]]:
    """
    Fetch ConfigEntry list + current values dict for a target.

    :param mass: MusicAssistant instance.
    :param target_type: "provider" | "core" | "player".
    :param target_id: The target identifier.
    """
    cfg: ProviderConfig | CoreConfig | PlayerConfig
    if target_type == "provider":
        cfg = await mass.config.get_provider_config(target_id)
        entries = await mass.config.get_provider_config_entries(target_id)
    elif target_type == "core":
        cfg = await mass.config.get_core_config(target_id)
        entries = await mass.config.get_core_config_entries(target_id)
    elif target_type == "player":
        cfg = await mass.config.get_player_config(target_id)
        entries = await mass.config.get_player_config_entries(target_id)
    else:
        raise ToolError(f"unknown target_type {target_type!r}")
    current = {k: getattr(v, "value", None) for k, v in getattr(cfg, "values", {}).items()}
    return list(entries), current


def _audit_id() -> str:
    """Return a sortable, unique audit id (monotonic, no Date.now/random)."""
    return f"cfg-{time.time_ns():x}"


def _action_outcome(mass: MusicAssistant, result: ConfigActionResult) -> dict[str, Any]:
    """
    Render a config action's outcome as the tool's extra_data payload.

    :param mass: The Music Assistant instance, used to localize the outcome message.
    :param result: The outcome reported by the action handler.
    """
    message = result.message
    if result.translation_key:
        message = (
            mass.translations.get_translation(
                f"config_actions.{result.translation_key}", owner=result.translation_owner
            )
            or message
        )
    return {
        key: value
        for key, value in (("message", message), ("open_url", result.open_url))
        if value is not None
    }


def _confirm_prompt(target_type: str, target_id: str, keys: list[str]) -> str:
    """
    Build the confirmation prompt for a write (core warns about restart).

    :param target_type: "provider" | "core" | "player".
    :param target_id: The target identifier.
    :param keys: Config keys being written.
    """
    if target_type == "core":
        return (
            f"Save core {target_id!r} config ({', '.join(keys)})? "
            "Core changes may restart subsystems and interrupt ALL playback."
        )
    return f"Save {target_type} {target_id!r} config ({', '.join(keys)})?"


async def _do_save(
    mass: MusicAssistant, target_type: str, target_id: str, values: dict[str, Any]
) -> None:
    """
    Delegate to MA's atomic save_*_config (validate+encrypt+persist+reload).

    :param mass: MusicAssistant instance.
    :param target_type: "provider" | "core" | "player".
    :param target_id: The target identifier.
    :param values: Plaintext key→value map to persist (MA encrypts SECURE_STRING).
    """
    try:
        if target_type == "provider":
            cfg = await mass.config.get_provider_config(target_id)
            await mass.config.save_provider_config(
                getattr(cfg, "domain", target_id), values, instance_id=target_id
            )
        elif target_type == "core":
            await mass.config.save_core_config(target_id, values)
        elif target_type == "player":
            await mass.config.save_player_config(target_id, values)
        else:
            raise ToolError(f"unknown target_type {target_type!r}")
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"config save failed: {exc}") from exc


async def _write_single(
    mass: MusicAssistant,
    target_type: str,
    target_id: str,
    key: str,
    value: Any,
    *,
    dry_run: bool,
    ctx: Context | None,
    require_confirmation: bool,
    secret_writes_enabled: bool | Callable[[], bool],
) -> SetValueResult:
    """
    Validate → secret-gate → diff → (confirm → audit → save) for one key.

    :param mass: MusicAssistant instance.
    :param target_type: "provider" | "core" | "player".
    :param target_id: The target identifier.
    :param key: Config key to write.
    :param value: Proposed value (plaintext; MA encrypts SECURE_STRING).
    :param dry_run: When True, return a diff without writing.
    :param ctx: FastMCP context (may be None in unit tests).
    :param require_confirmation: When True, elicit confirmation before writing.
    :param secret_writes_enabled: Bool or callable returning bool; resolved
        per request so a hot-swapped toggle takes effect immediately.
    """
    entries_list, current = await _resolve_entries(mass, target_type, target_id)
    entries = {e.key: e for e in entries_list}
    if key not in entries:
        raise ToolError(f"unknown key {key!r} for {target_type} {target_id!r}")
    parsed = coerce(entries[key], value)
    gate_secret_writes(
        entries, {key: parsed}, secret_tag_enabled=_resolve_secret_enabled(secret_writes_enabled)
    )
    diff = compute_diff(
        target_type=target_type,
        target_id=target_id,
        entries=entries,
        current=current,
        proposed={key: parsed},
    )
    requires_reload = bool(entries[key].requires_reload)
    if dry_run:
        return SetValueResult(
            target_type=target_type,
            target_id=target_id,
            key=key,
            applied=False,
            requires_reload=requires_reload,
            audit_log_id="",
            diff=diff,
        )
    await confirm_or_raise(
        ctx, _confirm_prompt(target_type, target_id, [key]), enabled=require_confirmation
    )
    audit = _audit_id()
    LOGGER.info(
        "config_write target=%s id=%s key=%s audit_id=%s", target_type, target_id, key, audit
    )
    await _do_save(mass, target_type, target_id, {key: parsed})
    return SetValueResult(
        target_type=target_type,
        target_id=target_id,
        key=key,
        applied=True,
        requires_reload=requires_reload,
        audit_log_id=audit,
        diff=None,
    )


async def _write_bulk(
    mass: MusicAssistant,
    target_type: str,
    target_id: str,
    values: dict[str, Any],
    *,
    dry_run: bool,
    ctx: Context | None,
    require_confirmation: bool,
    secret_writes_enabled: bool | Callable[[], bool],
) -> SaveResult:
    """
    Validate-all → secret-gate → diff → (confirm → audit → atomic save) for a payload.

    :param mass: MusicAssistant instance.
    :param target_type: "provider" | "core" | "player".
    :param target_id: The target identifier.
    :param values: Proposed key→value map (plaintext; MA encrypts SECURE_STRING).
    :param dry_run: When True, return a diff without writing.
    :param ctx: FastMCP context (may be None in unit tests).
    :param require_confirmation: When True, elicit confirmation before writing.
    :param secret_writes_enabled: Bool or callable returning bool; resolved
        per request so a hot-swapped toggle takes effect immediately.
    """
    import json  # noqa: PLC0415

    if len(json.dumps(values, default=str)) > _SAVE_PAYLOAD_CAP_BYTES:
        raise ToolError("save payload exceeds 64 KB cap")
    entries_list, current = await _resolve_entries(mass, target_type, target_id)
    entries = {e.key: e for e in entries_list}
    parsed: dict[str, Any] = {}
    for key, raw in values.items():
        if key not in entries:
            raise ToolError(f"unknown key {key!r} for {target_type} {target_id!r}")
        parsed[key] = coerce(entries[key], raw)
    gate_secret_writes(
        entries, parsed, secret_tag_enabled=_resolve_secret_enabled(secret_writes_enabled)
    )
    diff = compute_diff(
        target_type=target_type,
        target_id=target_id,
        entries=entries,
        current=current,
        proposed=parsed,
    )
    requires_reload = any(entries[k].requires_reload for k in parsed)
    if dry_run:
        return SaveResult(
            target_type=target_type,
            target_id=target_id,
            applied=False,
            changes=diff.changes,
            requires_reload=requires_reload,
            audit_log_id="",
            diff=diff,
        )
    await confirm_or_raise(
        ctx, _confirm_prompt(target_type, target_id, list(parsed)), enabled=require_confirmation
    )
    audit = _audit_id()
    LOGGER.info(
        "config_save target=%s id=%s keys=%s audit_id=%s",
        target_type,
        target_id,
        sorted(parsed),
        audit,
    )
    await _do_save(mass, target_type, target_id, parsed)
    return SaveResult(
        target_type=target_type,
        target_id=target_id,
        applied=True,
        changes=diff.changes,
        requires_reload=requires_reload,
        audit_log_id=audit,
        diff=None,
    )


def build_config_server(
    mass: MusicAssistant,
    *,
    require_confirmation: bool = True,
    secret_writes_enabled: bool | Callable[[], bool] = True,
    lean_schema: bool = False,
) -> FastMCP:
    """
    Build the ``config`` sub-server.

    :param mass: MusicAssistant instance.
    :param require_confirmation: When True (default), every write elicits
        confirmation before mutating.
    :param secret_writes_enabled: Bool or zero-arg callable returning bool.
        When False (or the callable returns False), SECURE_STRING writes are
        rejected. The runtime passes a callable so a hot-swapped permission
        toggle takes effect on the next request without a rebuild.
    :param lean_schema: When True, tools omit their ``outputSchema`` to shrink
        the namespace's context footprint for hosts without tool-search.
    """
    sub = FastMCP(name="config")
    target = lean_schema_view(sub) if lean_schema else sub
    _register_read_tools(target, mass)
    _register_provider_write_tools(
        target,
        mass,
        require_confirmation=require_confirmation,
        secret_writes_enabled=secret_writes_enabled,
    )
    _register_core_write_tools(
        target,
        mass,
        require_confirmation=require_confirmation,
        secret_writes_enabled=secret_writes_enabled,
    )
    _register_player_write_tools(
        target,
        mass,
        require_confirmation=require_confirmation,
        secret_writes_enabled=secret_writes_enabled,
    )
    return sub


def _register_read_tools(sub: FastMCP, mass: MusicAssistant) -> None:
    @sub.tool(
        tags={Tag.CONFIG_READ},
        annotations=_readonly("List configurable targets"),
        timeout=TIMEOUT_FAST,
    )
    async def list_targets() -> ConfigTargetList:
        """
        List every configurable provider, core controller, and player.

        See also: config_get_provider / config_get_core / config_get_player
        for a single target's values, config_get_entries for the editable
        schema.
        """
        providers = [
            ConfigTarget(
                target_type="provider",
                target_id=getattr(c, "instance_id", ""),
                domain=getattr(c, "domain", ""),
                name=getattr(c, "name", "") or getattr(c, "domain", ""),
                enabled=bool(getattr(c, "enabled", True)),
            )
            for c in await mass.config.get_provider_configs()
        ]
        core = [
            ConfigTarget(
                target_type="core",
                target_id=getattr(c, "domain", ""),
                domain=getattr(c, "domain", ""),
                name=getattr(c, "domain", ""),
                enabled=True,
            )
            for c in await mass.config.get_core_configs()
        ]
        players = [
            ConfigTarget(
                target_type="player",
                target_id=getattr(c, "player_id", ""),
                domain=getattr(c, "provider", ""),
                name=getattr(c, "name", "") or getattr(c, "player_id", ""),
                enabled=bool(getattr(c, "enabled", True)),
            )
            for c in await mass.config.get_player_configs()
        ]
        return ConfigTargetList(providers=providers, core=core, players=players)

    @sub.tool(
        tags={Tag.CONFIG_READ},
        annotations=_readonly("Get provider config"),
        timeout=TIMEOUT_FAST,
    )
    async def get_provider(instance_id: str) -> ProviderConfigDump:
        """
        Return a provider's stored config values (SECURE_STRING masked).

        See also: config_get_entries for editable schema, config_set_provider_value
        to change one value, config_save_provider for bulk.

        :param instance_id: The provider instance identifier.
        """
        try:
            cfg = await mass.config.get_provider_config(instance_id)
        except Exception as exc:
            raise ToolError(f"provider instance_id={instance_id!r} not found") from exc
        values, truncated = _values_from_raw(cfg.to_dict())
        return ProviderConfigDump(
            instance_id=instance_id,
            domain=getattr(cfg, "domain", ""),
            values=values,
            truncated=truncated,
        )

    @sub.tool(
        tags={Tag.CONFIG_READ},
        annotations=_readonly("Get core config"),
        timeout=TIMEOUT_FAST,
    )
    async def get_core(domain: str) -> CoreConfigDump:
        """
        Return a core controller's stored config values (SECURE_STRING masked).

        See also: config_set_core_value / config_save_core to change them.

        :param domain: Core controller domain (e.g. "webserver", "streams").
        """
        try:
            cfg = await mass.config.get_core_config(domain)
        except Exception as exc:
            raise ToolError(f"core domain={domain!r} not found") from exc
        values, truncated = _values_from_raw(cfg.to_dict())
        return CoreConfigDump(domain=domain, values=values, truncated=truncated)

    @sub.tool(
        tags={Tag.CONFIG_READ},
        annotations=_readonly("Get player config"),
        timeout=TIMEOUT_FAST,
    )
    async def get_player(player_id: str) -> PlayerConfigDump:
        """
        Return a player's stored config values (SECURE_STRING masked).

        See also: config_set_player_value to change one, config_get_dsp for EQ/DSP.

        :param player_id: The player identifier.
        """
        try:
            cfg = await mass.config.get_player_config(player_id)
        except Exception as exc:
            raise ToolError(f"player_id={player_id!r} not found") from exc
        values, truncated = _values_from_raw(cfg.to_dict())
        return PlayerConfigDump(
            player_id=player_id,
            provider=getattr(cfg, "provider", ""),
            values=values,
            truncated=truncated,
        )

    @sub.tool(
        tags={Tag.CONFIG_READ},
        annotations=_readonly("Get editable config entries"),
        timeout=TIMEOUT_FAST,
    )
    async def get_entries(target_type: str, target_id: str) -> ConfigEntryList:
        """
        Return the editable ConfigEntry schema for a target.

        See also: config_set_*_value to write a key. Action-driven entries are
        triggered separately via config_trigger_provider_action.

        :param target_type: "provider" | "core" | "player".
        :param target_id: The target identifier (instance_id / domain / player_id).
        """
        entries, current = await _resolve_entries(mass, target_type, target_id)
        dumps = [_entry_dump(e, current.get(e.key)) for e in entries]
        return ConfigEntryList(
            target_type=target_type, target_id=target_id, entries=dumps, truncated=False
        )

    @sub.tool(
        tags={Tag.CONFIG_READ},
        annotations=_readonly("Get player DSP config"),
        timeout=TIMEOUT_FAST,
    )
    async def get_dsp(player_id: str) -> DSPConfigDump:
        """
        Return a player's DSP configuration (enabled, gains, filter chain).

        See also: config_save_dsp to change it.

        :param player_id: The player identifier.
        """
        try:
            await mass.config.get_player_config(player_id)
        except Exception as exc:
            raise ToolError(f"player_id={player_id!r} not found") from exc
        dsp = mass.config.get_player_dsp_config(player_id)
        raw = dsp.to_dict()
        return DSPConfigDump(
            player_id=player_id,
            enabled=bool(raw.get("enabled", False)),
            input_gain=float(raw.get("input_gain", 0.0)),
            output_gain=float(raw.get("output_gain", 0.0)),
            filters=list(raw.get("filters", [])),
        )


def _register_provider_write_tools(
    sub: FastMCP,
    mass: MusicAssistant,
    *,
    require_confirmation: bool,
    secret_writes_enabled: bool | Callable[[], bool],
) -> None:
    @sub.tool(
        tags={Tag.CONFIG_WRITE_PROVIDER},
        annotations=ToolAnnotations(
            title="Set provider config value",
            destructiveHint=True,
            idempotentHint=False,
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def set_provider_value(
        instance_id: str, key: str, value: Any, dry_run: bool = False, ctx: Context | None = None
    ) -> SetValueResult:
        """
        Set one provider config value.

        Validates the value, gates SECURE_STRING writes behind
        config:write:secret, then delegates to MA's atomic
        save_provider_config (validate, encrypt, persist, reload).
        ``dry_run=True`` returns a before/after diff without writing.
        See also: config_get_entries for editable keys.

        :param instance_id: Provider instance identifier.
        :param key: ConfigEntry key to set.
        :param value: New value.
        :param dry_run: When True, return a diff and do not persist.
        :param ctx: FastMCP context (auto-populated).
        """
        return await _write_single(
            mass,
            "provider",
            instance_id,
            key,
            value,
            dry_run=dry_run,
            ctx=ctx,
            require_confirmation=require_confirmation,
            secret_writes_enabled=secret_writes_enabled,
        )

    @sub.tool(
        tags={Tag.CONFIG_WRITE_PROVIDER},
        annotations=ToolAnnotations(
            title="Save provider config (bulk)",
            destructiveHint=True,
            idempotentHint=False,
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def save_provider(
        instance_id: str,
        values: dict[str, Any],
        dry_run: bool = False,
        ctx: Context | None = None,
    ) -> SaveResult:
        """
        Bulk-save provider config values (atomic at MA's layer).

        :param instance_id: Provider instance identifier.
        :param values: key->value map to apply.
        :param dry_run: When True, return a diff and do not persist.
        :param ctx: FastMCP context (auto-populated).
        """
        return await _write_bulk(
            mass,
            "provider",
            instance_id,
            values,
            dry_run=dry_run,
            ctx=ctx,
            require_confirmation=require_confirmation,
            secret_writes_enabled=secret_writes_enabled,
        )

    @sub.tool(
        tags={Tag.CONFIG_WRITE_PROVIDER},
        annotations=ToolAnnotations(
            title="Trigger provider config action",
            destructiveHint=True,
            idempotentHint=False,
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def trigger_provider_action(
        instance_id: str,
        action_key: str,
        ctx: Context | None = None,
    ) -> ActionResult:
        """
        Invoke a provider config action (e.g. QR login, clear auth).

        Always elicits confirmation, even when require_confirmation is off.

        :param instance_id: Provider instance identifier.
        :param action_key: The action ConfigEntry key.
        :param ctx: FastMCP context (auto-populated).
        """
        await confirm_or_raise(
            ctx,
            f"Run provider action {action_key!r} on {instance_id!r}?",
            enabled=True,
        )
        result = await mass.config.invoke_provider_config_action(instance_id, action_key)
        audit = _audit_id()
        LOGGER.info(
            "config_action provider=%s action=%s audit_id=%s", instance_id, action_key, audit
        )
        if isinstance(result, ConfigActionResult):
            return ActionResult(
                instance_id=instance_id,
                action_key=action_key,
                new_entries=[],
                extra_data=_action_outcome(mass, result),
                audit_log_id=audit,
            )
        return ActionResult(
            instance_id=instance_id,
            action_key=action_key,
            new_entries=[_entry_dump(e, getattr(e, "value", None)) for e in result],
            extra_data={},
            audit_log_id=audit,
        )


def _register_core_write_tools(
    sub: FastMCP,
    mass: MusicAssistant,
    *,
    require_confirmation: bool,
    secret_writes_enabled: bool | Callable[[], bool],
) -> None:
    @sub.tool(
        tags={Tag.CONFIG_WRITE_CORE},
        annotations=ToolAnnotations(
            title="Set core config value",
            destructiveHint=True,
            idempotentHint=False,
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def set_core_value(
        domain: str, key: str, value: Any, dry_run: bool = False, ctx: Context | None = None
    ) -> SetValueResult:
        """
        Set one core controller config value.

        Core changes may restart subsystems and interrupt all playback.
        ``dry_run=True`` previews without writing. See also: config_get_core
        for current values.

        :param domain: Core controller domain (e.g. "webserver", "streams").
        :param key: ConfigEntry key.
        :param value: New value.
        :param dry_run: When True, return a diff and do not persist.
        :param ctx: FastMCP context (auto-populated).
        """
        return await _write_single(
            mass,
            "core",
            domain,
            key,
            value,
            dry_run=dry_run,
            ctx=ctx,
            require_confirmation=require_confirmation,
            secret_writes_enabled=secret_writes_enabled,
        )

    @sub.tool(
        tags={Tag.CONFIG_WRITE_CORE},
        annotations=ToolAnnotations(
            title="Save core config (bulk)",
            destructiveHint=True,
            idempotentHint=False,
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def save_core(
        domain: str, values: dict[str, Any], dry_run: bool = False, ctx: Context | None = None
    ) -> SaveResult:
        """
        Bulk-save core controller config values.

        :param domain: Core controller domain.
        :param values: key->value map.
        :param dry_run: When True, return a diff and do not persist.
        :param ctx: FastMCP context (auto-populated).
        """
        return await _write_bulk(
            mass,
            "core",
            domain,
            values,
            dry_run=dry_run,
            ctx=ctx,
            require_confirmation=require_confirmation,
            secret_writes_enabled=secret_writes_enabled,
        )


def _register_player_write_tools(
    sub: FastMCP,
    mass: MusicAssistant,
    *,
    require_confirmation: bool,
    secret_writes_enabled: bool | Callable[[], bool],
) -> None:
    @sub.tool(
        tags={Tag.CONFIG_WRITE_PLAYER},
        annotations=ToolAnnotations(
            title="Set player config value", destructiveHint=True, idempotentHint=False
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def set_player_value(
        player_id: str, key: str, value: Any, dry_run: bool = False, ctx: Context | None = None
    ) -> SetValueResult:
        """
        Set one player config value (volume limits, crossfade, output protocol, ...).

        See also: config_get_player for current values, config_save_dsp for EQ/DSP.

        :param player_id: The player identifier.
        :param key: ConfigEntry key.
        :param value: New value.
        :param dry_run: When True, return a diff and do not persist.
        :param ctx: FastMCP context (auto-populated).
        """
        return await _write_single(
            mass,
            "player",
            player_id,
            key,
            value,
            dry_run=dry_run,
            ctx=ctx,
            require_confirmation=require_confirmation,
            secret_writes_enabled=secret_writes_enabled,
        )

    @sub.tool(
        tags={Tag.CONFIG_WRITE_PLAYER},
        annotations=ToolAnnotations(
            title="Save player config (bulk)", destructiveHint=True, idempotentHint=False
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def save_player(
        player_id: str, values: dict[str, Any], dry_run: bool = False, ctx: Context | None = None
    ) -> SaveResult:
        """
        Bulk-save player config values.

        :param player_id: The player identifier.
        :param values: key->value map.
        :param dry_run: When True, return a diff and do not persist.
        :param ctx: FastMCP context (auto-populated).
        """
        return await _write_bulk(
            mass,
            "player",
            player_id,
            values,
            dry_run=dry_run,
            ctx=ctx,
            require_confirmation=require_confirmation,
            secret_writes_enabled=secret_writes_enabled,
        )

    @sub.tool(
        tags={Tag.CONFIG_WRITE_PLAYER},
        annotations=ToolAnnotations(
            title="Save player DSP config", destructiveHint=True, idempotentHint=False
        ),
        timeout=TIMEOUT_INTERACTIVE,
    )
    async def save_dsp(
        player_id: str, dsp: dict[str, Any], dry_run: bool = False, ctx: Context | None = None
    ) -> SaveResult:
        """
        Save a player's DSP configuration (enabled, gains, filter chain).

        The payload is parsed via DSPConfig.from_dict and validated (gains
        must be within -60..60 dB, filters well-formed). See also:
        config_get_dsp for the current DSP shape.

        :param player_id: The player identifier.
        :param dsp: DSPConfig as a dict (enabled, input_gain, output_gain, filters).
        :param dry_run: When True, report the proposed change without writing.
        :param ctx: FastMCP context (auto-populated).
        """
        import json  # noqa: PLC0415

        from music_assistant_models.dsp import DSPConfig  # noqa: PLC0415

        if len(json.dumps(dsp, default=str)) > _SAVE_PAYLOAD_CAP_BYTES:
            raise ToolError("save payload exceeds 64 KB cap")
        try:
            cfg = DSPConfig.from_dict(dsp)
            cfg.validate()
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"invalid DSP config: {exc}") from exc
        if dry_run:
            return SaveResult(
                target_type="player_dsp",
                target_id=player_id,
                applied=False,
                changes=[],
                requires_reload=False,
                audit_log_id="",
                diff=None,
            )
        await confirm_or_raise(
            ctx, f"Save DSP config for player {player_id!r}?", enabled=require_confirmation
        )
        audit = _audit_id()
        LOGGER.info("config_save_dsp player=%s audit_id=%s", player_id, audit)
        try:
            await mass.config.save_dsp_config(player_id, cfg)
        except Exception as exc:
            raise ToolError(f"DSP save failed: {exc}") from exc
        return SaveResult(
            target_type="player_dsp",
            target_id=player_id,
            applied=True,
            changes=[],
            requires_reload=False,
            audit_log_id=audit,
            diff=None,
        )
