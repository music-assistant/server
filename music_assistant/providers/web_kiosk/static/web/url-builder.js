(function () {
    'use strict';
    var output = document.getElementById('url');
    function refresh() {
        var target = new URL('/kiosk', output.dataset.base);
        var mode = document.querySelector('input[name="mode"]:checked').value;
        if (mode === 'html5') target.searchParams.set('sendspin', '0');
        ['controls', 'party', 'viz', 'lyrics', 'queue', 'show_player_id', 'show_group', 'remote', 'lite', 'wake_lock', 'fullscreen'].forEach(function (name) {
            var option = document.getElementById(name);
            if (option && option.checked) target.searchParams.set(name, '1');
        });
        ['device_id', 'sendspin_url'].forEach(function (name) {
            var value = document.getElementById(name).value.trim();
            if (value) target.searchParams.set(name, value);
        });
        var token = document.getElementById('token').value.trim();
        if (token) target.hash = 'token=' + encodeURIComponent(token);
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
