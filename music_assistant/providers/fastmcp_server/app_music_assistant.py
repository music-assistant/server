"""Optional Prefab MCP App using the canonical dynamic execution pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from fastmcp import Context  # noqa: TC002 -- FastMCP resolves injected annotations at runtime.

from .errors import ToolFailureCode, tool_failure

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .dynamic_api import DynamicAPIAdapter

APP_TOOL_NAME = "app_music_assistant"
_APP_NAME = "Music Assistant"


class MusicAssistantAction(StrEnum):
    """Closed app action vocabulary; arbitrary MA command names are forbidden."""

    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
    PREVIOUS = "previous"
    NEXT = "next"
    POWER = "power"
    MUTE = "mute"
    VOLUME = "volume"
    QUEUE_MOVE = "queue_move"
    QUEUE_REMOVE = "queue_remove"


def register_music_assistant_app(mcp: FastMCP, adapter: DynamicAPIAdapter) -> None:
    """Lazily register one model-visible UI and two app-only backend tools."""
    import os  # noqa: PLC0415

    from fastmcp import FastMCPApp  # noqa: PLC0415
    from mcp.types import ToolAnnotations  # noqa: PLC0415

    if os.environ.get("PREFAB_RENDERER_URL"):
        raise RuntimeError("External Prefab renderers are disabled by the MCP App CSP")
    os.environ["PREFAB_BUNDLED_RENDERER"] = "1"
    app = FastMCPApp(_APP_NAME)

    @app.tool(name="ma_app_state")
    async def ma_app_state(
        player_id: str | None = None, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Refresh permitted player, now-playing and queue state for the app."""
        return await _load_state(adapter, player_id, _required_context(ctx))

    @app.tool(name="ma_app_action")
    async def ma_app_action(
        action: MusicAssistantAction,
        player_id: str,
        item_id: str | None = None,
        target_index: int | None = None,
        value: int | bool | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Execute one closed player or queue action and return refreshed state."""
        context = _required_context(ctx)
        try:
            await _execute_action(
                adapter,
                action,
                player_id,
                item_id=item_id,
                target_index=target_index,
                value=value,
                ctx=context,
            )
        except TypeError, ValueError:
            raise tool_failure(
                ToolFailureCode.INVALID_ARGUMENTS,
                "Invalid app action arguments",
            ) from None
        return {
            "ok": True,
            "action": action.value,
            "state": await _load_state(adapter, player_id, context),
        }

    @app.ui(
        name=APP_TOOL_NAME,
        title="Music Assistant",
        description="Open a compact player, Now Playing, transport, volume, power and queue UI.",
        annotations=ToolAnnotations(
            title="Music Assistant",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def app_music_assistant(
        player_id: str | None = None,
        ctx: Context | None = None,
    ) -> Any:
        """Open the Music Assistant player and queue interface."""
        state = await _load_state(adapter, player_id, _required_context(ctx))
        visible = (await adapter.catalog_context()).view.by_name
        return _build_prefab(
            state,
            ma_app_state=ma_app_state,
            ma_app_action=ma_app_action,
            visible_names=frozenset(visible),
        )

    mcp.add_provider(app)


def _required_context(ctx: Context | None) -> Context:
    """Fail closed if a handler is invoked without FastMCP request context."""
    if ctx is None:
        from .errors import ToolFailureCode, tool_failure  # noqa: PLC0415

        raise tool_failure(ToolFailureCode.EXECUTION_FAILED, "MCP request context is unavailable")
    return ctx


async def _call(
    adapter: DynamicAPIAdapter,
    command: str,
    arguments: dict[str, Any],
    ctx: Context,
) -> Any:
    """Invoke one canonical MA command through the shared bounded pipeline."""
    envelope = await adapter.call(
        f"ma_api:{command}",
        arguments,
        response_mode="compact",
        fields=None,
        max_items=200,
        ctx=ctx,
    )
    return envelope.get("data")


async def _load_state(
    adapter: DynamicAPIAdapter,
    player_id: str | None,
    ctx: Context,
) -> dict[str, Any]:
    """Read both required state sections through query capabilities."""
    players_value = await _call(adapter, "players/all", {}, ctx)
    queues_value = await _call(adapter, "player_queues/all", {}, ctx)
    players = [item for item in players_value or [] if isinstance(item, dict)]
    queues = [item for item in queues_value or [] if isinstance(item, dict)]
    selected = player_id or _first_identifier(players, "player_id")
    player = next((item for item in players if item.get("player_id") == selected), None)
    queue: dict[str, Any] | None = None
    items: list[dict[str, Any]] = []
    if selected:
        queue_value = await _call(
            adapter,
            "player_queues/get_active_queue",
            {"player_id": selected},
            ctx,
        )
        if isinstance(queue_value, dict):
            queue = queue_value
            queue_id = queue.get("queue_id")
            if isinstance(queue_id, str) and queue_id:
                items_value = await _call(
                    adapter,
                    "player_queues/items",
                    {"queue_id": queue_id, "limit": 200},
                    ctx,
                )
                items = [item for item in items_value or [] if isinstance(item, dict)]
    return {
        "players": players,
        "queues": queues,
        "selected_player_id": selected,
        "player": player,
        "queue": queue,
        "items": items,
    }


async def _execute_action(
    adapter: DynamicAPIAdapter,
    action: MusicAssistantAction,
    player_id: str,
    *,
    item_id: str | None,
    target_index: int | None,
    value: int | bool | None,
    ctx: Context,
) -> None:
    """Translate a closed action into one canonical command and arguments."""
    simple = {
        MusicAssistantAction.PLAY: "players/cmd/play",
        MusicAssistantAction.PAUSE: "players/cmd/pause",
        MusicAssistantAction.STOP: "players/cmd/stop",
        MusicAssistantAction.PREVIOUS: "players/cmd/previous",
        MusicAssistantAction.NEXT: "players/cmd/next",
    }
    if action in simple:
        await _call(adapter, simple[action], {"player_id": player_id}, ctx)
        return
    if action is MusicAssistantAction.POWER:
        await _call(
            adapter,
            "players/cmd/power",
            {"player_id": player_id, "powered": _required_bool(value)},
            ctx,
        )
        return
    if action is MusicAssistantAction.MUTE:
        await _call(
            adapter,
            "players/cmd/volume_mute",
            {"player_id": player_id, "muted": _required_bool(value)},
            ctx,
        )
        return
    if action is MusicAssistantAction.VOLUME:
        volume = _required_int(value, "volume")
        if not 0 <= volume <= 100:
            raise ValueError("volume must be between 0 and 100")
        await _call(
            adapter,
            "players/cmd/volume_set",
            {"player_id": player_id, "volume_level": volume},
            ctx,
        )
        return

    queue_value = await _call(
        adapter,
        "player_queues/get_active_queue",
        {"player_id": player_id},
        ctx,
    )
    queue_id = queue_value.get("queue_id") if isinstance(queue_value, dict) else None
    if not isinstance(queue_id, str) or not queue_id or not item_id:
        raise ValueError("A current queue item is required")
    if action is MusicAssistantAction.QUEUE_REMOVE:
        await _call(
            adapter,
            "player_queues/delete_item",
            {"queue_id": queue_id, "item_id_or_index": item_id},
            ctx,
        )
        return
    if action is MusicAssistantAction.QUEUE_MOVE:
        wanted = _required_int(target_index, "target_index")
        items_value = await _call(
            adapter,
            "player_queues/items",
            {"queue_id": queue_id, "limit": 200},
            ctx,
        )
        current = next(
            (
                int(item.get("index", index))
                for index, item in enumerate(items_value or [])
                if isinstance(item, dict)
                and item.get("queue_item_id", item.get("item_id")) == item_id
            ),
            None,
        )
        if current is None:
            raise ValueError("Queue item no longer exists")
        await _call(
            adapter,
            "player_queues/move_item",
            {"queue_id": queue_id, "queue_item_id": item_id, "pos_shift": wanted - current},
            ctx,
        )
        return
    raise ValueError("Unsupported app action")


def _required_bool(value: int | bool | None) -> bool:
    if not isinstance(value, bool):
        raise TypeError("A boolean value is required")
    return value


def _required_int(value: int | bool | None, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _first_identifier(items: list[dict[str, Any]], key: str) -> str | None:
    for item in items:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _build_prefab(
    state: dict[str, Any],
    *,
    ma_app_state: Any,
    ma_app_action: Any,
    visible_names: frozenset[str],
) -> Any:
    """Build an adaptive, self-contained Prefab component tree."""
    from prefab_ui import PrefabApp  # noqa: PLC0415
    from prefab_ui.actions import CallTool, ShowToast  # noqa: PLC0415
    from prefab_ui.components import (  # noqa: PLC0415
        Badge,
        Button,
        Card,
        Column,
        Heading,
        Row,
        Select,
        SelectOption,
        Separator,
        Slider,
        Text,
    )

    player_id = state.get("selected_player_id")
    player = state.get("player") or {}
    queue = state.get("queue") or {}
    current = queue.get("current_item") or player.get("current_media") or {}

    def action_button(
        label: str,
        action: MusicAssistantAction,
        command: str,
        **arguments: Any,
    ) -> None:
        if f"ma_api:{command}" not in visible_names:
            return
        Button(
            label,
            size="sm",
            variant="outline",
            on_click=CallTool(
                ma_app_action,
                arguments={
                    "action": action.value,
                    "player_id": "{{ selected_player }}",
                    **arguments,
                },
                on_success=ShowToast("Music Assistant updated", variant="success"),
                on_error=ShowToast("Action was not completed", variant="error"),
            ),
        )

    with (
        PrefabApp(
            title="Music Assistant",
            state={"selected_player": player_id, "volume": player.get("volume_level", 0)},
            css_class="max-w-3xl mx-auto",
        ) as ui,
        Column(gap=4),
    ):
        with Row(justify="between", align="center"):
            Heading("Music Assistant", level=2)
            Button(
                "Refresh",
                size="sm",
                variant="ghost",
                on_click=CallTool(
                    ma_app_state,
                    arguments={"player_id": "{{ selected_player }}"},
                    on_success=ShowToast("State refreshed", variant="success"),
                    on_error=ShowToast("Refresh failed", variant="error"),
                ),
            )
        with Select(name="selected_player", value=player_id, placeholder="Select a player"):
            for candidate in state["players"]:
                identifier = candidate.get("player_id")
                if isinstance(identifier, str):
                    SelectOption(value=identifier, label=str(candidate.get("name") or identifier))

        with Card(css_class="p-4"):
            Heading("Now Playing", level=3)
            title = current.get("name") if isinstance(current, dict) else None
            Text(str(title or "Nothing playing"), bold=True)
            Text(str(player.get("name") or player_id or "No player selected"))
            Badge(str(player.get("state") or queue.get("state") or "idle"))
            with Row(gap=2, css_class="flex-wrap mt-3"):
                action_button("Previous", MusicAssistantAction.PREVIOUS, "players/cmd/previous")
                action_button("Play", MusicAssistantAction.PLAY, "players/cmd/play")
                action_button("Pause", MusicAssistantAction.PAUSE, "players/cmd/pause")
                action_button("Stop", MusicAssistantAction.STOP, "players/cmd/stop")
                action_button("Next", MusicAssistantAction.NEXT, "players/cmd/next")
                action_button(
                    "Power",
                    MusicAssistantAction.POWER,
                    "players/cmd/power",
                    value=not bool(player.get("powered", True)),
                )
                action_button(
                    "Mute",
                    MusicAssistantAction.MUTE,
                    "players/cmd/volume_mute",
                    value=not bool(player.get("volume_muted", False)),
                )
            if "ma_api:players/cmd/volume_set" in visible_names:
                Slider(
                    name="volume",
                    value=int(player.get("volume_level") or 0),
                    min=0,
                    max=100,
                    onChange=CallTool(
                        ma_app_action,
                        arguments={
                            "action": MusicAssistantAction.VOLUME.value,
                            "player_id": "{{ selected_player }}",
                            "value": "{{ volume }}",
                        },
                    ),
                )

        Heading("Queue", level=3)
        for index, item in enumerate(state["items"]):
            item_id = item.get("queue_item_id", item.get("item_id"))
            if not isinstance(item_id, str):
                continue
            with Card(css_class="p-3"), Row(justify="between", align="center"):
                with Column(gap=1):
                    Text(str(item.get("name") or "Queue item"), bold=True)
                    Text(str(item.get("artist") or item.get("artists") or ""))
                with Row(gap=1):
                    action_button(
                        "↑",
                        MusicAssistantAction.QUEUE_MOVE,
                        "player_queues/move_item",
                        item_id=item_id,
                        target_index=max(0, index - 1),
                    )
                    action_button(
                        "↓",
                        MusicAssistantAction.QUEUE_MOVE,
                        "player_queues/move_item",
                        item_id=item_id,
                        target_index=index + 1,
                    )
                    action_button(
                        "Remove",
                        MusicAssistantAction.QUEUE_REMOVE,
                        "player_queues/delete_item",
                        item_id=item_id,
                    )
            Separator()
    return ui
