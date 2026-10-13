/* Web Kiosk player. Music is chosen in Music Assistant. This page shows it. */
(function () {
    'use strict';

    var params = new URLSearchParams(location.search);
    // /web is the player. kiosk=1 remains so older links still open this screen.
    var SENDSPIN = params.get('sendspin') === '1';
    if (SENDSPIN) document.documentElement.classList.add('sendspin');
    var FLAG = function (name) { return params.get(name) !== '0'; };
    var SHOW = { controls: FLAG('controls'), party: FLAG('party'), viz: FLAG('viz'), lyrics: FLAG('lyrics') };

    var LS = {
        get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
        set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* noop */ } }
    };
    var deviceId = params.get('device_id') || LS.get('wk_device_id');
    if (!deviceId) { deviceId = 'wk-' + crypto.randomUUID(); LS.set('wk_device_id', deviceId); }
    var sendspinClientId = '';
    var sendspinModule = null;
    // True only while the Sendspin server is actually streaming audio here.
    var sendspinStreaming = false;
    var MA_URL = (params.get('ma_url') || LS.get('wk_ma_url') || '').replace(/\/$/, '');
    var TOKEN = params.get('token') || LS.get('wk_token') || '';
    if (MA_URL) LS.set('wk_ma_url', MA_URL);
    if (TOKEN) LS.set('wk_token', TOKEN);

    var playerId = '';
    var ws = null;
    var msgSeq = 0;
    var playing = false;
    var current = { title: '—', artist: '—', image: '', duration: 0, start: 0 };
    // Seconds into the served file, applied once the audio element has metadata.
    var pendingSeek = null;
    // Song position shown while a seek waits for the rebuilt stream.
    var displayHold = null;
    var seekPointer = false;
    var volumeTimer = null;
    var queue = [];
    var queueIndex = -1;
    var lyricsLines = [];
    var lyricsIdx = -1;
    var endReported = false;

    var audio = new Audio();
    audio.volume = 1;
    // Browsers withhold sound until the document receives a tap or a key.
    // That one gesture covers later tracks. A reload asks again.
    var AUDIO_PROMPT = 'Tap or press a key to start audio';
    var audioPromptOn = false;
    var htmlUnlocked = false;
    var sendspinUnlocked = !SENDSPIN;
    var audioArmed = false;
    var silentUrl = '';

    async function rpc(command, args) {
        // Same origin as this page. The kiosk server forwards the call to MA,
        // because the browser will not call MA's port from this one.
        if (!TOKEN) throw new Error('Configure token');
        var res = await fetch('/api', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN },
            body: JSON.stringify({ command: command, args: args || {}, message_id: String(++msgSeq) })
        });
        if (!res.ok) throw new Error(await res.text());
        return res.json();
    }

    function setStatus(t) {
        if (!t && audioPromptOn) t = AUDIO_PROMPT;
        var el = document.getElementById('kiosk-status');
        el.textContent = t || '';
        el.classList.toggle('hidden', !t);
        el.classList.toggle('audio-prompt', el.textContent === AUDIO_PROMPT);
    }
    function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
    function fmt(sec) {
        sec = Math.max(0, Math.floor(sec || 0));
        var m = Math.floor(sec / 60), s = sec % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
    }
    function songTime() {
        if (displayHold != null) return displayHold;
        return (current.start || 0) + (audio.currentTime || 0);
    }
    function imageUrl(item) {
        var img = '';
        if (item) {
            if (typeof item.image === 'string') img = item.image;
            else if (typeof item.image_path === 'string') img = item.image_path;
            else if (typeof item.thumb === 'string') img = item.thumb;
            // Covers arrive as metadata.images[].proxy_id. The kiosk serves
            // that id on the same origin as this page.
            if (!img && item.metadata && Array.isArray(item.metadata.images) && item.metadata.images[0]) {
                var first = item.metadata.images[0];
                if (typeof first.proxy_id === 'string' && first.proxy_id) return '/imageproxy/' + first.proxy_id;
                if (typeof first.path === 'string') img = first.path;
            }
        }
        if (!img) return '';
        var proxyAt = img.indexOf('/imageproxy/');
        if (proxyAt >= 0) return img.slice(proxyAt);
        if (img.indexOf('http') === 0 || img.indexOf('/') === 0) return img;
        return '/imageproxy/' + img;
    }
    function artistStr(item) {
        if (!item) return '';
        if (item.artist_str) return item.artist_str;
        if (Array.isArray(item.artists) && item.artists.length) {
            return item.artists.map(function (a) { return a && a.name ? a.name : ''; }).filter(Boolean).join(', ');
        }
        if (item.artist && typeof item.artist === 'string') return item.artist;
        return '';
    }
    function streamUrl(path) {
        // The stream path stays the same after a seek. A cache buster makes
        // the browser fetch the new remainder instead of the previous file.
        return path + (path.indexOf('?') >= 0 ? '&' : '?') + 'r=' + Date.now();
    }

    function connectWS() {
        var proto = location.protocol === 'https:' ? 'wss' : 'ws';
        var qs = 'device_id=' + encodeURIComponent(deviceId);
        if (sendspinClientId) qs += '&sendspin_client_id=' + encodeURIComponent(sendspinClientId);
        ws = new WebSocket(proto + '://' + location.host + '/ws?' + qs);
        ws.onmessage = function (ev) { handleWS(JSON.parse(ev.data)); };
        ws.onclose = function () { ws = null; setTimeout(connectWS, 2000); };
        ws.onerror = function () { try { ws.close(); } catch (e) { /* noop */ } };
    }
    function sendWS(obj) { if (ws && ws.readyState === WebSocket.OPEN) { try { ws.send(JSON.stringify(obj)); } catch (e) { /* noop */ } } }
    function reportTrackEnded() {
        if (endReported || isPrimeSource() || sendspinStreaming) return;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        sendWS({ type: 'ended' });
        endReported = true;
    }

    function applyPendingSeek() {
        if (pendingSeek == null || audio.readyState < 1) return;
        var target = pendingSeek;
        var limit = Number.isFinite(audio.duration) ? audio.duration : target;
        try {
            audio.currentTime = Math.min(Math.max(0, target), Math.max(0, limit));
        } catch (e) {
            return;
        }
        pendingSeek = null;
        paintClock();
    }

    function handleWS(msg) {
        switch (msg.type) {
            case 'welcome':
                playerId = msg.player_id;
                setStatus('');
                if (TOKEN) { fetchQueue(); fetchParty(); }
                if (SENDSPIN) initSendspin();
                if (audio.ended) reportTrackEnded();
                break;
            case 'play':
                endReported = false;
                // A new track retires the previous curve. The matching wave
                // message may already be waiting, or it arrives next.
                if (typeof msg.wave_seq === 'number') takeServerWave(msg.wave_seq);
                // Sendspin mode still receives the Web Kiosk stream. Music Assistant
                // uses that output unless a Sendspin stream is already playing here.
                if (sendspinStreaming) break;
                if (msg.path) {
                    pendingSeek = null;
                    current = {
                        title: msg.title || '',
                        artist: msg.artist || '',
                        image: msg.image_url || '',
                        duration: msg.duration || 0,
                        start: typeof msg.start === 'number' ? msg.start : 0
                    };
                    displayHold = null;
                    renderNow();
                    audio.src = streamUrl(msg.path);
                    startPlayback();
                    playing = true; sync();
                    fetchQueue();
                }
                break;
            case 'stop':
                if (msg.showNotification && !confirm('Stop playback?')) break;
                audio.pause(); audio.removeAttribute('src');
                pendingSeek = null;
                displayHold = null;
                current = { title: '—', artist: '—', image: '', duration: 0, start: 0 };
                lyricsLines = [];
                lyricsIdx = -1;
                pendingLyrics = null;
                renderNow();
                playing = false; sync();
                break;
            case 'pause':
                audio.pause(); playing = false; sync();
                break;
            case 'resume':
                if (!sendspinStreaming) startPlayback();
                playing = true; sync();
                break;
            case 'seek':
                // Seconds into the file being served, not seconds into the song.
                if (typeof msg.position === 'number') {
                    pendingSeek = msg.position;
                    applyPendingSeek();
                }
                break;
            case 'volume':
                audio.volume = (msg.level || 0) / 100;
                document.getElementById('kiosk-volume').value = msg.level || 0;
                break;
            case 'wave':
                if (typeof msg.seq === 'number' && msg.seq !== waveGen) {
                    pendingWave = { gen: msg.seq, bins: msg.bins };
                    break;
                }
                applyWaveBins(msg.bins);
                break;
            case 'lyrics':
                if (typeof msg.seq === 'number' && msg.seq !== waveGen) {
                    pendingLyrics = { gen: msg.seq, lines: msg.lines };
                    break;
                }
                applyLyrics(msg.lines);
                break;
            case 'sendspin':
                if (msg.url) location.href = msg.url;
                break;
        }
    }

    setInterval(function () {
        if (playing && !sendspinStreaming && ws && ws.readyState === WebSocket.OPEN) {
            // Seconds into the served file. Music Assistant adds the seek origin.
            sendWS({ type: 'position', position: audio.currentTime });
        }
    }, 5000);

    function silentWavUrl() {
        if (silentUrl) return silentUrl;
        var samples = 800;
        var pcmBytes = samples * 2;
        var buffer = new ArrayBuffer(44 + pcmBytes);
        var view = new DataView(buffer);
        function write(offset, text) {
            for (var i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
        }
        write(0, 'RIFF');
        view.setUint32(4, 36 + pcmBytes, true);
        write(8, 'WAVE');
        write(12, 'fmt ');
        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);
        view.setUint16(22, 1, true);
        view.setUint32(24, 8000, true);
        view.setUint32(28, 16000, true);
        view.setUint16(32, 2, true);
        view.setUint16(34, 16, true);
        write(36, 'data');
        view.setUint32(40, pcmBytes, true);
        var bytes = new Uint8Array(buffer);
        var binary = '';
        for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
        silentUrl = 'data:audio/wav;base64,' + btoa(binary);
        return silentUrl;
    }
    function isPrimeSource() {
        var src = audio.getAttribute('src') || '';
        return !src || src.indexOf('data:audio/wav;base64,') === 0;
    }
    function showAudioPrompt() {
        audioPromptOn = true;
        setStatus(AUDIO_PROMPT);
    }
    function hideAudioPrompt() {
        audioPromptOn = false;
        var el = document.getElementById('kiosk-status');
        if (el && el.textContent === AUDIO_PROMPT) setStatus('');
    }
    function markHtmlUnlocked() {
        htmlUnlocked = true;
        hideAudioPrompt();
        maybeDisarmAudio();
    }
    function maybeDisarmAudio() {
        if (!htmlUnlocked || !sendspinUnlocked) return;
        document.removeEventListener('pointerdown', onAudioGesture, true);
        document.removeEventListener('keydown', onAudioGesture, true);
        audioArmed = false;
    }
    function onAudioGesture(ev) {
        if (ev.type === 'keydown' && ev.repeat) return;
        var needHtml = !htmlUnlocked;
        var needSendspin = SENDSPIN && !sendspinUnlocked;
        if (!needHtml && !needSendspin) {
            maybeDisarmAudio();
            return;
        }
        // Keep this turn synchronous: play() and AudioContext.resume() only
        // count when they start inside the gesture.
        if (ev.type === 'keydown') {
            ev.stopPropagation();
            if (ev.key === ' ') ev.preventDefault();
        }
        if (needSendspin && window.__sendspinPlayer) {
            window.__sendspinPlayer.unlock().then(function () {
                sendspinUnlocked = true;
                maybeDisarmAudio();
            }).catch(function () { /* the next gesture tries again */ });
        }
        if (!needHtml) return;
        if (isPrimeSource()) {
            if (!audio.getAttribute('src')) audio.src = silentWavUrl();
            var primed = audio.play();
            if (!primed || !primed.then) { markHtmlUnlocked(); return; }
            primed.then(markHtmlUnlocked).catch(function () {});
            return;
        }
        var started = audio.play();
        if (!started || !started.then) { markHtmlUnlocked(); return; }
        started.then(function () {
            if (!sendspinStreaming) markHtmlUnlocked();
        }).catch(function () {});
    }
    function armAudioUnlock() {
        if (audioArmed) return;
        audioArmed = true;
        document.addEventListener('pointerdown', onAudioGesture, true);
        document.addEventListener('keydown', onAudioGesture, true);
    }
    function prepareAudioUnlock() {
        armAudioUnlock();
        var probe = new Audio(silentWavUrl());
        var attempt = probe.play();
        if (!attempt || !attempt.then) return;
        attempt.then(function () {
            probe.pause();
            probe.removeAttribute('src');
            markHtmlUnlocked();
        }).catch(function (err) {
            if (!htmlUnlocked && err && err.name === 'NotAllowedError') showAudioPrompt();
        });
    }
    function startPlayback() {
        if (sendspinStreaming) return;
        var attempt = audio.play();
        if (!attempt || !attempt.then) { markHtmlUnlocked(); return; }
        attempt.then(function () {
            if (!sendspinStreaming) markHtmlUnlocked();
        }).catch(function (err) {
            var name = err && err.name;
            if (name === 'AbortError') return;
            if (name === 'NotAllowedError') {
                armAudioUnlock();
                showAudioPrompt();
                return;
            }
            setStatus((err && err.message) || 'Playback failed');
        });
    }
    function cmd(name, args) {
        if (!playerId) {
            setStatus('Not connected yet');
            return Promise.reject(new Error('Not connected yet'));
        }
        return rpc(name, Object.assign({ player_id: playerId }, args || {})).catch(function (e) {
            setStatus(e.message || String(e));
            return Promise.reject(e);
        });
    }

    function clearImage(img) {
        img.onload = null;
        img.onerror = null;
        img.removeAttribute('src');
    }
    function renderNow() {
        var src = current.image ? imageUrl({ image: current.image }) : '';
        var box = document.getElementById('kiosk-artbox');
        var art = document.getElementById('kiosk-art');
        var bg = document.getElementById('kiosk-bg-img');
        function showEmblem() {
            box.classList.remove('has-art');
            clearImage(art);
            clearImage(bg);
            bg.style.opacity = '0';
        }
        art.alt = current.title && current.title !== '—' ? current.title : '';
        document.getElementById('kiosk-title').textContent = current.title || '—';
        document.getElementById('kiosk-artist').textContent = current.artist || '—';
        if (!src) {
            showEmblem();
            paintClock();
            return;
        }
        // Keep the emblem up until the file decodes. An empty src paints a broken icon.
        art.onload = function () {
            if (art.getAttribute('src') !== src) return;
            box.classList.add('has-art');
            bg.style.opacity = '1';
        };
        art.onerror = function () {
            if (art.getAttribute('src') !== src) return;
            showEmblem();
        };
        bg.onerror = function () {
            if (bg.getAttribute('src') !== src) return;
            clearImage(bg);
            bg.style.opacity = '0';
        };
        box.classList.remove('has-art');
        art.src = src;
        bg.src = src;
        if (art.complete) {
            if (art.naturalWidth > 0) {
                box.classList.add('has-art');
                bg.style.opacity = '1';
            } else {
                showEmblem();
            }
        }
        paintClock();
    }
    function paintClock() {
        var dur = current.duration || 0;
        document.getElementById('kiosk-dur').textContent = dur ? fmt(dur) : '0:00';
        if (seekPointer) return;
        var t = songTime();
        document.getElementById('kiosk-time').textContent = fmt(t);
        if (dur) document.getElementById('kiosk-seek').value = String(Math.round((t / dur) * 1000));
    }
    function sync() {
        var playBtn = document.getElementById('k-play');
        playBtn.textContent = playing ? '⏸' : '▶';
        playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
        // Freeze the Sendspin clock at the pause so the bars stay on that moment.
        if (sendspinStreaming && vizWasPlaying && !playing && waveAnchorAt) {
            waveAnchor += (performance.now() - waveAnchorAt) / 1000;
            waveAnchorAt = performance.now();
        }
        vizWasPlaying = playing;
        syncViz();
    }

    // Stored RMS of the whole track, 1800 bins. The bars are a window of that
    // curve around the playhead. A missing analysis leaves the canvas empty.
    var VIZ_BARS = 48;
    var wave = null;
    var waveKey = '';
    var waveReq = 0;
    var wavePending = false;
    var waveDuration = 0;
    var waveAnchor = 0;
    var waveAnchorAt = 0;
    var vizSeekUntil = 0;
    var vizFrame = 0;
    var vizWasPlaying = false;
    var waveGen = 0;
    var pendingWave = null;
    var pendingLyrics = null;

    function applyWaveBins(bins) {
        wavePending = false;
        wave = Array.isArray(bins) && bins.length ? bins : null;
        if (!vizFrame) paintViz();
    }
    function applyLyrics(lines) {
        lyricsLines = Array.isArray(lines) ? lines : [];
        lyricsIdx = -1;
        paintLyrics();
        // viz=0 still follows the lyric window while the track plays.
        if (playing) syncViz();
    }
    function takeServerWave(seq) {
        waveGen = seq;
        if (pendingWave && pendingWave.gen === seq) {
            var bins = pendingWave.bins;
            pendingWave = null;
            applyWaveBins(bins);
        } else {
            pendingWave = null;
            wave = null;
            wavePending = false;
            waveKey = '';
            if (!vizFrame) paintViz();
        }
        if (pendingLyrics && pendingLyrics.gen === seq) {
            var lines = pendingLyrics.lines;
            pendingLyrics = null;
            applyLyrics(lines);
        } else {
            pendingLyrics = null;
            lyricsLines = [];
            lyricsIdx = -1;
            paintLyrics();
        }
    }

    function vizDuration() {
        if (current.duration) return current.duration;
        if (playing || displayHold != null) return waveDuration || 0;
        return 0;
    }
    function vizTime() {
        // HTML audio is the clock, including after a seek. Sendspin removes
        // that element, so follow the queue position plus the time since.
        if (sendspinStreaming && waveAnchorAt) {
            var extra = playing ? (performance.now() - waveAnchorAt) / 1000 : 0;
            return waveAnchor + extra;
        }
        return songTime();
    }
    function noteQueueForViz(q) {
        var item = null;
        if (queueIndex >= 0 && queueIndex < queue.length) item = queue[queueIndex];
        if (!item && q && q.current_item) item = q.current_item;
        var mi = item && item.media_item;
        var dur = 0;
        if (item && typeof item.duration === 'number' && item.duration > 0) dur = item.duration;
        else if (mi && typeof mi.duration === 'number' && mi.duration > 0) dur = mi.duration;
        if (dur) waveDuration = dur;
        if (sendspinStreaming && displayHold == null && !seekPointer && performance.now() >= vizSeekUntil) {
            var elapsed = null;
            if (q && typeof q.corrected_elapsed_time === 'number') elapsed = q.corrected_elapsed_time;
            else if (q && typeof q.elapsed_time === 'number') elapsed = q.elapsed_time;
            if (elapsed != null) {
                waveAnchor = elapsed;
                waveAnchorAt = performance.now();
            }
        }
        loadWave(item);
    }
    function waveMaps(item) {
        var mi = item && (item.media_item || item);
        var maps = mi && mi.provider_mappings;
        if (!Array.isArray(maps)) return [];
        return maps.filter(function (m) {
            return m && m.item_id && (m.provider_instance || m.provider_domain);
        });
    }
    function loadWave(item) {
        if (!SHOW.viz) return;
        var maps = waveMaps(item);
        if (!maps.length) return;
        var key = maps.map(function (m) {
            return (m.provider_instance || m.provider_domain) + ':' + m.item_id;
        }).join('|');
        // Retry a track that had no analysis yet. Skip while a request is open
        // or the bins are already here.
        if (key === waveKey && (wave || wavePending)) return;
        var req = ++waveReq;
        waveKey = key;
        wavePending = true;
        function tryAt(i) {
            if (req !== waveReq) return;
            if (i >= maps.length) {
                wavePending = false;
                if (!wave) paintViz();
                return;
            }
            var m = maps[i];
            rpc('audio_analysis/wave_form', {
                item_id: String(m.item_id),
                provider_instance_id_or_domain: String(m.provider_instance || m.provider_domain)
            }).then(function (bins) {
                if (req !== waveReq) return;
                if (Array.isArray(bins) && bins.length) {
                    wavePending = false;
                    wave = bins;
                    paintViz();
                    return;
                }
                tryAt(i + 1);
            }).catch(function () {
                if (req !== waveReq) return;
                tryAt(i + 1);
            });
        }
        tryAt(0);
    }
    function paintViz() {
        var cv = document.getElementById('kiosk-viz');
        if (!cv) return;
        var w = cv.clientWidth || window.innerWidth;
        var h = cv.clientHeight || window.innerHeight;
        if (w < 1 || h < 1) return;
        if (cv.width !== w || cv.height !== h) {
            cv.width = w;
            cv.height = h;
        }
        var ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, cv.width, cv.height);
        var dur = vizDuration();
        if (!SHOW.viz || !wave || !wave.length || !dur) return;
        var pos = vizTime();
        if (pos < 0) pos = 0;
        if (pos > dur) pos = dur;
        var n = wave.length;
        var origin = (pos / dur) * n - (VIZ_BARS - 1) / 2;
        var gap = 2;
        var bw = cv.width / VIZ_BARS;
        for (var i = 0; i < VIZ_BARS; i++) {
            var idx = origin + i;
            var v = 0;
            if (idx >= 0 && idx < n) {
                var i0 = Math.floor(idx);
                var i1 = Math.min(n - 1, i0 + 1);
                var frac = idx - i0;
                var a = Number(wave[i0]) || 0;
                var b = Number(wave[i1]) || 0;
                v = a + (b - a) * frac;
            }
            if (v < 0) v = 0;
            if (v > 1) v = 1;
            var bh = v * cv.height * 0.42;
            if (bh < 1) continue;
            ctx.fillStyle = 'rgba(79,140,255,' + (0.28 + 0.62 * v).toFixed(3) + ')';
            ctx.fillRect(i * bw, cv.height - bh, Math.max(1, bw - gap), bh);
        }
    }
    function vizLoop() {
        vizFrame = requestAnimationFrame(vizLoop);
        if (SHOW.viz) paintViz();
        paintLyrics();
    }
    function syncViz() {
        var follow = playing && (SHOW.viz || (SHOW.lyrics && lyricsLines.length));
        if (!follow) {
            if (vizFrame) { cancelAnimationFrame(vizFrame); vizFrame = 0; }
            if (SHOW.viz) paintViz();
            paintLyrics();
            return;
        }
        if (!vizFrame) vizFrame = requestAnimationFrame(vizLoop);
    }
    function lyricIndex(t, dur) {
        var timed = false;
        var i;
        for (i = 0; i < lyricsLines.length; i++) {
            if (typeof lyricsLines[i].t === 'number') { timed = true; break; }
        }
        if (!timed) {
            if (!dur || dur <= 0) return 0;
            var pos = t < 0 ? 0 : t;
            if (pos > dur) pos = dur;
            var at = Math.floor((pos / dur) * lyricsLines.length);
            if (at >= lyricsLines.length) at = lyricsLines.length - 1;
            return at;
        }
        var idx = -1;
        for (i = 0; i < lyricsLines.length; i++) {
            if (typeof lyricsLines[i].t !== 'number') continue;
            if (lyricsLines[i].t <= t) idx = i;
            else break;
        }
        return idx;
    }
    function paintLyrics() {
        var el = document.getElementById('kiosk-lyrics');
        if (!el) return;
        if (!SHOW.lyrics || !lyricsLines.length) {
            if (!el.classList.contains('hidden')) el.classList.add('hidden');
            if (el.childElementCount) el.textContent = '';
            lyricsIdx = -1;
            return;
        }
        var idx = lyricIndex(vizTime(), vizDuration());
        if (idx === lyricsIdx && el.childElementCount === 3) return;
        lyricsIdx = idx;
        var prev = idx > 0 ? lyricsLines[idx - 1].text : '';
        var cur = idx >= 0 ? lyricsLines[idx].text : '';
        var next = idx >= 0 && idx + 1 < lyricsLines.length ? lyricsLines[idx + 1].text : (idx < 0 && lyricsLines.length ? lyricsLines[0].text : '');
        el.classList.remove('hidden');
        el.innerHTML = '<div class="l">' + esc(prev) + '</div>' +
            '<div class="l current">' + esc(cur) + '</div>' +
            '<div class="l">' + esc(next) + '</div>';
    }
    window.addEventListener('resize', function () { if (!vizFrame) paintViz(); });

    async function fetchParty() {
        var el = document.getElementById('kiosk-party');
        if (!SHOW.party) { el.classList.add('hidden'); return; }
        try {
            var res = await fetch('/api/party');
            if (!res.ok) { el.classList.add('hidden'); return; }
            var info = await res.json();
            if (!info || !info.active) {
                el.classList.add('hidden');
                el.dataset.version = '';
                return;
            }
            var version = info.version || '1';
            if (el.dataset.version === version && !el.classList.contains('hidden')) return;
            el.dataset.version = version;
            el.classList.remove('hidden');
            el.innerHTML = '<img src="/api/party/qr.svg?v=' + encodeURIComponent(version) + '" alt="Join">' +
                (info.name ? '<div class="name">' + esc(info.name) + '</div>' : '') +
                (info.qr_text ? '<div class="qr">' + esc(info.qr_text) + '</div>' : '');
        } catch (e) { el.classList.add('hidden'); }
    }

    function fetchQueue() {
        if (!playerId || !TOKEN) return;
        rpc('player_queues/get_active_queue', { player_id: playerId }).then(function (q) {
            if (!q || !q.queue_id) return;
            return rpc('player_queues/items', { queue_id: q.queue_id, limit: 200 }).then(function (items) {
                queue = Array.isArray(items) ? items : [];
                queueIndex = q.current_index != null ? q.current_index : -1;
                renderQueue();
                noteQueueForViz(q);
            });
        }).catch(function () { /* queue is optional until playback starts */ });
    }
    function renderQueue() {
        var el = document.getElementById('kiosk-queue');
        if (!queue.length) { el.classList.add('hidden'); return; }
        el.classList.remove('hidden');
        el.innerHTML = queue.map(function (qi, i) {
            var mi = qi.media_item || {};
            var img = imageUrl(mi);
            return '<div class="qrow' + (i === queueIndex ? ' active' : '') + '">' +
                (img ? '<img src="' + img + '" alt="">' : '') +
                '<div class="qmeta"><div class="qt">' + esc(mi.name || qi.name || '') + '</div>' +
                '<div class="qa">' + esc(artistStr(mi) || '') + '</div></div></div>';
        }).join('');
    }

    function seekToSong(seconds) {
        var dur = current.duration || 0;
        var t = Math.max(0, dur ? Math.min(dur, seconds) : seconds);
        displayHold = t;
        // Keep the energy window on the seek target while Sendspin has no
        // audio element to read the position from. Ignore a stale queue poll
        // for a moment so it does not pull the bars back.
        vizSeekUntil = performance.now() + 2000;
        if (sendspinStreaming) {
            waveAnchor = t;
            waveAnchorAt = performance.now();
        }
        document.getElementById('kiosk-time').textContent = fmt(t);
        if (dur) document.getElementById('kiosk-seek').value = String(Math.round((t / dur) * 1000));
        if (!vizFrame) paintViz();
        paintLyrics();
        // The queue rebuilds the stream. Do not move audio.currentTime to the
        // song position: that file starts at the seek point.
        cmd('players/cmd/seek', { position: Math.round(t) }).catch(function () {
            displayHold = null;
            paintClock();
        });
    }
    function applyVolume(level, immediate) {
        level = Math.max(0, Math.min(100, Math.round(level)));
        audio.volume = level / 100;
        document.getElementById('kiosk-volume').value = String(level);
        clearTimeout(volumeTimer);
        var send = function () {
            cmd('players/cmd/volume_set', { volume_level: level }).catch(function () {});
        };
        if (immediate) send();
        else volumeTimer = setTimeout(send, 80);
    }

    function bindControls() {
        document.getElementById('k-play').onclick = function () {
            cmd(playing ? 'players/cmd/pause' : 'players/cmd/play').catch(function () {});
        };
        document.getElementById('k-next').onclick = function () { cmd('players/cmd/next').catch(function () {}); };
        document.getElementById('k-prev').onclick = function () { cmd('players/cmd/previous').catch(function () {}); };
        document.getElementById('kiosk-volume').oninput = function (e) {
            applyVolume(Number(e.target.value), false);
        };
        var kseek = document.getElementById('kiosk-seek');
        kseek.addEventListener('pointerdown', function () { seekPointer = true; });
        kseek.addEventListener('pointerup', function () { seekPointer = false; });
        kseek.addEventListener('pointercancel', function () { seekPointer = false; });
        kseek.oninput = function (e) {
            var t = (Number(e.target.value) / 1000) * (current.duration || 0);
            document.getElementById('kiosk-time').textContent = fmt(t);
        };
        kseek.onchange = function (e) {
            seekPointer = false;
            seekToSong((Number(e.target.value) / 1000) * (current.duration || 0));
        };
        audio.ontimeupdate = function () {
            paintClock();
            if (!vizFrame) paintLyrics();
        };
        audio.addEventListener('loadedmetadata', applyPendingSeek);
        audio.addEventListener('canplay', applyPendingSeek);
        audio.onended = function () {
            // The page often has no Music Assistant token, so next goes over
            // the player socket. The provider then advances the queue.
            if (isPrimeSource() || sendspinStreaming) return;
            playing = false;
            sync();
            endReported = false;
            reportTrackEnded();
        };
    }

    var hideTimer = null;
    function showKioskControls() {
        if (!SHOW.controls) return;
        var el = document.getElementById('kiosk-controls');
        el.classList.add('visible');
        clearTimeout(hideTimer);
        hideTimer = setTimeout(function () { el.classList.remove('visible'); }, 3500);
    }

    document.addEventListener('keydown', function (e) {
        if (e.target.tagName === 'INPUT') return;
        switch (e.key) {
            case ' ': e.preventDefault(); cmd(playing ? 'players/cmd/pause' : 'players/cmd/play').catch(function () {}); break;
            case 'ArrowRight': seekToSong(songTime() + 10); break;
            case 'ArrowLeft': seekToSong(Math.max(0, songTime() - 10)); break;
            case 'ArrowUp': e.preventDefault(); applyVolume(Number(document.getElementById('kiosk-volume').value) + 5, true); break;
            case 'ArrowDown': e.preventDefault(); applyVolume(Number(document.getElementById('kiosk-volume').value) - 5, true); break;
            case 'n': case 'N': cmd('players/cmd/next').catch(function () {}); break;
            case 'p': case 'P': cmd('players/cmd/previous').catch(function () {}); break;
        }
    });

    function getDefaultSendspinUrl() {
        return 'http://' + location.hostname + ':8927';
    }
    function showPairPin(pin) {
        var el = document.getElementById('kiosk-pair');
        if (!el) return;
        if (!pin) { el.classList.add('hidden'); el.textContent = ''; return; }
        el.classList.remove('hidden');
        el.textContent = 'Pairing code ' + pin;
    }
    async function loadSendspinModule() {
        if (!sendspinModule) sendspinModule = await import('./sendspin-js/index.js');
        return sendspinModule;
    }
    async function prepareSendspinIdentity() {
        try {
            var module = await loadSendspinModule();
            var identity = module.loadSendspinClientIdentity();
            sendspinClientId = identity && identity.clientId ? identity.clientId : '';
        } catch (e) {
            sendspinClientId = '';
        }
    }
    async function initSendspin() {
        if (!SENDSPIN || window.__sendspinPlayer) return;
        setStatus('Connecting Sendspin…');
        try {
            var module = await loadSendspinModule();
            var sendspinUrl = params.get('sendspin_url') || getDefaultSendspinUrl();
            var cfg = {
                baseUrl: sendspinUrl,
                clientName: 'Web Kiosk',
                productName: 'Web Kiosk',
                correctionMode: 'sync',
                onStateChange: function () { setSync('synced'); },
                onPairingPin: function (pin) { showPairPin(pin); }
            };
            // opus-encdec is not vendored; browsers without WebCodecs stay on FLAC/PCM.
            if (typeof AudioDecoder === 'undefined') cfg.codecs = ['flac', 'pcm'];
            var player = new module.SendspinPlayer(cfg);
            window.__sendspinPlayer = player;
            wrapSendspinHooks(player.core);
            if (!sendspinUnlocked) {
                player.unlock().then(function () {
                    sendspinUnlocked = true;
                    maybeDisarmAudio();
                }).catch(function () { /* the next gesture tries again */ });
            }
            await player.connect();
            setSync('synced');
            setStatus('');
        } catch (e) {
            setSync('error');
            setStatus('Sendspin error: ' + e.message);
            sendspinUnlocked = true;
            maybeDisarmAudio();
        }
    }
    function wrapSendspinHooks(core) {
        // The core exposes these callbacks as setters only. Read the stored
        // handler, then chain ours in front so the scheduler still runs.
        function chain(name, before) {
            var previous = core['_' + name];
            core[name] = function () {
                before.apply(this, arguments);
                if (typeof previous === 'function') return previous.apply(core, arguments);
            };
        }
        chain('onStreamStart', function () {
            sendspinStreaming = true;
            audio.pause();
            audio.removeAttribute('src');
            pendingSeek = null;
            playing = true;
            sync();
            fetchQueue();
        });
        chain('onStreamEnd', function () {
            sendspinStreaming = false;
            if (audio.paused) { playing = false; sync(); }
        });
        chain('onStreamClear', function () {
            sendspinStreaming = false;
        });
    }
    function setSync(state) {
        var el = document.getElementById('kiosk-sync');
        el.className = state;
        el.textContent = state === 'synced' ? 'SYNC' : state === 'error' ? 'ERROR' : 'SYNCING…';
    }

    async function boot() {
        document.body.classList.add('kiosk');
        bindControls();
        renderNow();
        sync();
        prepareAudioUnlock();
        await prepareSendspinIdentity();
        connectWS();
        if (SHOW.party) {
            fetchParty();
            setInterval(fetchParty, 10000);
        }
        if (TOKEN) setInterval(fetchQueue, 15000);
        if (SHOW.controls) {
            document.addEventListener('mousemove', showKioskControls);
            document.addEventListener('touchstart', showKioskControls, { passive: true });
            showKioskControls();
        }
    }
    boot();
})();
