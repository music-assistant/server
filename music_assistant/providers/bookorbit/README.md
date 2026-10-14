# BookOrbit music provider

Music Assistant can use [BookOrbit](https://github.com/bookorbit/bookorbit) as an audiobook source. BookOrbit is a self-hosted server for your audiobook library; this provider connects Music Assistant to that server so you can browse, search, stream, and sync listening progress.

This provider is **audiobooks only** (no podcasts or ebooks in v1). It is modeled on the [Audiobookshelf provider](https://www.music-assistant.io/music-providers/audiobookshelf/) but talks to BookOrbit’s native `/api/v1` API.

## Features

| | |
| --- | --- |
| Subscription | Free (self-hosted) |
| Self-hosted | Yes |
| Media types | Audiobooks |
| Login | Username and password |
| Multi-instance | Yes (one BookOrbit login per Music Assistant user) |
| Progress sync | Both directions (just before play and on MA progress updates) |
| Browse | Libraries → authors, narrators, series, audiobooks |
| Search | Audiobooks, authors, narrators |

## Configuration

You need:

- **Server** — Base URL of your BookOrbit instance, including scheme and port if needed. Examples:
  - `https://bookorbit.example.com/`
  - `http://192.168.1.10:3000/`
  - `http://bookorbit.bookorbit.svc:3000/` (in-cluster Music Assistant → in-cluster BookOrbit)
  - `http://127.0.0.1:3000/` (local dev with `kubectl port-forward`)

- **Username** — A BookOrbit user account that can access your audiobook libraries.

- **Password** — That user’s password.

**API token** — Reserved for a future BookOrbit release; leave empty and use username/password.

### Production and network path

Prefer a **direct URL** on your LAN or inside the cluster. Music Assistant must reach BookOrbit without browser-only edge auth (for example Cloudflare Access on a public hostname). If MA runs in Kubernetes, use the cluster Service URL rather than the public ingress when possible.

### Multi-user Music Assistant

Because `multi_instance` is enabled, add the provider once per MA user with the BookOrbit account that should own progress for that user. See [Music Assistant user management](https://www.music-assistant.io/usage/user-management/) for linking MA users to provider instances.

### Advanced settings

- **Verify SSL** — Validate TLS certificates for `https://` URLs (default: on). Turn off only for local testing with self-signed certificates.

## After setup

Run a **library sync** so audiobooks, authors, and narrators appear. Browse under the provider’s library folders. Playback progress in Music Assistant is written back to BookOrbit; progress changed in BookOrbit is read just before play.

## Known limitations

- Audiobooks only; mixed libraries are filtered to audio formats at sync time.
- No podcast or ebook support in v1.
- Official docs on [music-assistant.io](https://www.music-assistant.io/) will be added when this provider is accepted upstream; until then this README is the reference for setup.

## Upstream documentation

When merged, add a page under `music-providers/bookorbit` in the [Music Assistant documentation](https://github.com/music-assistant/docs) mirroring this content, and set `documentation` in `manifest.json` to `https://music-assistant.io/music-providers/bookorbit`.
