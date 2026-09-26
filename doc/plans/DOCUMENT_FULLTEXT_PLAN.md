# Plan: Full-Text Search and Rendering of DTP / Document Formats

Status: **planning** (no code in this PR — this captures the agreed direction).

Goal: extend Arcology so that word-processor / DTP documents — Microsoft Word,
Impression, Ovation / Ovation Pro, TechWriter / EasiWriter, etc. — are

1. **full-text searchable** (their text content feeds the `content:` search), and
2. **viewable** in the browser, the way Sprite→PNG and DrawFile→SVG conversions
   already are.

These are two independent tracks with very different cost:

- **Track 1 — text extraction** (near-term): convert each document to plain text.
  Cheap, works in the public image, powers search *and* a basic text viewer.
- **Track 2 — faithful rendering** (long-term): run the real application inside an
  emulator to print the document to PostScript, then `ps2pdf` it. Heavy,
  licence-gated, but visually faithful.

The key finding from examining the code is that **the pipeline already
generalises**: everything downstream of "a file that converts to a text (or PDF)
output" is done. Adding a format is almost entirely about writing its converter.

---

## The existing pipeline (what we build on)

The convertible-file path, component by component:

| Stage | Where | What it does |
|---|---|---|
| **Type detection** | `arcology_shared/artefact_types.py` (`RISCOS_VIEWABLE_FILETYPES`, `VIEWABLE_EXTENSIONS`, `viewable_artefact_type()`) | Maps a file (by RISC OS filetype hex, then extension) to a convertible `ArtefactType`. |
| **Classification** | `arcology_shared/content_categories.py` (`classify_content` → `ContentCategory.CONVERTIBLE`) | Decides that a `FORMAT_CONVERT` job is worth queueing for an extraction. |
| **Conversion** | `worker/arcworker/analyses/images.py` (`_convert_file_to_outputs`, `process_format_convert`) | Dispatches on `ArtefactType`: Sprite→PNG, Draw→SVG, **Text→.txt**, image→PNG. Runs in Mode 1 (direct upload) and Mode 2 (extraction scan, tagging each output with `source_file` = the `ExtractedFile.path`). |
| **Search indexing** | `myapp/services/search_index.py` (`handle_search_documents`) | Indexes *any* `type: 'text'` output into `search_documents`; the `content:` search queries it (PostgreSQL FTS, SQLite ILIKE fallback). **Added by the document-FTS work (PR #708).** |
| **Viewing** | `myapp/templates/artefacts/viewer.html` (`type == 'text'` path) and `_file_listing.html` (the "View" link, gated on `viewable_filenames`) | Renders text outputs in the artefact viewer; the file-list row shows a magnifying-glass **"View"** link for any file that produced a `FORMAT_CONVERT` output. |

### Key insight: a text output is searchable *and* viewable, for free

`handle_search_documents` keys off the output's `type == 'text'`, and the viewer /
"View" link key off *any* completed `FORMAT_CONVERT` output. So the moment a
converter emits:

```python
{'type': 'text', 'text': <capped text>, 'text_truncated': <bool>, 'filename': <saved .txt>}
```

that file becomes **both full-text-searchable and viewable** with no new plumbing.
This is already true for `ACORN_TEXT` today, and will be automatic for every new
text-producing format.

---

## Track 1 — Text extraction (near-term)

### What adding one format requires

Per format, the mechanical wiring is small (mirrors the flux-format checklist in
`CLAUDE.md`):

1. **`ArtefactType`** member in `arcology_shared/enums.py` (e.g. `MS_WORD`,
   `ACORN_IMPRESSION`, `ACORN_OVATION`, `ACORN_TECHWRITER`).
2. **Register detection** in `RISCOS_VIEWABLE_FILETYPES` + `RISCOS_VIEWABLE_SUFFIXES`
   (RISC OS filetypes — Impression, TechWriter `&D01`, …), `VIEWABLE_EXTENSIONS`
   (`.doc`, `.docx`), and `EXTENSION_MAP` / `ANALYSIS_MAP` (`→ [FORMAT_CONVERT]`)
   for direct uploads. Delete the "intentionally omitted" note at
   `arcology_shared/artefact_types.py:253`.
3. **Converter tool** in `worker/arcworker/tools/` producing UTF-8 text.
4. **`elif` branch** in `_convert_file_to_outputs()` that calls it and emits the
   standard text-output dict — ideally via a small shared helper (`_text_output()`)
   that writes the `.txt`, caps the indexed `text` (`_INDEX_TEXT_CAP`), and sets
   `text_truncated`, so the FTS contract lives in one place rather than being
   copy-pasted per format.
5. **Worker `Dockerfile`** dependency for the tool.
6. **Tests** (mirror `ci/test_document_search.py` + a converter unit test).

Steps 1, 2, 4, 5, 6 are boilerplate. **The whole difficulty is step 3, the
converter** — and it varies wildly by format.

### Converter feasibility

| Format | Difficulty | Approach |
|---|---|---|
| **MS Word `.doc`** | Easy | `antiword` / `catdoc` (small, purpose-built legacy-`.doc` → text). |
| **MS Word `.docx`** | Easy | `python-docx`, or unzip + `word/document.xml`, or LibreOffice headless. |
| **TechWriter / EasiWriter** (`&D01`/`&DDC`, Icon Technology) | Medium–hard | No off-the-shelf tool; likely a bespoke parser, or find a community converter. The apps export RTF/text but that doesn't help offline. |
| **Impression** (Computer Concepts) | Hard | Proprietary, frame-based; often a document *directory* rather than a single file — needs directory-artefact handling + a bespoke text extractor. |
| **Ovation / Ovation Pro** (David Pilling) | Hard | Proprietary DTP; bespoke parser. |

`LibreOffice --headless --convert-to txt` is a possible catch-all for the MS
formats (and RTF) but is a heavy container dependency; `antiword`/`catdoc` are far
lighter for the common `.doc` case. The RISC OS DTP formats almost certainly need
custom Python parsers written against the format docs — genuine
research/reverse-engineering effort, which is why they were parked.

### Design decisions

- **Text-first.** For FTS, linear plain text is all that's needed, and it doubles
  as a serviceable viewer (same as `ACORN_TEXT` today). Faithful DTP rendering is
  Track 2 — keep it separate.
- **Files without RISC OS metadata.** Detection here is filetype/extension-based,
  which is reliable for RISC OS files but not for documents extracted from
  DOS/FAT images or generic ZIPs with no filetype. Full coverage there depends on
  **content/magic detection — the spike in issue #607.** Soft dependency, not a
  blocker for the RISC OS-native case.
- **Caps.** `_INDEX_TEXT_CAP` (currently 200 KB) bounds the *indexed* copy; the
  full text stays in the `.txt` output (viewable). DTP docs can be larger than
  retro text files — the cap only affects the search excerpt, flagged via
  `truncated`.

### Recommended staging (Track 1)

1. **Generalise the text-output path** (small PR): extract `_text_output()` and
   confirm Mode 1 + Mode 2 index and view for a second text-producing type. Zero
   converter risk.
2. **MS Word** (PR): the easy, high-value proof that the whole chain
   (detect → convert → index → `content:` search → View link) works end-to-end
   for a non-Acorn format, using `antiword`/`catdoc`.
3. **One PR per RISC OS DTP format** as converters become available.

---

## Track 2 — Faithful rendering via emulation (long-term)

### Approach

Run the real application headless inside a RISC OS emulator, print the document to
PostScript through RISC OS's printer-driver system, extract the `.ps`, and convert
it to PDF with `ps2pdf` (Ghostscript) *outside* the emulation.

Big advantage over per-format parsers: **the app is the spec.** One
emulation-render harness covers Impression, Ovation, TechWriter, ArtWorks — even
Draw/Sprite — because each app already knows how to print itself. No
reverse-engineering per format.

### How it fits the pipeline

It is just **another converted output**, with `type: 'pdf'` instead of `'text'`.
Output storage, content-gating (`output_blocked_for`), the file-list "View" link,
and `viewable_filenames` all apply unchanged. The only new UI piece is a
PDF-viewing path (embed / pdf.js / link) alongside the existing image/text/SVG
ones.

Given its weight and isolation needs it should be its **own analysis type** (like
`MEDIA_TRANSCODE` / `REPLAY_PROCESS` are separate heavy jobs), not a
`FORMAT_CONVERT` mode — different dependencies, much longer timeouts, sandbox.

### Considerations

- **Licensing is the real gate, not the tech.** The RISC OS ROM and the DTP app
  binaries can't ship in a public image. This likely has to be an **opt-in,
  self-hosted feature where the operator supplies the ROM + app images**, with the
  render analysis simply not offered when they're absent. Design a
  "capability present/absent" switch in from the start.
- **Untrusted-input sandboxing.** A real app parses arbitrary (possibly malicious)
  documents inside the emulator — the sandbox must contain both emulator escapes
  and the app's own parsing bugs. The worker's per-step deadline becomes
  load-bearing since GUI-app automation is flaky.
- **`pdftotext` is a bonus text source, not a reliable one.** If the PostScript
  drew real text operators, `pdftotext` on the PDF yields searchable text; if the
  RISC OS PS driver outlined the fonts, the PDF is all vectors and yields nothing.
  So Track 1 (direct text extraction) stays the reliable `content:` path; the
  render path may *also* feed FTS opportunistically.
- **Emulator choice.** RPCEmu (Risc PC / A7000-class, runs the 32-bit apps),
  scripted headless via Obey files, is the likely shape. Arculator (Archimedes)
  is an alternative for older content.

---

## Relationship between the tracks

The two tracks are independent and compose cleanly — both are just outputs hanging
off the same converted-output pipeline:

- **Text extraction** works in the public image, powers search, and gives a basic
  viewer. Do it first.
- **Emulator rendering** gives faithful viewing, is heavy and self-hosted/opt-in,
  and can *optionally* contribute text. Do it much later.

Neither blocks the other.

## Open questions

- Do Impression/Ovation documents arrive as single files or directories in
  practice, and does the extraction pipeline preserve that structure?
- Is a single generic `DOCUMENT` artefact type (with sub-detection) preferable to
  one type per application, given the viewer treats them identically once
  converted?
- For Track 2: warm pooled emulator vs. cold boot per document; how to script
  load-and-print deterministically; where the operator supplies ROM/app images.
- Coverage for metadata-less documents depends on #607 (content-based type
  detection) — sequence accordingly.
