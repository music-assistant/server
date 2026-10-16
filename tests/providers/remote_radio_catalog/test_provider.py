"""Exercise real providers and HTTP, with controlled MA config/cache boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession, web
from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature, ProviderType
from music_assistant_models.errors import ResourceTemporarilyUnavailable
from music_assistant_models.provider import ProviderManifest

from music_assistant.helpers.compare import compare_item_ids, compare_radio
from music_assistant.providers.remote_radio_catalog import (
    CACHE_KEY,
    CONF_CATALOG_URL,
    RemoteRadioCatalogProvider,
    setup,
)
from music_assistant.providers.remote_radio_catalog.catalog import parse_catalog
from music_assistant.providers.remote_radio_catalog.setup_flow import run_setup

if TYPE_CHECKING:
    from music_assistant_models.media_items import Radio

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession

DOMAIN = "remote_radio_catalog"


@pytest.fixture
async def harness() -> AsyncGenerator[SimpleNamespace]:  # noqa: PLR0915
    """Provide real local HTTP responses and strictly instance-keyed fake MA storage."""
    documents: dict[str, list[dict[str, str]] | int | str | bytes] = {
        "a": [{"id": "same", "name": "Radio", "url": "https://stream.example/a"}],
        "b": [{"id": "same", "name": "Radio", "url": "https://stream.example/b"}],
    }
    counts = {"a": 0, "b": 0}
    gates: dict[str, asyncio.Event] = {}

    async def serve(request: web.Request) -> web.Response:
        source = request.match_info["source"]
        counts[source] += 1
        if source in gates:
            await gates[source].wait()
        payload = documents[source]
        if isinstance(payload, int):
            return web.Response(status=payload)
        if isinstance(payload, str):
            return web.Response(text=payload)
        if isinstance(payload, bytes):
            return web.Response(body=payload)
        return web.json_response(payload)

    app = web.Application()
    app.router.add_get("/{source}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    snapshots: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
    raw_configs: dict[str, Any] = {}
    timers: dict[str, Any] = {}
    tracked: dict[str, asyncio.Task[Any]] = {}

    async def cache_get(
        key: str, *, provider: str, checksum: str, **_kwargs: Any
    ) -> dict[str, str] | None:
        cached = snapshots.get((provider, key))
        return cached[1] if cached and cached[0] == checksum else None

    async def cache_set(
        key: str, data: dict[str, str], *, provider: str, checksum: str, **_kwargs: Any
    ) -> None:
        snapshots[provider, key] = (checksum, data)

    async def cache_delete(key: str, *, provider: str) -> None:
        snapshots.pop((provider, key), None)

    def config_get(key: str, _default: Any = None) -> Any:
        if key == "providers":
            return raw_configs
        _, instance_id, _ = key.split("/")
        return raw_configs[instance_id]["setup_data"]

    def create_task(
        coro: Coroutine[Any, Any, Any], *, task_id: str, **_kwargs: Any
    ) -> asyncio.Task[Any]:
        if task_id in tracked and not tracked[task_id].done():
            coro.close()
            return tracked[task_id]
        task = asyncio.create_task(coro)
        tracked[task_id] = task
        # Match MA: always retrieve detached task exceptions.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    async with ClientSession() as session:
        mass = SimpleNamespace(
            http_session=session,
            cache=SimpleNamespace(
                get=AsyncMock(side_effect=cache_get),
                set=AsyncMock(side_effect=cache_set),
                delete=AsyncMock(side_effect=cache_delete),
            ),
            config=SimpleNamespace(
                get=config_get,
                decrypt_string=lambda value: value,
                get_raw_provider_config_value=lambda _instance, _key: None,
            ),
            music=SimpleNamespace(
                start_sync=AsyncMock(),
                unschedule_provider_sync=AsyncMock(),
                library_supported=lambda _provider, media_type: media_type == MediaType.RADIO,
            ),
            call_later=MagicMock(
                side_effect=lambda delay, target, *args, task_id: timers.update(
                    {task_id: (delay, target, args)}
                )
            ),
            cancel_timer=lambda task_id: timers.pop(task_id, None),
            get_task=lambda task_id: tracked.get(task_id),
            create_task=create_task,
            load_provider_config=AsyncMock(),
        )
        providers = []

        async def make(
            instance: str = "a",
            source: str | None = None,
            legacy_minutes: int | None = None,
            name: str | None = None,
        ) -> RemoteRadioCatalogProvider:
            source = source or instance
            instance_id = f"{DOMAIN}--{instance}"
            url = f"http://127.0.0.1:{port}/{source}"
            raw_configs[instance_id] = {
                "instance_id": instance_id,
                "domain": DOMAIN,
                "setup_data": {CONF_CATALOG_URL: url},
            }
            config = ProviderConfig(
                type=ProviderType.MUSIC,
                domain=DOMAIN,
                instance_id=instance_id,
                name=name,
                values={}
                if legacy_minutes is None
                else {
                    "refresh_interval": ConfigEntry(
                        key="refresh_interval",
                        type=ConfigEntryType.INTEGER,
                        value=legacy_minutes,
                    )
                },
            )
            manifest_path = Path("music_assistant/providers") / DOMAIN / "manifest.json"
            manifest = ProviderManifest.from_dict(json.loads(manifest_path.read_text()))
            provider = await setup(cast("MusicAssistant", mass), manifest, config)
            await provider.handle_async_init()
            await provider.loaded_in_mass()
            providers.append(provider)
            return provider

        yield SimpleNamespace(
            make=make,
            mass=mass,
            documents=documents,
            counts=counts,
            gates=gates,
            snapshots=snapshots,
            timers=timers,
            tracked=tracked,
        )
        for gate in gates.values():
            gate.set()
        for provider in providers:
            await provider.unload()
    await runner.cleanup()


async def radios(provider: RemoteRadioCatalogProvider) -> list[Radio]:
    """Collect one native library sync iterator."""
    return [radio async for radio in provider.get_library_radios()]


async def test_instances_namespaced_without_custom_scheduling(harness: SimpleNamespace) -> None:
    """Three native configurations have distinct mappings and no provider-owned timers."""
    first = await harness.make("a", name="My Icecast Stations")
    second = await harness.make("b", name="Community Radio")
    third = await harness.make("c", source="a", name="Personal Radio Collection")
    assert first.manifest.multi_instance
    assert all(not provider.is_streaming_provider for provider in (first, second, third))
    a, b, c = await asyncio.gather(*(radios(p) for p in (first, second, third)))
    assert a[0].item_id == b[0].item_id == c[0].item_id == "same"
    assert len({a[0].uri, b[0].uri, c[0].uri}) == 3
    assert not compare_item_ids(a[0], b[0])
    assert all(ProviderFeature.SEARCH not in p.supported_features for p in (first, second, third))
    assert next(iter(a[0].provider_mappings)).details == "My Icecast Stations"
    assert next(iter(b[0].provider_mappings)).provider_instance == second.instance_id
    assert all(
        mapping.url is None for radio in (a[0], b[0], c[0]) for mapping in radio.provider_mappings
    )
    assert harness.counts == {"a": 2, "b": 1}
    harness.mass.music.start_sync.assert_not_awaited()
    harness.mass.call_later.assert_not_called()
    assert (await first.get_stream_details("same", MediaType.RADIO)).path.endswith("/a")
    assert (await second.get_stream_details("same", MediaType.RADIO)).path.endswith("/b")


@pytest.mark.parametrize("failure", [503, "<html>Bad gateway</html>", '{"stations":false}'])
async def test_failure_keeps_snapshot_without_deleting_or_affecting_other(
    harness: SimpleNamespace, failure: int | str
) -> None:
    """HTTP and parse failures preserve the good cache, fail sync, and recover locally."""
    first, second = await harness.make("a"), await harness.make("b")
    await asyncio.gather(radios(first), radios(second))
    old_snapshot = harness.snapshots[first.instance_id, CACHE_KEY]
    harness.documents["a"] = failure
    with pytest.raises(ResourceTemporarilyUnavailable):
        await radios(first)
    assert (await first.browse(""))[0].item_id == "same"
    assert harness.snapshots[first.instance_id, CACHE_KEY] == old_snapshot
    assert (await second.browse(""))[0].item_id == "same"
    assert harness.counts == {"a": 2, "b": 1}
    harness.documents["a"] = [{"id": "new", "url": "https://stream.example/new"}]
    first._retry_after = 0
    assert (await radios(first))[0].item_id == "new"
    assert (await second.browse(""))[0].item_id == "same"


async def test_overlap_uses_one_http_request_and_waiter_cancellation_is_isolated(
    harness: SimpleNamespace,
) -> None:
    """Library and browse join the same in-flight instance download."""
    first = await harness.make()
    gate = harness.gates["a"] = asyncio.Event()
    browsing = asyncio.create_task(first.browse(""))
    syncing = asyncio.create_task(radios(first))
    resolving = asyncio.create_task(first.get_radio("same"))
    async with asyncio.timeout(2):
        while harness.counts["a"] == 0:
            await asyncio.sleep(0)
    browsing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await browsing
    gate.set()
    results, radio = await asyncio.gather(syncing, resolving)
    assert results[0].item_id == radio.item_id == "same"
    assert harness.counts["a"] == 1


async def test_unload_cancels_only_own_inflight_download(
    harness: SimpleNamespace,
) -> None:
    """Unloading a blocked request cannot cancel another instance's request."""
    first, second = await harness.make("a"), await harness.make("b")
    harness.gates["a"] = asyncio.Event()
    pending = asyncio.create_task(first.browse(""))
    async with asyncio.timeout(2):
        while harness.counts["a"] == 0:
            await asyncio.sleep(0)
    assert (await radios(second))[0].item_id == "same"
    await first.unload(is_removed=True)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert first._fetch_task.cancelled()
    assert (await radios(second))[0].item_id == "same"
    harness.mass.music.unschedule_provider_sync.assert_not_awaited()


