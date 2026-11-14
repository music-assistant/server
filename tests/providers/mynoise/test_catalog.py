"""Tests for the myNoise catalog parsing and fetching."""

from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.mynoise.catalog import (
    Machine,
    MachineLayer,
    bundled_catalog,
    fetch_catalog,
    fetch_machine_details,
    parse_cfg_blob,
    parse_index_page,
    patron_login,
)
from music_assistant.providers.mynoise.constants import (
    CACHE_CATEGORY_MACHINE,
    CDN_URL,
)

INDEX_HTML = """
<html>
<h1>Landscapes</h1>
<a href='/NoiseMachines/japaneseGardenNoiseGenerator.php' class='JAPANGARDEN'>Japanese Garden</a>
<a href='/NoiseMachines/rainNoiseGenerator.php' class='RAIN'>Rain&nbsp;&nbsp;</a>
<h1><a href='/donate.php' class='powertip' title='For &#8471;atrons only.<br><br>Thanks!'>&#8471;</a> Magic</h1>
<a href='/NoiseMachines/magicX.php' class='MAGIC_ONE'>Magic One</a>
<h1>Empty</h1>
<h1>Deep Focus</h1>
<a href='/NoiseMachines/rainNoiseGenerator.php' class='RAIN other-class'>Rain</a>
</html>
"""

CFG_HTML = """
<script>
var CFG = {"title":"Thunder","mediaTitle":"Thunderstorm",
"srcA":["/thunder/l1","/thunder/l2"],"srcB":["/thunder/l1b"],
"defaultCurve":[0.8,0.4]};
</script>
"""


