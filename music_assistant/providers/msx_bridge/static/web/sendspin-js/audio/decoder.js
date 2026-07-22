/**
 * Audio decoder pipeline for Sendspin protocol.
 *
 * Decodes compressed audio (PCM, Opus, FLAC) into raw Float32Array PCM samples.
 * This module has no Web Audio playback concerns — it only produces decoded data.
 */
export class SendspinDecoder {
    constructor(onDecodedChunk, currentGeneration) {
        // Native Opus decoder (WebCodecs API)
        this.webCodecsDecoder = null;
        this.webCodecsDecoderReady = null;
        this.webCodecsFormat = null;
        this.useNativeOpus = true;
        this.nativeDecoderQueue = [];
        // Fallback Opus decoder (opus-encdec library)
        this.opusDecoder = null;
        this.opusDecoderModule = null;
        this.opusDecoderReady = null;
        // FLAC decoding context (OfflineAudioContext, no playback needed)
        this.flacDecodingContext = null;
        this.flacDecodingContextSampleRate = 0;
        this.flacDecodingContextChannels = 0;
        this.onDecodedChunk = onDecodedChunk;
        this.currentGeneration = currentGeneration;
    }
    /**
     * Handle a binary audio message from the WebSocket.
     * Parses the message, decodes the audio, and emits a DecodedAudioChunk.
     */
    async handleBinaryMessage(data, format, generation) {
        // First byte contains role type and message slot
        const firstByte = new Uint8Array(data)[0];
        // Type 4 is audio chunk (Player role, slot 0)
        if (firstByte === 4) {
            // Next 8 bytes are server timestamp in microseconds (big-endian int64)
            const timestampView = new DataView(data, 1, 8);
            const serverTimeUs = Number(timestampView.getBigInt64(0, false));
            // Rest is audio data
            const audioData = data.slice(9);
            // For Opus: use native decoder (non-blocking async path)
            if (format.codec === "opus" && this.useNativeOpus) {
                await this.initWebCodecsDecoder(format);
                if (this.useNativeOpus && this.webCodecsDecoder) {
                    if (this.queueToNativeOpusDecoder(audioData, serverTimeUs, generation)) {
                        return; // Async path - callback handles output
                    }
                    // Fall through to fallback on error
                }
            }
            // Fallback decode path (PCM, FLAC, or Opus via opus-encdec)
            try {
                const decoded = await this.decode(audioData, format);
                if (decoded && generation === this.currentGeneration()) {
                    this.onDecodedChunk({
                        samples: decoded.samples,
                        sampleRate: decoded.sampleRate,
                        serverTimeUs,
                        generation,
                    });
                }
            }
            catch (error) {
                console.error("Sendspin: Failed to decode audio buffer:", error);
            }
        }
    }
    async decode(audioData, format) {
        if (format.codec === "opus") {
            return this.decodeOpusWithEncdec(audioData, format);
        }
        else if (format.codec === "flac") {
            return this.decodeFLAC(audioData, format);
        }
        else if (format.codec === "pcm") {
            return this.decodePCM(audioData, format);
        }
        return null;
    }
    // ========================================
    // PCM Decoder
    // ========================================
    decodePCM(audioData, format) {
        const bytesPerSample = (format.bit_depth || 16) / 8;
        const dataView = new DataView(audioData);
        const numSamples = audioData.byteLength / (bytesPerSample * format.channels);
        const samples = [];
        for (let ch = 0; ch < format.channels; ch++) {
            samples.push(new Float32Array(numSamples));
        }
        // Decode PCM data (interleaved format)
        for (let channel = 0; channel < format.channels; channel++) {
            const channelData = samples[channel];
            for (let i = 0; i < numSamples; i++) {
                const offset = (i * format.channels + channel) * bytesPerSample;
                let sample = 0;
                if (format.bit_depth === 16) {
                    sample = dataView.getInt16(offset, true) / 32768.0;
                }
                else if (format.bit_depth === 24) {
                    const byte1 = dataView.getUint8(offset);
                    const byte2 = dataView.getUint8(offset + 1);
                    const byte3 = dataView.getUint8(offset + 2);
                    let int24 = (byte3 << 16) | (byte2 << 8) | byte1;
                    if (int24 & 0x800000) {
                        int24 |= 0xff000000;
                    }
                    sample = int24 / 8388608.0;
                }
                else if (format.bit_depth === 32) {
                    sample = dataView.getInt32(offset, true) / 2147483648.0;
                }
                channelData[i] = sample;
            }
        }
        return { samples, sampleRate: format.sample_rate };
    }
    // ========================================
    // FLAC Decoder (uses OfflineAudioContext)
    // ========================================
    getFlacDecodingContext(sampleRate, channels) {
        if (!this.flacDecodingContext ||
            this.flacDecodingContextSampleRate !== sampleRate ||
            this.flacDecodingContextChannels !== channels) {
            this.flacDecodingContext = new OfflineAudioContext(channels, 1, sampleRate);
            this.flacDecodingContextSampleRate = sampleRate;
            this.flacDecodingContextChannels = channels;
        }
        return this.flacDecodingContext;
    }
    async decodeFLAC(audioData, format) {
        try {
            let dataToEncode = audioData;
            if (format.codec_header) {
                // Decode Base64 codec header and prepend to audio data
                const headerBytes = Uint8Array.from(atob(format.codec_header), (c) => c.charCodeAt(0));
                const combined = new Uint8Array(headerBytes.length + audioData.byteLength);
                combined.set(headerBytes, 0);
                combined.set(new Uint8Array(audioData), headerBytes.length);
                dataToEncode = combined.buffer;
            }
            const ctx = this.getFlacDecodingContext(format.sample_rate, format.channels);
            const audioBuffer = await ctx.decodeAudioData(dataToEncode);
            // Extract Float32Array per channel from AudioBuffer
            const samples = [];
            for (let ch = 0; ch < audioBuffer.numberOfChannels; ch++) {
                samples.push(new Float32Array(audioBuffer.getChannelData(ch)));
            }
            return { samples, sampleRate: audioBuffer.sampleRate };
        }
        catch (error) {
            console.error("Error decoding FLAC data:", error);
            return null;
        }
    }
    // ========================================
    // Opus - Native WebCodecs Decoder
    // ========================================
    async initWebCodecsDecoder(format) {
        const tryConfigureExistingDecoder = () => {
            if (!this.webCodecsDecoder)
                return false;
            const matchesFormat = !!this.webCodecsFormat &&
                this.webCodecsFormat.sample_rate === format.sample_rate &&
                this.webCodecsFormat.channels === format.channels;
            if (this.webCodecsDecoder.state === "configured" && matchesFormat) {
                return true;
            }
            if (this.webCodecsDecoder.state === "closed") {
                return false;
            }
            try {
                this.webCodecsDecoder.configure({
                    codec: "opus",
                    sampleRate: format.sample_rate,
                    numberOfChannels: format.channels,
                });
                this.webCodecsFormat = format;
                return true;
            }
            catch {
                return false;
            }
        };
        if (tryConfigureExistingDecoder()) {
            return;
        }
        if (this.webCodecsDecoderReady) {
            await this.webCodecsDecoderReady;
            if (tryConfigureExistingDecoder()) {
                return;
            }
            try {
                this.webCodecsDecoder?.close();
            }
            catch {
                // Ignore close errors; we'll recreate below.
            }
            this.webCodecsDecoder = null;
            this.webCodecsDecoderReady = null;
            this.webCodecsFormat = null;
        }
        if (this.webCodecsDecoderReady) {
            await this.webCodecsDecoderReady;
            return;
        }
        this.webCodecsDecoderReady = this.createWebCodecsDecoder(format);
        await this.webCodecsDecoderReady;
    }
    async createWebCodecsDecoder(format) {
        if (typeof AudioDecoder === "undefined") {
            this.useNativeOpus = false;
            return;
        }
        try {
            const support = await AudioDecoder.isConfigSupported({
                codec: "opus",
                sampleRate: format.sample_rate,
                numberOfChannels: format.channels,
            });
            if (!support.supported) {
                console.log("[NativeOpus] WebCodecs Opus not supported, will use fallback");
                this.useNativeOpus = false;
                return;
            }
            this.webCodecsDecoder = new AudioDecoder({
                output: (audioData) => this.handleAudioData(audioData),
                error: (error) => {
                    console.error("[NativeOpus] WebCodecs decoder error:", error);
                },
            });
            this.webCodecsDecoder.configure({
                codec: "opus",
                sampleRate: format.sample_rate,
                numberOfChannels: format.channels,
            });
            this.webCodecsFormat = format;
            console.log(`[NativeOpus] Using WebCodecs AudioDecoder: ${format.sample_rate}Hz, ${format.channels}ch`);
        }
        catch (error) {
            console.warn("[NativeOpus] WebCodecs init failed, will use fallback:", error);
            this.useNativeOpus = false;
        }
    }
    // Handle decoded audio data from native Opus decoder
    handleAudioData(audioData) {
        try {
            const outputTimestampUs = Number(audioData.timestamp);
            const metadata = this.nativeDecoderQueue.shift();
            if (!metadata) {
                console.warn(`[NativeOpus] Dropping frame with empty decode queue (out ts=${outputTimestampUs})`);
                audioData.close();
                return;
            }
            const { serverTimeUs, generation } = metadata;
            if (generation !== this.currentGeneration()) {
                console.warn(`[NativeOpus] Dropping old-stream frame (ts=${serverTimeUs}, gen=${generation} != current=${this.currentGeneration()})`);
                audioData.close();
                return;
            }
            const channels = audioData.numberOfChannels;
            const frames = audioData.numberOfFrames;
            const fmt = audioData.format;
            let interleaved;
            if (fmt === "f32-planar") {
                interleaved = new Float32Array(frames * channels);
                for (let ch = 0; ch < channels; ch++) {
                    const channelData = new Float32Array(frames);
                    audioData.copyTo(channelData, { planeIndex: ch });
                    for (let i = 0; i < frames; i++) {
                        interleaved[i * channels + ch] = channelData[i];
                    }
                }
            }
            else if (fmt === "f32") {
                interleaved = new Float32Array(frames * channels);
                audioData.copyTo(interleaved, { planeIndex: 0 });
            }
            else if (fmt === "s16-planar") {
                interleaved = new Float32Array(frames * channels);
                for (let ch = 0; ch < channels; ch++) {
                    const channelData = new Int16Array(frames);
                    audioData.copyTo(channelData, { planeIndex: ch });
                    for (let i = 0; i < frames; i++) {
                        interleaved[i * channels + ch] = channelData[i] / 32768.0;
                    }
                }
            }
            else if (fmt === "s16") {
                const int16Data = new Int16Array(frames * channels);
                audioData.copyTo(int16Data, { planeIndex: 0 });
                interleaved = new Float32Array(frames * channels);
                for (let i = 0; i < frames * channels; i++) {
                    interleaved[i] = int16Data[i] / 32768.0;
                }
            }
            else {
                console.warn(`[NativeOpus] Unsupported AudioData format: ${fmt}`);
                audioData.close();
                return;
            }
            this.emitDeinterleavedChunk(interleaved, serverTimeUs, channels, generation);
            audioData.close();
        }
        catch (e) {
            console.error("[NativeOpus] Error in output callback:", e);
            audioData.close();
        }
    }
    emitDeinterleavedChunk(interleaved, serverTimeUs, channels, generation) {
        if (!this.webCodecsFormat)
            return;
        const numFrames = interleaved.length / channels;
        const samples = [];
        for (let ch = 0; ch < channels; ch++) {
            const channelData = new Float32Array(numFrames);
            for (let i = 0; i < numFrames; i++) {
                channelData[i] = interleaved[i * channels + ch];
            }
            samples.push(channelData);
        }
        this.onDecodedChunk({
            samples,
            sampleRate: this.webCodecsFormat.sample_rate,
            serverTimeUs,
            generation,
        });
    }
    queueToNativeOpusDecoder(audioData, serverTimeUs, generation) {
        if (!this.webCodecsDecoder ||
            this.webCodecsDecoder.state !== "configured") {
            return false;
        }
        try {
            this.nativeDecoderQueue.push({
                serverTimeUs,
                generation,
            });
            const chunk = new EncodedAudioChunk({
                type: "key",
                timestamp: serverTimeUs,
                data: audioData,
            });
            this.webCodecsDecoder.decode(chunk);
            return true;
        }
        catch (error) {
            if (this.nativeDecoderQueue.length > 0) {
                this.nativeDecoderQueue.pop();
            }
            console.error("[NativeOpus] WebCodecs queue error:", error);
            return false;
        }
    }
    // ========================================
    // Opus - Fallback (opus-encdec library)
    // ========================================
    resolveOpusDecoderModule(moduleExport) {
        const maybeDefault = moduleExport?.default;
        const maybeCommonJs = moduleExport?.["module.exports"];
        const resolved = maybeDefault ?? maybeCommonJs ?? moduleExport;
        if (!resolved || typeof resolved !== "object") {
            throw new Error("[Opus] Invalid libopus decoder module export");
        }
        return resolved;
    }
    resolveOggOpusDecoderClass(wrapperExport) {
        const maybeDefault = wrapperExport?.default;
        const maybeCommonJs = wrapperExport?.["module.exports"];
        const wrapper = maybeDefault ?? maybeCommonJs ?? wrapperExport;
        const resolved = wrapper?.OggOpusDecoder ?? wrapper;
        if (typeof resolved !== "function") {
            throw new Error("[Opus] OggOpusDecoder class export not found");
        }
        return resolved;
    }
    async waitForOpusReady(target) {
        if (target.isReady)
            return;
        if (Object.isExtensible(target)) {
            await new Promise((resolve) => {
                target.onready = () => resolve();
            });
            return;
        }
        while (!target.isReady) {
            await new Promise((resolve) => setTimeout(resolve, 20));
        }
    }
    async initOpusEncdecDecoder(format) {
        if (this.opusDecoderReady) {
            await this.opusDecoderReady;
            return;
        }
        this.opusDecoderReady = (async () => {
            console.log("[Opus] Initializing decoder (opus-encdec)...");
            const [DecoderModuleExport, DecoderWrapperExport] = await Promise.all([
                import("opus-encdec/dist/libopus-decoder.js"),
                import("opus-encdec/src/oggOpusDecoder.js"),
            ]);
            this.opusDecoderModule =
                this.resolveOpusDecoderModule(DecoderModuleExport);
            const OggOpusDecoderClass = this.resolveOggOpusDecoderClass(DecoderWrapperExport);
            await this.waitForOpusReady(this.opusDecoderModule);
            this.opusDecoder = new OggOpusDecoderClass({
                rawOpus: true,
                decoderSampleRate: format.sample_rate,
                outputBufferSampleRate: format.sample_rate,
                numberOfChannels: format.channels,
            }, this.opusDecoderModule);
            await this.waitForOpusReady(this.opusDecoder);
            console.log("[Opus] Decoder ready");
        })();
        await this.opusDecoderReady;
    }
    async decodeOpusWithEncdec(audioData, format) {
        try {
            await this.initOpusEncdecDecoder(format);
            const uint8Array = new Uint8Array(audioData);
            const decodedSamples = [];
            this.opusDecoder.decodeRaw(uint8Array, (samples) => {
                decodedSamples.push(new Float32Array(samples));
            });
            if (decodedSamples.length === 0) {
                console.warn("[Opus] Fallback decoder produced no samples");
                return null;
            }
            // Convert interleaved samples to per-channel arrays
            const interleavedSamples = decodedSamples[0];
            const numFrames = interleavedSamples.length / format.channels;
            const samples = [];
            for (let ch = 0; ch < format.channels; ch++) {
                const channelData = new Float32Array(numFrames);
                for (let i = 0; i < numFrames; i++) {
                    channelData[i] = interleavedSamples[i * format.channels + ch];
                }
                samples.push(channelData);
            }
            return { samples, sampleRate: format.sample_rate };
        }
        catch (error) {
            console.error("[Opus] Decode error:", error);
            return null;
        }
    }
    // ========================================
    // Lifecycle
    // ========================================
    /** Clear decoder state (on stream change/clear). Drops in-flight async decodes. */
    clearState() {
        this.nativeDecoderQueue = [];
        try {
            this.webCodecsDecoder?.close();
        }
        catch {
            // Ignore close errors
        }
        this.webCodecsDecoder = null;
        this.webCodecsDecoderReady = null;
        this.webCodecsFormat = null;
    }
    /** Full cleanup (on disconnect). Releases all decoder resources. */
    close() {
        this.clearState();
        if (this.opusDecoder) {
            this.opusDecoder = null;
            this.opusDecoderModule = null;
            this.opusDecoderReady = null;
        }
        // Reset native Opus flag for next session
        this.useNativeOpus = true;
        this.flacDecodingContext = null;
        this.flacDecodingContextSampleRate = 0;
        this.flacDecodingContextChannels = 0;
    }
}
//# sourceMappingURL=decoder.js.map
