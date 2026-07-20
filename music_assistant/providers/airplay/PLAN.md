# AirPlay Provider Overhaul — Unified `cliairplay` Binary

**Status:** MA-side integration of the unified binary contract is code-complete.
Binary side is device-validated: RAOP, AP2 RAOP-compat, native AP2 (transient AND
HomeKit-PIN pair-verify), PTP timing with multi-room sync via a shared daemon, and
24-bit hi-res over the realtime stream. Remaining: on-device validation of the
MA-driven flows, CI-built binaries for all platforms.

**Branch:** `airplay-unified-binary` (freshly cut from `origin/dev`; the earlier
`gifted-clarke` branch was ~1067 commits behind and is abandoned — its work is
stashed as `airplay-unified-wip` and backed up under `/tmp/airplay-backup/`).

---

## 1. Goal

Replace the two separate AirPlay streaming binaries with a single unified one:

| Old | Source | Problems |
|-----|--------|----------|
| `cliraop` | music-assistant/libraop (C, MIT) | RAOP/AirPlay 1 only |
| `cliap2`  | music-assistant/cliairplay (wraps owntone, GPL) | broken AP2 sync, slow start, no 24-bit, heavy owntone dependency, second codebase |

**New:** one binary `cliairplay` (built in `~/Workdir/music-assistant/cliairplay/`)
that handles both RAOP and AirPlay 2 through a `--protocol raop|airplay2` flag, built
on top of philippe44/libraop (MIT) with our own AP2 implementation layered on — no
owntone dependency.

---

## 2. What works today

### RAOP (AirPlay 1)
- Full playback via libraop's `raopcl`. 16-bit ALAC. Tested audible on Sonos.

### AirPlay 2 — RAOP-compatible flow
- The fallback path for devices that can't do a native session: `auth-setup`
  (MFi X25519 exchange, no stored credentials) → RAOP `ANNOUNCE`/`SETUP`/`RECORD`.
- 16-bit ALAC tested audible on Sonos. Note: Sonos (and JBL/WiiM) actually take
  the NATIVE flow via transient pairing — auto-selection prefers it; compat
  remains the safety net.

### AirPlay 2 — native flow (all tested devices)
- Full HAP pair-verify against a real Apple TV 4K:
  X25519 ECDH + Ed25519 signatures + HKDF-SHA512 + ChaCha20-Poly1305.
- Encrypted RTSP channel (HAP framing: 2-byte LE length + ciphertext + 16-byte tag,
  nonce = 4 zero bytes + 8-byte LE counter, 1024-byte max frames).
- Binary-plist session SETUP (`deviceID` colon-hex + `sessionUUID` + `timingPort` +
  `timingProtocol`), events reverse-connection, stream SETUP (`streams` array), RECORD.
- Encrypted RTP audio: ALAC → ChaCha20-Poly1305 (key = X25519 shared secret,
  nonce = 4 zero bytes + 2-byte seqnum, AAD = 8 bytes of RTP header) + sync packets.
- EAR-VERIFIED on a Sonos stereo pair + a second Sonos (transient pairing) and
  an Apple TV 4K (HomeKit-PIN pair-verify), including multi-room sync on a
  shared PTP daemon clock and MIXED RAOP+AP2 groups on one `--start-unix-ms`.
  Device-reported render latency (`arrivalToRenderLatencyMs`, Apple TV ~107 ms,
  HomePod ~69 ms) is parsed and surfaced for diagnostics but NOT applied to the
  timeline: receivers already self-compensate it, so applying it made those
  devices play early. Real downstream latency (TV / AV receiver / amplifier) is
  per-household and dialed in manually via the player's `sync_adjust`.

### 24-bit ALAC
- Encoder fixed: the libcodecs `alac_wrapper.cpp` hardcoded `mFormatFlags=1` (16-bit);
  our `alac_ext.cpp` override sets it from the actual bit depth (16→1, 20→2, 24→3, 32→4).
