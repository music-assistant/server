---
applyTo: "**"
---

<!-- Generated; additive to copilot-instructions.md + AGENTS.md. -->
# Cross-repo: the frontend is a client of this API

The Music Assistant web frontend (`music-assistant/frontend`, Vue/TypeScript) consumes this server's API commands, shared models, and wire/streaming contract, so a server change can break it silently. When a PR changes an API command, a shared model, the wire contract, or `API_SCHEMA_VERSION`:

- **Read the frontend before assuming it is unaffected.** Use the GitHub MCP to inspect `music-assistant/frontend` — its code and its open PRs — for how the changed command, field, or model is consumed, and flag a break or a needed companion change.
- The frontend **gates newer-server commands on `schema_version`**, so a backwards-incompatible client-facing addition — a new or changed API command, a shared-model change, or a new remote-access channel label — must bump `API_SCHEMA_VERSION`. ([frontend#1911](https://github.com/music-assistant/frontend/pull/1911#discussion_r3408564733): "setLocale now checks the server's schema_version and skips the command on servers < 32")
- Behavior **all API clients need** (volume, queue, filtering) belongs in the server, not as a frontend workaround. ([frontend#1569](https://github.com/music-assistant/frontend/pull/1569#issuecomment-4124730842): "We should not accept this to be implemented in the frontend at all")
- The frontend **will not add a silent fallback that masks a broken server contract** — a change to a field's presence or shape must surface there, not be hidden. ([frontend#2083](https://github.com/music-assistant/frontend/pull/2083#discussion_r3565206399): "a fallback would mask a broken server contract")
