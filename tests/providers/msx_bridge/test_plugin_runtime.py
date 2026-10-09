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


@pytest.mark.parametrize("close_code", [1000, 1001, 1006])
def test_server_close_reconnects_without_reopening_msx(close_code: int) -> None:
    """Even a clean server shutdown is temporary while the plugin session is active."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Native plugin runtime tests require Node.js")
    html = (Path(http_server.__file__).parent / "static/plugin.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    runner = NODE_RUNNER.split("vm.runInNewContext", 1)[0]
    runner += """
const retries = new Map(); let sequence = 0; const sockets = [];
context.setTimeout = fn => { const id = ++sequence; retries.set(id, fn); return id; };
context.clearTimeout = id => retries.delete(id);
context.WebSocket = class extends Socket {
    constructor(url) { super(); this.url = url; sockets.push(this); }
    close() { this.readyState = 3; if (this.onclose) this.onclose({code: 1000}); }
};
vm.runInNewContext(input.script, context);
handler.handleRequest('init', null, () => {});
ws.onopen();
// Flush debug timers; the device ID timeout is cancelled by its callback.
for (const [id, callback] of [...retries]) { retries.delete(id); callback(); }
const original = ws;
original.readyState = 3; original.onclose({code: input.code});
if (retries.size !== 1) throw new Error('Expected exactly one reconnect timer');
for (const [id, callback] of [...retries]) { retries.delete(id); callback(); }
if (sockets.length !== 2 || ws.url !== original.url) throw new Error('Device did not reconnect');
handler.handleRequest('init', null, () => {});
if (sockets.filter(s => s.readyState === 1).length !== 1) throw new Error('Repeated init leaked sockets');
process.stdout.write('ok');
"""
    result = subprocess.run(  # noqa: S603 - fixed local runtime and shipped plugin
        [node, "-e", runner],
        input=json.dumps({"script": script, "code": close_code}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "ok"


def test_stop_notification_stops_decoder_before_showing_notice() -> None:
    """Stop must not offer Continue on an already stopped MA queue."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Native plugin runtime tests require Node.js")
    html = (Path(http_server.__file__).parent / "static/plugin.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    runner = (
        NODE_RUNNER.split("vm.runInNewContext", 1)[0]
        + """
const actions=[];
context.tvx.InteractionPlugin.executeAction=action=>actions.push(action);
vm.runInNewContext(input.script, context);
handler.handleRequest('init',null,()=>{});
ws.onmessage({data:JSON.stringify({type:'stop',showNotification:true})});
process.stdout.write(JSON.stringify(actions));
"""
    )
    result = subprocess.run(  # noqa: S603 - fixed runtime and shipped plugin
        [node, "-e", runner],
        input=json.dumps({"script": script}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == ["[player:eject|player:hide|info:Playback stopped.]"]


def test_native_seek_requests_source_position_without_faking_decoder_seek() -> None:
    """A source offset belongs in the MA seek request, not in decoder telemetry."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Native plugin runtime tests require Node.js")
    html = (Path(http_server.__file__).parent / "static/plugin.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    runner = (
        NODE_RUNNER.split("vm.runInNewContext", 1)[0]
        + """
const actions=[];
context.tvx.InteractionPlugin.executeAction=action=>actions.push(action);
vm.runInNewContext(input.script,context);
handler.handleRequest('init',null,()=>{});
ws.onmessage({data:JSON.stringify({type:'clock_reset',playback_id:'generation',source_offset:120,source_duration:180,served_duration:60})});
handler.handleEvent({event:'video:play'});
handler.handleData({message:'seek:+10'});
timer();
process.stdout.write(JSON.stringify({sent,actions}));
"""
    )
    result = subprocess.run(  # noqa: S603 - fixed runtime and shipped plugin
        [node, "-e", runner],
        input=json.dumps({"script": script}),
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert {"type": "seek_request", "position": 130, "playback_id": "generation"} in data["sent"]
    assert {"type": "position", "position": 0, "playback_id": "generation"} in data["sent"]
    assert "player:label:position:2:00" in data["actions"]
    assert "player:label:duration:3:00" in data["actions"]
    assert not any(action.startswith("player:seek:") for action in data["actions"])


@pytest.mark.parametrize("version", ["0.1.145", "0.1.146", "0.1.165", None])
@pytest.mark.parametrize("duration", [180, 0])
@pytest.mark.parametrize("delivery", ["immediate", "delayed", "stale", "missing"])
def test_native_progress_respects_framework_version(
    version: str | None, duration: int, delivery: str
) -> None:
    """Older MSX keeps native progress without unsupported override actions."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Native plugin runtime tests require Node.js")
    static = Path(http_server.__file__).parent / "static"
    script = (static / "plugin.html").read_text().split("<script>", 1)[1].split("</script>", 1)[0]
    runner = (
        NODE_RUNNER.split("vm.runInNewContext", 1)[0]
        + """
const actions=[], errors=[];
context.window.addEventListener=()=>{};
context.Date=Date;
vm.runInNewContext(input.library, context);
context.Date={now:()=>0};
context.tvx.PluginTools.checkFramework=context.window.TVXPluginTools.checkFramework;
const info={info:{framework:{name:'MSX',version:input.version},application:{version:'9.9.9'}}};
const callbacks=[];
context.tvx.InteractionPlugin.requestData=(id,cb)=>{
    if (input.delivery==='immediate') cb(info);
    else callbacks.push(cb);
};
context.tvx.InteractionPlugin.executeAction=action=>{
    actions.push(action);
    if ((!input.version || input.version==='0.1.145') && action.startsWith('player:progress:')) {
        errors.push("Unknown player progress action: '"+action.slice(16)+"'");
    }
};
vm.runInNewContext(input.script,context);
handler.handleRequest('init',null,()=>{});
if (input.delivery==='stale') {
    handler.handleRequest('init',null,()=>{});
    callbacks[0]({info:{framework:{name:'MSX',version:'0.1.165'}}});
}
ws.onmessage({data:JSON.stringify({type:'clock_reset',playback_id:'generation',
    source_offset:120,source_duration:input.duration,served_duration:60})});
if (input.delivery!=='immediate') {
    timer();
    if (actions.some(action=>action.startsWith('player:progress:'))) throw Error('Premature override');
    if (input.delivery!=='missing') callbacks.at(-1)(info);
    actions.length=0;
}
handler.handleEvent({event:'video:play'});
timer();
process.stdout.write(JSON.stringify({sent,actions,errors}));
"""
    )
    result = subprocess.run(  # noqa: S603 - fixed runtime and shipped plugin
        [node, "-e", runner],
        input=json.dumps(
            {
                "script": script,
                "library": (static / "tvx-plugin.min.js").read_text(),
                "version": version,
                "duration": duration,
                "delivery": delivery,
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    data = json.loads(result.stdout)
    assert data["errors"] == []
    assert {"type": "position", "position": 0, "playback_id": "generation"} in data["sent"]
    assert "player:label:position:2:00" in data["actions"]
    progress = [action for action in data["actions"] if action.startswith("player:progress:")]
    if delivery != "missing" and version in ("0.1.146", "0.1.165"):
        assert f"player:progress:position:{120 if duration else -1}" in progress
        assert f"player:progress:duration:{duration if duration else -1}" in progress
    else:
        assert progress == []