- Rides the native AP2 REALTIME stream. Ear-verified on Apple TV at 44.1/24 and
  48/24 (correct speed, reference-receiver decode). Input from FFmpeg is s32le,
  truncated to s24le in the binary. Per-player opt-in in MA: device format
  tables are unreliable in both directions (Apple TV renders unadvertised
  24-bit; Sonos 200-accepts 48/24 then plays silence).

---

## 3. The critical fixes (hard-won findings)

1. **`X-Apple-HKP: 3` for pair-verify.** We first used `4` (transient pairing).
   For PIN-based (stored-credential) pairings — the only kind MA produces — pair-verify
   must use `X-Apple-HKP: 3`, matching what owntone sends for `PAIR_CLIENT_HOMEKIT_NORMAL`.
   Using `4` returned HTTP 200 but TLV error `0x02` (authentication) and silently
   dropped all encrypted messages afterward. This was the single biggest blocker.

2. **client_id = DACP ID as UPPERCASE ASCII string** (e.g. `b"AABBCCDD11223344"`),
   not raw bytes. Must match what MA's pair-setup sent (owntone formats it via
   `%016PRIX64`). MA's `provider.dacp_id` is already uppercased.

3. **Audio encryption key = the X25519 shared secret** from pair-verify, NOT the `shk`
   we send in the stream SETUP plist. First 32 bytes used directly.

4. **Audio nonce = 4 zero bytes + 2-byte network-order seqnum** (+ 6 zero) — not an
   8-byte counter. AAD = RTP header bytes 4-11 (timestamp + SSRC).

5. **RTP SSRC must equal the `streamConnectionID`** sent in the stream SETUP.

6. **Sequence: session SETUP → open events reverse TCP connection → stream SETUP →
   RECORD.** RECORD before the events connection times out. Stream SETUP before the
   events connection returns 500.

7. **Session SETUP `deviceID` is 8-byte colon hex** (`B2:76:3A:DA:DB:41:4A:27`), the
   DACP id, not a 6-byte MAC.

8. **Credentials format (192 hex chars):** 64-byte Ed25519 secret (32 seed + 32 pub) +
   32-byte server public key. OpenSSL uses the 32-byte seed; produces identical
   signatures to owntone/libsodium's 64-byte key.

---

## 4. Repositories

- **libraop** (submodule): `philippe44/libraop` — upstream MIT, unchanged.
- **cliairplay (our new binary):** built locally in `~/Workdir/music-assistant/cliairplay/`,
  pushed to **https://github.com/music-assistant/airplay-cli** (private, `main` branch).
  Distinct from `music-assistant/libraop` and `music-assistant/cliairplay` (the old owntone
  wrapper). `bin/` is gitignored — prebuilt binaries belong in CI/releases, not source.

### C source layout (`cliairplay/src/`)
```
cliairplay.c     CLI entry, arg parsing, RAOP + AP2 dispatch, audio loop
ap2_client.c/.h  AP2 orchestration: RAOP-compat + native flow, RTP send, sync packets
ap2_hap.c/.h     HAP pair-verify, encrypted RTSP framing, shared-secret access
ap2_session.c/.h native-AP2 RTSP session helper
ap2_rtsp.c/.h    RTSP request/response helpers
ap2_plist.c/.h   minimal binary-plist writer (handles nested streams array)
ap2_bplist.cpp/.h C++ bplist bridge (reads device responses)
ap2_ptp.c/.h     NTP timing responder + PTP offset placeholder
alac_ext.cpp/.h  ALAC encoder override fixing 24-bit mFormatFlags
```

---

## 5. MA provider integration (Python)

The unified binary changed the provider from a two-binary + `protocols/` package model
to a single `stream.py`. On the fresh `airplay-unified-binary` branch these changes must
be **re-applied on top of the current dev code** (which has since evolved — new
`get_source_ip_for_target` helper, Sendspin derived transports, JBL AP2 preference,
`strings.json`, AP2-grouping-allowed). Reconciliation, not overwrite.