async def test_remove_preserves_other_cache_and_reload_recovers_offline(
    harness: SimpleNamespace,
) -> None:
    """Deleting one instance removes just its snapshot, even when URLs are identical."""
    first = await harness.make("a")
    second = await harness.make("b", source="a")
    await asyncio.gather(radios(first), radios(second))
    await first.unload(is_removed=True)
    assert (first.instance_id, CACHE_KEY) not in harness.snapshots
    assert (second.instance_id, CACHE_KEY) in harness.snapshots
    await second.unload()
    harness.documents["a"] = 503
    restored = await harness.make("b", source="a")
    assert (await restored.browse(""))[0].item_id == "same"
    with pytest.raises(ResourceTemporarilyUnavailable):
        await radios(restored)


async def test_url_and_name_changes_are_instance_local(harness: SimpleNamespace) -> None:
    """Native reload of an edited instance cannot reuse the previous URL's data."""
    first = await harness.make("a", name="Original")
    second = await harness.make("b")
    await asyncio.gather(radios(first), radios(second))
    await first.unload()
    changed = await harness.make("a", source="b", name="Changed")
    assert changed._catalog is None
    assert (await changed.get_stream_details("same", MediaType.RADIO)).path.endswith("/b")
    assert next(iter((await radios(changed))[0].provider_mappings)).details == "Changed"
    assert next(iter((await second.browse(""))[0].provider_mappings)).details == second.name
    assert harness.counts == {"a": 1, "b": 3}


