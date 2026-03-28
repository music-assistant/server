## Summary

Extends the Deezer provider with mood and genre Flow playlists (Happy, Chill, Focus, Party, Rock, Metal, etc.) building on top of #3077.

These Flow variants are only available through Deezer's unofficial GW API (`radio.getUserRadio`), so the changes go into `gw_client.py` and `__init__.py`.

Available Flows are discovered dynamically via the `page.get` GW endpoint instead of being hardcoded, so regional and user-specific variations are picked up automatically. Each Flow gets proper cover art from Deezer's CDN.

The recommendations view now includes two new folders:
- **Deezer Mood Flows** — Happy, Chill, Focus, Melancholy, Party, Love, Motivation
- **Deezer Genre Flows** — Rock, Metal, Electronic, Classical, etc. (varies by region)

Also wraps `get_user_recommended_albums` and `get_user_recommended_artists` in try/except since the Deezer API occasionally returns errors on those endpoints, which would otherwise prevent all recommendations from loading.

## Test Plan

- Tested locally, mood and genre Flows show up in recommendations and play back correctly
- Verified that recommendations still load when `get_user_recommended_artists` returns an error

Suggested label: `enhancement`