Changes to port from the abandoned branch (backed up in `/tmp/airplay-backup/`):
- **New `stream.py`** — single `AirPlayStream` class replacing `protocols/_protocol.py`,
  `protocols/raop.py`, `protocols/airplay2.py`. Parses normalized `[STATUS]` stderr.
- **`helpers.py`** `get_cli_binary()` — drop the `protocol` arg; always locate
  `cliairplay-<os>-<arch>`; `--check` returns `cliairplay check`. (Dev's version still
  takes `protocol` and looks for cliraop/cliap2 — must be updated, keeping dev's new
  `get_source_ip_for_target`-based `resolve_if_ip`.)
- **`constants.py`** — drop `CONF_ENCRYPTION`, `CONF_ALAC_ENCODE`, `AIRPLAY2_MIN_LOG_LEVEL`
  legacy keys (binary always ALAC-encodes and is always "encrypted" per protocol).
- **`player.py`** — remove AP2 SET_MEMBERS/grouping restrictions (dev already removed the
  "AP2 can't group" note, so this largely converges); drop the ALAC/encryption config
  entries; keep dev's JBL AP2-preference and disabled-option work.
- **`sendspin_bridge.py`** — use `AirPlayStream` instead of `RaopStream`/`AirPlay2Stream`;
  keep dev's first-class derived-transport rework.
- **`bin/`** — remove `cliraop-*` and `cliap2-*`; add `cliairplay-<os>-<arch>` (only
  `cliairplay-macos-arm64` committed; CI builds the other targets).
