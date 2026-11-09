# Player lifecycle scenarios

Acceptance scenarios for attaching and detaching the protocols of a device: what a client sees
when a provider finds, loses or re-identifies a device, after a restart, or when the user removes
something. They drive a real server only through what providers and clients use (register and
unregister, reported state, `players/all`, events, queues, the config API), so they stay valid
while the players controller is rebuilt for the player split
(music-assistant/backlog `docs/plans/player-split.md`, "Scenario suite").

## The harness

`harness.py` holds the scenario DSL, `conftest.py` the `scenario` fixture, `test_harness.py` the
tests of the harness itself.

- **Server**: a real `MusicAssistant` per scenario on a temporary folder (`full_mass_context`).
  `restart()` boots a new one on the same `settings.json` and cache, `seed_config()` does the
  same with values merged into (or, for a None value, deleted from) the stored config.
- **Providers**: every player provider domain except `universal_player`, `universal_group` and
  `sync_group` loads as a `FakePlayerProvider`. It keeps its real domain, as protocol linking keys
  on it (priority, one protocol per domain), and a domain can have several instances.
- **Devices**: `provider.protocol(...)` creates a device and its `FakeProtocol`. The scenario sets
  what it reports; its commands are recorded in `protocol.commands` and applied to its state.
  `role="native"` or `role="audio"` stands for the role of the protocol on its device. A device
  is present from `attach()` until `detach()` (or `set_present()`), and like a real provider the
  fake one attaches the present devices when it loads (boot, reload) and 5 s after a player of it
  gets enabled. A protocol object belongs to one server and provider instance: after a restart or
  a reload, `scenario.protocol(protocol_id)` returns the current one.
- **Time**: a virtual clock, starting at 1000.0. It stands still, also while the server works,
  and only moves through `advance(seconds)`, which stops at every timer that falls due on the way
  (the 15 s and 45 s protocol evaluation delays, debounces, polls), and `idle()`, which lets 6 s
  pass for the server's debounces and follow-up checks. `time.monotonic()` and `time.time()` of
  every server module follow it.
- **Settling**: every action runs what is due at the current time until the loop is quiet:
  nothing runnable, no timer due, no work in a thread. What the server put on a timer only runs
  as the clock moves, so assert steady state, or that something did not happen, after `idle()`.
- **Failures**: a failure the server only logs (an ERROR record, a record with a traceback, an
  "Exception in task") and a failed `command()` that is never awaited fail the scenario when it
  closes; `expect_log(substring)` allows one on purpose.
- **ARP**: `scenario.arp[ip] = mac` is the MAC address an IP address resolves to.

| Action | Does |
|---|---|
| `add_provider(domain)`, `reload_provider(provider)`, `remove_provider(provider)` | load a new fake provider instance, reload one, remove one as a user does |
| `attach(protocol, role=None)`, `detach(protocol)` | the provider found the device (again, possibly in a new role), or lost it |
| `report(protocol, **state)` | the device reports new state (`available`, `powered`, `identifiers`, ...) |
| `remove(player_id)`, `enable(id)`, `disable(id)`, `configure_player(id, **values)` | what a user does with a player |
| `add_power_control(id, name)`, `switch(control, powered)` | an external power control (select it with `configure_player(id, power_control=...)`), switched by hand |
| `command(coro)` | run a command in the background, e.g. one that waits for a device; await the task |
| `advance(seconds)`, `idle()`, `settle()` | let time pass, let the debounces run, run what is due now |
| `restart()`, `seed_config(values)` | boot again on the stored config, optionally changed first |

| Observation | Returns |
|---|---|
| `visible_players()` | the `players/all` listing as `VisiblePlayer` records |
| `players_carrying(*protocols)` | the visible players carrying these protocols |
| `single_player(*protocols)` | the one visible player carrying them, a precondition (raises `ScenarioError`) |
| `player(id)`, `queue(id)`, `await player_config(id)` | a player state, a queue, a stored player config |
| `events(*types)`, `clear_events()` | the emitted events in order, from the boot on; repeats of an update event collapsed |
| `provider(domain_or_id)`, `protocol(protocol_id)` | the loaded fake provider, the current protocol object of a device |

For a client command the DSL does not wrap, call the server's own API on `scenario.mass` (e.g.
`await scenario.mass.players.cmd_power(player_id, True)`) and `await scenario.settle()` after it.

What `Scenario._patch_environment()` patches:

- `MusicAssistant.__load_provider_manifests` (private), so a fake domain has a manifest without
  requirements, dependencies or network discovery that allows several instances; the events of a
  boot are captured from there on;
- `music_assistant.mass.load_provider_module`, which returns the fake provider for a fake domain;
- `music_assistant.helpers.util.get_mac_address`, the ARP lookup;
- `time` and `run_in_executor` of the test's event loop, and the `time` (or `time()` and
  `monotonic()`) names of every loaded server module, for the clock and for knowing about thread
  work;
- `aiosqlite.core.Connection._execute` (private), the thread dispatch of the database, which does
  not go through the loop's executor.

`settle()` and `advance()` read the private `_ready` and `_scheduled` of the event loop, and
`add_provider()` calls the private `config._create_provider_instance`, the end of the
add-provider setup flow (which itself would import the real provider's setup module). Scenarios
use none of these.

## Example

```python
async def test_native_device_attaches_as_available_player(scenario: Scenario) -> None:
    """A native protocol that attaches is listed as an available player with a queue."""
    sonos = await scenario.add_provider("sonos")
    speaker = sonos.protocol("RINCON_1", "Kitchen", identifiers={IdentifierType.MAC_ADDRESS: MAC})

    await scenario.attach(speaker)
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

Take player ids from the observations rather than from the protocol ids: new players get minted
ids once a player is no longer the provider's object.

## One xfail marker per phase

A scenario states one behaviour. When it asserts today's behaviour that the target changes, its
docstring says so. When it describes the target and fails today, it carries the phase that makes it
pass (issue numbers refer to music-assistant/backlog):

```python
@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase 1 (#415): a player outlives its detached protocol",
)
```

The marker is strict, so a scenario that starts to pass fails the run until its marker is removed.
An expected failure only counts when the test module raises it itself, in the test (not a fixture):
`conftest.py` turns any other into a failure, so a failing harness precondition (`ScenarioError`),
a server error or a renamed internal can not pass as the expected one. `raises=AssertionError`
narrows it to the scenario's own asserts; leave it out only where the target API does not exist
yet and an AttributeError in the test is the expected failure. A phase is done when all of its
markers are gone; the scenarios of today's behaviour it changes are rewritten in the same PR.
