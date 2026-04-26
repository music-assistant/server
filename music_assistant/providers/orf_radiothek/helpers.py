"""Typed dataclasses + parsers for ORF Radiothek / ORF Sound provider."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StreamRef:
    """Stream reference parsed from ORF bundle."""

    url: str
    format: str | None = None


@dataclass(frozen=True, slots=True)
class OrfStation:
    """Create an ORF station from a bundle entry."""

    id: str
    name: str
    live_stream_url_template: str
    hide_from_stations: bool = False
    # optional fields that exist in bundle.json and can be useful later
    timeshift_hls_url_template: str | None = None
    timeshift_progressive_url_template: str | None = None
    podcasts_available: bool | None = None

    @classmethod
    def from_bundle_item(cls, station_id: str, obj: dict[str, Any]) -> OrfStation | None:
        """Create an ORF station from a bundle entry."""
        tmpl = obj.get("liveStreamUrlTemplate")
        if not isinstance(tmpl, str) or "{quality}" not in tmpl:
            return None
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            name = station_id

        # optional extras (keep loose; bundle varies)
        ts = obj.get("timeshift")
        ts_hls = ts.get("liveStreamUrlTemplateHls") if isinstance(ts, dict) else None
        ts_prog = ts.get("liveStreamUrlTemplateProgressive") if isinstance(ts, dict) else None
        if not isinstance(ts_hls, str):
            ts_hls = None
        if not isinstance(ts_prog, str):
            ts_prog = None

        podcasts = obj.get("podcasts")
        podcasts_avail = podcasts.get("available") if isinstance(podcasts, dict) else None
        if not isinstance(podcasts_avail, bool):
            podcasts_avail = None

        return cls(
            id=station_id,
            name=name,
            live_stream_url_template=tmpl,
            hide_from_stations=bool(obj.get("hideFromStations")),
            timeshift_hls_url_template=ts_hls,
            timeshift_progressive_url_template=ts_prog,
            podcasts_available=podcasts_avail,
        )


@dataclass(frozen=True, slots=True)
class PrivateStation:
    """Private (non-ORF) radio station definition."""

    id: str
    name: str
    streams: tuple[StreamRef, ...] = ()
    image_urls: tuple[str, ...] = ()

    @classmethod
    def from_bundle_item(cls, obj: dict[str, Any]) -> PrivateStation | None:
        """Create a private station from a bundle entry."""
        sid = obj.get("station")
        if not isinstance(sid, str) or not sid:
            return None
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            name = sid

        # streams
        streams_in = obj.get("streams")
        streams: list[StreamRef] = []
        if isinstance(streams_in, list):
            for s in streams_in:
                if not isinstance(s, dict):
                    continue
                url = s.get("url")
                if not isinstance(url, str) or not url:
                    continue
                fmt = s.get("format")
                if not isinstance(fmt, str):
                    fmt = None
                streams.append(StreamRef(url=url, format=fmt))

        # images (provider only needs URLs; keep it flat)
        imgs: list[str] = []
        image = obj.get("image")
        if isinstance(image, dict) and isinstance(image.get("src"), str):
            imgs.append(image["src"])
        image_large = obj.get("imageLarge")
        if isinstance(image_large, dict):
            for mode in ("light", "dark"):
                v = image_large.get(mode)
                if isinstance(v, dict) and isinstance(v.get("src"), str):
                    imgs.append(v["src"])

        # dedupe while preserving order
        seen: set[str] = set()
        deduped = []
        for u in imgs:
            if u in seen:
                continue
            seen.add(u)
            deduped.append(u)

        return cls(id=sid, name=name, streams=tuple(streams), image_urls=tuple(deduped))


@dataclass(frozen=True, slots=True)
class PodcastImage:
    """Holds ORF image versions (path URLs)."""

    versions: dict[str, str]

    @classmethod
    def from_obj(cls, obj: Any) -> PodcastImage | None:
        """Create a podcast image from a raw object."""
        if not isinstance(obj, dict):
            return None
        image = obj.get("image")
        if not isinstance(image, dict):
            return None
        versions = image.get("versions")
        if not isinstance(versions, dict):
            return None
        out: dict[str, str] = {}
        for k, v in versions.items():
            if not isinstance(v, dict):
                continue
            path = v.get("path")
            if isinstance(path, str) and path:
                out[str(k)] = path
        return cls(out) if out else None

    def best(
        self, preference: Iterable[str] = ("premium", "standard", "id3art", "thumbnail")
    ) -> str | None:
        """Return the best matching image URL by preference."""
        for key in preference:
            p = self.versions.get(key)
            if p:
                return p
        # fallback: any
        for p in self.versions.values():
            if p:
                return p
        return None


@dataclass(frozen=True, slots=True)
class OrfPodcast:
    """ORF podcast metadata."""

    id: int
    title: str
    station: str | None = None
    channel: str | None = None
    slug: str | None = None
    description: str | None = None
    author: str | None = None
    image: PodcastImage | None = None

    @classmethod
    def from_index_item(cls, obj: dict[str, Any]) -> OrfPodcast | None:
        """Create an ORF podcast from an index entry."""
        pid = obj.get("id")
        if not isinstance(pid, int):
            return None
        title = obj.get("title")
        if not isinstance(title, str) or not title:
            title = str(pid)

        station = obj.get("station")
        if not isinstance(station, str):
            station = None
        channel = obj.get("channel")
        if not isinstance(channel, str):
            channel = None
        slug = obj.get("slug")
        if not isinstance(slug, str):
            slug = None
        desc = obj.get("description")
        if not isinstance(desc, str):
            desc = None
        author = obj.get("author")
        if not isinstance(author, str):
            author = None

        img = PodcastImage.from_obj(obj)

        return cls(
            id=pid,
            title=title,
            station=station,
            channel=channel,
            slug=slug,
            description=desc,
            author=author,
            image=img,
        )


@dataclass(frozen=True, slots=True)
class Enclosure:
    """Podcast episode enclosure."""

    url: str
    mime_type: str | None = None
    length_bytes: int | None = None

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> Enclosure | None:
        """Create an enclosure from a raw object."""
        url = obj.get("url")
        if not isinstance(url, str) or not url:
            return None
        mt = obj.get("type")
        if not isinstance(mt, str):
            mt = None
        ln = obj.get("length")
        if not isinstance(ln, int):
            ln = None
        return cls(url=url, mime_type=mt, length_bytes=ln)


@dataclass(frozen=True, slots=True)
class OrfPodcastEpisode:
    """ORF podcast episode metadata."""

    guid: str
    title: str
    description: str | None = None
    published: str | None = None  # keep as string; provider already formats timestamps itself
    expiry: str | None = None
    duration_ms: int | None = None
    enclosures: tuple[Enclosure, ...] = ()
    link_url: str | None = None
    image: PodcastImage | None = None

    @classmethod
    def from_detail_item(cls, obj: dict[str, Any]) -> OrfPodcastEpisode | None:
        """Create a podcast episode from a detail entry."""
        guid = obj.get("guid")
        if not isinstance(guid, str) or not guid:
            return None
        title = obj.get("title")
        if not isinstance(title, str) or not title:
            title = guid

        desc = obj.get("description")
        if not isinstance(desc, str):
            desc = None

        published = obj.get("published")
        if not isinstance(published, str):
            published = None
        expiry = obj.get("expiry")
        if not isinstance(expiry, str):
            expiry = None

        dur = obj.get("duration")
        if not isinstance(dur, int) or dur <= 0:
            dur = None

        link = obj.get("url")
        if not isinstance(link, str):
            link = None

        enc_in = obj.get("enclosures")
        encs: list[Enclosure] = []
        if isinstance(enc_in, list):
            for e in enc_in:
                if isinstance(e, dict):
                    enc = Enclosure.from_obj(e)
                    if enc:
                        encs.append(enc)

        img = PodcastImage.from_obj(obj)

        return cls(
            guid=guid,
            title=title,
            description=desc,
            published=published,
            expiry=expiry,
            duration_ms=dur,
            enclosures=tuple(encs),
            link_url=link,
            image=img,
        )


# ----------------------------
# Parsers
# ----------------------------


def parse_orf_stations(bundle: dict[str, Any], include_hidden: bool) -> list[OrfStation]:
    """Parse ORF stations from the bundle payload."""
    stations = bundle.get("stations")
    if not isinstance(stations, dict):
        return []
    out: list[OrfStation] = []
    for sid, obj in stations.items():
        if not isinstance(sid, str) or not isinstance(obj, dict):
            continue
        st = OrfStation.from_bundle_item(sid, obj)
        if not st:
            continue
        if st.hide_from_stations and not include_hidden:
            continue
        out.append(st)
    return out


def parse_private_stations(bundle: dict[str, Any]) -> list[PrivateStation]:
    """Parse private stations from the bundle payload."""
    priv = bundle.get("privates")
    if not isinstance(priv, list):
        return []
    out: list[PrivateStation] = []
    for obj in priv:
        if not isinstance(obj, dict):
            continue
        st = PrivateStation.from_bundle_item(obj)
        if st:
            out.append(st)
    return out


def parse_orf_podcasts_index(payload: Any) -> list[OrfPodcast]:
    """Parse ORF podcast index payload."""
    # payload is expected to be dict[station_key -> list[podcast_obj]]
    if not isinstance(payload, dict):
        return []
    out: list[OrfPodcast] = []
    for arr in payload.values():
        if not isinstance(arr, list):
            continue
        for pod in arr:
            if not isinstance(pod, dict):
                continue
            if pod.get("isOnline") is not True:
                continue
            item = OrfPodcast.from_index_item(pod)
            if item:
                out.append(item)
    return out


def parse_orf_podcast_episodes(payload: Any) -> list[OrfPodcastEpisode]:
    """Parse podcast episodes from a detail payload."""
    if not isinstance(payload, dict):
        return []
    eps = payload.get("episodes")
    if not isinstance(eps, list):
        return []
    out: list[OrfPodcastEpisode] = []
    for ep in eps:
        if not isinstance(ep, dict):
            continue
        item = OrfPodcastEpisode.from_detail_item(ep)
        if item:
            out.append(item)
    return out
