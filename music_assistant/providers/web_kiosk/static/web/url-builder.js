(function () {
    'use strict';
    var output = document.getElementById('url');
    function refresh() {
        var target = new URL('/kiosk', output.dataset.base);
        var mode = document.querySelector('input[name="mode"]:checked').value;
        if (mode === 'html5') target.searchParams.set('sendspin', '0');
        ['controls', 'party', 'viz', 'lyrics', 'queue', 'show_player_id'].forEach(function (name) {
            if (document.getElementById(name).checked) target.searchParams.set(name, '1');
        });
        ['ma_url', 'device_id'].forEach(function (name) {
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