class _FakeResponse:
    """Minimal aiohttp response stand-in (async context manager)."""

    def __init__(
        self,
        html: str = "",
        body: bytes = b"",
        error: Exception | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._html = html
        self._body = body
        self._error = error
        self.headers = headers or {}

    async def __aenter__(self) -> Self:
        if self._error:
            raise self._error
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def text(self) -> str:
        return self._html

    async def read(self) -> bytes:
        return self._body

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error


def _mass(http_response: _FakeResponse | None = None, cached: Any = None) -> MagicMock:
    """Build a MusicAssistant mock with stubbed cache and http session."""
    mass = MagicMock()
    mass.cache.get = AsyncMock(return_value=cached)
    mass.cache.set = AsyncMock()
    mass.http_session.get = MagicMock(return_value=http_response or _FakeResponse())
    mass.http_session.post = MagicMock(return_value=http_response or _FakeResponse())
    return mass


def _serialized_catalog() -> dict[str, Any]:
    return {
        "categories": [{"id": "x", "name": "X", "machine_ids": ["ONE"]}],
        "machines": [
            {
                "machine_id": "ONE",
                "name": "One",
                "category": "X",
                "generator_path": "/a.php",
                "image_url": "",
            }
        ],
    }


# ------------------------------------------------------------------ parsing


def test_parse_cfg_blob_valid() -> None:
    """The CFG javascript object is extracted as a dict."""
    cfg = parse_cfg_blob(CFG_HTML)
    assert cfg is not None
    assert cfg["mediaTitle"] == "Thunderstorm"
    assert cfg["srcA"] == ["/thunder/l1", "/thunder/l2"]


def test_parse_cfg_blob_missing_or_invalid() -> None:
    """Pages without a CFG object (or with invalid JSON) parse to None."""
    assert parse_cfg_blob("<html><body>no config</body></html>") is None
    assert parse_cfg_blob("var CFG = {invalid json};") is None


def test_parse_index_page() -> None:
    """The index page parses into categories and machines."""
    categories, machines = parse_index_page(INDEX_HTML)
    assert set(machines) == {"JAPANGARDEN", "RAIN"}
    assert [cat.name for cat in categories] == ["Landscapes", "Deep Focus"]

    garden = machines["JAPANGARDEN"]
    assert garden.name == "Japanese Garden"
    assert garden.category == "Landscapes"
    assert garden.generator_path == "/NoiseMachines/japaneseGardenNoiseGenerator.php"
    assert garden.image_url.endswith("/Data/JAPANGARDEN/fb.jpg")


def test_parse_index_page_skips_magic_and_entities() -> None:
    """Patron-only MAGIC machines are skipped and &nbsp; entities are stripped."""
    _, machines = parse_index_page(INDEX_HTML)
    assert "MAGIC_ONE" not in machines
    assert machines["RAIN"].name == "Rain"


def test_parse_index_page_patron_includes_magic() -> None:
    """With include_patron the MAGIC machines and their own category are kept."""
    categories, machines = parse_index_page(INDEX_HTML, include_patron=True)
    assert "MAGIC_ONE" in machines
    magic_cat = next(cat for cat in categories if cat.id == "magic")
    # the patron marker icon inside the heading is stripped from the display name
    assert magic_cat.name == "Magic"
    assert magic_cat.machine_ids == ["MAGIC_ONE"]


def test_parse_index_page_dedupes_across_categories() -> None:
    """A machine listed in two categories is one item, referenced by both."""
    categories, machines = parse_index_page(INDEX_HTML)
    assert sum(cat.machine_ids.count("RAIN") for cat in categories) == 2
    assert len(machines) == 2


def test_bundled_catalog_is_usable() -> None:
    """The shipped fallback catalog has machines and no MAGIC entries."""
    categories, machines = bundled_catalog()
    assert machines
    assert categories
    assert not any(mid.startswith("MAGIC_") for mid in machines)
    known = machines.get("JAPANGARDEN")
    assert known is not None
    assert known.name


# ------------------------------------------------------------- fetch_catalog


async def test_fetch_catalog_cache_hit() -> None:
    """A cached catalog is served without any http call."""
    mass = _mass(cached=_serialized_catalog())
    catalog = await fetch_catalog(mass)
    assert set(catalog.machines) == {"ONE"}
    mass.http_session.get.assert_not_called()


async def test_fetch_catalog_refreshes_and_caches() -> None:
    """A cache miss scrapes the index page and stores the result."""
    mass = _mass(http_response=_FakeResponse(html=INDEX_HTML))
    catalog = await fetch_catalog(mass)
    assert set(catalog.machines) == {"JAPANGARDEN", "RAIN"}
    mass.cache.set.assert_awaited_once()


async def test_fetch_catalog_falls_back_to_bundled() -> None:
    """When the site is unreachable and nothing is cached, use the bundled catalog."""
    mass = _mass(http_response=_FakeResponse(error=aiohttp.ClientError("offline")))
    catalog = await fetch_catalog(mass)
    assert catalog.machines == bundled_catalog()[1]


async def test_fetch_catalog_prefers_stale_cache() -> None:
    """When the site is unreachable, a stale cached catalog beats the bundled one."""
    mass = _mass(
        http_response=_FakeResponse(error=aiohttp.ClientError("offline")),
        cached=_serialized_catalog(),
    )
    catalog = await fetch_catalog(mass)
    assert set(catalog.machines) == {"ONE"}


async def test_fetch_catalog_patron_includes_magic_in_own_cache() -> None:
    """The patron catalog keeps MAGIC machines and caches under a separate key."""
    mass = _mass(http_response=_FakeResponse(html=INDEX_HTML))
    catalog = await fetch_catalog(mass, patron=True)
    assert "MAGIC_ONE" in catalog.machines
    assert mass.cache.set.await_args.args[0] == "catalog_patron"


# ------------------------------------------------------------- patron_login


async def test_patron_login_success() -> None:
    """Login succeeds when the auth script 302s to authentication_OK."""
    mass = _mass(http_response=_FakeResponse(headers={"Location": "/authentication_OK.php"}))
    assert await patron_login(mass, "me@example.com") is True


async def test_patron_login_rejected() -> None:
    """An email unknown to myNoise 302s to authentication_KO."""
    mass = _mass(http_response=_FakeResponse(headers={"Location": "/authentication_KO.php"}))
    assert await patron_login(mass, "nobody@example.com") is False


# ---------------------------------------------------- fetch_machine_details


async def test_fetch_machine_details_parses_cfg() -> None:
    """The generator page CFG becomes CDN urls with extension and levels."""
    mass = _mass(http_response=_FakeResponse(html=CFG_HTML))
    machine = await fetch_machine_details(mass, Machine(machine_id="THUNDER", name="Thunder"))
    assert machine.name == "Thunderstorm"
    assert len(machine.layers) == 2
    first, second = machine.layers
    assert first.url_a == f"{CDN_URL}/thunder/l1.mp3"
    assert first.url_b == f"{CDN_URL}/thunder/l1b.mp3"
    assert first.default_level == 0.8
    # missing srcB entry falls back to the A-variant
    assert second.url_b == f"{CDN_URL}/thunder/l2.mp3"
    assert second.default_level == 0.4
    mass.cache.set.assert_awaited_once()
    assert mass.cache.set.await_args.kwargs["category"] == CACHE_CATEGORY_MACHINE


async def test_fetch_machine_details_live_uses_wav() -> None:
    """Live machines play .wav layer files instead of the mp3 default."""
    html = 'var CFG = {"live":true,"srcA":["/live/l1"]};'
    mass = _mass(http_response=_FakeResponse(html=html))
    machine = await fetch_machine_details(mass, Machine(machine_id="LIVE", name="Live"))
    assert machine.layers[0].url_a == f"{CDN_URL}/live/l1.wav"


async def test_fetch_machine_details_cache_hit() -> None:
    """Cached layer definitions are reused without an http call."""
    cached = [{"url_a": "a", "url_b": "b", "default_level": 0.6}]
    mass = _mass(cached=cached)
    machine = await fetch_machine_details(mass, Machine(machine_id="THUNDER", name="Thunder"))
    assert machine.layers == [MachineLayer("a", "b", 0.6)]
    mass.http_session.get.assert_not_called()


@pytest.mark.parametrize(
    "html",
    ["<html>no cfg here</html>", 'var CFG = {"mediaTitle":"broken"};'],
)
async def test_fetch_machine_details_unparsable_raises(html: str) -> None:
    """A page without a usable CFG (no srcA) raises MediaNotFoundError."""
    mass = _mass(http_response=_FakeResponse(html=html))
    with pytest.raises(MediaNotFoundError):
        await fetch_machine_details(mass, Machine(machine_id="X", name="X"))
