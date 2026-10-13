import { base64urlEncode, base64urlDecode } from "./noise/base64url.js";
import { CPace, CPaceError, SHARE_SIZE, TAG_SIZE } from "./pake/cpace.js";
import { DEFAULT_MIN_PIN_DIGITS, MAX_PIN_DIGITS, MIN_PIN_DIGITS, NONCE_SIZE, commitNonce, derivePin, generateNonce, isValidStaticPin, } from "./pake/pin.js";
import { sha256 } from "../vendor/noble-hashes/sha2.js";
const utf8 = (s) => new TextEncoder().encode(s);
function concatBytes(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let off = 0;
    for (const p of parts) {
        out.set(p, off);
        off += p.length;
    }
    return out;
}
/** CPace session-id label. sid = label || Noise handshake hash || counter. */
const PAKE_SID_LABEL = "sendspin-pair-pake-v1";
/** Label for the key that wraps the PSK in PIN pairing (PSK Wrapping). */
const PSK_WRAP_LABEL = utf8("sendspin-pair-psk-wrap-v1");
/** CPace associated data: distinct per side to prevent a reflected MAC. */
const CPACE_AD_A = utf8("server");
const CPACE_AD_B = utf8("client");
/** A PIN pairing attempt must complete within this bound (spec: 2 minutes). */
const ATTEMPT_TIMEOUT_MS = 120000;
/** Lifetime of an open pairing window, from the gesture until client/pair-init. */
const WINDOW_LIFETIME_MS = 300000;
/** Dynamic PIN escalates to gesture-gating at this many consecutive failures. */
const ESCALATION_THRESHOLD = 10;
/** Dynamic PIN below this length is gesture-gated: short PINs are bought with a gesture. */
const SHORT_PIN_LENGTH = 6;
/** Persisted PIN failure counter (dynamic PIN only, not partitioned by server). */
const FAILURES_STORAGE_KEY = "sendspin-pair-failures";
const PIN_METHODS = ["dynamic_pin", "static_pin"];
/** Only advertise a locations hint the integrator actually configured. */
function withLocations(descriptor, locations) {
    return locations?.length ? { ...descriptor, locations } : descriptor;
}
/** A languages hint that is not a non-empty list of tags is treated as absent. */
function readLanguages(value) {
    if (!Array.isArray(value) || value.length === 0)
        return undefined;
    return value.every((tag) => typeof tag === "string" && tag !== "")
        ? value
        : undefined;
}
export class PairingManager {
    constructor(deps) {
        this.deps = deps;
        this.pendingPsk = null;
        this.phase = "idle";
        this.method = null;
        this.cpace = null;
        this.nonceB = null;
        this.attemptTimer = null;
        this.windowTimer = null;
        /** Whether the operator's pairing-window gesture is currently live. */
        this.windowOpen = false;
        /** The CPace sid for the current PIN attempt (for PSK wrapping). */
        this.currentSid = null;
        /** Pairing server/activate messages received since the last Noise handshake. */
        this.pairingActivateCount = 0;
        /** The counter for the current attempt (pairing_index and CPace sid counter). */
        this.attemptIndex = 0;
        /** The dynamic PIN length for the current attempt, from the activation. */
        this.pinLength = null;
        if (deps.staticPin !== undefined && !isValidStaticPin(deps.staticPin)) {
            throw new Error("staticPin must be exactly 8 decimal digits");
        }
        this.minPinLength = Math.min(MAX_PIN_DIGITS, Math.max(MIN_PIN_DIGITS, deps.minPinLength ?? DEFAULT_MIN_PIN_DIGITS));
        if (!deps.storage && deps.onPin) {
            // Spec requires the failure counter to survive reboots. Without storage
            // it is in-memory only and escalation resets on restart.
            console.warn("sendspin: dynamic PIN pairing is enabled without storage, so the failure counter will not persist across reboots.");
        }
        this.failures = this.loadFailures();
    }
    /** The pairing-method descriptors to advertise in client/hello. */
    descriptors() {
        const out = [
            withLocations({ method: "pairing_psk" }, this.deps.pairingPskLocations),
        ];
        if (this.deps.staticPin !== undefined) {
            out.push(withLocations({ method: "static_pin" }, this.deps.staticPinLocations));
        }
        if (this.deps.onPin) {
            out.push({
                method: "dynamic_pin",
                out_channels: this.deps.pinOutChannels ?? ["display"],
                min_pin_length: this.minPinLength,
            });
        }
        return out;
    }
    /**
     * Whether dynamic PIN has escalated to gesture-gating (spec: 10 failures).
     * Escalation is not an error state: the method stays offered, and every
     * attempt needs openPairingWindow() until a successful round de-escalates it.
     */
    isDynamicPinEscalated() {
        return this.failures >= ESCALATION_THRESHOLD;
    }
    /**
     * Operator gesture that opens the pairing window. If an attempt is already
     * waiting on it the attempt starts immediately, otherwise the window admits
     * one attempt within its lifetime (~5 minutes).
     */
    openPairingWindow() {
        if (this.phase === "await-window") {
            this.startAttempt();
            return;
        }
        this.windowOpen = true;
        if (this.windowTimer)
            clearTimeout(this.windowTimer);
        this.windowTimer = setTimeout(() => this.closeWindow(), WINDOW_LIFETIME_MS);
    }
    /**
     * Close the pairing window. The window is device state, not connection
     * state: it survives handshakes and drops until an attempt consumes it or
     * its lifetime runs out.
     */
    closeWindow() {
        this.windowOpen = false;
        if (this.windowTimer)
            clearTimeout(this.windowTimer);
        this.windowTimer = null;
    }
    /** Cancel an in-progress pairing attempt (sends pair/abort user_cancelled). */
    cancelPairing() {
        if (this.phase === "idle")
            return;
        this.abort("user_cancelled");
    }
    /** Called for every server/activate. Returns true if it consumed a pairing activation. */
    onActivate(activities, pairing) {
        const isPairing = activities.includes("pairing");
        if (!isPairing) {
            // Non-pairing activate in place of pair-finalize = leave-pairing.
            this.abandonAttempt("server_cancelled");
            return false;
        }
        // A pairing activate arriving mid-attempt supersedes it: the server has
        // moved on, and any message still carrying the old index is discarded.
        this.abandonAttempt("superseded");
        // Each pairing activate is one attempt, indexed for pairing_index and sid.
        this.pairingActivateCount += 1;
        this.attemptIndex = this.pairingActivateCount;
        const method = pairing?.method;
        const supported = this.descriptors().map((d) => d.method);
        // pairing_psk exactly when the matched PSK is the Pairing PSK, a PIN method otherwise.
        const fitsPsk = (method === "pairing_psk") ===
            (this.deps.matchedCategory() === "pairing");
        if (!method) {
            // A required field no conformant server omits, so it is a protocol error
            // rather than a rejection: close without any application-level message.
            // The log is local, so it still tells the integrator what went wrong.
            console.warn("sendspin: server/activate carried no pairing method, so the server is not speaking the current specification.");
            this.fail();
            return true;
        }
        // A method the PSK disallows or the client no longer offers is something a
        // conformant server can produce, since its view of the config may be stale.
        if (!fitsPsk || !supported.includes(method)) {
            this.abort("method_not_supported");
            return true;
        }
        this.method = method;
        if (method === "pairing_psk") {
            this.sendFinalize();
            // Arm the attempt timer and leave a non-idle phase so the attempt can be
            // cancelled and times out if the server never sends server/pair-finalize.
            this.phase = "await-finalize";
            this.armAttemptTimer();
            this.deps.onEvent?.("started");
            return true;
        }
        if (method === "dynamic_pin") {
            const length = pairing.pin_length;
            // Same protocol-error treatment as a missing method: pin_length_unacceptable
            // is defined over a value that is present but out of range.
            if (typeof length !== "number" || !Number.isInteger(length)) {
                this.fail();
                return true;
            }
            if (length < this.minPinLength || length > MAX_PIN_DIGITS) {
                this.abort("pin_length_unacceptable");
                return true;
            }
            this.pinLength = length;
            this.languages = readLanguages(pairing.languages);
        }
        if (this.isGestureGated() && !this.windowOpen) {
            this.phase = "await-window";
            // pair-pending does not start the attempt, so no attempt timer runs. The
            // server bounds the wait and cancels with a non-pairing server/activate.
            this.deps.sendControl({
                type: "client/pair-pending",
                payload: { pairing_index: this.attemptIndex },
            });
            this.deps.onEvent?.("pending");
            return true;
        }
        this.startAttempt();
        return true;
    }
    /**
     * Whether the selected method withholds client/pair-init until a window is
     * open: static PIN always, dynamic PIN when escalated or the PIN is short.
     */
    isGestureGated() {
        if (this.method === "static_pin")
            return true;
        if (this.method !== "dynamic_pin")
            return false;
        return this.isDynamicPinEscalated() || this.pinLength < SHORT_PIN_LENGTH;
    }
    /** server/pair-init: the server's nonce contribution (dynamic PIN). */
    onPairInit(payload) {
        // Leftover from an ended attempt (kept-open connection): discard silently.
        if (this.phase === "idle")
            return;
        if (this.phase !== "await-init" || this.method !== "dynamic_pin") {
            return this.fail();
        }
        const nonceA = this.decode(payload.nonce_A, NONCE_SIZE);
        if (!nonceA)
            return this.fail();
        const h = this.deps.handshakeHash();
        const pin = derivePin(h, nonceA, this.nonceB, this.pinLength);
        this.currentSid = this.sid(h, this.attemptIndex);
        this.cpace = CPace.start({
            role: "responder",
            prs: new TextEncoder().encode(pin),
            sid: this.currentSid,
            ada: CPACE_AD_A,
            adb: CPACE_AD_B,
        });
        this.phase = "await-auth";
        this.deps.onPin?.(pin, this.languages);
    }
    /** server/pair-auth: the server's CPace public share (both PIN methods). */
    onPairAuth(payload) {
        if (this.phase === "idle")
            return; // leftover from an ended attempt
        if (this.phase !== "await-auth" || !this.cpace)
            return this.fail();
        const peerShare = this.decode(payload.pake_msg_1, SHARE_SIZE);
        if (!peerShare)
            return this.fail();
        this.deps.sendControl({
            type: "client/pair-auth",
            payload: { pake_msg_2: base64urlEncode(this.cpace.publicShare) },
        });
        try {
            this.cpace.derive(peerShare);
        }
        catch (e) {
            if (e instanceof CPaceError)
                return this.fail();
            throw e;
        }
        this.phase = "await-confirm";
    }
    /** server/pair-confirm: verify the server's tag, then confirm and finalize. */
    onPairConfirm(payload) {
        if (this.phase === "idle")
            return; // leftover from an ended attempt
        if (this.phase !== "await-confirm" || !this.cpace)
            return this.fail();
        const serverKc = this.decode(payload.server_kc, TAG_SIZE);
        if (!serverKc)
            return this.fail();
        if (!this.cpace.verify(serverKc)) {
            if (this.method === "dynamic_pin")
                this.recordFailure();
            return this.abort("pin_mismatch");
        }
        // Reset on a verified server_kc, whether or not the attempt finalizes.
        if (this.method === "dynamic_pin")
            this.resetFailures();
        const confirm = {
            client_kc: base64urlEncode(this.cpace.tag()),
        };
        if (this.method === "dynamic_pin") {
            confirm.nonce_B = base64urlEncode(this.nonceB);
        }
        this.deps.sendControl({ type: "client/pair-confirm", payload: confirm });
        // client/pair-finalize follows immediately, without waiting (spec).
        this.phase = "await-finalize";
        this.sendFinalize();
    }
    onPairFinalize() {
        if (!this.pendingPsk)
            return;
        this.deps.pskStore.addLongTerm(this.pendingPsk, this.deps.serverId());
        this.clearAttempt();
        this.deps.onEvent?.("finalized");
    }
    /**
     * Inbound pair/abort from the server: discard the attempt. The sender closes
     * the connection when needed, so the receiver keeps it open. A pair/abort for
     * an already-ended attempt has no effect.
     */
    onAbort(reason) {
        if (this.phase === "idle" && !this.pendingPsk)
            return;
        this.clearAttempt();
        this.deps.onEvent?.("aborted", reason);
    }
    /** Discard any in-flight pairing state and the activate counter (on handshake/close). */
    reset() {
        this.clearAttempt();
        this.pairingActivateCount = 0;
        this.attemptIndex = 0;
    }
    /** Send client/pair-init. The window's lifetime ends here (spec: it runs
     * from the gesture until client/pair-init is sent). */
    startAttempt() {
        this.closeWindow();
        this.armAttemptTimer();
        this.deps.onEvent?.("started");
        if (this.method === "dynamic_pin") {
            this.nonceB = generateNonce();
            this.phase = "await-init";
            this.deps.sendControl({
                type: "client/pair-init",
                payload: {
                    pairing_index: this.attemptIndex,
                    commit_B: base64urlEncode(commitNonce(this.nonceB)),
                },
            });
            return;
        }
        const h = this.deps.handshakeHash();
        this.currentSid = this.sid(h, this.attemptIndex);
        this.cpace = CPace.start({
            role: "responder",
            prs: new TextEncoder().encode(this.deps.staticPin),
            sid: this.currentSid,
            ada: CPACE_AD_A,
            adb: CPACE_AD_B,
        });
        this.phase = "await-auth";
        this.deps.sendControl({
            type: "client/pair-init",
            payload: { pairing_index: this.attemptIndex },
        });
    }
    /** Mint the long-term PSK and send client/pair-finalize. */
    sendFinalize() {
        // A Sendspin PSK must be a 32-byte CSPRNG value, not a clamped X25519 private key.
        const psk = crypto.getRandomValues(new Uint8Array(32));
        this.pendingPsk = psk;
        if (this.cpace && this.currentSid) {
            // PIN flow: seal the PSK under a key derived from the CPace output.
            const kWrap = sha256(concatBytes(PSK_WRAP_LABEL, this.currentSid, this.cpace.isk));
            const wrapped = this.deps.aeadSeal(kWrap, psk);
            this.deps.sendControl({
                type: "client/pair-finalize",
                payload: { wrapped_psk: base64urlEncode(wrapped) },
            });
            return;
        }
        // Pairing PSK flow: the PSK travels directly.
        this.deps.sendControl({
            type: "client/pair-finalize",
            payload: { long_term_psk: base64urlEncode(psk) },
        });
    }
    sid(handshakeHash, index) {
        const label = utf8(PAKE_SID_LABEL);
        const sid = new Uint8Array(label.length + handshakeHash.length + 4);
        sid.set(label, 0);
        sid.set(handshakeHash, label.length);
        // counter: big-endian uint32 of the attempt index.
        new DataView(sid.buffer).setUint32(label.length + handshakeHash.length, index, false);
        return sid;
    }
    armAttemptTimer() {
        this.attemptTimer = setTimeout(() => this.abort("attempt_timeout"), ATTEMPT_TIMEOUT_MS);
    }
    /**
     * Send pair/abort with reason and discard state. The connection stays open
     * for a retry. Only concurrent_attempt closes it.
     */
    abort(reason) {
        this.clearAttempt();
        this.deps.sendControl({ type: "pair/abort", payload: { reason } });
        this.deps.onEvent?.("aborted", reason);
        if (reason === "concurrent_attempt")
            this.deps.close();
    }
    /** Protocol violation or malformed field: fail closed without an abort reason. */
    fail() {
        this.clearAttempt();
        this.deps.close();
    }
    clearAttempt() {
        if (this.attemptTimer)
            clearTimeout(this.attemptTimer);
        this.attemptTimer = null;
        if (this.method && PIN_METHODS.includes(this.method)) {
            this.deps.onPin?.(null);
        }
        this.pendingPsk = null;
        this.phase = "idle";
        this.method = null;
        this.cpace = null;
        this.currentSid = null;
        this.nonceB = null;
        this.pinLength = null;
        this.languages = undefined;
    }
    /**
     * End an attempt the server walked away from, without sending pair/abort.
     * The event lets the app drop any "waiting for gesture" UI, which a silent
     * cancel would otherwise leave up (the server's pair/abort is only a SHOULD).
     */
    abandonAttempt(detail) {
        if (this.phase === "idle" && !this.pendingPsk)
            return;
        this.clearAttempt();
        this.deps.onEvent?.("aborted", detail);
    }
    decode(value, size) {
        if (typeof value !== "string")
            return null;
        try {
            const raw = base64urlDecode(value);
            return raw.length === size ? raw : null;
        }
        catch {
            return null;
        }
    }
    loadFailures() {
        try {
            const raw = this.deps.storage?.getItem(FAILURES_STORAGE_KEY);
            if (!raw)
                return 0;
            const stored = JSON.parse(raw);
            return stored.dynamic_pin ?? 0;
        }
        catch {
            return 0;
        }
    }
    saveFailures() {
        this.deps.storage?.setItem(FAILURES_STORAGE_KEY, JSON.stringify({ dynamic_pin: this.failures }));
    }
    recordFailure() {
        this.failures += 1;
        this.saveFailures();
    }
    resetFailures() {
        this.failures = 0;
        this.saveFailures();
    }
}
//# sourceMappingURL=pairing.js.map
