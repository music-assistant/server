# Yandex Music Provider

Music Assistant provider for [Yandex Music](https://music.yandex.ru).

## Configuration

- **Yandex Music Token** — OAuth token for the Yandex Music API. Required for search, library, and streaming.
- **Audio quality** — Choose High (320 kbps) or Lossless (FLAC) when available.

## Obtaining an OAuth token

Yandex Music does not offer an official public OAuth flow for third-party apps. The token is the same one used by the web or mobile app. You can obtain it in one of these ways:

1. **Browser developer tools (web)**
   Log in to [music.yandex.ru](https://music.yandex.ru), open Developer Tools (F12) → Application/Storage → look for cookies or local storage that contain a token, or inspect network requests to the API and copy the `Authorization` header or token from the request.

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

## Development and testing

See the project root `DEVELOPMENT.md` for setting up the environment. Provider tests live under `tests/providers/yandex_music/`.
