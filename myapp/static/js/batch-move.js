(function () {
    'use strict';

    const toolbar = document.querySelector('[data-batch-move-toolbar]');
    if (!toolbar) return;

    // A selection is discarded once it is this old, so a stale cross-page
    // pick cannot silently linger (the server would otherwise reject it at
    // confirm time with a confusing message).
    const selectionTtlMs = 6 * 60 * 60 * 1000;
    const storageKey = `arcology.batch-move.${toolbar.dataset.itemUuid}`;
    if (toolbar.dataset.clearSource) {
        try {
            sessionStorage.removeItem(`arcology.batch-move.${toolbar.dataset.clearSource}`);
        } catch (_error) { /* no-op */ }
    }
    const toggle = document.querySelector('[data-batch-move-toggle]');
    const wraps = Array.from(document.querySelectorAll('[data-batch-move-checkbox-wrap]'));
    const checkboxes = Array.from(document.querySelectorAll('[data-batch-move-checkbox]'));
    const pageCheckbox = toolbar.querySelector('[data-batch-move-page]');
    const count = toolbar.querySelector('[data-batch-move-count]');
    const values = toolbar.querySelector('[data-batch-move-values]');
    const submit = toolbar.querySelector('[data-batch-move-submit]');
    const clear = toolbar.querySelector('[data-batch-move-clear]');
    let active = false;
    let selected;

    try {
        const stored = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
        if (Array.isArray(stored)) {
            // Legacy shape: a bare array of UUIDs with no timestamp.
            selected = new Set(stored);
        } else if (stored && Array.isArray(stored.ids)
                   && Date.now() - Number(stored.savedAt) < selectionTtlMs) {
            selected = new Set(stored.ids);
        } else {
            selected = new Set();
            sessionStorage.removeItem(storageKey);
        }
    } catch (_error) {
        selected = new Set();
    }

    function save() {
        const ids = Array.from(selected);
        try {
            sessionStorage.setItem(storageKey, JSON.stringify({ids, savedAt: Date.now()}));
        } catch (_error) { /* no-op */ }
        values.value = JSON.stringify(ids);
        count.textContent = String(ids.length);
        submit.disabled = ids.length === 0;
        checkboxes.forEach((checkbox) => { checkbox.checked = selected.has(checkbox.value); });
        const checkedHere = checkboxes.filter((checkbox) => checkbox.checked).length;
        pageCheckbox.checked = checkboxes.length > 0 && checkedHere === checkboxes.length;
        pageCheckbox.indeterminate = checkedHere > 0 && checkedHere < checkboxes.length;
    }

    function setActive(value) {
        active = value;
        toolbar.classList.toggle('d-none', !active);
        wraps.forEach((wrap) => wrap.classList.toggle('d-none', !active));
        toggle.classList.toggle('active', active);
        save();
    }

    toggle.addEventListener('click', () => setActive(!active));
    checkboxes.forEach((checkbox) => checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(checkbox.value);
        else selected.delete(checkbox.value);
        save();
    }));
    pageCheckbox.addEventListener('change', () => {
        checkboxes.forEach((checkbox) => {
            if (pageCheckbox.checked) selected.add(checkbox.value);
            else selected.delete(checkbox.value);
        });
        save();
    });
    clear.addEventListener('click', () => {
        selected.clear();
        save();
    });

    if (selected.size) setActive(true);
    else save();
}());

// vim: ts=4 sw=4 et
