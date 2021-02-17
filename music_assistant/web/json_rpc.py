"""JSON RPC API endpoint (mostly) compatible with LMS."""

from aiohttp.web import Request, Response
from music_assistant.helpers.web import require_local_subnet


@require_local_subnet
async def json_rpc_endpoint(request: Request):
    """
    Implement basic jsonrpc interface compatible with LMS.

    for some compatability with tools that talk to LMS
    only support for basic commands
    """
    # pylint: disable=too-many-branches
    data = await request.json()
    params = data["params"]
    player_id = params[0]
    cmds = params[1]
    cmd_str = " ".join(cmds)
    if cmd_str == "play":
        await request.app["mass"].players.cmd_play(player_id)
    elif cmd_str == "pause":
        await request.app["mass"].players.cmd_pause(player_id)
    elif cmd_str == "stop":
        await request.app["mass"].players.cmd_stop(player_id)
    elif cmd_str == "next":
        await request.app["mass"].players.cmd_next(player_id)
    elif cmd_str == "previous":
        await request.app["mass"].players.cmd_previous(player_id)
    elif "power" in cmd_str:
        powered = cmds[1] if len(cmds) > 1 else False
        if powered:
            await request.app["mass"].players.cmd_power_on(player_id)
        else:
            await request.app["mass"].players.cmd_power_off(player_id)
    elif cmd_str == "playlist index +1":
        await request.app["mass"].players.cmd_next(player_id)
    elif cmd_str == "playlist index -1":
        await request.app["mass"].players.cmd_previous(player_id)
    elif "mixer volume" in cmd_str and "+" in cmds[2]:
        player = request.app["mass"].players.get_player(player_id)
        volume_level = player.volume_level + int(cmds[2].split("+")[1])
        await request.app["mass"].players.cmd_volume_set(player_id, volume_level)
    elif "mixer volume" in cmd_str and "-" in cmds[2]:
        player = request.app["mass"].players.get_player(player_id)
        volume_level = player.volume_level - int(cmds[2].split("-")[1])
        await request.app["mass"].players.cmd_volume_set(player_id, volume_level)
    elif "mixer volume" in cmd_str:
        await request.app["mass"].players.cmd_volume_set(player_id, cmds[2])
    elif cmd_str == "mixer muting 1":
        await request.app["mass"].players.cmd_volume_mute(player_id, True)
    elif cmd_str == "mixer muting 0":
        await request.app["mass"].players.cmd_volume_mute(player_id, False)
    elif cmd_str == "button volup":
        await request.app["mass"].players.cmd_volume_up(player_id)
    elif cmd_str == "button voldown":
        await request.app["mass"].players.cmd_volume_down(player_id)
    elif cmd_str == "button power":
        await request.app["mass"].players.cmd_power_toggle(player_id)
    else:
        return Response(text="command not supported")
    return Response(text="success")
