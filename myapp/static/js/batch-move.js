(function () {
    'use strict';

    const toolbar = document.querySelector('[data-batch-move-toolbar]');
    if (!toolbar) return;

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
        selected = new Set(JSON.parse(sessionStorage.getItem(storageKey) || '[]'));
    } catch (_error) {
        selected = new Set();
    }

    function save() {
        const ids = Array.from(selected);
        try { sessionStorage.setItem(storageKey, JSON.stringify(ids)); } catch (_error) { /* no-op */ }
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