async def test_valid_empty_catalog_is_not_an_error(harness: SimpleNamespace) -> None:
    """An intentional empty list is a valid snapshot, unlike a malformed download."""
    first = await harness.make()
    harness.documents["a"] = []
    assert await radios(first) == []
    assert first._last_error is None


async def test_setup_uses_native_flow_and_reconfigure_only_changes_url() -> None:
    """Legacy minute settings are dropped on reconfiguration without losing the URL."""
    for kind in ("setup", "reconfigure"):
        session = SimpleNamespace(
            context=SimpleNamespace(
                kind=kind, setup_data={"refresh_interval": 5}, values={"refresh_interval": 60}
            ),
            form=AsyncMock(return_value={CONF_CATALOG_URL: "https://example.org/radios.m3u"}),
            finish=AsyncMock(),
        )
        await run_setup(cast("SetupSession", session))
        fields = session.form.await_args.args[0]
        assert len(fields) == 1
        assert session.finish.await_args.args[0] == {
            CONF_CATALOG_URL: "https://example.org/radios.m3u"
        }


@pytest.mark.parametrize(
    "text",
    [
        "#EXTM3U\n#EXTINF:-1,Radio\nhttps://stream.example/live\n",
        '[{"id":"radio","name":"Radio","url":"https://stream.example/live"}]',
        '{"stations":[{"name":"Radio","url":"https://stream.example/live"}]}',
        '{"icestats":{"source":{"server_name":"Radio","listenurl":"https://stream.example/live"}}}',
    ],
)
def test_supported_catalog_formats(text: str) -> None:
    """M3U/M3U8, plain JSON, envelope JSON and Icecast use the same normalized model."""
    stations = parse_catalog(text, "https://example.org/catalog")
    assert len(stations) == 1
    assert next(iter(stations.values())).name == "Radio"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "<html>Error</html>",
        '{"unknown":[]}',
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\nsegment.ts",
        '[{"url":"file:///etc/passwd"}]',
        '[{"id":"same","url":"https://a.example"},{"id":"same","url":"https://b.example"}]',
    ],
)
def test_malformed_catalogs_are_rejected_atomically(text: str) -> None:
    """Never mistake error pages, HLS segments or conflicting IDs for valid stations."""
    with pytest.raises(ValueError, match=r"."):
        parse_catalog(text, "https://example.org/catalog")


