# Metadata Controller

This package owns Music Assistant's metadata management: enriching library items with metadata from the music and metadata providers, resolving and serving images, and looking up artwork for radio streams. Per-method behaviour lives in the docstrings; this file covers how the package is structured and the cross-cutting design decisions.

## Package Layout

The `MetaDataController` is composed from a set of mixins, each in its own module, mirroring the Player Controller (`controllers/players/`). All behaviour is reachable on the single controller instance; the split is purely for organising a large surface.

- `controller.py` — the `MetaDataController` itself: lifecycle, config entries, preferred-language handling, the public enrichment entrypoint and the scheduled maintenance tasks. Combines the mixins below with `CoreController`.
- `images.py` (`ImageProxyMixin`) — image resolution, the opaque image-id system, thumbnail rendering/caching, the `/imageproxy` endpoint, palette extraction and playlist collages.
- `radio.py` (`RadioArtworkMixin`) — resolving radio-stream artwork by matching the station's now-playing metadata against the library and MusicBrainz/online providers.
- `enrichment.py` (`MetadataEnrichmentMixin`) — the per-mediatype routines that merge provider metadata into library items.
- `helpers.py` — pure functions that don't need the controller instance.
- `constants.py` — shared constants (config keys, cache categories, task ids, the locale map and imageproxy tunables).
- `strings.json` — translatable strings for this core module (the `manifest` name/description shown in the UI). It is only discovered because the controller lives in its own folder: `scripts/build_translations.py` concatenates each `controllers/<domain>/strings.json` into the source catalogue under the `core.<domain>` namespace.

## Design Notes

- **Image ids / imageproxy.** Images are addressed by an opaque, deterministic id (`sha256(provider + path)`) instead of carrying the raw provider/path on the query string. The id is exposed to clients as the `proxy_id` field on `MediaItemImage` (injected during outbound serialization) and fetched at `/imageproxy/<image_id>?size=&fmt=`. The mapping is registered when the id is generated — write-through an in-process LRU in front of the cache controller, so resolving a freshly generated id never blocks on SQLite — and resolved back to `(provider, path)` when the endpoint serves the thumbnail. Because only server-registered ids resolve, the endpoint cannot be coerced into fetching an arbitrary URL.
- **Local-over-online.** Provider mappings are processed in priority order so local sources win over streaming/online ones, and online metadata is only fetched when enabled and (for most types) when an item actually needs a refresh. Online genres are not merged on top of locally-supplied ones when "prefer local genres" is set.
- **Refresh interval.** Enrichment for a given item only re-runs every `REFRESH_INTERVAL` (90 days) unless a refresh is forced, keeping load on the free online services low. Artist bios are re-derived each refresh and picked by a fixed preferred-language-first fallback policy.
- **Radio artwork.** Stations send free-form `artist - title` strings, so the radio subsystem normalizes and heuristically re-orders the names before matching them against the library and MusicBrainz, and caches both hits and misses to avoid hammering the providers.
- **Maintenance tasks.** The missing-artist-metadata scan, playlist refresh and thumbnail-cache cleanup run daily at a per-instance randomized time so independent installations don't all hit the shared MusicBrainz mirror at once.
