(function () {
    'use strict';
    var output = document.getElementById('url');
    function refresh() {
        var displayEl = document.getElementById('display');
        var profileEl = document.getElementById('profile');
        var display = !!(displayEl && displayEl.checked);
        var profile = profileEl ? profileEl.value : '';
        var target = new URL(display ? '/view' : '/kiosk', output.dataset.base);
        if (profile) target.searchParams.set('profile', profile);
        if (display) {
            target.searchParams.set('display', '1');
            var displayPlayerEl = document.getElementById('display_player');
            var displayPlayer = displayPlayerEl ? displayPlayerEl.value.trim() : '';
            if (displayPlayer) target.searchParams.set('player', displayPlayer);
            else target.searchParams.set('party', '1');
        } else if (document.querySelector('input[name="mode"]:checked').value === 'html5') {
            target.searchParams.set('sendspin', '0');
        }
        ['controls', 'party', 'viz', 'lyrics', 'queue', 'show_player_id', 'show_group', 'remote', 'lite', 'wake_lock', 'fullscreen', 'lock'].forEach(function (name) {
            var option = document.getElementById(name);
            if (option && option.checked) target.searchParams.set(name, '1');
        });
        if (!display) {
            ['device_id', 'sendspin_url'].forEach(function (name) {
                var value = document.getElementById(name).value.trim();
                if (value) target.searchParams.set(name, value);
            });
            var token = document.getElementById('token').value.trim();
            if (token) target.hash = 'token=' + encodeURIComponent(token);
        }
        output.value = target.toString();
    }
    document.addEventListener('input', refresh);
    document.addEventListener('change', refresh);
    document.getElementById('copy').onclick = function () {
        output.select();
        if (navigator.clipboard) navigator.clipboard.writeText(output.value).catch(function () {});
        else document.execCommand('copy');
    };
    document.getElementById('open').onclick = function () { window.open(output.value, '_blank', 'noopener'); };
    refresh();
})();
