// Depth of buffered audio the server streams ahead, in seconds. Servers gate
// the send queue on both bytes (buffer_capacity) and duration; aiosendspin's
// duration horizon is 30s. Sizing the advertised byte capacity below that depth
// makes bytes the binding limit and starves the buffer on high-rate codecs.
const BUFFER_DEPTH_SECONDS = 30;
// FLAC falls back to verbatim frames on incompressible audio, where the frame
// headers put the stream slightly above raw PCM. Measured at 195.4 kB/s for
// 48kHz/16-bit stereo (192.0 kB/s raw) with full-scale decorrelated noise.
const FLAC_WORST_CASE_EXPANSION = 1.02;
// libopus tops out at 512 kbps for stereo. Servers pick their own bitrate
// (aiosendspin uses the libopus default, ~96 kbps), so assume the ceiling
// rather than tying the capacity to any one server's encoder settings.
const OPUS_MAX_BYTES_PER_SECOND = 64000;
/** Detect which audio codecs the current browser supports. */
export function getBrowserSupportedCodecs() {
    const userAgent = typeof navigator !== "undefined" ? navigator.userAgent : "";
    const isSafari = /^((?!chrome|android).)*safari/i.test(userAgent);
    const isFirefox = /firefox/i.test(userAgent);
    // Check if native Opus decoder is available (requires secure context)
    const hasNativeOpus = typeof AudioDecoder !== "undefined";
    if (!hasNativeOpus) {
        if (typeof window !== "undefined" && !window.isSecureContext) {
            console.warn("[Opus] Running in insecure context, falling back to FLAC/PCM");
        }
        else {
            console.warn("[Opus] Native decoder not available, falling back to FLAC/PCM");
        }
    }
    if (isSafari) {
        // Safari: No FLAC support
        return new Set(["pcm", "opus"]);
    }
    if (isFirefox) {
        // Firefox: Opus has audio glitches with both native and opus-encdec decoders
        return new Set(["pcm", "flac"]);
    }
    if (hasNativeOpus) {
        // Native Opus available (Chrome, Edge)
        return new Set(["pcm", "opus", "flac"]);
    }
    // No WebCodecs AudioDecoder (insecure context or unsupported browser)
    return new Set(["pcm", "flac"]);
}
/** Build supported format list from requested codecs, filtering by browser support. */
export function getSupportedFormats(codecs) {
    const browserSupported = getBrowserSupportedCodecs();
    const formats = [];
    for (const codec of codecs) {
        if (!browserSupported.has(codec)) {
            continue;
        }
        if (codec === "opus") {
            // Opus requires 48kHz
            formats.push({
                codec: "opus",
                sample_rate: 48000,
                channels: 2,
                bit_depth: 16,
            });
        }
        else {
            // PCM and FLAC support both sample rates
            formats.push({ codec, sample_rate: 48000, channels: 2, bit_depth: 16 });
            formats.push({ codec, sample_rate: 44100, channels: 2, bit_depth: 16 });
        }
    }
    if (formats.length === 0) {
        throw new Error(`No supported codecs: requested [${codecs.join(", ")}], ` +
            `browser supports [${[...browserSupported].join(", ")}]`);
    }
    return formats;
}
/** Worst-case wire byte rate for a single advertised format. */
function getWireByteRate(format) {
    const pcmByteRate = format.sample_rate * format.channels * Math.ceil(format.bit_depth / 8);
    switch (format.codec) {
        case "opus":
            return OPUS_MAX_BYTES_PER_SECOND;
        case "flac":
            return pcmByteRate * FLAC_WORST_CASE_EXPANSION;
        default:
            return pcmByteRate;
    }
}
/**
 * Buffer capacity to advertise for a set of supported formats, in bytes.
 *
 * The server picks one of the advertised formats, so the capacity is sized for
 * the highest byte rate among them: enough for the full stream-ahead depth even
 * on incompressible FLAC without making bytes the binding limit before the
 * server's duration horizon.
 *
 * @param formats - Formats advertised in `client/hello`, as returned by `getSupportedFormats`
 */
export function getDefaultBufferCapacity(formats) {
    const worstByteRate = Math.max(...formats.map(getWireByteRate));
    return Math.ceil(worstByteRate * BUFFER_DEPTH_SECONDS);
}
//# sourceMappingURL=codec-support.js.map
