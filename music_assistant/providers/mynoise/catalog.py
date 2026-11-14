"""
Catalog handling for the myNoise provider.

The myNoise website has no public API, but every noise machine page embeds a
`CFG` JavaScript object that contains everything we need: the machine title,
the CDN urls of its audio layer files and the default mixer settings.
The machine catalog (name, category, generator page) is scraped from the
public index page and shipped with the provider as a bundled fallback so the
provider also works (for known machines) without internet access at first
startup.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from music_assistant_models.errors import MediaNotFoundError

from .constants import (
    AUDIO_EXTENSION,
    AUTH_OK_PATH,
    AUTH_URL,
    CACHE_CATEGORY_CATALOG,
    CACHE_CATEGORY_MACHINE,
    CATALOG_REFRESH_INTERVAL,
    CDN_URL,
    INDEX_URL,
    MACHINE_REFRESH_INTERVAL,
    USER_AGENT,
    WEBSITE_URL,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.mass import MusicAssistant


@dataclass
class MachineLayer:
    """A single audio layer of a noise machine."""

    url_a: str
    url_b: str
    # relative default volume level (0..1) for this band, if known
    default_level: float = 0.5


@dataclass
class Machine:
    """Definition of a myNoise noise machine."""

    machine_id: str  # e.g. THUNDER
    name: str
    category: str = ""
    generator_path: str = ""  # e.g. /NoiseMachines/thunderNoiseGenerator.php
    layers: list[MachineLayer] = field(default_factory=list)
    image_url: str = ""

    @property
    def layer_count(self) -> int:
        """Return the number of audio layers of this machine."""
        return len(self.layers)


@dataclass
class Category:
    """A category of noise machines from the myNoise index page."""

    id: str
    name: str
    machine_ids: list[str] = field(default_factory=list)


def _require_machines(machines: dict[str, Machine]) -> None:
    """Raise when an index page parse produced no machines."""
    if not machines:
        raise MediaNotFoundError("myNoise index page parse returned no machines")


def _heading_title(raw: str) -> str:
    """Return an h1 heading's display title, ignoring inner markup (patron links)."""
    # drop the patron donation link whole: its title attribute contains '>'-ish
    # markup that would confuse a plain tag-strip
    title = re.sub(r"<a\b.*?</a>", "", raw, flags=re.DOTALL)
    title = unescape(re.sub(r"<[^>]+>", "", title)).strip()
    # the patron-only marker icon (\u2117) decorates patron headings, it is not a name
    return title.replace("\u2117", "").strip()