async def test_refreshing_one_instance_does_not_fetch_or_sync_another(
    harness: SimpleNamespace,
) -> None:
    """Native sync refreshes only its source and never starts another sync."""
    first, second = await harness.make("a"), await harness.make("b")
    await asyncio.gather(radios(first), radios(second))
    harness.documents["a"] = [
        {"id": "same", "name": "Live sport", "url": "https://stream.example/new"}
    ]
    updated = await radios(first)
    assert updated[0].name == "Live sport"
    assert (await first.get_stream_details("same", MediaType.RADIO)).path.endswith("/new")
    assert harness.counts == {"a": 2, "b": 1}
    harness.documents["a"] = 503
    with pytest.raises(ResourceTemporarilyUnavailable):
        await radios(first)
    harness.mass.music.start_sync.assert_not_awaited()
    harness.mass.call_later.assert_not_called()
    assert (await second.browse(""))[0].name == "Radio"
    assert harness.counts == {"a": 3, "b": 1}


async def test_legacy_interval_does_not_change_native_sync_or_lose_cache(
    harness: SimpleNamespace,
) -> None:
    """Old experimental configs safely use MA's defaults and retain cached stations."""
    first = await harness.make("a", legacy_minutes=5)
    await radios(first)
    await first.unload()
    restored = await harness.make("a", legacy_minutes=5)
    assert (await restored.browse(""))[0].item_id == "same"
    assert harness.counts == {"a": 1, "b": 0}
    assert await restored.get_config_entries() == ()
    assert restored.get_default_library_sync_schedule(MediaType.RADIO).every == 12
    harness.mass.call_later.assert_not_called()


async def test_cancelled_fetch_is_not_cached_as_empty_success(harness: SimpleNamespace) -> None:
    """Canceling background work leaves the next request eligible to retry."""
    first = await harness.make()
    gate = harness.gates["a"] = asyncio.Event()
    pending = asyncio.create_task(radios(first))
    async with asyncio.timeout(2):
        while harness.counts["a"] == 0:
            await asyncio.sleep(0)
    first._fetch_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    gate.set()
    assert (await radios(first))[0].item_id == "same"
    assert harness.counts["a"] == 2


async def test_shared_service_radio_matching_still_works(harness: SimpleNamespace) -> None:
    """The provider leaves normal matching for shared radio services unchanged."""
    first, second = await harness.make("a"), await harness.make("b")
    a, b = await asyncio.gather(radios(first), radios(second))
    assert compare_radio(a[0], a[0])
    for radio in (a[0], b[0]):
        for mapping in radio.provider_mappings:
            mapping.is_unique = False
    assert compare_radio(a[0], b[0])


async def test_non_utf8_station_title_does_not_reject_catalog(harness: SimpleNamespace) -> None:
    """A bad title byte is replaced without dropping an otherwise valid station list."""
    first = await harness.make()
    harness.documents["a"] = b"#EXTM3U\n#EXTINF:-1,Radio \xe9\nhttps://stream.example/live\n"
    result = await radios(first)
    assert len(result) == 1
    assert result[0].name == "Radio \ufffd"
    assert first._last_error is None


async def test_failed_initial_fetch_is_throttled_per_instance(harness: SimpleNamespace) -> None:
    """Repeated calls cannot hammer a failed source while a different one still loads."""
    first, second = await harness.make("a"), await harness.make("b")
    harness.documents["a"] = 503
    for _ in range(2):
        with pytest.raises(ResourceTemporarilyUnavailable):
            await first.browse("")
    assert (await radios(second))[0].name == "Radio"
    assert harness.counts == {"a": 1, "b": 1}


async def test_empty_snapshot_is_reused_until_native_sync(harness: SimpleNamespace) -> None:
    """Browsing a valid empty list is not mistaken for an uncached catalog."""
    first = await harness.make()
    harness.documents["a"] = []
    assert await radios(first) == []
    assert await first.browse("") == []
    assert await first.browse("") == []
    assert harness.counts["a"] == 1
    assert await radios(first) == []
    assert harness.counts["a"] == 2
