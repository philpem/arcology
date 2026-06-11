/* Arcology site-wide helpers (moved from the inline <script> in _base.html). */

function copyToClipboard(text, btn) {
    navigator.clipboard.writeText(text).then(function() {
        var icon = btn.querySelector('i');
        icon.className = 'bi bi-clipboard-check';
        setTimeout(function() { icon.className = 'bi bi-clipboard'; }, 1500);
    });
}
