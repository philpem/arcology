/* Arcology artefact file-listing behaviour.
 *
 * Four independent features, each active only when its DOM is present:
 *   - Hash DB mode: file selection bar + add-to-database modal
 *     (product list read from the #hashdb-product-cache JSON island)
 *   - File restriction modal (CSRF token read from the modal's add form;
 *     per-file action URL from the trigger button's data-restrictions-url)
 *   - Directory tree panel (lazy-loaded from #dir-tree-panel's data-tree-url)
 *   - Archive details modal
 *
 * Extracted verbatim from templates/artefacts/_file_listing.html.
 */

/* ── Hash DB mode: selection bar + add-to-database modal ────────────────── */
(function() {
    const bar = document.getElementById('hashdb-bar');
    if (!bar) return;  // not in hashdb mode

    // Products per database, pre-loaded from the server via a JSON island.
    const cacheEl = document.getElementById('hashdb-product-cache');
    const productCache = cacheEl ? JSON.parse(cacheEl.textContent) : {};

    function updateBar() {
        const checkboxes = document.querySelectorAll('.file-checkbox:checked');
        const count = checkboxes.length;
        document.getElementById('selected-count').textContent = count;
        document.getElementById('modal-file-count').textContent = count;
        const btn = document.getElementById('add-to-db-btn');
        if (count > 0) {
            bar.style.removeProperty('display');
        } else {
            bar.style.display = 'none';
        }
        btn.disabled = count === 0;
    }

    // Select all on page
    const selectAll = document.getElementById('select-all-files');
    if (selectAll) {
        selectAll.addEventListener('change', function() {
            document.querySelectorAll('.file-checkbox').forEach(cb => cb.checked = this.checked);
            updateBar();
        });
    }

    document.querySelectorAll('.file-checkbox').forEach(cb => {
        cb.addEventListener('change', updateBar);
    });

    document.getElementById('deselect-all-btn')?.addEventListener('click', function() {
        document.querySelectorAll('.file-checkbox').forEach(cb => cb.checked = false);
        if (selectAll) selectAll.checked = false;
        updateBar();
    });

    // Load products when database changes
    const dbSelect = document.getElementById('modal-database-id');
    const productSelect = document.getElementById('modal-product-id');
    const newProductFields = document.getElementById('new-product-fields');
    const newDatabaseFields = document.getElementById('new-database-fields');
    const newDatabaseName = document.getElementById('new-database-name');

    if (dbSelect) {
        dbSelect.addEventListener('change', function() {
            const dbId = this.value;
            if (!dbId) {
                productSelect.innerHTML = '<option value="">— select a database first —</option>';
                newProductFields.classList.add('d-none');
                newDatabaseFields.classList.add('d-none');
                newDatabaseName.required = false;
                return;
            }
            if (dbId === 'new') {
                newDatabaseFields.classList.remove('d-none');
                newDatabaseName.required = true;
                // New DB means new product is required
                populateProducts([]);
                return;
            }
            newDatabaseFields.classList.add('d-none');
            newDatabaseName.required = false;
            populateProducts(productCache[dbId] || []);
        });
    }

    function populateProducts(products) {
        productSelect.innerHTML = '<option value="">— New product… —</option>';
        products.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.title;
            productSelect.appendChild(opt);
        });
        // Auto-show new product fields when "New product…" is the only/selected option
        if (productSelect.value === '') {
            newProductFields.classList.remove('d-none');
        } else {
            newProductFields.classList.add('d-none');
        }
    }

    if (productSelect) {
        productSelect.addEventListener('change', function() {
            if (this.value === '') {
                newProductFields.classList.remove('d-none');
            } else {
                newProductFields.classList.add('d-none');
            }
        });
    }
})();

