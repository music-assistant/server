"""Exercise the shipped native MSX plugin with real provider WebSocket frames."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from music_assistant_models.player import PlayerMedia

from music_assistant.providers.msx_bridge import http_server

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from music_assistant.providers.msx_bridge.player import MSXPlayer
    from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider

NODE_RUNNER = """
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let handler, ws, timer;
const sent = [];
class Socket {
    static OPEN = 1;
    constructor() { this.readyState = 1; ws = this; }
    send(data) { sent.push(JSON.parse(data)); }
}
const context = {
    window: { location: { protocol: 'http:', host: 'ma:8099' } },
    document: { querySelectorAll: () => [] },
    console: { log() {}, warn() {} },
    navigator: { userAgent: 'MSX test' },
    WebSocket: Socket,
    Date: { now: () => 0 },
    setInterval: fn => { timer = fn; return 1; },
    clearInterval: () => { timer = null; },
    setTimeout: () => 1,
    clearTimeout() {},
    tvx: {
        VideoPlugin: { requestDeviceId: cb => cb({ deviceId: 'tv' }) },
        PluginTools: { onReady: fn => fn() },
        InteractionPlugin: { setupHandler: obj => { handler = obj; }, init() {}, executeAction() {} }
    }
};
vm.runInNewContext(input.script, context);
handler.handleRequest('init', null, () => {});
handler.handleEvent({ event: 'video:play' });
handler.handleEvent({ event: 'video:seek', position: 20 });
handler.handleEvent({ event: 'video:pause' });
for (const frame of input.frames) ws.onmessage({ data: JSON.stringify(frame) });
handler.handleEvent({ event: 'video:play' });
timer();
process.stdout.write(JSON.stringify(sent.filter(msg => msg.type === 'position').at(-1).position));
"""


def plugin_position_after_resume(frames: list[dict[str, Any]]) -> float:
    """Return the position reported by native playback after a pause and supplied frames."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Native plugin runtime tests require Node.js")
    html = (Path(http_server.__file__).parent / "static/plugin.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    result = subprocess.run(  # noqa: S603 - fixed local runtime and shipped plugin
        [node, "-e", NODE_RUNNER],
        input=json.dumps({"script": script, "frames": frames}),
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(result.stdout))


async def test_native_next_from_pause_starts_new_item_at_zero(
    provider: MSXBridgeProvider,
    player: MSXPlayer,
    mass_mock: Mock,
) -> None:
    """A real suppressed play_media transition resets the native clock before video:play."""
    server = http_server.MSXHTTPServer(provider, 0)
    provider.http_server = server
    ws = Mock(closed=False, send_str=AsyncMock())
    server._ws_clients[player.player_id] = {ws}
    tasks: list[asyncio.Task[None]] = []

    def schedule(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    mass_mock.create_task = schedule
    with player.suppress_ws_notify():
        await player.play_media(
            PlayerMedia(uri="http://ma/next", source_id="queue", queue_item_id="2")
        )
    await asyncio.gather(*tasks)
    frames = [json.loads(call.args[0]) for call in cast("AsyncMock", ws.send_str).await_args_list]
    assert plugin_position_after_resume(frames) == 0


def test_native_same_item_resume_preserves_paused_position() -> None:
    """A pause/resume or no-op navigation with no item transition retains twenty seconds."""
    assert plugin_position_after_resume([]) == 20
