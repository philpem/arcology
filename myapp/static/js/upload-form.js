/* Arcology upload form behaviour.
 *
 * - Parses Arcarc/TOSEC-style filenames to prefill the label and platform.
 * - Shows the DFI clock-override hint field for DFI flux images.
 * - Persists the "Format hints" collapse state across Upload More submits.
 * - refreshItemChoices(): reloads the item dropdown from the JSON endpoint
 *   given in the select element's data-choices-url attribute.
 *
 * Extracted verbatim from templates/artefacts/upload.html.
 */

async function refreshItemChoices(selectId) {
    const sel = document.getElementById(selectId);
    const savedVal = sel.value;
    const resp = await fetch(sel.dataset.choicesUrl);
    const items = await resp.json();
    sel.innerHTML = '';
    sel.add(new Option('-- Select item --', '0'));
    items.forEach(function(it) { sel.add(new Option(it.name, it.id)); });
    sel.value = savedVal;
}

(function () {
    'use strict';

    var HINTS_STORAGE_KEY = 'arcology_upload_hints_open';

    // Platform codes → human-readable name (case-insensitive lookup).
    // Used to pre-select the Platform hint dropdown from the filename.
    var PLATFORM_MAP = {
        'ibmpc':      'IBM PC',
        'c64':        'C64',
        'amiga':      'Amiga',
        'atarst':     'Atari ST',
        'bbc':        'BBC Micro',
        'cpc':        'Amstrad CPC',
        'msx':        'MSX',
        'spectrum':   'ZX Spectrum',
        'apple2':     'Apple II',
        'applemac':   'Apple Mac',
        'cpm':        'CP/M',
        'acornrisc':  'Acorn RISC OS',
        'riscos':     'Acorn RISC OS',
        'sirius':     'Victor 9000 (Sirius 1)',
        'victor9000': 'Victor 9000 (Sirius 1)',
        'victor9k':   'Victor 9000 (Sirius 1)'
    };

    // Disc-type codes → formatted string (inch symbol written as "in").
    var DISC_TYPE_MAP = {
        '8':       '8in',
        '525':     '5.25in',
        '525sd':   '5.25in SD',
        '525dd':   '5.25in DD',
        '525hd':   '5.25in HD',
        '525dd80': '5.25in DD 40t in 80t drive',
        '35':      '3.5in',
        '35sd':    '3.5in SD',
        '35dd':    '3.5in DD',
        '35hd':    '3.5in HD',
        '35ed':    '3.5in ED',
        '30':      '3in',
        'bbc80ss': 'BBC 80t SS',
        'bbc80ds': 'BBC 80t DS'
    };

    // Words kept lowercase in title-case (unless first word or after ' - ').
    var SMALL_WORDS = new Set([
        'a', 'an', 'the', 'and', 'but', 'or', 'nor', 'for', 'yet',
        'so', 'as', 'at', 'by', 'in', 'of', 'on', 'to', 'up', 'via'
    ]);

    /** Expand CamelCase into space-separated words. */
    function splitCamelCase(str) {
        return str
            .replace(/([a-z])([A-Z])/g, '$1 $2')
            .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2');
    }

    /** Apply title-case: keep small words lowercase unless they start a segment. */
    function titleCase(str) {
        return str.replace(/\S+/g, function (word, offset) {
            var lower = word.toLowerCase();
            // Always capitalise the first word of the whole string.
            if (offset === 0) return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
            if (SMALL_WORDS.has(lower)) return lower;
            return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
        });
    }

    /**
     * Normalise a raw name segment (publisher, title part, disc-title).
     * Splits CamelCase, applies title-case.
     */
    function normaliseName(raw) {
        return titleCase(splitCamelCase(raw));
    }

    /**
     * Normalise a title that may contain '-' subtitle separators.
     * Each '-'-delimited part is CamelCase-expanded and title-cased,
     * then parts are joined with ' - '.
     */
    function normaliseTitle(raw) {
        var parts = raw.split('-');
        return parts.map(function (p) { return normaliseName(p); }).join(' - ');
    }

    /**
     * Determine whether a segment represents a disc number.
     * Accepts "NofM" (e.g. "1of4") or a plain integer (e.g. "2").
     */
    function isDiscNumber(seg) {
        return /^\d+of\d+$/i.test(seg) || /^\d+$/.test(seg);
    }

    /** Format a disc-number segment as human-readable text. */
    function formatDiscNumber(seg) {
        var m = seg.match(/^(\d+)of(\d+)$/i);
        if (m) return m[1] + ' of ' + m[2];
        return seg; // plain integer
    }

    /**
     * Strip file extension(s). Always removes the last extension; if a second
     * extension remains (e.g. .scp.gz → .scp → bare), removes that too.
     */
    function stripExtensions(filename) {
        var s = filename.replace(/\.[^.]+$/, '');  // strip last extension
        return s.replace(/\.[^.]+$/, '');           // strip one more if present
    }

    /**
     * Parse an Arcarc/Tosec-style filename and return
     * { label: string, platformName: string|null }
     * or null if the filename does not match the expected pattern.
     *
     * Format: Publisher_Title[_DiscNum][_DiscTitle]__Platform-DiscType
     */
    function parseArtefactFilename(filename) {
        var base = stripExtensions(filename);

        // Must contain '__' separating metadata from platform/disc-type.
        var dblIdx = base.indexOf('__');
        if (dblIdx < 0) return null;

        var metaPart     = base.substring(0, dblIdx);
        var platformPart = base.substring(dblIdx + 2);

        // metaPart must have at least Publisher and Title.
        var metaSegs = metaPart.split('_');
        if (metaSegs.length < 2) return null;

        var publisher = metaSegs[0];
        var titleRaw  = metaSegs[1];
        var extraSegs = metaSegs.slice(2); // [DiscNum?, DiscTitle?]

        // platformPart: split on last '-' to separate Platform from DiscType.
        var lastDash = platformPart.lastIndexOf('-');
        if (lastDash < 0) return null;
        var platformRaw  = platformPart.substring(0, lastDash);
        var discTypeRaw  = platformPart.substring(lastDash + 1);

        // --- Disc number / disc title ---
        var discNumber = null;
        var discTitle  = null;
        if (extraSegs.length > 0) {
            if (isDiscNumber(extraSegs[0])) {
                discNumber = formatDiscNumber(extraSegs[0]);
                if (extraSegs.length > 1) {
                    discTitle = normaliseName(extraSegs[1]);
                }
            } else {
                // No disc number — first extra segment is the disc title.
                discTitle = normaliseName(extraSegs[0]);
            }
        }

        // --- Platform ---
        var platformKey  = platformRaw.toLowerCase();
        var platformName = PLATFORM_MAP[platformKey] || normaliseName(platformRaw);

        // --- Disc type ---
        var discType = DISC_TYPE_MAP[discTypeRaw.toLowerCase()] || discTypeRaw;

        // --- Assemble label ---
        var title = normaliseTitle(titleRaw);
        var pub   = normaliseName(publisher);

        var label = title + ' (' + pub + ')';

        if (discNumber !== null && discTitle !== null) {
            label += ' (' + discNumber + ' - ' + discTitle + ')';
        } else if (discNumber !== null) {
            label += ' (' + discNumber + ')';
        } else if (discTitle !== null) {
            label += ' (' + discTitle + ')';
        }

        label += ' (' + platformName + ')';
        label += ' (' + discType + ')';

        return { label: label, platformName: platformName };
    }

    /** Return true if filename has a .dfi or .dfi.* (compressed) extension. */
    function isDfi(filename) {
        var lower = filename.toLowerCase();
        return lower.endsWith('.dfi') ||
               lower.endsWith('.dfi.gz') ||
               lower.endsWith('.dfi.zst') ||
               lower.endsWith('.dfi.bz2');
    }

    // --- Hints section collapse ---

    var hintsEl      = document.getElementById('hints-section');
    var hintsToggle  = document.getElementById('hints-toggle');
    var hintsChevron = document.getElementById('hints-chevron');
    if (!hintsEl) return;  // not on the upload page
    var hintsCollapse = bootstrap.Collapse.getOrCreateInstance(hintsEl, { toggle: false });

    function setChevron(open) {
        if (open) {
            hintsChevron.classList.remove('bi-chevron-down');
            hintsChevron.classList.add('bi-chevron-up');
        } else {
            hintsChevron.classList.remove('bi-chevron-up');
            hintsChevron.classList.add('bi-chevron-down');
        }
    }

    // Restore state from previous submit (Upload More flow).
    if (sessionStorage.getItem(HINTS_STORAGE_KEY) === '1') {
        hintsCollapse.show();
        setChevron(true);
    }

    hintsEl.addEventListener('show.bs.collapse', function () {
        sessionStorage.setItem(HINTS_STORAGE_KEY, '1');
        setChevron(true);
        hintsToggle.setAttribute('aria-expanded', 'true');
    });
    hintsEl.addEventListener('hide.bs.collapse', function () {
        sessionStorage.setItem(HINTS_STORAGE_KEY, '0');
        setChevron(false);
        hintsToggle.setAttribute('aria-expanded', 'false');
    });

    hintsToggle.addEventListener('click', function () {
        hintsCollapse.toggle();
    });

    // Wire up the file input.
    document.getElementById('file').addEventListener('change', function (e) {
        var file = e.target.files[0];
        if (!file) return;

        var labelField = document.getElementById('label');
        var result     = parseArtefactFilename(file.name);

        // Prefill label only if currently empty.
        if (labelField.value.trim() === '') {
            labelField.value = result ? result.label : stripExtensions(file.name);
        }

        // Pre-select platform: match option text case-insensitively.
        if (result && result.platformName) {
            var sel    = document.getElementById('platform_id');
            var needle = result.platformName.toLowerCase();
            for (var i = 0; i < sel.options.length; i++) {
                if (sel.options[i].text.toLowerCase() === needle) {
                    sel.value = sel.options[i].value;
                    break;
                }
            }
        }

        // Show DFI clock override field only for DFI files; expand hints section.
        var dfiGroup = document.getElementById('dfi-clock-group');
        if (isDfi(file.name)) {
            dfiGroup.classList.remove('d-none');
            hintsCollapse.show();
        } else {
            dfiGroup.classList.add('d-none');
            document.getElementById('dfi_clock_mhz').value = '';
        }
    });
}());


