"""
Regression tests for domain-less core config blocks (issue #6278).

The background tasks controller persists its scheduler state with a raw path write to
``core/tasks/scheduled_task_states``. On a fresh install that block does not exist yet,
so the underlying ``set`` helper created it on the fly and left a stub without the
``domain`` key that ``CoreConfig`` requires. Listing the core configs then crashed with
"Field 'domain' of type str is missing in CoreConfig instance".

Two complementary fixes are covered here. The write path creates a valid CoreConfig base
object first, so a raw write can no longer leave a domain-less stub behind (root cause).
The read path fills in a missing ``domain``, so installs that already carry such a stub
on disk render their core config again.
"""

from __future__ import annotations

from music_assistant.constants import CONF_CORE, CONFIGURABLE_CORE_CONTROLLERS
from music_assistant.mass import MusicAssistant


async def test_fresh_install_leaves_a_parsable_tasks_core_config(mass: MusicAssistant) -> None:
    """
    A first start on empty storage must not leave a domain-less core/tasks block behind.

    This is the reported scenario. No settings.json exists yet, so the repair in
    ``migrate()`` never runs and only the write path can keep the block valid.
    """
    raw_conf = mass.config.get(f"{CONF_CORE}/tasks")
    assert raw_conf["domain"] == "tasks"
    # the startup task registrations have persisted their state into the same block
    assert raw_conf["scheduled_task_states"]

    configs = await mass.config.get_core_configs()
    assert {config.domain for config in configs} == set(CONFIGURABLE_CORE_CONTROLLERS)


async def test_persisting_task_state_keeps_the_core_config_valid(mass: MusicAssistant) -> None:
    """A raw task-state write into an absent core block leaves a parsable CoreConfig."""
    mass.config.remove(f"{CONF_CORE}/tasks")
    assert mass.config.get(f"{CONF_CORE}/tasks") is None

    mass.tasks._set_persisted_task_states({"some_task": {"status": "idle"}})

    raw_conf = mass.config.get(f"{CONF_CORE}/tasks")
    assert raw_conf["domain"] == "tasks"
    assert raw_conf["scheduled_task_states"] == {"some_task": {"status": "idle"}}
    # the state write must not have clobbered the config values structure
    assert raw_conf["values"] == {}


async def test_persisting_task_state_repairs_an_existing_stub(mass: MusicAssistant) -> None:
    """A stub written by an older version is repaired by the next task-state write."""
    mass.config.set(f"{CONF_CORE}/tasks", {"scheduled_task_states": {"old_task": {}}})

    mass.tasks._set_persisted_task_states({"some_task": {"status": "idle"}})

    assert mass.config.get(f"{CONF_CORE}/tasks/domain") == "tasks"


async def test_get_core_configs_survives_a_domain_less_stub(mass: MusicAssistant) -> None:
    """Listing the core configs works even when a stored block has no 'domain' key."""
    mass.config.set(f"{CONF_CORE}/tasks", {"scheduled_task_states": {"some_task": {}}})

    configs = await mass.config.get_core_configs()

    by_domain = {config.domain for config in configs}
    assert by_domain == set(CONFIGURABLE_CORE_CONTROLLERS)


async def test_get_core_config_survives_a_domain_less_stub(mass: MusicAssistant) -> None:
    """Reading a single core config works even when its stored block has no 'domain' key."""
    mass.config.set(f"{CONF_CORE}/tasks", {"scheduled_task_states": {"some_task": {}}})

    config = await mass.config.get_core_config("tasks")

    assert config.domain == "tasks"
