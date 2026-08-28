---
applyTo: "**"
---

<!-- Generated; additive to copilot-instructions.md + AGENTS.md. -->
# Cross-repo awareness

This server's API and data models are a shared contract consumed by other repos. When a PR touches that contract, check the owning/consuming repo (its code and open PRs) via the GitHub MCP before approving.

## The frontend is a client of this API

The Music Assistant web frontend (`music-assistant/frontend`, Vue/TypeScript) consumes this server's API commands, shared models, and wire/streaming contract, so a server change can break it silently. When a PR changes an API command, a shared model, the wire contract, or `API_SCHEMA_VERSION`:

- **Read the frontend before assuming it is unaffected.** Use the GitHub MCP to inspect `music-assistant/frontend` — its code and its open PRs — for how the changed command, field, or model is consumed, and flag a break or a needed companion change.
- The frontend **gates newer-server commands on `schema_version`**, so a backwards-incompatible client-facing addition — a new or changed API command, a shared-model change, or a new remote-access channel label — must bump `API_SCHEMA_VERSION`. ([frontend#1911](https://github.com/music-assistant/frontend/pull/1911#discussion_r3408564733): "setLocale now checks the server's schema_version and skips the command on servers < 32")
- Behavior **all API clients need** (volume, queue, filtering) belongs in the server, not as a frontend workaround. ([frontend#1569](https://github.com/music-assistant/frontend/pull/1569#issuecomment-4124730842): "We should not accept this to be implemented in the frontend at all")
- The frontend **will not add a silent fallback that masks a broken server contract** — a change to a field's presence or shape must surface there, not be hidden. ([frontend#2083](https://github.com/music-assistant/frontend/pull/2083#discussion_r3565206399): "a fallback would mask a broken server contract")

## Shared models (`music-assistant/models`)

The data models are not server code — they are a versioned shared contract (`music-assistant-models` on PyPI, exact-pinned by the server and consumed by the frontend and mobile clients). When a server change adds, renames, or reshapes anything serialized over the API (media items, players, queues, config entries, enums), that part belongs in the models repo, not server-local. Read the current model definitions via the GitHub MCP (`music-assistant/models`, `music_assistant_models/`) before approving. Flag:

- **Belongs in models, ships models-first.** A serialized-shape change goes in a models PR, gets released, then the server PR bumps the `music-assistant-models` pin — never a server-local edit, hand-rolled dict, or raw-SQL shape that bypasses the model layer.
- **Backwards compatible by default.** New fields get defaults (appended after existing public fields); enum values are deprecated in place with a removal TODO, never removed/renamed; prefer an optional flag on an existing model over a new media type, and gate client-visible behavior on `schema_version`.
- **Server state stays server-side.** Data only the server needs is kept in the server repo or added as a non-serialized field (`serialize="omit"`, `deserialize=pass_through`); models never import server code or grow heavy dependencies.
- **Don't duplicate the contract.** Reuse existing models, enums (`ProviderFeature`, `ArtistType`), and fields (the `uri`, `metadata`, `extra_attributes`) before proposing new ones.
