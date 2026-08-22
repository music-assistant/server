# #5902 salvage inventory (Revision 3)

Branch `marcelveldt/spotify-connect-redirect-phase2` @ `5899d006a` (draft PR #5902, parked).
Where the four surviving pieces live, what they depend on, and what has to be cut off them.
Everything not listed here is mode/provisioning/outsourced-queue work and is OUT.

**Whole-branch caveats**
- The branch predates `f8749bcc1` (#5910). Rebasing conflicts in
  `spotify_connect/provider.py::on_source_selected` — resolve to dev's version and keep only the
  `redirect_pending` capture line on top (if the redirect survives at all).
- `spotify/provider.py` is the hub: `_connect_mode` (line ~1705) gates lines 157/165/826/932/951.
  Every survivor below is reachable without it except where noted.

---

## A. Premium + duplicate-account gating — cleanest salvage, no redirect dependency

| What | Where |
|---|---|
| `_verify_account()` | `providers/spotify/setup_flow.py:214` |
| `_account_in_use()` | `providers/spotify/setup_flow.py:247` |
| call site | `setup_flow.py:145`, in `run_setup` right after the PKCE exchange |
| `SpotifyProvider.account_id` | `providers/spotify/provider.py:234` |
| strings | `spotify/strings.json` → `setup_flow.abort.premium_required`, `setup_flow.abort.account_already_configured` |
| tests | `tests/providers/spotify/test_setup_flow_playback_mode.py:259,284,302` |

Commit: `b1b0060f4` (+ `6d7e4c417` for the premium half, `5899d006a` copy tweak).
Dependencies: none on the redirect. One `/me` call for both checks, using the access token from
the code exchange (deliberate: minting a fresh one rotates and revokes the just-stored refresh
token — keep that comment). `AbortFlow` import.
Cut: the test file also covers mode/provisioning steps — take only the three `test_account_*`
tests, and `_make_session` needs `mass.providers = []`.

## B. Account verification — Soloist-path only per Rev 2 §0.1

| What | Where |
|---|---|
| `_verify_connect_account()` | `providers/spotify/connect_redirect.py:199` |
| `_get_connect_devices()` | `connect_redirect.py:232` |
| `_get_player_data()` | `connect_redirect.py:432` — the market/country-free call path (204→None) |
| `_stamp_verified_connect_account()` | `providers/spotify/setup_flow.py:498` (flow-side, post-pairing) |
| plugin side | `spotify_connect/provider.py`: `verified_account_id`, `set_verified_account_id()`, `get_backend_account_id()`; backend contract `get_account_id()` in `base.py`, go-librespot impl reads `status.username` |
| tests | `test_connect_redirect.py:180,192,206` |

Commit: `7bd416476`, hardened in `b3fb6bf05` (made side-effect-free — never activate a session
during a lookup; keep that property).
Note: in-memory cache only, so a provider reload re-verifies. Deliberate.

## C. Opportunistic redirect — the piece Rev 3 keeps

| What | Where |
|---|---|
| `find_active_connect_delegate()` | `connect_redirect.py:97` — THE narrowed capability |
| `_is_redirect_target()` | `connect_redirect.py:186` |
| `_iter_connect_plugins()` | `connect_redirect.py:193` (only needed by the mode path — see below) |
| hook | `MusicProvider.get_playback_delegate()` in `models/music_provider.py`; provider impl `provider.py:951` |
| queue seam | `controllers/player_queues/delegation.py` (`PlaybackDelegationMixin`) |
| tests | `test_connect_redirect.py:216,238` (`test_librespot_mode_*` — rename; they test exactly the narrowed behaviour) |

Commit: `7bd416476`; gate corrected in `7c5a164f8` (requires `plugin.active_player_id ==
target_player_id`, not just `session_active` — without it a stale leftover source steals a session
that moved to another player).
Already mode-independent: `find_active_connect_delegate` is the `else` branch of `_connect_mode`,
so it survives by DELETING the mode branch above it. `find_connect_delegate()` (:70) and
`_iter_connect_plugins()` are the mode path — out.
Open: `_apply_playback_delegation` in delegation.py is written around "all items share one
delegate → swap the batch for the AudioSource". Whether that shape survives depends on the
AudioSource-as-player-source investigation.

## D. Context mapping — useful for the redirect

| What | Where |
|---|---|
| `_delegate_context()` | `connect_redirect.py:348` — container → `spotify:{album,playlist,artist,show,audiobook}:id` + start offset |
| `_delegate_item_uri()` / `_delegate_item_uris()` | `connect_redirect.py:334` / `:313` (100-uri cap) |
| `_item_id_for_this_provider()` | `connect_redirect.py:413` (own-instance mapping preferred) |
| `_web_api_play()` | `connect_redirect.py:245` — PUT me/player/play?device_id, context+offset or uris |
| tests | `test_connect_redirect.py:279,311,332,346,364,376,396,408` |

Commits: `7bd416476`, then `7c5a164f8` (library-uri start items resolve via the resolved batch,
never by slicing the uri tail), `8fbc9e0c9` (podcasts/audiobooks), `fb59b39a8`
(**`spotify:audiobook:` is context-ONLY — Spotify 400s it inside `uris`**), `b1b0060f4`
(100-uri cap).
Structural note: all of C and D live in `SpotifyConnectRedirect` (`connect_redirect.py`, 470
lines, extracted in `3220bde74`, mirrors `streaming.py`). Its `logger`/`throttler` properties
delegate to the provider so `@throttle_with_retries` works and the dev-throttler swap stays live —
keep that if the class is reused.

---

## Explicitly OUT (do not rebuild)

`setup_flow.py` `_setup_connect_playback` / `_provision_connect_instance` / `_load_connect_instance`
/ `_choose_playback_backend` / `_setup_playback` / `_ask_connect_consent` / `_ask_connect_api_key`
/ `_find_connect_device` / `_await_connect_device` and their steps + strings; `helpers.py`
`ensure_connect_instance` / `get_system_wide_connect_config_id` /
`has_running_system_wide_connect` + `_ENSURE_CONNECT_LOCK`; `constants.py`
`CONF_PLAYBACK_BACKEND` / `BACKEND_CONNECT`; plugin `CONF_SETUP_PENDING` / `CONF_SYSTEM_MANAGED`
+ the `handle_async_init` gate; plugin flow player→device_name split;
`ConfigController.create_pending_provider_config`; `playback_requires_delegate` +
`_connect_mode`; `play_on_delegate`'s ADD/NEXT session-enqueue coercion.

Note the Soloist ceremony steps (consent, API key, pairing intro + `/me/player/devices` polling)
are listed OUT *as plugin provisioning*, but Rev 2 §0.1 re-homes the same UX onto the provider if
a Soloist backend lands — reuse the step bodies and strings from `8bb92cadb` / `5247d1dc2` rather
than writing them again.
