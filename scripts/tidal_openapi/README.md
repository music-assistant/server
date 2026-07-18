# Tidal official API models

The Tidal provider is migrating its read/write operations (catalog, search,
favorites, playlists) from the unofficial `api.tidal.com/v1` API to the official
`openapi.tidal.com/v2` API.

Some functionality stays on the unofficial API because the official API does not
offer a usable equivalent at the third-party access tier:

- **Playback** (the official API only serves 30s previews) and **lyrics**.
- **Recommendations** (the whole feed). The editorial content
  (curated playlists, charts, new-music modules) is gated to a higher access
  tier (`dynamicPages`/`dynamicModules` are not reachable). The personalized
  mixes are only reachable via the `userRecommendations` endpoint, which is
  **deprecated** with a ~6-month removal window, so building on it would just
  buy a forced rewrite back to `pages/*`. The existing `pages/*` scraper
  therefore stays as the single source for recommendations.

This directory holds the tooling for the typed models generated from the
official API's OpenAPI spec.

## Files

- `tidal-api-oas.json` — vendored copy of the official spec (source of truth for
  generation and for spotting upstream changes). Not shipped in the package.
- `generate_models.py` — regenerates the TypedDict models from the vendored spec.

The generated output lives at
`music_assistant/providers/tidal/_openapi_models.py` and **must not be edited by
hand**. Only the `*_Attributes` payloads are generated: the JSON:API envelope
(`data` / `included` / `links`) is handled generically in the provider's api
client, so it is not modelled here.

## Regenerating the models

```sh
python scripts/tidal_openapi/generate_models.py
```

This runs `datamodel-code-generator` via `uvx` (no project dependency added) and
formats the result with `ruff`. To cover a new area in a later slice, add its
`*_Attributes` schema name to `SEED_SCHEMAS` in `generate_models.py` and rerun.

## Refreshing the vendored spec

The official spec evolves (new fields, deprecations with a stated 6-month
window). To pick up changes:

```sh
curl -sSL https://tidal-music.github.io/tidal-api-reference/tidal-api-oas.json \
  -o scripts/tidal_openapi/tidal-api-oas.json
git diff scripts/tidal_openapi/tidal-api-oas.json   # review what changed upstream
python scripts/tidal_openapi/generate_models.py     # regenerate models
```

The `git diff` on the vendored spec is the low-effort way to stay ahead of
deprecations: it surfaces exactly which fields/endpoints changed so the handful
we use can be adjusted deliberately.