/* Chunked upload for large files.
 *
 * Progressive enhancement: when the selected file is at or above the
 * server-configured threshold, the form submit is intercepted and the file is
 * uploaded in chunks via the session-authenticated /artefacts/chunked/* routes
 * (init -> chunk -> complete), with resume support via /status.  Smaller files
 * (or browsers without JS) fall through to the normal multipart form POST.
 *
 * The threshold, chunk size and init URL come from data- attributes rendered on
 * the form, so client and server stay in sync from one config source.
 */
(function () {
    'use strict';

    var form = document.getElementById('upload-form');
    if (!form || !window.fetch || !window.File || !File.prototype.slice) return;

    var THRESHOLD = parseInt(form.dataset.chunkThreshold, 10) || (100 * 1024 * 1024);
    var CHUNK_SIZE = parseInt(form.dataset.chunkSize, 10) || (10 * 1024 * 1024);
    var INIT_URL = form.dataset.chunkInitUrl;
    var BASE_URL = INIT_URL.replace(/\/init$/, '');
    // Patient per-chunk retry so a brief server outage (e.g. a redeploy
    // mid-upload) is ridden out rather than failing the whole upload.
    var MAX_CHUNK_RETRIES = 7;
    var MAX_RETRY_BACKOFF_MS = 15000;
    // Async-finalise polling.
    var FINALIZE_POLL_MIN_MS = 2000;
    var FINALIZE_POLL_MAX_MS = 10000;
    var FINALIZE_POLL_TIMEOUT_MS = 4 * 3600 * 1000;
    // Speed/ETA: rolling window over last N completed chunks.
    var SPEED_WINDOW = 5;

    var alertEl = document.getElementById('chunk-upload-alert');
    var progressEl = document.getElementById('chunk-upload-progress');
    var barEl = document.getElementById('chunk-upload-bar');
    var pctEl = document.getElementById('chunk-upload-percent');
    var statusEl = document.getElementById('chunk-upload-status');

    function csrfToken() {
        var el = document.getElementById('csrf_token') ||
                 form.querySelector('input[name="csrf_token"]');
        return el ? el.value : '';
    }

    function showError(msg) {
        if (!alertEl) return;
        alertEl.textContent = msg;
        alertEl.classList.remove('d-none');
    }

    function clearError() {
        if (alertEl) alertEl.classList.add('d-none');
    }

    function formatSpeed(bps) {
        if (bps >= 1024 * 1024) return (bps / (1024 * 1024)).toFixed(1) + ' MB/s';
        if (bps >= 1024) return Math.round(bps / 1024) + ' KB/s';
        return Math.round(bps) + ' B/s';
    }

    function formatEta(secs) {
        secs = Math.ceil(secs);
        if (secs >= 3600) {
            var h = Math.floor(secs / 3600);
            var m = Math.floor((secs % 3600) / 60);
            return h + 'h ' + m + 'm';
        }
        if (secs >= 60) {
            var m = Math.floor(secs / 60);
            var s = secs % 60;
            return m + 'm ' + s + 's';
        }
        return secs + 's';
    }

    // chunkRecords: [{t, bytes}] for chunks actually sent this session (not resumed).
    // Used to compute a rolling-window upload speed and ETA.
    var chunkRecords = [];

    function computeSpeed() {
        var win = chunkRecords.slice(-SPEED_WINDOW);
        if (win.length < 2) return null;
        var elapsed = (win[win.length - 1].t - win[0].t) / 1000;
        if (elapsed <= 0) return null;
        var bytes = 0;
        for (var k = 1; k < win.length; k++) bytes += win[k].bytes;
        return bytes / elapsed;
    }

    function setProgress(done, total, fileSize) {
        var pct = total ? Math.round((done / total) * 100) : 0;
        if (barEl) {
            barEl.style.width = pct + '%';
            barEl.setAttribute('aria-valuenow', String(pct));
        }
        if (pctEl) pctEl.textContent = pct + '%';
        if (statusEl) {
            var text = 'Uploading ' + done + ' / ' + total + ' chunks';
            var speed = computeSpeed();
            if (speed !== null) {
                text += ' · ' + formatSpeed(speed);
                var remainingBytes = Math.max(0, (fileSize || 0) - done * CHUNK_SIZE);
                if (remainingBytes > 0) text += ' · ETA ' + formatEta(remainingBytes / speed);
            }
            statusEl.textContent = text;
        }
    }

    function sessionKey(file) {
        return 'arco_chunk_' + file.name + '_' + file.size + '_' + file.lastModified;
    }

    // Resume bookkeeping lives in localStorage so an interrupted upload can be
    // resumed even after a browser restart (the server keeps the chunks on its
    // persisted volume).  Wrapped so private-mode storage failures are silent.
    function resumeGet(key) {
        try { return window.localStorage.getItem(key); } catch (e) { return null; }
    }
    function resumeSet(key, val) {
        try { window.localStorage.setItem(key, val); } catch (e) { /* ignore */ }
    }
    function resumeDel(key) {
        try { window.localStorage.removeItem(key); } catch (e) { /* ignore */ }
    }

    function delay(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function collectHints() {
        var hints = {};
        var platSel = document.getElementById('platform_id');
        if (platSel && platSel.value && platSel.value !== '0') {
            hints.platform = platSel.options[platSel.selectedIndex].text;
        }
        var dfi = document.getElementById('dfi_clock_mhz');
        if (dfi && dfi.value) {
            var n = parseInt(dfi.value, 10);
            if (!isNaN(n)) hints.dfi_clock_mhz = n;
        }
        return hints;
    }

    function fieldVal(id) {
        var el = document.getElementById(id);
        return el ? el.value : null;
    }

    function fieldChecked(id) {
        var el = document.getElementById(id);
        return el ? el.checked : false;
    }

    async function postJSON(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            body: JSON.stringify(body),
        });
    }

    async function errorMessage(resp) {
        try {
            var data = await resp.json();
            return data.error || ('Upload failed (HTTP ' + resp.status + ')');
        } catch (e) {
            return 'Upload failed (HTTP ' + resp.status + ')';
        }
    }

    async function initSession(file, totalChunks) {
        var hints = collectHints();
        var resp = await postJSON(INIT_URL, {
            filename: file.name,
            total_chunks: totalChunks,
            total_size: file.size,
            item_id: fieldVal('item_id'),
            label: fieldVal('label'),
            artefact_type: fieldVal('artefact_type'),
            description: fieldVal('description'),
            is_private: fieldChecked('is_private'),
            auto_analyse: fieldChecked('auto_analyse'),
            hints: Object.keys(hints).length ? hints : null,
        });
        if (!resp.ok) throw new Error(await errorMessage(resp));
        return (await resp.json()).upload_uuid;
    }

    async function fetchReceived(uploadUuid) {
        try {
            var resp = await fetch(BASE_URL + '/' + uploadUuid + '/status', {
                headers: { 'X-CSRFToken': csrfToken() },
            });
            if (!resp.ok) return null;
            return (await resp.json()).received_chunks || [];
        } catch (e) {
            return null;
        }
    }

    async function sendChunk(uploadUuid, index, file) {
        var start = index * CHUNK_SIZE;
        var blob = file.slice(start, Math.min(start + CHUNK_SIZE, file.size));
        var url = BASE_URL + '/' + uploadUuid + '/chunk/' + index;
        var lastErr = null;
        // Retry network errors and 5xx with exponential backoff; a 4xx is a
        // permanent failure (e.g. session gone, bad index) so fail fast.
        for (var attempt = 0; attempt <= MAX_CHUNK_RETRIES; attempt++) {
            if (attempt > 0) await delay(Math.min(Math.pow(2, attempt) * 500, MAX_RETRY_BACKOFF_MS));
            var resp;
            try {
                resp = await fetch(url, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/octet-stream',
                        'X-CSRFToken': csrfToken(),
                    },
                    body: blob,
                });
            } catch (e) {
                lastErr = e;  // network error: retry
                continue;
            }
            if (resp.ok) return;
            if (resp.status < 500) throw new Error(await errorMessage(resp));
            lastErr = new Error(await errorMessage(resp));  // 5xx: retry
        }
        throw lastErr || new Error('Chunk ' + index + ' failed');
    }

    async function completeSession(uploadUuid) {
        // Drive finalise asynchronously: /complete returns immediately and we
        // poll for the result, so a large server-side assembly never trips the
        // request timeout (and an assembly orphaned by a redeploy is re-driven
        // on the next poll).
        var resp = await postJSON(BASE_URL + '/' + uploadUuid + '/complete', { async: true });
        if (resp.status === 201) {
            return (await resp.json()).redirect;  // old server: synchronous
        }
        if (!resp.ok) throw new Error(await errorMessage(resp));
        return await pollFinalize(uploadUuid);
    }

    async function pollFinalize(uploadUuid) {
        if (statusEl) statusEl.textContent = 'Assembling on server…';
        var interval = FINALIZE_POLL_MIN_MS;
        var deadline = Date.now() + FINALIZE_POLL_TIMEOUT_MS;
        var url = BASE_URL + '/' + uploadUuid + '/complete/status';
        while (Date.now() < deadline) {
            var resp;
            try {
                resp = await fetch(url, { headers: { 'X-CSRFToken': csrfToken() } });
            } catch (e) {
                // Network error (server may be redeploying): keep waiting.
                await delay(interval);
                interval = Math.min(interval * 2, FINALIZE_POLL_MAX_MS);
                continue;
            }
            if (resp.status === 404) {
                throw new Error('Upload session expired before finishing');
            }
            if (resp.status === 200 || resp.status === 202) {
                var body = await resp.json();
                if (body.state === 'done') return body.redirect;
                if (body.state === 'failed') {
                    throw new Error(body.error || 'Server failed to finalise the upload');
                }
            }
            await delay(interval);
            interval = Math.min(interval * 2, FINALIZE_POLL_MAX_MS);
        }
        throw new Error('Timed out waiting for the server to finalise the upload');
    }

    function goAfterUpload(redirect) {
        if (fieldChecked('upload_more')) {
            window.location = window.location.pathname + '?upload_more=1';
        } else {
            window.location = redirect;
        }
    }

    async function finalizeState(uploadUuid) {
        // Parsed /complete/status body, or null if the session is gone/unreachable.
        try {
            var resp = await fetch(BASE_URL + '/' + uploadUuid + '/complete/status', {
                headers: { 'X-CSRFToken': csrfToken() },
            });
            if (resp.status === 200 || resp.status === 202) return await resp.json();
            return null;
        } catch (e) {
            return null;
        }
    }

    async function runChunkedUpload(file) {
        var totalChunks = Math.ceil(file.size / CHUNK_SIZE) || 1;
        var storageKey = sessionKey(file);
        var uploadUuid = null;
        var received = [];
        chunkRecords = [];  // reset speed tracking for this upload session

        // Resume a previous interrupted upload of the same file.  Check the
        // finalise state first: an already-finished session must not re-upload
        // chunks (the server deleted them and now rejects writes with 409), and
        // a failed session must be restarted rather than retried forever.
        var saved = resumeGet(storageKey);
        if (saved) {
            var fstate = await finalizeState(saved);
            if (fstate && fstate.state === 'done') {
                resumeDel(storageKey);
                goAfterUpload(fstate.redirect);
                return;
            }
            if (fstate && fstate.state === 'assembling') {
                // All chunks are in and finalise is already running — just wait.
                if (statusEl) statusEl.textContent = 'Assembling on server…';
                var redirectA = await pollFinalize(saved);
                resumeDel(storageKey);
                goAfterUpload(redirectA);
                return;
            }
            if (fstate && fstate.state === 'pending') {
                // Still uploading — skip the chunks the server already holds.
                var got = await fetchReceived(saved);
                if (got !== null) {
                    uploadUuid = saved;
                    received = got;
                } else {
                    resumeDel(storageKey);
                }
            } else {
                // 'failed', or the session is gone: discard and start fresh.
                resumeDel(storageKey);
            }
        }

        if (!uploadUuid) {
            uploadUuid = await initSession(file, totalChunks);
            resumeSet(storageKey, uploadUuid);
        }

        var receivedSet = {};
        received.forEach(function (i) { receivedSet[i] = true; });

        var done = received.length;
        setProgress(done, totalChunks, file.size);
        for (var i = 0; i < totalChunks; i++) {
            if (receivedSet[i]) continue;
            await sendChunk(uploadUuid, i, file);
            done += 1;
            var chunkBytes = Math.min(CHUNK_SIZE, file.size - i * CHUNK_SIZE);
            chunkRecords.push({ t: Date.now(), bytes: chunkBytes });
            setProgress(done, totalChunks, file.size);
        }

        if (statusEl) statusEl.textContent = 'Finishing…';
        var redirect = await completeSession(uploadUuid);
        resumeDel(storageKey);
        goAfterUpload(redirect);
    }

    form.addEventListener('submit', function (e) {
        var fileInput = document.getElementById('file');
        var file = fileInput && fileInput.files[0];
        if (!file || file.size < THRESHOLD) return;  // small file: normal POST

        e.preventDefault();
        clearError();

        var submitBtn = form.querySelector('button[type="submit"]');
        if (submitBtn) submitBtn.disabled = true;
        if (progressEl) progressEl.classList.remove('d-none');

        runChunkedUpload(file).catch(function (err) {
            showError(err && err.message ? err.message : 'Upload failed');
            if (submitBtn) submitBtn.disabled = false;
            if (progressEl) progressEl.classList.add('d-none');
        });
    });
}());
