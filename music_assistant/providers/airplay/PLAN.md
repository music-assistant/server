# AirPlay Provider Overhaul — Unified `cliairplay` Binary

**Status:** In progress. Native AP2 pipeline established end-to-end against a real
Apple TV (pair-verify → encrypted RTSP → SETUP → RECORD → encrypted RTP). Audible
playback still being validated. RAOP + AP2 RAOP-compat fully working and audible.

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

### AirPlay 2 — RAOP-compatible flow (Sonos & most third-party)
- Sonos advertises AP2 but its firmware rejects the native `streams` SETUP plist
  (400 Bad Request). It works via the RAOP-compatible path: `auth-setup` (MFi
  X25519 exchange, no stored credentials) → RAOP `ANNOUNCE`/`SETUP`/`RECORD`.
- Detected automatically when no HAP credentials are provided.
- 16-bit ALAC tested audible on Sonos.

### AirPlay 2 — native flow (Apple TV / HomePod)
- Full HAP pair-verify against a real Apple TV (tvOS 26.4, AppleTV11,1):
  X25519 ECDH + Ed25519 signatures + HKDF-SHA512 + ChaCha20-Poly1305.
- Encrypted RTSP channel (HAP framing: 2-byte LE length + ciphertext + 16-byte tag,
  nonce = 4 zero bytes + 8-byte LE counter, 1024-byte max frames).
- Binary-plist session SETUP (`deviceID` colon-hex + `sessionUUID` + `timingPort` +
  `timingProtocol`), events reverse-connection, stream SETUP (`streams` array), RECORD.
- Encrypted RTP audio: ALAC → ChaCha20-Poly1305 (key = X25519 shared secret,
  nonce = 4 zero bytes + 2-byte seqnum, AAD = 8 bytes of RTP header) + sync packets.
- Session establishes end-to-end; audio-audible validation still open.

### 24-bit ALAC
- Encoder fixed: the libcodecs `alac_wrapper.cpp` hardcoded `mFormatFlags=1` (16-bit);
  our `alac_ext.cpp` override sets it from the actual bit depth (16→1, 20→2, 24→3, 32→4).
- Only usable over the **native AP2** path (Sonos won't take 24-bit ALAC over RAOP;
  RAOP path stays 16-bit). Input from FFmpeg is s32le for 24-bit, truncated to s24le.

---

## 3. The critical fixes (hard-won findings)

1. **`X-Apple-HKP: 3` for pair-verify.** We first used `4` (transient pairing).
   For PIN-based (stored-credential) pairings — the only kind MA produces — pair-verify
   must use `X-Apple-HKP: 3`, matching what owntone sends for `PAIR_CLIENT_HOMEKIT_NORMAL`.
   Using `4` returned HTTP 200 but TLV error `0x02` (authentication) and silently
   dropped all encrypted messages afterward. This was the single biggest blocker.

2. **client_id = DACP ID as UPPERCASE ASCII string** (e.g. `b"B2763ADADB414A27"`),
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
  `cliairplay-macos-arm64` built so far; Linux aarch64/x86_64 + macos-x86_64 still to build).
- CLI args the provider passes: `--protocol`, `--latency`, `--volume`, `--dacp`,
  `--activeremote`, `--cmdpipe`, `--ntpstart`, `--samplerate`, `--bitdepth`, `--auth`
  (HAP creds → native flow), mDNS props (`--et/--md/--am/--udn`), `--password`, `--if`,
  device address + `-` (stdin).

---

## 6. Remaining work

**Done since this plan was first written:** the provider is reconciled onto latest
dev (branch `airplay-unified-binary`), and the C project is pushed to
[music-assistant/airplay-cli](https://github.com/music-assistant/airplay-cli)
(private) — its own `TODO.md` there tracks the binary-side roadmap.

### Binary distribution
- Only `cliairplay-macos-arm64` is built; still need linux-aarch64, linux-x86_64,
  macos-x86_64. The prebuilt macOS binary committed to `bin/` is accepted for **local
  testing only** — the end goal is to **build the binaries in the MA container build
  process** rather than committing them. Set up CI in the airplay-cli repo first.

### cliairplay features (tracked in airplay-cli `TODO.md`)
- **PTP timing** — Apple/Samsung native AP2 need it; `ap2_ptp.c` is an NTP responder +
  offset placeholder only. Open question: daemon mode inside the binary vs a
  centralized PTP client in the MA provider.
- **Buffered streaming mode** — not implemented.
- **24-bit audio** — encoder fix in place but **untested end-to-end**; native AP2 only;
  may depend on PTP and/or buffered streaming.

### Latency trim (done — value pending on-device tuning)
Dev's session-establishment-latency system compensated for the **old cliap2 (owntone)
AP2 binary's slow, variable session start** — the very problem the new binary solves.
Removed:
- The user-configurable `CONF_SESSION_ESTABLISHMENT_LATENCY` setting + its 150–4000 ms
  range + the `session_establishment_latency_ms` property (Apple's own UI has no such knob).
- The AP2 lead is now a single fixed internal `AIRPLAY2_CONNECT_TIME_MS` (1000 ms, down
  from ~1400 ms) — not user-configurable; the binary controls the whole chain.
- RAOP timing is **unchanged** (`RAOP_CONNECT_TIME_MS + output_buffer_duration_ms`).
- Also dropped the orphaned `encryption` / `alac_encode` / `AIRPLAY2_SYNC_WARN` config
  strings left over from the binary unification.

Still open: **measure the new binary's real AP2 establishment time on-device** and tune
`AIRPLAY2_CONNECT_TIME_MS`. The robust end-state is to start the playback clock off the
binary's `[STATUS] connected` event rather than a fixed pre-roll guess.

### Validation
- Confirm audible **native AP2** playback on Apple TV (session establishes; sound unconfirmed).
- **24-bit** end-to-end over native AP2.
- **Multi-room sync** across RAOP + AP2 mixed groups.

---

## 7. Test references

- Sonos Move 2 "Move speaker Woonkamer": `192.168.1.224:7000` (RAOP + AP2 RAOP-compat).
- Apple TV "Slaapkamer": `192.168.1.17:7000`, AppleTV11,1 / tvOS 26.4 (native AP2).
  Credentials live in MA settings (`airplay_credentials`, Fernet-encrypted with
  `base64.urlsafe_b64encode(server_id[:32])`); DACP id `B2763ADADB414A27`.
- Test tone: `ffmpeg -f lavfi -i "sine=frequency=440:duration=5" -ar 44100 -ac 2 -f s16le`.
- Sonos needs a metadata command (`ACTION=SENDMETA`) after connect before it emits audio.
