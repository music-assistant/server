/* Web Kiosk player. Music is chosen in Music Assistant. This page shows it. */
(function () {
    'use strict';

    var params = new URLSearchParams(location.search);
    // /kiosk is the player; /web and kiosk=1 remain compatible aliases.
    var REQUESTED_SENDSPIN = params.get('sendspin') !== '0';
    var SENDSPIN = REQUESTED_SENDSPIN && supportsSendspin();
    if (SENDSPIN && location.protocol === 'https:') {
        try {
            var endpoint = new URL(params.get('sendspin_url') || getDefaultSendspinUrl());
            SENDSPIN = endpoint.protocol === 'https:' || endpoint.protocol === 'wss:';
        } catch (e) { SENDSPIN = false; }
    }
    if (SENDSPIN) document.documentElement.classList.add('sendspin');
    var FLAG = function (name) { return params.get(name) === '1'; };
    var REMOTE = FLAG('remote');
    var LITE = FLAG('lite');
    var KEEP_AWAKE = FLAG('wake_lock');
    document.documentElement.classList.toggle('lite', LITE);
    var SHOW = { controls: FLAG('controls'), party: FLAG('party'), viz: FLAG('viz'), lyrics: FLAG('lyrics'), queue: FLAG('queue'), playerId: FLAG('show_player_id'), group: FLAG('show_group') };
    document.documentElement.classList.toggle('controls', SHOW.controls);
    document.documentElement.classList.toggle('no-grid', !window.CSS || !CSS.supports || !CSS.supports('display', 'grid'));

    var memoryStorage = Object.create(null);
    var LS = {
        get: function (k) {
            if (Object.prototype.hasOwnProperty.call(memoryStorage, k)) return memoryStorage[k];
            try { memoryStorage[k] = localStorage.getItem(k); } catch (e) { memoryStorage[k] = null; }
            return memoryStorage[k];
        },
        set: function (k, v) { memoryStorage[k] = String(v); try { localStorage.setItem(k, String(v)); } catch (e) { /* memory is retained for this page */ } }
    };
    var sdkStorage = { getItem:LS.get, setItem:LS.set };
    var deviceId = params.get('device_id') || LS.get('wk_device_id');
    if (!deviceId) { deviceId = createDeviceId(); LS.set('wk_device_id', deviceId); }

    function createDeviceId() {
        if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return 'wk-' + crypto.randomUUID();
        // randomUUID requires a secure context; LAN kiosks also run over plain HTTP.
        var bytes = new Uint8Array(16);
        if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') crypto.getRandomValues(bytes);
        else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        var hex = Array.from(bytes, function (b) { return ('0' + b.toString(16)).slice(-2); }).join('');
        return 'wk-' + hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16)
            + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
    }
    var sendspinClientId = '';
    var pendingHtml5Fallback = false;
    var sendspinModule = null;
    var sendspinConnecting = false;
    // True only while the Sendspin server is actually streaming audio here.
    var sendspinStreaming = false;
    var sendspinState = null;
    var sendspinOwnsPlayback = false;
    var sendspinAudioAt = 0;
    var sendspinReceivedAudio = false;
    var fragment = new URLSearchParams(location.hash.slice(1));
    var TOKEN = fragment.get('token') || params.get('token') || LS.get('wk_token') || '';
    if (fragment.has('token') || params.has('token')) {
        fragment.delete('token');
        params.delete('token');
        var query = params.toString();
        var hash = fragment.toString();
        history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + (hash ? '#' + hash : ''));
    }
    if (TOKEN) LS.set('wk_token', TOKEN);

    var playerId = '';
    var customName = '';
    var serverVersion = '';
    var browserModel = 'Web browser';
    var hasTrack = false;
    var partyExpanded = false;
    var ws = null;
    var reconnectTimer = null;
    var bootReady = false;
    var wakeLock = null;
    var wakePending = false;
    var wakeGeneration = 0;
    var msgSeq = 0;
    var playing = false;
    var current = { title: '—', artist: '—', image: '', duration: 0, start: 0 };
    // Seconds into the served file, applied once the audio element has metadata.
    var pendingSeek = null;
    // Song position shown while a seek waits for the rebuilt stream.
    var displayHold = null;
    var seekPointer = false;
    var volumeTimer = null;
    var volumePanelOpen = false;
    var queue = [];
    var queueIndex = -1;
    var lyricsLines = [];
    var lyricsIdx = -1;
    var renderedLyrics = null;
    var endReported = false;
    var errorReported = false;
    var playbackGeneration = '';

    var audio = new Audio();
    audio.volume = 1;
    var mediaVolumeWritable = true;
    try {
        audio.volume = 0.5;
        mediaVolumeWritable = Math.abs(audio.volume - 0.5) < 0.01;
        audio.volume = 1;
    } catch (e) { mediaVolumeWritable = false; }
    // Browsers withhold sound until the document receives a tap or a key.
    // That one gesture covers later tracks. A reload asks again.
    var AUDIO_PROMPT = 'Tap or press a key to start audio';
    var audioPromptOn = false;
    var htmlUnlocked = false;
    var sendspinUnlocked = !SENDSPIN;
    var audioArmed = false;
    var activationEvents = window.PointerEvent ? ['pointerdown', 'keydown'] : ['touchstart', 'touchend', 'mousedown', 'click', 'keydown'];
    var silentUrl = '';

    function rpc(command, args) {
        // Same origin as this page. The kiosk server forwards the call to MA,
        // because the browser will not call MA's port from this one.
        if (!TOKEN) return Promise.reject(new Error('Configure token'));
        return fetch('/api', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN },
            body: JSON.stringify({ command: command, args: args || {}, message_id: String(++msgSeq) })
        }).then(function (res) {
            if (!res.ok) return res.text().then(function (text) { throw new Error(text); });
            return res.json();
        });
    }

    function setStatus(t) {
        if (!t && audioPromptOn) t = AUDIO_PROMPT;
        var el = document.getElementById('kiosk-status');
        el.textContent = t || '';
        el.classList.toggle('hidden', !t);
        el.classList.toggle('audio-prompt', el.textContent === AUDIO_PROMPT);
    }
    function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
    function replaceContent(el, child) {
        while (el.firstChild) el.removeChild(el.firstChild);
        if (child) el.appendChild(child);
    }
    function setIconHref(el, href) {
        el.setAttribute('href', href);
        el.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', href);
    }
    function fmt(sec) {
        sec = Math.max(0, Math.floor(sec || 0));
        var m = Math.floor(sec / 60), s = sec % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
    }
    function songTime() {
        if (displayHold != null) {
            if (!sendspinStreaming || performance.now() < vizSeekUntil) return displayHold;
            displayHold = null;
        }
        if (sendspinStreaming) {
            var progress = window.__sendspinPlayer && window.__sendspinPlayer.trackProgress;
            if (progress && typeof progress.positionMs === 'number') return progress.positionMs / 1000;
            if (waveAnchorAt) return waveAnchor + (playing ? (performance.now() - waveAnchorAt) / 1000 : 0);
        }
        return (current.start || 0) + (audio.currentTime || 0) * (current.speed || 1);
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
        if (navigator.onLine === false || (ws && ws.readyState <= WebSocket.OPEN)) return;
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
        var proto = location.protocol === 'https:' ? 'wss' : 'ws';
        var qs = 'device_id=' + encodeURIComponent(deviceId);
        qs += '&volume_control=' + (mediaVolumeWritable || sendspinStreaming ? '1' : '0');
        if (SHOW.lyrics) qs += '&lyrics=1';
        if (sendspinClientId) qs += '&sendspin_client_id=' + encodeURIComponent(sendspinClientId);
        var socket = new WebSocket(proto + '://' + location.host + '/ws?' + qs);
        ws = socket;
        socket.onmessage = function (ev) { if (ws === socket) handleWS(JSON.parse(ev.data), socket); };
        socket.onclose = function () {
            if (ws !== socket) return;
            ws = null;
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connectWS, 2000);
        };
        socket.onerror = function () { try { socket.close(); } catch (e) { /* noop */ } };
    }
    function recoverPage(forceReconnect) {
        if (!bootReady || document.hidden || navigator.onLine === false) return;
        if (forceReconnect && ws) {
            var previous = ws;
            ws = null;
            previous.close();
        }
        connectWS();
        if (playing && !sendspinStreaming && audio.paused && !isPrimeSource()) startPlayback();
        var player = window.__sendspinPlayer;
        if (SENDSPIN && player) {
            if (!sendspinReceivedAudio) sendspinAudioAt = performance.now();
            if (sendspinStreaming && playing) player.unlock().catch(function () {
                sendspinUnlocked = false;
                armAudioUnlock();
                showAudioPrompt();
            });
            if (!player.isConnected && !sendspinConnecting) {
                sendspinConnecting = true;
                Promise.resolve().then(function () { return player.connect(); }).then(function () {
                    sendspinConnecting = false;
                }, function () {
                    sendspinConnecting = false;
                    if (window.__sendspinPlayer === player) useHtml5();
                });
            }
        }
        paintClock();
        paintLyrics(true);
        syncViz();
        refreshWakeLock();
        if (SHOW.party) fetchParty();
        if (SHOW.queue && TOKEN) fetchQueue();
    }
    window.addEventListener('online', function () { recoverPage(true); });
    window.addEventListener('pageshow', function (event) { recoverPage(!!event.persisted); });
    document.addEventListener('visibilitychange', function () {
        if (document.hidden && vizFrame) { cancelAnimationFrame(vizFrame); vizFrame = 0; }
        refreshWakeLock();
        recoverPage(false);
    });
    function refreshWakeLock() {
        if (!KEEP_AWAKE || !navigator.wakeLock || !navigator.wakeLock.request) return;
        if (document.hidden) {
            wakeGeneration++;
            var previous = wakeLock;
            wakeLock = null;
            if (previous) Promise.resolve(previous.release()).catch(function () {});
            return;
        }
        if (wakePending || (wakeLock && !wakeLock.released)) return;
        var generation = wakeGeneration;
        wakePending = true;
        Promise.resolve().then(function () { return navigator.wakeLock.request('screen'); }).then(function (lock) {
            wakePending = false;
            if (document.hidden || generation !== wakeGeneration) {
                Promise.resolve(lock.release()).catch(function () {});
                if (!document.hidden) refreshWakeLock();
                return;
            }
            wakeLock = lock;
            lock.addEventListener('release', function () { if (wakeLock === lock) wakeLock = null; });
        }).catch(function () { wakePending = false; });
    }
    function sendWS(obj) { if (ws && ws.readyState === WebSocket.OPEN) { try { ws.send(JSON.stringify(obj)); } catch (e) { /* noop */ } } }
    function reportVolumeCapability() {
        sendWS({ type:'capabilities', volume_control:mediaVolumeWritable || sendspinStreaming });
        syncVolume(audio.volume * 100);
    }
    function reportPlaybackError(generation, reason) {
        // Only the stream that is still current may fail. A replaced stream's
        // late rejection or abort must not stop the new playback.
        if (!generation || generation !== playbackGeneration || errorReported) return;
        if (isPrimeSource() || sendspinStreaming) return;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        sendWS({ type: 'playback_error', playback_generation: generation, reason: reason });
        errorReported = true;
        playing = false; sync();
        setStatus('Playback failed');
    }

    function reportTrackEnded() {
        if (endReported || !playbackGeneration || !audio.ended || isPrimeSource() || sendspinStreaming) return;
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        sendWS({ type: 'ended', playback_generation: playbackGeneration, position: audio.currentTime });
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

    function handleWS(msg, socket) {
        switch (msg.type) {
            case 'sendspin_challenge':
                loadSendspinModule().then(function (module) {
                    if (ws !== socket) return;
                    sendWS({type:'sendspin_proof', proof:module.createSendspinClaimProof(sdkStorage, msg)});
                }).catch(function () { if (ws !== socket) return; useHtml5(); if (ws) ws.close(); });
                break;
            case 'welcome':
                if (msg.device_info && typeof msg.device_info.model === 'string') browserModel = msg.device_info.model;
                playerId = msg.player_id;
                customName = typeof msg.custom_name === 'string' ? msg.custom_name : '';
                serverVersion = typeof msg.server_version === 'string' ? msg.server_version : '';
                renderNow();
                var playerLabel = document.getElementById('kiosk-player-id');
                playerLabel.textContent = 'Player ID: ' + playerId;
                playerLabel.classList.toggle('hidden', !SHOW.playerId);
                setStatus('');
                sync();
                reportVolumeCapability();
                if (TOKEN) { fetchQueue(); fetchParty(); }
                if (SENDSPIN) initSendspin();
                else if (pendingHtml5Fallback) sendWS({type:'sendspin_fallback'});
                if (audio.ended) reportTrackEnded();
                break;
            case 'player':
                customName = typeof msg.custom_name === 'string' ? msg.custom_name : '';
                if (!hasTrack) renderNow();
                break;
            case 'group':
                var groupLabel = document.getElementById('kiosk-group');
                groupLabel.textContent = msg.group_name || '';
                groupLabel.classList.toggle('hidden', !SHOW.group || !msg.group_id || !msg.group_name);
                break;
            case 'media':
                if (typeof msg.wave_seq === 'number') takeServerWave(msg.wave_seq);
                hasTrack = !!msg.title;
                current = {title:msg.title || '', artist:msg.artist || '', image:msg.image_url || '', duration:msg.duration || 0, start:0};
                displayHold = null;
                renderNow();
                sync();
                break;
            case 'play':
                endReported = false;
                errorReported = false;
                // A new track retires the previous curve. The matching wave
                // message may already be waiting, or it arrives next.
                if (typeof msg.wave_seq === 'number') takeServerWave(msg.wave_seq);
                // Sendspin mode still receives the Web Kiosk stream. Music Assistant
                // uses that output unless a Sendspin stream is already playing here.
                if (sendspinStreaming) break;
                if (msg.path) {
                    sendspinOwnsPlayback = false;
                    pendingHtml5Fallback = false;
                    playbackGeneration = typeof msg.playback_generation === 'string' ? msg.playback_generation : '';
                    pendingSeek = null;
                    hasTrack = true;
                    current = {
                        title: msg.title || '',
                        artist: msg.artist || '',
                        image: msg.image_url || '',
                        duration: msg.duration || 0,
                        start: typeof msg.start === 'number' ? msg.start : 0,
                        speed: typeof msg.playback_speed === 'number' && msg.playback_speed > 0 ? msg.playback_speed : 1
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
                sendspinOwnsPlayback = false;
                playbackGeneration = '';
                endReported = true;
                audio.pause(); audio.removeAttribute('src');
                pendingSeek = null;
                displayHold = null;
                hasTrack = false;
                current = { title: '—', artist: '—', image: '', duration: 0, start: 0 };
                lyricsLines = [];
                lyricsIdx = -1;
                pendingLyrics = null;
                renderNow();
                playing = false; sync();
                hideAudioPrompt();
                setStatus(msg.showNotification ? 'Playback stopped' : '');
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
                if (sendspinStreaming && window.__sendspinPlayer && window.__sendspinPlayer.volume !== (msg.level || 0)) window.__sendspinPlayer.setVolume(msg.level || 0);
                syncVolume(msg.level || 0);
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
                if (msg.url) {
                    var target = new URL(msg.url, location.protocol + '//' + location.host);
                    ['controls', 'party', 'viz', 'lyrics', 'queue', 'show_player_id', 'show_group', 'remote', 'lite', 'wake_lock', 'fullscreen', 'device_id', 'sendspin_url'].forEach(function (name) {
                        if (params.has(name) && !target.searchParams.has(name)) target.searchParams.set(name, params.get(name));
                    });
                    location.href = target.toString();
                }
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
        activationEvents.forEach(function (name) { document.removeEventListener(name, onAudioGesture, true); });
        audioArmed = false;
    }
    function onAudioGesture(ev) {
        refreshWakeLock();
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
        activationEvents.forEach(function (name) { document.addEventListener(name, onAudioGesture, true); });
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
        var generation = playbackGeneration;
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
            reportPlaybackError(generation, name || 'Error');
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
        var logo = SENDSPIN ? '/web/sendspin.svg' : '/web/html5.svg';
        var title = hasTrack ? current.title || '—' : customName || playerId || 'Connecting…';
        var artist = hasTrack ? current.artist || '—' : 'Music Assistant' + (serverVersion ? ' ' + serverVersion : '');
        var box = document.getElementById('kiosk-artbox');
        var art = document.getElementById('kiosk-art');
        var bg = document.getElementById('kiosk-bg-img');
        function showEmblem() {
            box.classList.remove('has-art');
            clearImage(art);
            art.src = logo;
            art.alt = SENDSPIN ? 'Sendspin' : 'HTML5';
            box.classList.add('has-art', 'project-art');
            clearImage(bg);
            bg.src = logo;
            bg.style.opacity = '1';
        }
        art.alt = title;
        document.getElementById('kiosk-title').textContent = title;
        document.getElementById('kiosk-artist').textContent = artist;
        document.getElementById('kiosk-bar-title').textContent = title;
        document.getElementById('kiosk-bar-artist').textContent = artist;
        var barArt = document.getElementById('kiosk-bar-art');
        var barBox = document.getElementById('kiosk-bar-artbox');
        barArt.onload = function () { barBox.classList.add('has-art'); barArt.classList.remove('hidden'); };
        barArt.onerror = function () { barBox.classList.remove('has-art'); barArt.classList.add('hidden'); };
        barBox.classList.add('project-art');
        barArt.src = logo;
        barArt.alt = SENDSPIN ? 'Sendspin' : 'HTML5';
        if (!src) {
            showEmblem();
            paintClock();
            return;
        }
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
        box.classList.remove('has-art', 'project-art');
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
        var dur = vizDuration();
        document.getElementById('kiosk-dur').textContent = dur ? fmt(dur) : '0:00';
        if (seekPointer) return;
        var t = songTime();
        document.getElementById('kiosk-time').textContent = fmt(t);
        var seek = document.getElementById('kiosk-seek');
        var position = dur ? Math.max(0, Math.min(1000, Math.round((t / dur) * 1000))) : 0;
        seek.disabled = !playerId || !dur;
        seek.value = String(position);
        seek.style.setProperty('--progress', (position / 10) + '%');
    }
    function sync() {
        var playBtn = document.getElementById('k-play');
        setIconHref(document.getElementById('k-play-icon'), playing ? '#k-icon-pause' : '#k-icon-play');
        playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
        playBtn.title = playing ? 'Pause' : 'Play';
        ['k-play', 'k-prev', 'k-next', 'k-volume-button', 'kiosk-volume'].forEach(function (id) {
            document.getElementById(id).disabled = !playerId || (id === 'kiosk-volume' && !sendspinStreaming && !mediaVolumeWritable);
        });
        // Freeze the Sendspin clock at the pause so the bars stay on that moment.
        if (sendspinStreaming && vizWasPlaying && !playing && waveAnchorAt) {
            waveAnchor += (performance.now() - waveAnchorAt) / 1000;
            waveAnchorAt = performance.now();
        }
        if (sendspinStreaming && !vizWasPlaying && playing && waveAnchorAt) waveAnchorAt = performance.now();
        vizWasPlaying = playing;
        paintClock();
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
    var lastVizPaint = 0;
    var lastLyricsPaint = 0;
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
        var progress = sendspinStreaming && window.__sendspinPlayer && window.__sendspinPlayer.trackProgress;
        if (progress && progress.durationMs > 0) return progress.durationMs / 1000;
        if (current.duration) return current.duration;
        if (playing || displayHold != null) return waveDuration || 0;
        return 0;
    }
    function vizTime() {
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
        var now = performance.now();
        if (SHOW.viz && now - lastVizPaint >= (LITE ? 1000 / 15 : 1000 / 30)) { lastVizPaint = now; paintViz(); }
        if (now - lastLyricsPaint >= 100) { lastLyricsPaint = now; paintLyrics(); }
    }
    function syncViz() {
        var follow = !document.hidden && playing && (SHOW.viz || (SHOW.lyrics && lyricsLines.length));
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
    function paintLyrics(resized) {
        var el = document.getElementById('kiosk-lyrics');
        if (!el) return;
        if (!SHOW.lyrics || !lyricsLines.length) {
            document.documentElement.classList.remove('lyrics-visible');
            if (!el.classList.contains('hidden')) el.classList.add('hidden');
            if (el.childElementCount) el.textContent = '';
            lyricsIdx = -1;
            renderedLyrics = null;
            return;
        }
        document.documentElement.classList.add('lyrics-visible');
        el.classList.remove('hidden');
        var wide = window.innerWidth > 768 && !document.documentElement.classList.contains('no-grid');
        var layout = wide ? 'wide' : 'compact';
        var idx = lyricIndex(vizTime(), vizDuration());
        var changed = renderedLyrics !== lyricsLines || el.dataset.layout !== layout;
        if (wide) {
            var content;
            if (changed) {
                content = document.createElement('div');
                content.classList.add('lyrics-content');
                lyricsLines.forEach(function (line) {
                    var row = document.createElement('div');
                    row.classList.add('l');
                    row.textContent = line.text;
                    content.appendChild(row);
                });
                replaceContent(el, content);
            }
            content = el.children[0];
            if (changed || idx !== lyricsIdx) {
                Array.from(content.children).forEach(function (row, i) {
                    row.classList.toggle('current', i === idx);
                    row.classList.toggle('far', Math.abs(i - idx) > 1);
                });
            }
            if (changed || idx !== lyricsIdx || resized) {
                var active = content.children[Math.max(0, idx)];
                content.style.transform = 'translateY(' + (el.clientHeight / 2 - active.offsetTop - active.offsetHeight / 2) + 'px)';
            }
            lyricsIdx = idx;
            renderedLyrics = lyricsLines;
            el.dataset.layout = layout;
            return;
        }
        if (!changed && idx === lyricsIdx) return;
        lyricsIdx = idx;
        renderedLyrics = lyricsLines;
        el.dataset.layout = layout;
        var prev = idx > 0 ? lyricsLines[idx - 1].text : '';
        var cur = idx >= 0 ? lyricsLines[idx].text : '';
        var next = idx >= 0 && idx + 1 < lyricsLines.length ? lyricsLines[idx + 1].text : (idx < 0 && lyricsLines.length ? lyricsLines[0].text : '');
        el.classList.remove('hidden');
        el.innerHTML = '<div class="l">' + esc(prev) + '</div>' +
            '<div class="l current">' + esc(cur) + '</div>' +
            '<div class="l">' + esc(next) + '</div>';
    }
    window.addEventListener('resize', function () {
        paintLyrics(true);
        if (!vizFrame) paintViz();
    });

    function setPartyExpanded(open) {
        var party = document.getElementById('kiosk-party');
        partyExpanded = open && !party.classList.contains('hidden') && !!party.dataset.version;
        var expanded = document.getElementById('kiosk-party-expanded');
        expanded.classList.toggle('hidden', !partyExpanded);
        party.setAttribute('aria-expanded', String(partyExpanded));
        party.setAttribute('aria-label', partyExpanded ? 'Close Party QR' : 'Enlarge Party QR');
        if (partyExpanded) {
            document.getElementById('kiosk-party-art').src = '/api/party/qr.svg?v=' + encodeURIComponent(party.dataset.version);
        }
    }

    function hideParty() {
        var party = document.getElementById('kiosk-party');
        party.classList.add('hidden');
        party.dataset.version = '';
        document.documentElement.classList.remove('party-active');
        setPartyExpanded(false);
    }

    function fetchParty() {
        var el = document.getElementById('kiosk-party');
        if (!SHOW.party) { hideParty(); return; }
        return fetch('/api/party').then(function (res) {
            if (!res.ok) throw new Error('Party unavailable');
            return res.json();
        }).then(function (info) {
            if (!info || !info.active) {
                hideParty();
                return;
            }
            var version = info.version || '1';
            if (el.dataset.version === version && !el.classList.contains('hidden')) return;
            el.dataset.version = version;
            el.classList.remove('hidden');
            document.documentElement.classList.add('party-active');
            el.title = [info.name || 'Party', info.qr_text || 'Enlarge QR'].join(' — ');
            var thumbnail = document.getElementById('kiosk-party-thumbnail');
            thumbnail.onerror = hideParty;
            thumbnail.src = '/api/party/qr.svg?v=' + encodeURIComponent(version);
            setPartyExpanded(partyExpanded);
        }).catch(hideParty);
    }

    function fetchQueue() {
        if (!SHOW.queue || !playerId || !TOKEN) return;
        rpc('player_queues/get_active_queue', { player_id: playerId }).then(function (q) {
            if (!q || !q.queue_id) return;
            var index = q.current_index != null ? q.current_index : -1;
            var offset = Math.max(0, index - 20);
            return rpc('player_queues/items', { queue_id: q.queue_id, offset: offset, limit: 200 }).then(function (items) {
                queue = Array.isArray(items) ? items : [];
                queueIndex = index < 0 ? -1 : index - offset;
                renderQueue();
                noteQueueForViz(q);
            });
        }).catch(function () { /* queue is optional until playback starts */ });
    }
    function renderQueue() {
        var el = document.getElementById('kiosk-queue');
        document.documentElement.classList.toggle('queue-active', !!queue.length);
        if (!queue.length) { el.classList.add('hidden'); return; }
        el.classList.remove('hidden');
        replaceContent(el);
        queue.forEach(function (qi, i) {
            var mi = qi.media_item || {};
            var row = document.createElement('div');
            row.classList.add('qrow');
            if (i === queueIndex) row.classList.add('active');
            var img = imageUrl(mi);
            if (img) {
                var artwork = document.createElement('img');
                artwork.src = img;
                artwork.alt = '';
                row.appendChild(artwork);
            }
            var meta = document.createElement('div');
            meta.classList.add('qmeta');
            var title = document.createElement('div');
            title.classList.add('qt');
            title.textContent = mi.name || qi.name || '';
            var artist = document.createElement('div');
            artist.classList.add('qa');
            artist.textContent = artistStr(mi) || '';
            meta.appendChild(title);
            meta.appendChild(artist);
            row.appendChild(meta);
            el.appendChild(row);
        });
    }

    function seekToSong(seconds) {
        var dur = vizDuration();
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
        if (dur) {
            var seek = document.getElementById('kiosk-seek');
            seek.value = String(Math.round((t / dur) * 1000));
            seek.style.setProperty('--progress', ((t / dur) * 100) + '%');
        }
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
        if (!sendspinStreaming && !mediaVolumeWritable) { setStatus('Use the device volume buttons'); return; }
        level = Math.max(0, Math.min(100, Math.round(level)));
        audio.volume = level / 100;
        if (sendspinStreaming && window.__sendspinPlayer && window.__sendspinPlayer.volume !== level) window.__sendspinPlayer.setVolume(level);
        syncVolume(level);
        clearTimeout(volumeTimer);
        var send = function () {
            cmd('players/cmd/volume_set', { volume_level: level }).catch(function () {});
        };
        if (immediate) send();
        else volumeTimer = setTimeout(send, 80);
    }

    function syncVolume(level) {
        if (!sendspinStreaming && !mediaVolumeWritable) {
            document.getElementById('kiosk-volume').disabled = true;
            document.getElementById('k-volume-level').textContent = 'Device';
            document.getElementById('k-volume-panel-level').textContent = 'Device';
            document.getElementById('k-volume-button').setAttribute('aria-label', 'Use device volume buttons');
            return;
        }
        level = Math.max(0, Math.min(100, Math.round(level)));
        var slider = document.getElementById('kiosk-volume');
        slider.value = String(level);
        slider.style.setProperty('--progress', level + '%');
        slider.setAttribute('aria-valuetext', level + '%');
        document.getElementById('k-volume-level').textContent = level + '%';
        document.getElementById('k-volume-panel-level').textContent = level + '%';
        document.getElementById('k-volume-button').setAttribute('aria-label', 'Volume: ' + level + '%');
        var icon = level === 0 ? 'volume-x' : level <= 50 ? 'volume-1' : 'volume-2';
        ['k-volume-icon', 'k-volume-slider-icon'].forEach(function (id) {
            setIconHref(document.getElementById(id), '#k-icon-' + icon);
        });
    }

    function setVolumePanel(open) {
        volumePanelOpen = open;
        document.getElementById('kiosk-controls').dataset.volumeOpen = String(open);
        document.getElementById('k-volume-button').setAttribute('aria-expanded', String(open));
        if (open) document.getElementById('kiosk-volume').focus();
        showKioskControls();
    }

    function bindControls() {
        document.getElementById('kiosk-party').onclick = function () { setPartyExpanded(!partyExpanded); };
        document.getElementById('kiosk-party-expanded').onclick = function () {
            setPartyExpanded(false);
            document.getElementById('kiosk-party').focus();
        };
        document.getElementById('k-volume-button').onclick = function () { setVolumePanel(!volumePanelOpen); };
        document.getElementById('k-volume-close').onclick = function () { setVolumePanel(false); };
        document.addEventListener(window.PointerEvent ? 'pointerdown' : 'mousedown', function (e) {
            if (volumePanelOpen && !document.getElementById('kiosk-volume-control').contains(e.target)) setVolumePanel(false);
        });
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
        if (!window.PointerEvent) {
            ['mousedown', 'touchstart'].forEach(function (name) { kseek.addEventListener(name, function () { seekPointer = true; }); });
            ['mouseup', 'touchend', 'touchcancel'].forEach(function (name) { document.addEventListener(name, function () { seekPointer = false; }); });
        }
        kseek.oninput = function (e) {
            var t = (Number(e.target.value) / 1000) * vizDuration();
            document.getElementById('kiosk-time').textContent = fmt(t);
            e.target.style.setProperty('--progress', (Number(e.target.value) / 10) + '%');
        };
        kseek.onchange = function (e) {
            seekPointer = false;
            seekToSong((Number(e.target.value) / 1000) * vizDuration());
        };
        audio.ontimeupdate = function () {
            paintClock();
            if (!vizFrame) paintLyrics();
        };
        audio.addEventListener('loadedmetadata', applyPendingSeek);
        audio.addEventListener('canplay', applyPendingSeek);
        audio.onerror = function () {
            // MEDIA_ERR_ABORTED is a replaced or cleared source, not a failure.
            var code = audio.error && audio.error.code;
            if (!code || code === 1 || !audio.getAttribute('src')) return;
            reportPlaybackError(playbackGeneration, 'MediaError' + code);
        };
        audio.onended = function () {
            // The page often has no Music Assistant token, so next goes over
            // the player socket. The provider then advances the queue.
            if (isPrimeSource() || sendspinStreaming || !audio.ended) return;
            playing = false;
            sync();
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
        var key = normalizedKey(e);
        showKioskControls();
        if (key === 'Escape' && partyExpanded) {
            if (e.preventDefault) e.preventDefault();
            setPartyExpanded(false);
            document.getElementById('kiosk-party').focus();
            return;
        }
        if (key === 'Escape' && volumePanelOpen) {
            if (e.preventDefault) e.preventDefault();
            setVolumePanel(false);
            document.getElementById('k-volume-button').focus();
            return;
        }
        if (REMOTE && remoteKey(e, key)) return;
        if (/^(INPUT|BUTTON|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
        switch (key) {
            case ' ': e.preventDefault(); cmd(playing ? 'players/cmd/pause' : 'players/cmd/play').catch(function () {}); break;
            case 'ArrowRight': seekToSong(songTime() + 10); break;
            case 'ArrowLeft': seekToSong(Math.max(0, songTime() - 10)); break;
            case 'ArrowUp': e.preventDefault(); applyVolume(Number(document.getElementById('kiosk-volume').value) + 5, true); break;
            case 'ArrowDown': e.preventDefault(); applyVolume(Number(document.getElementById('kiosk-volume').value) - 5, true); break;
            case 'n': case 'N': cmd('players/cmd/next').catch(function () {}); break;
            case 'p': case 'P': cmd('players/cmd/previous').catch(function () {}); break;
        }
    });

    function normalizedKey(e) {
        if (e.key && e.key !== 'Unidentified') return e.key;
        return { 13:'Enter', 27:'Escape', 32:' ', 37:'ArrowLeft', 38:'ArrowUp', 39:'ArrowRight', 40:'ArrowDown', 10009:'Escape', 461:'Escape' }[e.keyCode] || '';
    }
    function remoteKey(e, key) {
        if (key === 'Enter') {
            if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return false;
            var button = e.target && e.target.tagName === 'BUTTON' ? e.target : document.getElementById(SHOW.controls ? 'k-play' : 'kiosk-party');
            if (button && !button.disabled && !button.classList.contains('hidden') && button.getClientRects().length) {
                e.preventDefault();
                button.focus();
                button.click();
                return true;
            }
        }
        if (!/^Arrow/.test(key)) return false;
        if (e.target && e.target.tagName === 'INPUT' && (key === 'ArrowLeft' || key === 'ArrowRight')) return false;
        var ids = ['k-fullscreen', 'kiosk-party', 'k-prev', 'k-play', 'k-next', 'k-volume-button', 'kiosk-seek', 'kiosk-volume', 'k-volume-close'];
        var candidates = ids.map(function (id) { return document.getElementById(id); }).filter(function (el) {
            return el && !el.disabled && !el.classList.contains('hidden') && el.getClientRects().length;
        });
        var currentFocus = document.activeElement;
        var currentRect = currentFocus && currentFocus.getBoundingClientRect();
        var selected = null;
        var best = Infinity;
        candidates.forEach(function (el) {
            if (el === currentFocus) return;
            if (!currentRect || candidates.indexOf(currentFocus) < 0) {
                if (!selected || el.id === 'k-play') selected = el;
                return;
            }
            var rect = el.getBoundingClientRect();
            var dx = rect.left + rect.width / 2 - currentRect.left - currentRect.width / 2;
            var dy = rect.top + rect.height / 2 - currentRect.top - currentRect.height / 2;
            var horizontal = key === 'ArrowLeft' || key === 'ArrowRight';
            var forward = horizontal ? dx : dy;
            if (key === 'ArrowLeft' || key === 'ArrowUp') forward = -forward;
            var score = forward + Math.abs(horizontal ? dy : dx) * 2;
            if (forward > 2 && score < best) { best = score; selected = el; }
        });
        e.preventDefault();
        if (selected) selected.focus();
        return true;
    }

    function bindFullscreen() {
        var root = document.documentElement;
        var button = document.getElementById('k-fullscreen');
        var apis = [
            ['requestFullscreen', 'exitFullscreen', 'fullscreenEnabled', 'fullscreenElement', 'fullscreenchange'],
            ['webkitRequestFullscreen', 'webkitExitFullscreen', 'webkitFullscreenEnabled', 'webkitFullscreenElement', 'webkitfullscreenchange'],
            ['mozRequestFullScreen', 'mozCancelFullScreen', 'mozFullScreenEnabled', 'mozFullScreenElement', 'mozfullscreenchange'],
            ['msRequestFullscreen', 'msExitFullscreen', 'msFullscreenEnabled', 'msFullscreenElement', 'MSFullscreenChange']
        ];
        var api = null;
        if (FLAG('fullscreen')) apis.some(function (candidate) {
            if (typeof root[candidate[0]] !== 'function' || typeof document[candidate[1]] !== 'function'
                || document[candidate[2]] === false) return false;
            api = candidate;
            return true;
        });
        button.classList.toggle('hidden', !api);
        root.classList.toggle('fullscreen-button', !!api);
        if (!api) return;
        function refresh() {
            var active = !!document[api[3]];
            var label = active ? 'Exit fullscreen' : 'Enter fullscreen';
            button.setAttribute('aria-label', label);
            button.setAttribute('aria-pressed', String(active));
            button.title = label;
            setIconHref(document.getElementById('k-fullscreen-icon'), active ? '#k-icon-minimize' : '#k-icon-maximize');
        }
        button.onclick = function () {
            try {
                var result = document[api[3]] ? document[api[1]]() : root[api[0]]();
                Promise.resolve(result).then(refresh, function () { setStatus('Fullscreen unavailable'); refresh(); });
            } catch (e) { setStatus('Fullscreen unavailable'); refresh(); }
        };
        document.addEventListener(api[4], refresh);
        refresh();
    }

    function getDefaultSendspinUrl() {
        return 'http://' + location.hostname + ':8927';
    }
    function supportsSendspin() {
        return typeof BigInt === 'function' && typeof DataView.prototype.getBigInt64 === 'function'
            && typeof AudioContext === 'function' && typeof TextEncoder === 'function'
            && typeof TextDecoder === 'function' && typeof crypto !== 'undefined'
            && typeof crypto.getRandomValues === 'function';
    }
    function useHtml5() {
        if (SENDSPIN && playerId) {
            pendingHtml5Fallback = true;
            sendWS({type:'sendspin_fallback'});
        }
        SENDSPIN = false;
        sendspinStreaming = false;
        sendspinOwnsPlayback = false;
        sendspinState = null;
        reportVolumeCapability();
        sendspinUnlocked = true;
        var failed = window.__sendspinPlayer;
        window.__sendspinPlayer = null;
        if (failed && typeof failed.disconnect === 'function') failed.disconnect();
        document.documentElement.classList.remove('sendspin');
        setSync('fallback');
        renderNow();
        maybeDisarmAudio();
        setStatus('');
    }
    function showPairPin(pin) {
        var el = document.getElementById('kiosk-pair');
        if (!el) return;
        if (!pin) { el.classList.add('hidden'); el.textContent = ''; return; }
        el.classList.remove('hidden');
        el.textContent = 'Pairing code ' + pin;
    }
    var sendspinLoad = null;
    function loadSendspinModule() {
        if (sendspinModule) return Promise.resolve(sendspinModule);
        if (sendspinLoad) return sendspinLoad;
        sendspinLoad = new Promise(function (resolve, reject) {
            var script = document.createElement('script');
            script.src = '/web/sendspin-js/index.js';
            script.onload = function () {
                sendspinModule = window.WebKioskSendspin;
                if (sendspinModule) resolve(sendspinModule);
                else reject(new Error('Sendspin SDK unavailable'));
            };
            script.onerror = function () { sendspinLoad = null; reject(new Error('Sendspin SDK could not load')); };
            document.head.appendChild(script);
        });
        return sendspinLoad;
    }
    function prepareSendspinIdentity() {
        return loadSendspinModule().then(function (module) {
            var identity = module.loadSendspinClientIdentity(sdkStorage);
            sendspinClientId = identity && identity.clientId ? identity.clientId : '';
        }).catch(function () {
            sendspinClientId = '';
            useHtml5();
        });
    }
    function probeWithTimeout(promise) {
        return new Promise(function (resolve) {
            var timer = setTimeout(function () { resolve(false); }, 1500);
            Promise.resolve(promise).then(function (supported) { clearTimeout(timer); resolve(!!supported); }, function () { clearTimeout(timer); resolve(false); });
        });
    }
    var codecProbe = null;
    function verifiedCodecs() {
        if (codecProbe) return codecProbe;
        var opus = Promise.resolve(false);
        if (window.isSecureContext && typeof AudioDecoder !== 'undefined' && typeof AudioDecoder.isConfigSupported === 'function') {
            opus = Promise.resolve().then(function () {
                return AudioDecoder.isConfigSupported({ codec:'opus', sampleRate:48000, numberOfChannels:2 });
            }).then(function (result) { return result.supported; });
        }
        var flac = Promise.resolve(false);
        if (typeof OfflineAudioContext !== 'undefined') {
            flac = fetch('/web/flac-probe.flac').then(function (response) {
                if (!response.ok) throw new Error('FLAC probe unavailable');
                return response.arrayBuffer();
            }).then(function (sample) {
                var context = new OfflineAudioContext(2, 1, 48000);
                return new Promise(function (resolve, reject) {
                    var decoding = context.decodeAudioData(sample, resolve, reject);
                    if (decoding && decoding.then) decoding.then(resolve, reject);
                });
            }).then(function (buffer) { return buffer && buffer.length > 0; });
        }
        codecProbe = Promise.all([probeWithTimeout(opus), probeWithTimeout(flac)]).then(function (results) {
            var codecs = [];
            if (results[0]) codecs.push('opus');
            if (results[1]) codecs.push('flac');
            codecs.push('pcm');
            return codecs;
        });
        return codecProbe;
    }
    function initSendspin() {
        if (!SENDSPIN || window.__sendspinPlayer) return;
        sendspinConnecting = true;
        setStatus('Connecting Sendspin…');
        return loadSendspinModule().then(function (module) {
            return verifiedCodecs().then(function (codecs) { return { module:module, codecs:codecs }; });
        }).then(function (verified) {
            var module = verified.module;
            var sendspinUrl = params.get('sendspin_url') || getDefaultSendspinUrl();
            var cfg = {
                baseUrl: sendspinUrl,
                clientName: 'Web Kiosk',
                productName: 'Web Kiosk / ' + browserModel,
                correctionMode: 'sync',
                storage: sdkStorage,
                codecs: verified.codecs,
                onStateChange: function (state) {
                    setSync('synced');
                    sendspinState = state;
                    if (sendspinStreaming || sendspinOwnsPlayback) applySendspinState(state);
                    if (sendspinStreaming && typeof state.volume === 'number') syncVolume(state.volume);
                },
                onPairingPin: function (pin) { showPairPin(pin); }
            };
            var player = new module.SendspinPlayer(cfg);
            window.__sendspinPlayer = player;
            wrapSendspinHooks(player.core);
            if (!sendspinUnlocked) {
                player.unlock().then(function () {
                    sendspinUnlocked = true;
                    maybeDisarmAudio();
                }).catch(function () { /* the next gesture tries again */ });
            }
            return player.connect();
        }).then(function () {
            sendspinConnecting = false;
            if (!SENDSPIN) return;
            setSync('synced');
            setStatus('');
        }).catch(function (e) {
            sendspinConnecting = false;
            useHtml5();
            document.getElementById('kiosk-sync').title = e.message || 'Sendspin unavailable';
        });
    }
    function applySendspinState(state) {
        var metadata = state.serverState && state.serverState.metadata || {};
        if (state.serverState) {
            var nextHasTrack = !!(metadata.title || metadata.artist || metadata.artwork_url);
            var next = {
                title: metadata.title || '',
                artist: metadata.artist || '',
                image: metadata.artwork_url || '',
                duration: metadata.progress && metadata.progress.track_duration / 1000 || 0,
                start: 0
            };
            var changed = hasTrack !== nextHasTrack || current.title !== next.title
                || current.artist !== next.artist || current.image !== next.image
                || current.duration !== next.duration;
            hasTrack = nextHasTrack;
            current = next;
            if (changed) renderNow();
        }
        playing = state.isPlaying;
        if (!hasTrack && !playing && !sendspinStreaming) {
            sendspinOwnsPlayback = false;
            lyricsLines = [];
            applyLyrics([]);
        }
        sync();
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
        chain('onStreamStart', function (format, isFormatUpdate) {
            if (isFormatUpdate) return;
            sendspinAudioAt = performance.now();
            sendspinReceivedAudio = false;
            waveAnchor = songTime();
            waveAnchorAt = performance.now();
            sendspinStreaming = true;
            sendspinOwnsPlayback = true;
            reportVolumeCapability();
            audio.pause();
            audio.removeAttribute('src');
            pendingSeek = null;
            playing = true;
            if (sendspinState) applySendspinState(sendspinState);
            sync();
            fetchQueue();
        });
        chain('onStreamEnd', function () {
            sendspinStreaming = false;
            reportVolumeCapability();
            if (audio.paused) { playing = false; sync(); }
        });
        chain('onStreamClear', function () {
            sendspinStreaming = false;
            reportVolumeCapability();
        });
        chain('onAudioData', function () { sendspinReceivedAudio = true; });
    }
    function setSync(state) {
        var el = document.getElementById('kiosk-sync');
        el.className = state;
        el.textContent = state === 'synced' ? 'SYNC' : state === 'fallback' ? 'HTML5' : state === 'error' ? 'ERROR' : 'SYNCING…';
    }

    function boot() {
        document.body.classList.add('kiosk');
        bindControls();
        bindFullscreen();
        renderNow();
        if (REQUESTED_SENDSPIN && !SENDSPIN) setSync('fallback');
        sync();
        syncVolume(audio.volume * 100);
        prepareAudioUnlock();
        var ready = SENDSPIN ? prepareSendspinIdentity() : Promise.resolve();
        ready.then(function () {
        bootReady = true;
        connectWS();
        refreshWakeLock();
        setInterval(function () { if (sendspinStreaming) paintClock(); }, 500);
        if (SENDSPIN) setInterval(function () {
            if (sendspinStreaming && !sendspinReceivedAudio && sendspinUnlocked && playing && performance.now() - sendspinAudioAt > 10000) useHtml5();
        }, 1000);
        if (SHOW.party) {
            fetchParty();
            setInterval(fetchParty, 10000);
        }
        if (SHOW.queue && TOKEN) setInterval(fetchQueue, 15000);
        if (SHOW.controls) {
            document.addEventListener('mousemove', showKioskControls);
            document.addEventListener('touchstart', showKioskControls, { passive: true });
            showKioskControls();
        }
        });
    }
    boot();
})();
