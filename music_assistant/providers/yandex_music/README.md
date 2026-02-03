# Yandex Music Provider

Music Assistant provider for [Yandex Music](https://music.yandex.ru).

## Configuration

- **Yandex Music Token** — OAuth token for the Yandex Music API. Required for search, library, and streaming.
- **Audio quality** — Choose High (320 kbps) or Lossless (FLAC) when available.
  FLAC is only offered by the Yandex API when your account has **Yandex Music Plus** and the track has a lossless version in the catalog. The provider prefers **flac-mp4** and **aac-mp4** (Yandex moved to these formats around 2025). When you select Lossless, the provider first requests the stream via the get-file-info API; if that returns 401 Unauthorized (e.g. OAuth token not accepted for this endpoint), it retries with a different transport and, if still unauthorized, falls back to the standard download-info list and uses the best available quality (typically 320 kbps MP3). For lossless via get-file-info, a token or session that the endpoint accepts may be required (e.g. from the web client at [music.yandex.ru](https://music.yandex.ru)); if you have Plus and a track has lossless but you get 401, try updating the token from the web client. If the API returns only MP3 in the download-info list, the provider uses it and logs a warning.

## Obtaining an OAuth token

Yandex Music does not offer an official public OAuth flow for third-party apps. The token is the same one used by the web or mobile app. You can obtain it in one of these ways:

1. **Browser developer tools (web)**
   Log in to [music.yandex.ru](https://music.yandex.ru), then:
   - Open Developer Tools (F12) → **Network** tab.
   - In the filter box type `api.music.yandex` or leave all. Trigger a request (e.g. play a track, open Search, or refresh the page).
   - Click any request to `api.music.yandex.ru` or `api.music.yandex.net` in the list → **Headers** → **Request Headers**.
   - Find **Authorization**. The value is like `OAuth y0_AgAAAAA...` — the token is the part after `OAuth ` (e.g. `y0_AgAAAAA...`). Copy that string into the provider’s token field.

2. **Community tools**
   Some open-source tools and scripts can generate or extract a token by simulating the official client. Use them at your own risk and only from sources you trust.

3. **Documentation**
   For up-to-date methods, check the [Music Assistant documentation](https://music-assistant.io/music-providers/yandex/) or the [yandex-music Python library](https://github.com/MarshalX/yandex-music) and its discussions.

**Important:** Keep your token private. Do not share it or commit it to version control. The token grants access to your Yandex Music account.

## Supported features

- Search (tracks, albums, artists, playlists)
- Library (liked artists, albums, tracks; user playlists)
- Add/remove library items (like/unlike)
- Browse library (artists, albums, tracks, playlists)
- Streaming (HTTP direct links; quality selection)
- Lyrics (plain text and optional LRC when available from the API; same endpoint as the web client)

## Checking if a track has lossless (FLAC)

The library does not expose a "has lossless" field on the track; the API decides which codecs to return when you request download info (depending on catalog and account/subscription). Supported lossless codecs are **flac** and **flac-mp4** (preferred since ~2025). To see what formats the API returns for a given track, run from the repo root:

```bash
YANDEX_MUSIC_TOKEN=your_token python scripts/check_yandex_track_formats.py [track_id]
```

Example for track 132401416 (album 33801370):

```bash
YANDEX_MUSIC_TOKEN=... python scripts/check_yandex_track_formats.py 132401416
```

If the output shows only MP3, then for that track/account the API does not offer FLAC (either the track has no lossless in the catalog or the account needs Yandex Music Plus).

## Development and testing

See the project root `DEVELOPMENT.md` for setting up the environment. Provider tests live under `tests/providers/yandex_music/`.
