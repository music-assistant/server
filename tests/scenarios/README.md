# Player lifecycle scenarios

Acceptance scenarios of the players controller: what a client sees when a provider finds, loses
or re-identifies a device, after a restart, when the user removes, merges or groups players, and
what commands, outputs and volume do. They drive a real server only through what providers and
clients use (register and unregister, reported state, `players/all`, events, queues, the config API),
so they stay valid whatever the controller does inside.

## The harness

`harness.py` holds the scenario DSL, `conftest.py` the `scenario` fixture, `test_harness.py` the
tests of the harness itself.

- **Server**: a real `MusicAssistant` per scenario on a temporary folder (`full_mass_context`).
  `restart()` boots a new one on the same `settings.json` and cache, `seed_config()` does the
  same with values merged into (or, for a None value, deleted from) the stored config, and
  `seed_pre_split_config()` with player configs as a version from before the player split
  stored them, which the boot then migrates.
- **Providers**: every player provider domain except `universal_group` and `sync_group` loads
  as a `FakePlayerProvider`. It keeps its real domain, as protocol linking keys on it (priority,
  one protocol per domain), with as many instances as the real domain allows.
- **Devices**: `provider.protocol(...)` creates a device and its `FakeProtocol`. The scenario
  sets what it reports; its commands are recorded in `protocol.commands` and applied to its
  state. A device that a leader adds to its group (`set_members`) leaves the group it was in,
  as a real one does; `native_grouping_requires_own_stream` is reported like any other state. `role="native"`, `"audio"`, `"control"` or `"virtual"` (a group) stands for the role of
  the protocol on its device, `session_only=True` for a protocol that reports it exists only
  while attached (a browser session). Every fake protocol has one config entry of its own,
  `FAKE_SETTING`, like a provider setting of the device. A device is present from `register()`
  until `unregister()` (or `set_present()`), and like a real provider the fake one registers the
  present devices when it loads (boot, reload) and on every discovery request of the server
  (5 s after a player of it gets enabled, removed or merged, for example). A protocol object belongs to one server and provider instance: after a restart or a
  reload, `scenario.protocol(protocol_id)` returns the current one.
- **Music**: `play(player_id)` plays a track on a player through its queue, as a client does
  (`player_queues/play_media`). The track is one of the server's own `test` music provider
  (fake media items, no network), which loads on first use. No audio is decoded: the buffer a
  queue fills ahead of playback stays empty, as no fake protocol fetches audio, so ffmpeg never
  runs. The saved queue (`saved_queue()`) is what the server restores a queue from after a
  restart.
- **Time**: a virtual clock, starting at 1000.0. It stands still, also while the server works,
  and only moves through `advance(seconds)`, which stops at every timer that falls due on the way
  (the 15 s an audio protocol waits for the hardware address of its device, debounces, polls),
  and `idle()`, which lets 10 s pass for the server's debounces and follow-up checks and fails
  (`ScenarioError`) when the server still has work due within as long or a protocol evaluation
  pending at all, which a scenario lets happen with `advance()`.
  Timers due at one time run in the order they were armed. `time.monotonic()` and `time.time()`
  of every server module follow the clock.
- **Settling**: every action runs what is due at the current time until the loop is quiet:
  nothing runnable, no timer due, no work in a thread. What the server put on a timer only runs
  as the clock moves, so assert steady state, or that something did not happen, after `idle()`.
  A server call that waits for the clock (a timeout, a delay, a device to come online) never
  returns when it is awaited directly: run it with `command()`, move the clock, and await the
  task once it is done.
- **Failures**: when the scenario closes, it fails (`ScenarioError`) on every warning or worse
  the server logged (except the banners of a boot), every asyncio error, a provider that failed
  to unload, a `command()` that failed and was never awaited or failed in its cleanup, and any
  task or timer left once the server stopped; `expect_log(substring)` allows a log on purpose.
- **ARP**: `scenario.arp[ip] = mac` is the MAC address an IP address resolves to.

| Action | Does |
|---|---|
| `add_provider(domain)`, `reload_provider(provider)`, `remove_provider(provider)` | load a new fake provider instance, reload one, remove one as a user does |
| `register(protocol, role=None)`, `unregister(protocol)` | the provider found the device (again, possibly in a new role), or lost it |
| `report(protocol, **state)` | the device reports new state (`available`, `powered`, `identifiers`, ...); a None identifier value drops it |
| `remove(player_id)`, `enable(id)`, `disable(id)`, `configure_player(id, **values)` | what a user does with a player |
| `add_power_control(id, name)`, `switch(control, powered)` | an external power control (select it with `configure_player(id, power_control=...)`), switched by hand |
| `play(player_id)` | play a track on the player through its queue, as a client does; returns once the player reports playing |
| `command(coro)` | run a server call that may wait for the clock in the background; await the task once it is done |
| `advance(seconds)`, `idle()`, `settle()` | let time pass, let the debounces run, run what is due now |
| `restart()`, `seed_config(values)` | boot again on the stored config, optionally changed first |
| `seed_pre_split_config(players)` | boot again on player configs from before the player split, which the boot migrates |