def _slugify(name: str) -> str:
    """Create a browse-safe slug from a display name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "category"


def parse_cfg_blob(html: str) -> dict[str, Any] | None:
    """Extract the CFG javascript object (as dict) from a generator page."""
    match = re.search(r"var CFG = (\{.*?\});", html, re.DOTALL)
    if not match:
        return None
    try:
        # the CFG object is valid JSON in practice
        cfg: dict[str, Any] = json.loads(match.group(1))
        return cfg
    except json.JSONDecodeError:
        return None


def parse_index_page(
    html: str, include_patron: bool = False
) -> tuple[list[Category], dict[str, Machine]]:
    """
    Parse the myNoise index page into categories and (lightweight) machines.

    Patron-only MAGIC machines are skipped unless ``include_patron`` is set (the
    index lists them anonymously; only their generator pages need a login).
    """
    markers: list[tuple[int, str, Any]] = []
    # patron category headings embed a donation link inside the h1, so inner
    # tags must be stripped rather than excluded from the match
    for match in re.finditer(r"<h1>(.*?)</h1>", html):
        markers.append((match.start(), "cat", _heading_title(match.group(1))))
    anchor_pattern = re.compile(
        r"<a href='(/NoiseMachines/[^']+\.php)'[^>]*class='([^']*)'[^>]*>([^<]+)</a>"
    )
    for match in anchor_pattern.finditer(html):
        markers.append((match.start(), "mach", match.groups()))
    markers.sort()

    categories: list[Category] = []
    machines: dict[str, Machine] = {}
    current: Category | None = None
    for _, kind, value in markers:
        if kind == "cat":
            current = Category(id=_slugify(value), name=value)
            categories.append(current)
            continue
        if current is None:
            continue
        url, css_class, raw_name = value
        name = raw_name.replace("&nbsp;", "").strip()
        tokens = [
            token for token in css_class.split() if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", token)
        ]
        if not tokens or not name:
            continue
        machine_id = tokens[0]
        if machine_id.startswith("MAGIC_") and not include_patron:
            # the 'Magic' category is patron-only (login required), skip it
            continue
        if machine_id in machines:
            if machine_id not in current.machine_ids:
                current.machine_ids.append(machine_id)
            continue
        machines[machine_id] = Machine(
            machine_id=machine_id,
            name=name,
            category=current.name,
            generator_path=url,
            image_url=f"{WEBSITE_URL}/Data/{machine_id}/fb.jpg",
        )
        current.machine_ids.append(machine_id)
    categories = [cat for cat in categories if cat.machine_ids]
    return categories, machines


def bundled_catalog() -> tuple[list[Category], dict[str, Machine]]:
    """Load the catalog shipped with the provider as offline fallback."""
    path = os.path.join(os.path.dirname(__file__), "catalog.json")
    with open(path, encoding="utf-8") as catalog_file:
        data = json.load(catalog_file)
    machines: dict[str, Machine] = {}
    categories: list[Category] = []
    for cat in data["categories"]:
        category = Category(id=_slugify(cat["name"]), name=cat["name"])
        for item in cat["machines"]:
            machine_id = item["id"]
            if machine_id.startswith("MAGIC_"):
                continue
            if machine_id not in machines:
                machines[machine_id] = Machine(
                    machine_id=machine_id,
                    name=item["name"],
                    category=cat["name"],
                    generator_path=item["url"],
                    image_url=f"{WEBSITE_URL}/Data/{machine_id}/fb.jpg",
                )
            category.machine_ids.append(machine_id)
        if category.machine_ids:
            categories.append(category)
    return categories, machines


class Catalog:
    """Holds the (parsed) catalog of noise machines."""

    def __init__(self, categories: list[Category], machines: dict[str, Machine]) -> None:
        """Initialize the catalog."""
        self.categories = categories
        self.machines = machines


async def patron_login(mass: MusicAssistant, email: str) -> bool:
    """
    Authenticate as a myNoise patron (donor) with the email from the settings.

    The site's login form only posts an email address (no password) and answers
    with a 302 to authentication_OK.php (accepted) or authentication_KO.php
    (rejected). The redirect is deliberately *not* followed: the OK page itself
    redirects onward to /404.php, so the Location header is the only reliable
    outcome marker. The session cookie from the response is picked up by the
    shared http session's cookie jar and replayed on the patron-gated generator
    pages. Returns True when the email was accepted.
    """
    async with mass.http_session.post(
        AUTH_URL,
        data={"email": email, "pwd": ""},
        headers={"User-Agent": USER_AGENT},
        allow_redirects=False,
    ) as response:
        location = response.headers.get("Location", "")
        return urlparse(location).path == AUTH_OK_PATH


async def fetch_catalog(
    mass: MusicAssistant, force_refresh: bool = False, patron: bool = False
) -> Catalog:
    """Return the machine catalog, refreshing from the website when needed."""
    # keep patron and anonymous catalogs apart so MAGIC machines never leak
    # into (or disappear from) the wrong view
    cache_key = "catalog_patron" if patron else "catalog"
    cached = await mass.cache.get(
        cache_key,
        provider="mynoise",
        category=CACHE_CATEGORY_CATALOG,
    )
    if cached and not force_refresh:
        return _catalog_from_dict(cached)
    try:
        async with mass.http_session.get(INDEX_URL, headers={"User-Agent": USER_AGENT}) as response:
            html = await response.text()
        categories, machines = parse_index_page(html, include_patron=patron)
        _require_machines(machines)
        catalog = Catalog(categories, machines)
        await mass.cache.set(
            cache_key,
            _catalog_to_dict(catalog),
            provider="mynoise",
            category=CACHE_CATEGORY_CATALOG,
            expiration=CATALOG_REFRESH_INTERVAL,
        )
        return catalog
    except Exception:
        # fall back to the bundled catalog when the site is unreachable
        mass.logger.warning(
            "Failed to refresh the myNoise catalog, using the bundled fallback",
            exc_info=True,
        )
        fallback = Catalog(*bundled_catalog())
        if cached:
            # prefer a (possibly stale) online catalog over the bundled one
            return _catalog_from_dict(cached)
        return fallback


def _catalog_to_dict(catalog: Catalog) -> dict[str, Any]:
    """Serialize the catalog for caching."""
    return {
        "categories": [
            {"id": cat.id, "name": cat.name, "machine_ids": cat.machine_ids}
            for cat in catalog.categories
        ],
        "machines": [
            {
                "machine_id": machine.machine_id,
                "name": machine.name,
                "category": machine.category,
                "generator_path": machine.generator_path,
                "image_url": machine.image_url,
            }
            for machine in catalog.machines.values()
        ],
    }


def _catalog_from_dict(data: dict[str, Any]) -> Catalog:
    """Deserialize a cached catalog."""
    categories = [
        Category(id=item["id"], name=item["name"], machine_ids=item["machine_ids"])
        for item in data["categories"]
    ]
    machines = {
        item["machine_id"]: Machine(
            machine_id=item["machine_id"],
            name=item["name"],
            category=item.get("category", ""),
            generator_path=item.get("generator_path", ""),
            image_url=item.get("image_url", ""),
        )
        for item in data["machines"]
    }
    return Catalog(categories, machines)


async def fetch_machine_details(mass: MusicAssistant, machine: Machine) -> Machine:
    """Fetch (and cache) the layer files for the given machine from its page."""
    cached = await mass.cache.get(
        machine.machine_id,
        provider="mynoise",
        category=CACHE_CATEGORY_MACHINE,
    )
    if cached:
        machine.layers = [MachineLayer(**item) for item in cached]
        return machine
    url = f"{WEBSITE_URL}{machine.generator_path}"
    async with mass.http_session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
        html = await resp.text()
    cfg = parse_cfg_blob(html)
    if not cfg or not cfg.get("srcA"):
        msg = f"Could not parse machine definition for {machine.name}"
        raise MediaNotFoundError(msg)
    if cfg.get("mediaTitle"):
        machine.name = cfg["mediaTitle"]
    levels = cfg.get("defaultCurve") or []
    # the CFG layer urls are extension-less: the website appends .mp3/.ogg
    # client-side (live machines use .wav)
    ext = ".wav" if cfg.get("live") else AUDIO_EXTENSION
    layers: list[MachineLayer] = []
    for index, src_a in enumerate(cfg["srcA"]):
        src_b_list = cfg.get("srcB") or []
        src_b = src_b_list[index] if index < len(src_b_list) else src_a
        level = levels[index] if index < len(levels) else 0.5
        layers.append(
            MachineLayer(
                url_a=_absolute(src_a, ext),
                url_b=_absolute(src_b, ext),
                default_level=float(level),
            )
        )
    machine.layers = layers
    await mass.cache.set(
        machine.machine_id,
        [layer.__dict__ for layer in layers],
        provider="mynoise",
        category=CACHE_CATEGORY_MACHINE,
        expiration=MACHINE_REFRESH_INTERVAL,
    )
    return machine


def _absolute(url: str, ext: str = AUDIO_EXTENSION) -> str:
    """Make a layer url absolute and add the audio file extension if missing."""
    if not url.startswith("http"):
        url = f"{CDN_URL}{url}"
    if not url.lower().endswith((".mp3", ".ogg", ".wav", ".m4a", ".aac")):
        url += ext
    return url


async def iter_machines(mass: MusicAssistant) -> AsyncGenerator[Machine]:
    """Yield all machines in the catalog."""
    catalog = await fetch_catalog(mass)
    for machine in catalog.machines.values():
        yield machine
