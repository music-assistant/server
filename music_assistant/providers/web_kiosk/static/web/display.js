/* Web Kiosk display. Shows a Music Assistant player; it never plays audio. */
(function () {
    'use strict';

    var params = new URLSearchParams(location.search);
    var PROFILE = params.get('profile') || '';
    var PROFILE_DEFAULTS = {
        tv: { remote: 1, lite: 1, wake_lock: 1, fullscreen: 1, queue: 1, lyrics: 1 },
        wall: { fullscreen: 1, queue: 1, lyrics: 1, wake_lock: 1, show_group: 1 },
        guest: { lyrics: 1, party: 1, queue: 1 }
    };
    function want(name) {
        if (params.has(name)) return params.get(name) === '1';
        return !!(PROFILE_DEFAULTS[PROFILE] && PROFILE_DEFAULTS[PROFILE][name]);
    }
    var LYRICS_MODE = params.get('lyrics') || '';
    var SHOW = {
        lyrics: LYRICS_MODE === 'full' || want('lyrics'),
        queue: want('queue'),
        party: want('party'),
        group: want('show_group'),
        playerId: want('show_player_id')
    };
    var LITE = want('lite');
    var FULLSCREEN = want('fullscreen');
    var LOCK = want('lock');
    var KEEP_AWAKE = want('wake_lock');
    var REMOTE = want('remote');
    var PLAYER = params.get('player') || '';
    var PARTY = params.get('party') === '1' || !PLAYER;

    document.documentElement.classList.toggle('lite', LITE);
    document.documentElement.classList.toggle('tv', PROFILE === 'tv');
    document.documentElement.classList.toggle('wall', PROFILE === 'wall');
    document.documentElement.classList.toggle('guest', PROFILE === 'guest');
    document.documentElement.classList.toggle('lyrics-full', LYRICS_MODE === 'full' || params.get('full') === '1');

    var socket = null;
    var reconnectTimer = null;
    var playerId = '';
    var customName = '';
    var serverVersion = '';
    var groupName = '';
    var media = null;
    var duration = 0;
    var elapsed = 0;
    var speed = 1;
    var playing = false;
    var stateAt = 0;
    var lyricsLines = [];
    var partyVersion = '';
    var partyExpanded = false;
    var wakeLock = null;
    var wakePending = false;
    var ticker = null;
    var partyTimer = null;

    function $(id) { return document.getElementById(id); }
    function esc(value) { var d = document.createElement('div'); d.textContent = value == null ? '' : String(value); return d.innerHTML; }
    function replace(el, child) { while (el.firstChild) el.removeChild(el.firstChild); if (child) el.appendChild(child); }
    function setStatus(text) {
        var el = $('view-status');
        el.textContent = text || '';
        el.classList.toggle('hidden', !text);
    }
    function fmt(seconds) {
        if (!isFinite(seconds) || seconds < 0) seconds = 0;
        var s = Math.floor(seconds % 60);
        var m = Math.floor(seconds / 60) % 60;
        var h = Math.floor(seconds / 3600);
        return (h ? h + ':' + (m < 10 ? '0' : '') + m : m) + ':' + (s < 10 ? '0' : '') + s;
    }
    function imageUrl(value) {
        if (!value) return '';
        var img = String(value);
        if (img.indexOf('http') === 0 || img.indexOf('/') === 0) return img;
        return '/imageproxy/' + img;
    }
    function setArt(url) {
        var box = $('view-artbox');
        var art = $('view-art');
        var bg = $('view-bg-img');
        if (!url) {
            box.classList.remove('has-art');
            art.removeAttribute('src');
            bg.removeAttribute('src');
            bg.style.opacity = '0';
            return;
        }
        art.onload = function () { if (art.getAttribute('src') === url) { box.classList.add('has-art'); bg.style.opacity = '1'; } };
        art.onerror = function () { if (art.getAttribute('src') === url) { box.classList.remove('has-art'); bg.style.opacity = '0'; } };
        art.src = url;
        bg.src = url;
        art.alt = '';
        bg.alt = '';
    }
    function songPosition() {
        var pos = elapsed;
        if (playing && stateAt) pos += ((Date.now() - stateAt) / 1000) * (speed > 0 ? speed : 1);
        if (duration > 0 && pos > duration) pos = duration;
        return pos < 0 ? 0 : pos;
    }
    function paintClock() {
        var pos = songPosition();
        $('view-elapsed').textContent = fmt(pos);
        $('view-duration').textContent = duration > 0 ? fmt(duration) : '0:00';
        var bar = $('view-progress-bar').firstChild;
        bar.style.width = (duration > 0 ? Math.min(100, (pos / duration) * 100) : 0) + '%';
        paintLyrics(pos);
    }
    function paintLyrics(pos) {
        var el = $('view-lyrics');
        if (!SHOW.lyrics || !lyricsLines.length) {
            el.classList.add('hidden');
            return;
        }
        el.classList.remove('hidden');
        var timed = false;
        var i;
        for (i = 0; i < lyricsLines.length; i++) {
            if (typeof lyricsLines[i].t === 'number') { timed = true; break; }
        }
        var idx = -1;
        if (timed) {
            for (i = 0; i < lyricsLines.length; i++) {
                if (typeof lyricsLines[i].t !== 'number') continue;
                if (lyricsLines[i].t <= pos) idx = i;
                else break;
            }
        } else if (duration > 0) {
            idx = Math.floor((pos / duration) * lyricsLines.length);
            if (idx >= lyricsLines.length) idx = lyricsLines.length - 1;
        }
        var full = document.documentElement.classList.contains('lyrics-full');
        var count = full ? 5 : 3;
        var start = Math.max(0, idx - 1);
        var end = Math.min(lyricsLines.length, start + count);
        var fragment = document.createDocumentFragment();
        for (i = start; i < end; i++) {
            var line = document.createElement('p');
            line.className = 'l' + (i === idx ? ' current' : '');
            line.textContent = lyricsLines[i].text || '';
            fragment.appendChild(line);
        }
        replace(el, fragment);
    }
    function renderQueue(items) {
        var el = $('view-queue');
        document.documentElement.classList.toggle('queue-active', !!(SHOW.queue && items.length));
        if (!SHOW.queue || !items.length) {
            el.classList.add('hidden');
            return;
        }
        el.classList.remove('hidden');
        var fragment = document.createDocumentFragment();
        items.forEach(function (item) {
            var row = document.createElement('div');
            row.className = 'qrow' + (item.is_current ? ' active' : '');
            if (item.image_url) {
                var img = document.createElement('img');
                img.src = imageUrl(item.image_url);
                img.alt = '';
                row.appendChild(img);
            }
            var meta = document.createElement('div');
            meta.className = 'qmeta';
            var title = document.createElement('div');
            title.className = 'qt';
            title.textContent = item.title || '';
            var artist = document.createElement('div');
            artist.className = 'qa';
            artist.textContent = item.artist || '';
            meta.appendChild(title);
            meta.appendChild(artist);
            row.appendChild(meta);
            if (item.party_boosted || item.party_guest) {
                var badge = document.createElement('span');
                badge.className = 'qbadge' + (item.party_boosted ? ' boost' : '');
                badge.textContent = item.party_boosted ? 'Boost' : 'Guest';
                row.appendChild(badge);
            }
            fragment.appendChild(row);
        });
        replace(el, fragment);
    }
    function renderState(msg) {
        playerId = msg.player_id || playerId;
        customName = typeof msg.custom_name === 'string' ? msg.custom_name : customName;
        groupName = typeof msg.group_name === 'string' ? msg.group_name : '';
        playing = msg.playback_state === 'playing';
        speed = typeof msg.playback_speed === 'number' && msg.playback_speed > 0 ? msg.playback_speed : 1;
        elapsed = typeof msg.elapsed === 'number' ? msg.elapsed : 0;
        duration = typeof msg.duration === 'number' ? msg.duration : 0;
        stateAt = Date.now();
        media = msg.media && typeof msg.media === 'object' ? msg.media : null;
        var title = media && media.title ? media.title : (customName || playerId || 'Web Kiosk');
        var artist = media && media.artist ? media.artist : (media ? '' : 'Nothing playing');
        $('view-title').textContent = title;
        $('view-artist').textContent = artist;
        var group = $('view-group');
        group.textContent = groupName || '';
        group.classList.toggle('hidden', !SHOW.group || !groupName);
        var label = $('view-player-id');
        label.textContent = playerId ? 'Player ID: ' + playerId : '';
        label.classList.toggle('hidden', !SHOW.playerId || !playerId);
        setArt(media && media.image_url ? imageUrl(media.image_url) : '');
        if (media && media.duration && !duration) duration = media.duration;
        paintClock();
    }
    function renderIdle() {
        media = null;
        duration = 0;
        elapsed = 0;
        playing = false;
        stateAt = Date.now();
        $('view-title').textContent = customName || playerId || 'Web Kiosk';
        $('view-artist').textContent = 'Nothing playing';
        setArt('');
        paintClock();
    }
    function handleMessage(msg) {
        if (!msg || typeof msg.type !== 'string') return;
        switch (msg.type) {
            case 'welcome':
                playerId = msg.player_id || '';
                customName = typeof msg.custom_name === 'string' ? msg.custom_name : '';
                serverVersion = msg.server_version || '';
                if (!media) $('view-artist').textContent = 'Music Assistant' + (serverVersion ? ' ' + serverVersion : '');
                setStatus('');
                break;
            case 'display_state':
                renderState(msg);
                break;
            case 'queue':
                renderQueue(Array.isArray(msg.items) ? msg.items : []);
                break;
            case 'lyrics':
                lyricsLines = Array.isArray(msg.lines) ? msg.lines : [];
                paintClock();
                break;
            case 'stop':
                renderIdle();
                break;
            default:
                break;
        }
    }
    function connect() {
        if (socket && socket.readyState <= WebSocket.OPEN) return;
        clearTimeout(reconnectTimer);
        var proto = location.protocol === 'https:' ? 'wss' : 'ws';
        var qs = 'display=1';
        if (PLAYER) qs += '&player=' + encodeURIComponent(PLAYER);
        if (PARTY) qs += '&party=1';
        socket = new WebSocket(proto + '://' + location.host + '/ws?' + qs);
        socket.onmessage = function (event) {
            try { handleMessage(JSON.parse(event.data)); } catch (e) { /* ignore malformed frames */ }
        };
        socket.onclose = function () {
            socket = null;
            setStatus('Reconnecting…');
            reconnectTimer = setTimeout(connect, 2000);
        };
        socket.onerror = function () { try { socket.close(); } catch (e) { /* noop */ } };
    }
    function fetchParty() {
        if (!SHOW.party) return;
        fetch('/api/party', { cache: 'no-store' }).then(function (res) {
            if (!res.ok) throw new Error('no party');
            return res.json();
        }).then(function (data) {
            if (!data || !data.active) {
                hideParty();
                return;
            }
            if (!data.guest_access) {
                hideParty();
                setStatus('Guest requests are paused');
                return;
            }
            setStatus('');
            partyVersion = data.version || '';
            $('view-party-caption').textContent = data.qr_text || data.name || 'Join the party';
            $('view-party-art').src = '/api/party/qr.svg?v=' + encodeURIComponent(partyVersion);
            $('view-party-expanded-art').src = '/api/party/qr.svg?v=' + encodeURIComponent(partyVersion);
            $('view-party').classList.remove('hidden');
            if (partyExpanded) $('view-party-expanded').classList.remove('hidden');
        }).catch(function () { hideParty(); });
    }
    function hideParty() {
        $('view-party').classList.add('hidden');
        $('view-party').dataset.version = '';
        setPartyExpanded(false);
    }
    function setPartyExpanded(open) {
        partyExpanded = !!open && !!partyVersion;
        $('view-party-expanded').classList.toggle('hidden', !partyExpanded);
        $('view-party').setAttribute('aria-expanded', String(partyExpanded));
    }
    function bindParty() {
        $('view-party').onclick = function () { setPartyExpanded(!partyExpanded); };
        $('view-party').onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setPartyExpanded(!partyExpanded); } };
        $('view-party-expanded').onclick = function () { setPartyExpanded(false); };
        $('view-party-expanded').onkeydown = function (e) { if (e.key === 'Enter' || e.key === ' ' || e.key === 'Escape') { e.preventDefault(); setPartyExpanded(false); } };
    }
    function bindFullscreen() {
        var root = document.documentElement;
        var button = $('view-fullscreen');
        var apis = [
            ['requestFullscreen', 'exitFullscreen', 'fullscreenEnabled', 'fullscreenElement', 'fullscreenchange'],
            ['webkitRequestFullscreen', 'webkitExitFullscreen', 'webkitFullscreenEnabled', 'webkitFullscreenElement', 'webkitfullscreenchange'],
            ['mozRequestFullScreen', 'mozCancelFullScreen', 'mozFullScreenEnabled', 'mozFullScreenElement', 'mozfullscreenchange'],
            ['msRequestFullscreen', 'msExitFullscreen', 'msFullscreenEnabled', 'msFullscreenElement', 'MSFullscreenChange']
        ];
        var api = null;
        if (FULLSCREEN || LOCK) apis.some(function (candidate) {
            if (typeof root[candidate[0]] !== 'function' || typeof document[candidate[1]] !== 'function'
                || document[candidate[2]] === false) return false;
            api = candidate;
            return true;
        });
        button.classList.toggle('hidden', !api || LOCK);
        if (!api) return;
        function refresh() {
            var active = !!document[api[3]];
            button.setAttribute('aria-label', active ? 'Exit fullscreen' : 'Enter fullscreen');
            button.title = active ? 'Exit fullscreen' : 'Enter fullscreen';
            button.setAttribute('aria-pressed', String(active));
        }
        button.onclick = function () {
            try {
                var result = document[api[3]] ? document[api[1]]() : root[api[0]]();
                Promise.resolve(result).then(refresh, function () { setStatus('Fullscreen unavailable'); });
            } catch (e) { setStatus('Fullscreen unavailable'); }
        };
        document.addEventListener(api[4], refresh);
        if (LOCK) {
            var entered = false;
            var enter = function () {
                if (entered || document[api[3]]) return;
                entered = true;
                try {
                    Promise.resolve(root[api[0]]()).then(refresh, function () { entered = false; });
                } catch (e) { entered = false; }
            };
            document.addEventListener('pointerdown', enter, { once: true });
            document.addEventListener('keydown', enter, { once: true });
        }
        refresh();
    }
    function refreshWakeLock() {
        if (!KEEP_AWAKE || !('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
        if (wakeLock || wakePending) return;
        wakePending = true;
        navigator.wakeLock.request('screen').then(function (lock) {
            wakePending = false;
            wakeLock = lock;
            lock.addEventListener('release', function () { if (wakeLock === lock) wakeLock = null; });
        }).catch(function () { wakePending = false; });
    }
    function bindWakeLock() {
        if (!KEEP_AWAKE) return;
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') refreshWakeLock();
        });
        document.addEventListener('pointerdown', refreshWakeLock, { once: true });
        document.addEventListener('keydown', refreshWakeLock, { once: true });
        refreshWakeLock();
    }
    function bindKeys() {
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && partyExpanded) {
                e.preventDefault();
                setPartyExpanded(false);
            }
            if (REMOTE && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
                var queue = $('view-queue');
                if (!queue.classList.contains('hidden')) {
                    e.preventDefault();
                    queue.scrollTop += (e.key === 'ArrowDown' ? 40 : -40);
                }
            }
        });
    }
    function boot() {
        bindParty();
        bindFullscreen();
        bindWakeLock();
        bindKeys();
        ticker = setInterval(paintClock, 500);
        if (SHOW.party) {
            fetchParty();
            partyTimer = setInterval(fetchParty, 10000);
        }
        connect();
    }
    boot();
})();
