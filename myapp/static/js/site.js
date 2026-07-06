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

/* Navbar search dropdown (wide screens): focus the field when it opens, and let
   hover open it as a convenience alongside click / keyboard. */
document.addEventListener('DOMContentLoaded', function() {
    var toggle = document.getElementById('navbarSearchToggle');
    if (!toggle || typeof bootstrap === 'undefined' || !bootstrap.Dropdown) {
        return;
    }
    var item = toggle.closest('.nav-item');
    var input = item ? item.querySelector('[data-navbar-search-input]') : null;
    var dd = bootstrap.Dropdown.getOrCreateInstance(toggle);

    function focusInput() { if (input) { input.focus(); input.select(); } }

    // Clicking / tapping the icon always OPENS (never toggles shut).  A click is
    // always preceded by a hover, so letting Bootstrap toggle would close what
    // the hover just opened.  Swallow the toggle in the capture phase (before it
    // reaches Bootstrap's document handler) and just show + focus.  Escape, an
    // outside click, and touch still work because the toggle keeps
    // data-bs-toggle="dropdown".
    toggle.addEventListener('click', function(e) {
        e.preventDefault();
        e.stopPropagation();
        dd.show();
        focusInput();
    }, true);
    toggle.addEventListener('shown.bs.dropdown', focusInput);
    if (item) {
        item.addEventListener('mouseenter', function() { dd.show(); });
        item.addEventListener('mouseleave', function() {
            // Don't snap shut while the user is (or was) typing.
            if (input && (document.activeElement === input || input.value)) {
                return;
            }
            dd.hide();
        });
    }
});