/* ── File restriction management modal ──────────────────────────────────── */
(function() {
    function htmlEsc(s) {
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    var modal = document.getElementById('fileRestrictModal');
    if (!modal) return;

    var editSection = document.getElementById('frm-edit-section');
    var editForm    = document.getElementById('frm-edit-form');
    var editLabel   = document.getElementById('frm-edit-label');
    var editOldCat  = document.getElementById('frm-edit-old-category');
    var editNewCat  = document.getElementById('frm-edit-new-category');
    var editReason  = document.getElementById('frm-edit-reason');
    // CSRF token for the dynamically built remove-forms — same token the
    // server already rendered into the modal's static add form.
    var csrfToken   = document.querySelector('#frm-add-form input[name="csrf_token"]').value;

    document.getElementById('frm-edit-cancel').addEventListener('click', function() {
        editSection.style.display = 'none';
    });

    modal.addEventListener('show.bs.modal', function(event) {
        var btn = event.relatedTarget;
        // Server-rendered url_for() URL — no client-side URL construction.
        var restrUrl = btn.getAttribute('data-restrictions-url');
        var name = btn.getAttribute('data-file-name');
        var restrictions = JSON.parse(btn.getAttribute('data-restrictions') || '[]');

        document.getElementById('frm-filename').textContent = name;
        document.getElementById('frm-add-form').action = restrUrl;
        editForm.action = restrUrl;
        editSection.style.display = 'none';

        var listEl = document.getElementById('frm-current-list');
        if (restrictions.length === 0) {
            listEl.innerHTML = '<p class="text-muted small mb-0">No restrictions set on this file.</p>';
        } else {
            var html = '<p class="text-muted small mb-1">Current restrictions:</p>';
            restrictions.forEach(function(r) {
                var rval = r.v, rlabel = r.l, rreason = r.r, rcanEdit = r.e;
                var reasonAttr = rreason ? ' title="' + htmlEsc(rreason) + '"' : '';
                html += '<div class="d-flex align-items-center gap-1 mb-1">'
                      + '<form method="POST" action="' + htmlEsc(restrUrl) + '" class="d-inline">'
                      + '<input type="hidden" name="csrf_token" value="' + htmlEsc(csrfToken) + '">'
                      + '<input type="hidden" name="action" value="remove">'
                      + '<input type="hidden" name="category" value="' + htmlEsc(rval) + '">'
                      + '<button type="submit" class="btn btn-sm btn-outline-danger" title="Remove">'
                      + '<i class="bi bi-x-circle"></i> ' + htmlEsc(rlabel)
                      + '</button></form>';
                if (rreason) {
                    html += '<span class="text-muted small"' + reasonAttr + ' style="cursor:help">'
                          + '<i class="bi bi-info-circle"></i></span>';
                }
                if (rcanEdit) {
                    html += '<button type="button" class="btn btn-link btn-sm p-0 text-secondary frm-edit-btn"'
                          + ' data-val="' + htmlEsc(rval) + '" data-label="' + htmlEsc(rlabel) + '" data-reason="' + htmlEsc(rreason) + '"'
                          + ' title="Edit restriction"><i class="bi bi-pencil"></i></button>';
                }
                html += '</div>';
            });
            listEl.innerHTML = html;

            listEl.querySelectorAll('.frm-edit-btn').forEach(function(btn) {
                btn.addEventListener('click', function() {
                    editOldCat.value = this.dataset.val;
                    editNewCat.value = this.dataset.val;
                    editReason.value = this.dataset.reason;
                    editLabel.textContent = this.dataset.label;
                    editSection.style.display = '';
                    editSection.scrollIntoView({behavior: 'smooth', block: 'nearest'});
                });
            });
        }
    });
})();

/* ── Directory tree toggle ──────────────────────────────────────────────── */
(function () {
    var toggle = document.getElementById('dir-tree-toggle');
    if (!toggle) return;
    var panel  = document.getElementById('dir-tree-panel');
    var TREE_URL = panel.dataset.treeUrl;
    var loaded = false;
    var loading = false;

    // ── Collapse / expand helpers ────────────────────────────────────────────

    var SS_KEY = 'arcology_dirtree_expanded';

    function getExpandedPaths() {
        try { return new Set(JSON.parse(sessionStorage.getItem(SS_KEY) || '[]')); } catch (e) { return new Set(); }
    }

    function saveExpandedState() {
        var expanded = [];
        panel.querySelectorAll('.dir-tree-expand.expanded').forEach(function (btn) {
            var link = btn.closest('li') && btn.closest('li').querySelector('.dir-tree-link[data-path]');
            if (link) { expanded.push(link.dataset.path); }
        });
        try { sessionStorage.setItem(SS_KEY, JSON.stringify(expanded)); } catch (e) {}
    }

    function expandNode(btn) {
        var li = btn.closest('li');
        var ul = li && li.querySelector('.dir-tree-children');
        if (!ul) { return; }
        btn.classList.add('expanded');
        ul.style.display = '';
    }

    function collapseNode(btn) {
        var li = btn.closest('li');
        var ul = li && li.querySelector('.dir-tree-children');
        if (!ul) { return; }
        btn.classList.remove('expanded');
        ul.style.display = 'none';
    }

    function restoreExpandedState(expandedPaths) {
        panel.querySelectorAll('.dir-tree-expand').forEach(function (btn) {
            var link = btn.closest('li') && btn.closest('li').querySelector('.dir-tree-link[data-path]');
            if (link && expandedPaths.has(link.dataset.path)) {
                expandNode(btn);
            }
        });
    }

    // Expand all tree nodes that are ancestors of targetPath so that the
    // target node (and its parent folder) is visible in the tree.
    function expandToPath(targetPath) {
        if (!targetPath) { return; }
        var parts = targetPath.replace(/\/$/, '').split('/').filter(Boolean);
        for (var i = 0; i < parts.length; i++) {
            var ancestorPath = parts.slice(0, i + 1).join('/') + '/';
            var links = Array.from(panel.querySelectorAll('.dir-tree-link[data-path]'));
            var matchLink = links.find(function (el) { return el.dataset.path === ancestorPath; });
            if (matchLink) {
                var li = matchLink.closest('li');
                var btn = li && li.querySelector('.dir-tree-expand');
                if (btn && !btn.classList.contains('expanded')) { expandNode(btn); }
            }
        }
    }

    // ── Highlight active node ────────────────────────────────────────────────

    function highlightActive() {
        var params = new URLSearchParams(window.location.search);
        var path = params.get('path') || '';
        var partitionUuid = params.get('partition_uuid') || '';

        // Restore saved expand/collapse state, then ensure ancestors of the
        // current path are expanded (overrides any collapsed ancestors).
        restoreExpandedState(getExpandedPaths());
        expandToPath(path);

        // Clear existing highlights.
        panel.querySelectorAll('.dir-tree-link.active').forEach(function (el) {
            el.classList.remove('active');
        });

        // Prefer highlighting a partition header when a partition filter is
        // active and we are at the root of that partition (no path set).
        var node = null;
        if (partitionUuid && !path) {
            node = Array.from(panel.querySelectorAll('.dir-tree-link[data-partition]')).find(function (el) {
                return el.dataset.partition === partitionUuid;
            }) || null;
        }
        if (!node) {
            // Use dataset equality — avoids CSS SyntaxError for paths with '"'.
            node = Array.from(panel.querySelectorAll('.dir-tree-link')).find(function (el) {
                return el.dataset.path === path;
            }) || null;
        }
        if (node) {
            node.classList.add('active');
            node.scrollIntoView({ block: 'nearest' });
        }
    }

    // ── Click handling ───────────────────────────────────────────────────────

    function handleTreeClick(e) {
        // Expand/collapse chevron button — toggle subtree, don't navigate.
        var expandBtn = e.target.closest('.dir-tree-expand');
        if (expandBtn) {
            e.preventDefault();
            if (expandBtn.classList.contains('expanded')) {
                collapseNode(expandBtn);
            } else {
                expandNode(expandBtn);
            }
            saveExpandedState();
            return;
        }

        // Directory / partition link — navigate with params preserved.
        var link = e.target.closest('.dir-tree-link');
        if (!link) { return; }
        e.preventDefault();
        var params = new URLSearchParams(window.location.search);
        params.delete('page');
        if ('partition' in link.dataset) {
            params.set('partition_uuid', link.dataset.partition);
            params.delete('path');
        } else {
            params.set('path', link.dataset.path);
            // "All files" root link — also clear the partition filter.
            if (link.dataset.path === '') {
                params.delete('partition_uuid');
            }
        }
        window.location.search = params.toString();
    }

    // Register the click handler once at setup time, not inside the fetch
    // callback, so it can never be registered more than once regardless of
    // how many times the panel is opened and closed.
    panel.addEventListener('click', handleTreeClick);

    function openTree() {
        // Guard with both `loaded` and `loading` so a rapid open→close→open
        // sequence before the first fetch resolves does not dispatch a second
        // request (which would previously register handleTreeClick twice).
        if (!loaded && !loading) {
            loading = true;
            var params = new URLSearchParams(window.location.search);
            fetch(TREE_URL + '?' + params.toString())
                .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
                .then(function (html) {
                    panel.innerHTML = html;
                    loaded = true;
                    loading = false;
                    highlightActive();
                })
                .catch(function (err) {
                    loading = false;
                    panel.innerHTML = '<p class="text-muted small px-2 py-1">Could not load tree (' + err + ').</p>';
                });
        }
        panel.classList.remove('d-none');
        toggle.classList.add('active');
        try { localStorage.setItem('arcology_dirtree_open', '1'); } catch (e) {}
    }

    function closeTree() {
        panel.classList.add('d-none');
        toggle.classList.remove('active');
        try { localStorage.removeItem('arcology_dirtree_open'); } catch (e) {}
    }

    toggle.addEventListener('click', function () {
        if (panel.classList.contains('d-none')) { openTree(); } else { closeTree(); }
    });

    // Restore open state after navigation
    try {
        if (localStorage.getItem('arcology_dirtree_open') === '1') { openTree(); }
    } catch (e) {}
})();

/* ── Archive details modal ──────────────────────────────────────────────── */
(function() {
    var modal = document.getElementById('archiveInfoModal');
    if (!modal) return;
    modal.addEventListener('show.bs.modal', function(event) {
        var btn = event.relatedTarget;
        if (!btn) return;
        document.getElementById('aim-name').textContent = btn.getAttribute('data-archive-name') || '';
        document.getElementById('aim-format').textContent = btn.getAttribute('data-archive-format') || '(unknown)';
        document.getElementById('aim-comment').textContent = btn.getAttribute('data-archive-comment') || '';
    });
})();
