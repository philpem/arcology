/* Arcology site-wide helpers (moved from the inline <script> in _base.html). */

function copyToClipboard(text, btn) {
    navigator.clipboard.writeText(text).then(function() {
        var icon = btn.querySelector('i');
        icon.className = 'bi bi-clipboard-check';
        setTimeout(function() { icon.className = 'bi bi-clipboard'; }, 1500);
    });
}

/* Opt-in Bootstrap tooltips (e.g. the navbar storage chip). */
document.addEventListener('DOMContentLoaded', function() {
    if (typeof bootstrap === 'undefined' || !bootstrap.Tooltip) {
        return;
    }
    document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function(el) {
        bootstrap.Tooltip.getOrCreateInstance(el);
    });
});