| Observation | Returns |
|---|---|
| `visible_players()` | the `players/all` listing as `VisiblePlayer` records |
| `players_carrying(*protocols)` | the visible players carrying these protocols |
| `single_player(*protocols)` | the one visible player carrying them, a precondition (raises `ScenarioError`) |
| `player_of(protocol)` | the id of the player a protocol is attached to, None when none |
| `player(id)`, `queue(id)`, `await player_config(id)` | a player state, a queue, a stored player config |
| `await saved_queue(queue_id)` | the URIs of the saved items of a queue, None when none were ever saved; a pending (debounced) save is written first |
| `await player_configs()` | the stored player configs as the settings page lists them, disabled ones included |
| `protocol_config(protocol_id)` | the stored config values of a protocol object, None when none is stored |
| `await stored_config_mentioning(text)` | every key and value of the stored config (settings.json) that mentions the text, e.g. a protocol id |
| `wall_time()` | the wall clock time as the server reads it (`time.time()`), e.g. for an `elapsed_time_last_updated` a device reports |
| `timeline` | every command any device received, in order, as (protocol id, command), as each protocol records it in `protocol.commands` |
| `events(*types)`, `clear_events()` | the emitted events in order, from the boot on; repeats of an update event collapsed |
| `removals()` | the player id and the data of every `PLAYER_REMOVED` event in order, from the boot on |
| `provider(domain_or_id)`, `protocol(protocol_id)` | the loaded fake provider, the current protocol object of a device |

For a client command the DSL does not wrap, call the server's own API on `scenario.mass` (e.g.
`await scenario.mass.players.cmd_power(player_id, True)`) and `await scenario.settle()` after it.

What `Scenario._patch_environment()` patches:

- `MusicAssistant.__load_provider_manifests` (private), so a fake domain has a manifest without
  requirements, dependencies or network discovery; the events of a boot are captured from there
  on;
- `music_assistant.mass.load_provider_module`, which returns the fake provider for a fake domain;
- `MusicAssistant.unload_provider`, to record its failures, which stopping the server drops;
- `music_assistant.helpers.util.get_mac_address`, the ARP lookup;
- `StreamsAudio.get_audio_buffer`, the audio a queue decodes (with ffmpeg) ahead of playback:
  nothing is decoded, as no fake protocol fetches audio, and serving audio to a protocol fails
  with `ScenarioError`;
- `time` and `run_in_executor` of the test's event loop, `asyncio.events.TimerHandle` (for the
  arming order) and the `time` (or `time()` and `monotonic()`) names of every loaded server
  module, for the clock and for knowing about thread work;
- `aiosqlite.core.Connection._execute` (private), the thread dispatch of the database, which does
  not go through the loop's executor.

`settle()` and `advance()` read the private `_ready` and `_scheduled` of the event loop, `idle()`
reads the server's `_tracked_timers` and the players controller's `_pending_protocol_evaluations`,
`add_provider()` and `play()` call the private `config._create_provider_instance`, the end of the
add-provider setup flow (which itself would import the real provider's setup module), and
`saved_queue()` writes a pending save through the queue controller's private
`_save_queue_to_cache`, as the controller's own shutdown does. Scenarios use none of these.

## Example

```python
async def test_native_device_attaches_as_available_player(scenario: Scenario) -> None:
    """A native protocol that attaches is listed as an available player with a queue."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_1", "Kitchen", identifiers={IdentifierType.MAC_ADDRESS: MAC})

    await scenario.register(speaker)
    await scenario.idle()

    assert scenario.players_carrying(speaker) == [
        VisiblePlayer(
            player_id=ANY,
            name="Kitchen",
            available=True,
            powered=None,
            provider="sonos",
            outputs=(("sonos", True),),
        )
    ]
    assert scenario.queue(scenario.single_player(speaker).player_id) is not None
```

Take player ids from the observations rather than from the protocol ids: a new player gets an
id of its own.

## The scenarios document

Every scenario is written out in plain words in the players controller's
[SCENARIOS.md](../../music_assistant/controllers/players/SCENARIOS.md). A new test goes into exactly
one entry there: the entry whose rule it proves, or a new entry. A renamed test is renamed there
too. `tests/controllers/players/test_scenarios_document.py` fails when a test is named by no entry
or by two, when an entry names a test that does not exist, when a heading is no entry heading,
when an end-to-end line lacks what a run needs or why there is none, and when a scenario file
holds a test class: scenarios are functions at the top level of their file.

## Expected failures

A scenario states one behaviour. One that describes a behaviour the server does not have yet
carries a strict marker that names why:

```python
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="the id of a merged player becomes a legacy id",
)
```

The marker is strict, so a scenario that starts to pass fails the run until its marker is removed.
An expected failure only counts when the test module raises it itself, in the test (not a fixture),
and it is no `ScenarioError`: `conftest.py` turns any other into a failure, so a failing harness
precondition, a server error or a renamed internal can not pass as the expected one.
`raises=AssertionError` narrows it to the scenario's own asserts; where the API it needs does not
exist yet, use `raises=(AssertionError, AttributeError)`.