- CLI args the provider passes: `--protocol auto|raop|airplay2`, `--volume`, `--dacp`,
  `--activeremote`, `--cmdpipe`, `--start-unix-ms`, `--samplerate`, `--bitdepth`,
  `--latency` (only as explicit user override), `--auth` (HAP creds → native flow),
  `--secret` (legacy RAOP pairing), `--txt` (full _airplay TXT), mDNS props
  (`--et/--md/--am/--pk/--pw/--cn/--udn`), `--password`, `--if`, `--publish-ip`,
  `--ptp-shared` (while the provider's `--ptp-daemon` runs), device address + `-` (stdin).

---

## 6. Remaining work

**Done since this plan was first written:**
- The provider is reconciled onto latest dev (branch `airplay-unified-binary`); the
  C project lives at [music-assistant/airplay-cli](https://github.com/music-assistant/airplay-cli)
  (private) — its own `TODO.md` tracks the binary-side roadmap.
- Binary-side validation is complete (2026-07-19): native AP2 + PTP audible on
  Sonos speakers and an Apple TV 4K, multi-room sync verified across a mixed
  group, 24-bit hi-res audible over the realtime stream, HomeKit `--pair-setup`
  end-to-end.
- **MA-side integration of the new CLI contract (this branch):**
  - `--protocol auto` is the default; the per-player protocol config is an override
    only. The full `_airplay._tcp` TXT is passed via `--txt` for route selection.
  - Group start switched from MA-side NTP math to `--start-unix-ms` ("first sample
    audible at this instant", same value for every group member). All NTP helpers
    removed; MA budgets a fixed per-protocol setup lead (`AIRPLAY_RAOP_SETUP_LEAD_MS`
    1500 ms, `AIRPLAY_AP2_SETUP_LEAD_MS` 2500 ms — native AP2 needs more pre-fill
    headroom), the group taking the largest member lead.
  - `--latency` is only passed when the user explicitly configured an override
    (0 = automatic: binary default 2000 ms clamped to the device window).
  - One `cliairplay --ptp-daemon` per provider lifetime (spawn at setup, SIGTERM on
    unload, restart-once-on-crash); every AP2-capable stream gets `--ptp-shared`.
  - Per-player hi-res opt-in (`hires_playback`, advanced, AirPlay 2 only):
    advertises 44100/24 + 48000/24 and feeds the binary s32le with `--bitdepth 24`.
  - HAP pairing rewired through `cliairplay --pair-setup` (PIN via stdin,
    `CREDENTIALS:` from stdout); RAOP legacy pairing stays native Python.
  - `[STATUS] latency` (stdout) parsed and logged; `--publish-ip` passed when it
    differs from the resolved `--if` address; RAOP `--secret` restored; `--cn` added
    to the forwarded RAOP mDNS props.

### Binary distribution
- The airplay-cli repo cross-builds all four targets in CI (linux x86_64/aarch64,
  macos arm64/x86_64). The prebuilt macOS binary committed to `bin/` is accepted for
  **local testing only** — the end goal is to fetch pinned release binaries in the MA
  container build process rather than committing them.

### Validation (MA-driven, on-device)
- Full regression pass of the MA-driven flows: RAOP, AP2 RAOP-compat, native AP2
  (transient + pair-verify), mixed sync groups, late join, hi-res opt-in.
- Config-flow pairing of an Apple TV/HomePod through the new `--pair-setup` path.
- PTP daemon behaviour in the MA container (non-root 319/320 bind → degraded-mode
  warning path).

---

## 7. Test references

- Reference devices: a Sonos portable speaker (RAOP + AP2 RAOP-compat) and an
  Apple TV 4K (native AP2, HomeKit-PIN paired).
  Credentials live in MA settings (`airplay_credentials`, Fernet-encrypted with
  `base64.urlsafe_b64encode(server_id[:32])`); the DACP id is derived from the
  server id.
- Test tone: `ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ar 44100 -ac 2 -f s16le`.
- Sonos needs a metadata command (`ACTION=SENDMETA`) after connect before it emits audio.

---

## 8. Future architecture (planned)

### 8.1 Two player models

Split the provider into two implementations:

1. **Generic AirPlay devices** (Sonos, Samsung, JBL, WiiM, …) —
   `PlayerType.PROTOCOL`: an AirPlay streaming endpoint and nothing more. No
   Companion, no MRP. Full metadata support via the DMAP/SET_PARAMETER channel
   (already implemented; these devices require and consume it).
2. **Apple devices** (Apple TV, HomePod) — `PlayerType.PLAYER`: AirPlay for
   streaming PLUS complete device integration in the spirit of Home Assistant's
   apple_tv integration — know when external media is playing on the device,
   control power, playback and volume, track state even when MA is not
   streaming. This is the **Companion protocol** (plus MRP for the
   now-playing session, below).

### 8.2 MRP + Companion — and where each lives

Only genuine Apple devices speak MRP/Companion; generic receivers are fully
served by the DMAP metadata they already get.

- **MRP now-playing (during OUR stream)** rides the AirPlay 2 **data channel of
  the active session** (encrypted with the session's keys). The natural home is
  the **cli binary** — it owns the session, the channel and the keys, and MA
  already feeds it metadata over the cmdpipe; the binary would translate that
  to MRP protobuf frames. Benefits: tvOS now-playing display, and standby
  prevention (tvOS gates "media playing, don't sleep" on the MRP-established
  system session — the reason an Apple TV can sleep mid-stream today).
- **Companion protocol (device control, independent of streaming)** is a
  separate connection that exists with or without an active AirPlay session.
  The natural home is **MA/Python** (pyatv is the mature reference
  implementation and a candidate dependency) — power on/off, wake-on-play,
  external-playback state, volume/remote control.

### 8.3 Bonus: Apple TV as a cast display

Mirror of the cast-displays feature (cast button on the quiz / party /
fullscreen now-playing pages): render such a page on the Apple TV. Honest
assessment of the AirPlay routes:

- **MRP now-playing screen** — realistic v1: tvOS's own now-playing UI with our
  metadata/artwork/progress. "Now playing controls only", but cheap once MRP
  exists.
- **AirPlay screen stream (mirroring, type 110)** — technically the full
  answer (arbitrary page → H.264 → ATV), but requires a server-side headless
  render + encode pipeline; heavy, exploratory.
- **HLS video session** — hand the ATV a video URL of a server-rendered
  stream; middle ground, also exploratory.

Decision deferred until MRP lands; v1 = the MRP now-playing screen.
