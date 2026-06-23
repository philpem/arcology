# Similarity matching

Arcology can surface artefacts whose **content** is substantially the same even
when their raw bytes differ. Two copies of a game disc — one pristine, one with a
different high-score save — or the same files packed as a Spark archive on one
disc and a ZIP on another, are recognised as similar.

The key idea: compare the **decoded content** Arcology already extracts, not the
container bytes. By the time an upload has been analysed into `ExtractedFile`
rows, the differences that defeat naïve byte hashing — archive compression
method, floppy flux timing noise, sector layout — are gone.

There are two complementary layers.

## Layer 1 — content-set similarity (primary)

Each artefact is treated as the **set of content hashes** of the files it
contains (sha256, falling back to md5 when sha256 is absent). Similarity is the
**size-weighted Jaccard** of two sets:

```
score = (bytes of files present in BOTH) / (bytes of files present in EITHER)
```

Weighting by size means a small differing save-file barely dents the score,
while a changed main binary moves it a lot. The raw shared/total **file counts**
are stored alongside the score for display.

### Granularity: artefacts and components

Whole-artefact comparison answers *"are these two discs the same?"*. It cannot
answer *"do these two hard drives each contain a similar copy of !ArtWorks (or
Photoshop)?"* — on a populated drive the shared application is a small fraction
of the manifest, so the whole-disc score stays low.

So the same metric is also applied to **components** — directory subtrees:

- top-level directories,
- RISC OS application directories (the first `!`-prefixed path segment),
- any directory no deeper than `MAX_COMPONENT_DEPTH` whose subtree holds at least
  `MIN_COMPONENT_FILES` files (catches nested PC app folders such as
  `Program Files/Adobe/Photoshop`).

Directories with identical content sets are de-duplicated to the shallowest, and
each partition is capped at `MAX_COMPONENTS_PER_PARTITION` components.

### Ubiquitous-file guards

A hash shared by a great many artefacts (a common `!System` module, a near-empty
file) would both explode candidate-pair generation and inflate scores. Two
guards address this:

- **Cap** (`MAX_HASH_ARTEFACTS`, default 50): a hash present in more than this
  many artefacts does not, on its own, make a candidate pair. Genuine matches
  still surface through their rarer shared files, and the common file still
  counts toward those pairs' scores.
- **Optional IDF weighting** (`SIMILARITY_USE_IDF`, default off): weights each
  file by `log(1 + N/df)` so common files contribute less to the score. Enabling
  it changes stored scores, so it requires a full rebuild.

### Storage and refresh

Results are cached in three tables (`artefact_similarity`,
`artefact_components`, `component_similarity`). They are populated three ways:

- **Full rebuild** — `flask rebuild-similarity` recomputes everything (O(n²)).
- **Event-driven incremental** — after each `FILE_EXTRACTION` / `ARCHIVE_EXTRACT`
  completes, the API marks the artefact `similarity_dirty` and (gated by
  `SIMILARITY_AUTO_REFRESH`, default on; deduped per artefact) **queues a
  `SIMILARITY_REFRESH` job** for it. `SIMILARITY_REFRESH` is a **control-plane**
  analysis (`CONTROL_PLANE_ANALYSIS_TYPES`): the **task runner** claims it and
  runs `run_similarity_refresh_job` end-to-end **in-process** (no HTTP), the same
  way it owns the hashdb link/recognition/delete jobs. The driver calls
  `similarity_reset` (clears the artefact's rows and recreates its components so a
  restarted job re-runs cleanly) then loops bounded `similarity_match_step`
  batches, committing and heartbeating between batches and clearing
  `similarity_dirty` when done. Because it has direct DB access it is not bounded
  by a web-request statement_timeout, so large artefacts no longer trip the
  worker's read timeout. `recompute_for_artefact` remains the synchronous wrapper
  used by the CLI rebuild and tests.
- **Periodic delta (catch-up)** — every artefact whose extracted-file set changes
  is flagged `similarity_dirty`; that flag is a *durable* record of staleness, so
  a missed event-driven refresh (worker down, `SIMILARITY_AUTO_REFRESH` off, a
  failed job) is still reconciled. `flask refresh-similarity` (and the task
  runner's `TASKRUNNER_SIMILARITY_DELTA_INTERVAL` sweep, bounded by
  `TASKRUNNER_SIMILARITY_DELTA_MAX` per tick) drains the dirty set via
  `recompute_for_artefact`, clearing each flag. This is **exact** for content
  changes — a pairwise score depends only on the two artefacts in the pair, so
  draining the dirty set equals a full rebuild. The exception is `SIMILARITY_USE_IDF`,
  where collection-wide document frequencies drift as artefacts change; reconcile
  that occasionally with a full `rebuild-similarity` (e.g. a rare
  `TASKRUNNER_SIMILARITY_INTERVAL`). A full rebuild clears all dirty flags.

> **Global parameter changes still need a full rebuild.** The dirty-flag delta
> handles per-artefact *content* changes only. Changes that alter *every* score —
> a hash database's `exclude_from_similarity` flag, or toggling
> `SIMILARITY_USE_IDF` — are not captured by the flag and require
> `rebuild-similarity` (manually or via `TASKRUNNER_SIMILARITY_INTERVAL`).

Comparison is exact (no MinHash/LSH). At evaluation-collection scale this is
fine; the cap bounds the work. MinHash/LSH is the lever to reach for only if a
very large collection makes exact pairing too slow.

## Layer 2 — byte-level fuzzy hash (TLSH)

For content that has no extractable file set (a monolithic blob) or to answer
*"which single file changed between two otherwise-identical discs?"*, a
byte-level fuzzy hash complements Layer 1.

- The optional `py-tlsh` library is wrapped in `arcology_shared/fuzzyhash.py`,
  which **degrades gracefully** when the library is absent — it is deliberately
  kept out of `requirements.txt` and installed only in the Docker images, so CI
  and minimal installs are unaffected.
- A TLSH digest is stored on `artefacts` and `extracted_files`. The worker
  computes it per extracted file during enumeration and per artefact in
  `CHECKSUM_COMPUTE`. **Flux artefact types (SCP/DFI/A2R) are skipped** — their
  raw bytes carry timing noise that makes a byte-level fuzzy hash meaningless;
  hash the decoded sector image instead.
- `flask backfill-tlsh` fills artefact-level digests for pre-existing uploads.
  (Extracted-file digests cannot be backfilled without re-extraction; they
  populate going forward.)

TLSH distance is not expressible in SQL, so near-duplicate file lookups
pre-filter candidates and compute the distance in Python.

## Where it surfaces in the UI

- **Artefact page**: a "Similar Artefacts" sidebar card, and a full
  `…/similar` page listing similar artefacts and shared components.
- **File browser**: directories that match a component elsewhere get a
  "⤳ N" badge linking to a per-component page (`/components/<uuid>/similar`).
- **Extracted files**: a "near-duplicates" link (`/files/<uuid>/near-duplicates`)
  alongside the exact-duplicates view, powered by TLSH.

All queries are visibility-filtered, so private artefacts never leak — including
as an anonymous "92% similar to ⟨hidden⟩".

## Tunables

| Setting | Default | Where | Meaning |
|---------|---------|-------|---------|
| `MIN_STORE_SCORE` | `0.10` | `myapp/services/similarity.py` | Pairs below this score are not cached |
| `MIN_COMPONENT_FILES` | `2` | same | Minimum files for a directory to be a component |
| `MAX_COMPONENT_DEPTH` | `4` | same | Deepest directory the deep-scan rule considers |
| `MAX_COMPONENTS_PER_PARTITION` | `500` | same | Safety cap on components per partition |
| `MAX_HASH_ARTEFACTS` | `50` | same | Hashes more common than this make no candidate pairs |
| `SIMILARITY_AUTO_REFRESH` | `True` | config | Refresh cache after each extraction |
| `SIMILARITY_USE_IDF` | `False` | config | Rarity-weight scores (needs full rebuild) |

## Key code

| Path | Role |
|------|------|
| `myapp/services/similarity.py` | metric, rebuild, incremental refresh, query helpers |
| `arcology_shared/fuzzyhash.py` | optional TLSH wrapper |
| `myapp/cli/rebuild_similarity.py`, `backfill_tlsh.py` | maintenance commands |
| `myapp/database.py` | `ArtefactSimilarity`, `ArtefactComponent`, `ComponentSimilarity`, `tlsh` columns |
| `myapp/blueprints/artefacts.py` | `similar`, `component_similar`, `file_near_duplicates` routes |
| `worker/arcworker/analyses/metadata.py`, `tools/extraction.py` | TLSH computation |
| `ci/test_similarity.py` | unit + route tests |

See `doc/ADMIN_COMMANDS.md` for the `rebuild-similarity` and `backfill-tlsh`
command reference.

## Roadmap (post-evaluation enhancements)

What's described above is implemented. The items below refine it for large
collections and add a discovery feature. They split two distinct problems that
whole-disk matching has on big hard-disc images: **cost** (loading/comparing a
100k-file disc is slow and usually finds nothing) and **signal** (a disc derived
from a master — e.g. an Acorn J233 install — legitimately matches every other
J233 because they share the base OS, which is usually noise). `MAX_HASH_ARTEFACTS`
already caps the "many near-identical discs all match" explosion; these phases
address the rest.

### Phase 0 — Evaluate on real data (prerequisite)

Run `flask rebuild-similarity` on the real collection, then **`flask
similarity-stats`** to gather the evaluation numbers: collection scale, the
per-artefact file-count / size distributions, candidate-pair cost, score and
document-frequency histograms, the most ubiquitous files, component coverage, and
a list of the top matches to hand-label (useful vs. noise). **Every threshold in
the later phases is a number only this can set** — measure first, then choose.
The hand-labelled precision sample is the one part the tool can't do for you.

### Phase 1 — Base-system hashdb exclusion (primary signal fix) — **implemented**

Deterministically ignore operating-system / runtime files when judging
similarity, so two J233s match on *user* content, not the stock install.

- **Done:** `exclude_from_similarity` flag on `HashDatabase` (migration
  `20260620_105239_add_hashdb_exclude_from_similarity.py`, model column, toggle
  on the hashdb edit page).
- **Done:** the similarity content-set query (`_file_rows_query`) drops
  `ExtractedFile`s whose linked `KnownFile` belongs to an excluded database (an
  outer join on the existing `known_file_id` link, added only when at least one
  database is flagged — zero overhead otherwise). Applies to both granularities,
  so an all-OS `!System` shrinks to nothing while a user's `!ArtWorks` (not in
  the OS DB) still matches.
- Reserve the flag for the **base OS**, not application software — "you both have
  ArtWorks" is signal worth keeping.
- **Done:** changing the flag notes that the cache updates on the next rebuild
  (a re-flag does not auto-refresh, consistent with `SIMILARITY_USE_IDF`). The
  change takes effect on the next full `rebuild-similarity` — run manually by an
  admin, or automatically by the task runner when `TASKRUNNER_SIMILARITY_INTERVAL`
  is set.
- **Done — building the base-system DB:** the **"Base HashDB"** action on an
  artefact page (sidebar → *Create Hash Database from Artefact*) snapshots *every*
  file on a pristine OS / hard-drive image into a new hash database (one known
  file per unique content hash) and offers to flag it `exclude_from_similarity`
  in the same dialog (`create_hashdb_from_artefacts` in
  `myapp/services/hash_rescan.py`). Creation triggers the standard relink, so the
  rest of the collection links to it automatically; then `rebuild-similarity`
  applies the exclusion. **Use this, not `arco hashdb generate-riscos`** — the
  latter is *recognition*-oriented and deliberately discards the ubiquitous
  boilerplate that exclusion needs to capture.
- **Reference data:** point the action at the pristine J233 image (and any other
  base OS image). A curated NSRL/NIST *OS-files subset* can be `hashdb import`ed
  to cover PC operating systems (the full RDS is too large to import wholesale).
- Depends on files being linked first (existing hashdb link pipeline; the
  snapshot action runs it for you).

### Phase 2 — Large-disc cost control

Keep the valuable cases (re-image, same-master, shared apps) while removing the
cost and noise of monolithic whole-disc comparison on big discs.

- **Size/file-count gate:** above `SIMILARITY_MAX_WHOLE_DISK_FILES`, skip the
  whole-file-set Jaccard and instead **derive the artefact score from component
  matches**, weighted by each matched component's `file_count` / `total_bytes`
  (both already stored) — "78% of this disc's notable folders also appear on
  that disc". Bounded by the component caps, not total file count.
- **Count/size pre-gate:** only run the full set comparison for a candidate pair
  when their stored file-count and total-bytes are within
  `SIMILARITY_WHOLE_DISK_COUNT_RATIO` — re-images / same-master discs have
  near-identical counts; a disc merely sharing a few apps is skipped cheaply.
- **Exact-content fallback:** still emit a whole-disc match for true re-images
  via the existing blob SHA-256 (so gating never loses the "imaged twice" case).
- Component matching stays on for every disc — it is the cross-disc value and is
  already bounded.

### Phase 3 — Distinctiveness ("what's unusual about this disc")

The inverse lens of similarity, reusing the document-frequency machinery
(`_document_frequencies` / the rebuild inverted index): surface what a disc has
that others don't. Cheapest first:

- **"Unique to this image"** — files with `df == 1` (present on no other
  artefact): "47 files (3.2 MB) found nowhere else." Trivial from the inverted
  index.
- **"Distinctive contents" panel** — top-N highest-IDF files/folders on the
  artefact page, so a curator sees the interesting bits of an otherwise-stock
  disc at a glance.
- **Artefact-level distinctiveness metric** — weighted fraction of the disc that
  is rare; a fully-low-IDF disc is "just a stock install".
- Cache alongside similarity (df is collection-relative and moves as the
  collection grows); gate the UI on a minimum artefact count (small collections
  make everything look distinctive). High-IDF recurring files are good
  candidates to *add* to a hashdb.

### Phase 4 — MinHash/LSH (optional, only if Phase 0 shows it's needed)

If exact pairing proves too slow at the collection's scale, precompute a
fixed-size MinHash signature per artefact and LSH-bucket candidate pairs, so
whole-disc comparison stops scaling with file count while still catching
near-identical images. Largest effort; deferred until the data says it's needed.

### IDF weighting — disposition

`SIMILARITY_USE_IDF` stays as an implemented, **default-off** statistical
fallback for collections without a base-system DB loaded (it down-weights
*un-catalogued* boilerplate that Phase 1 can't know about). Phase 3 is its
stronger justification — as a distinctiveness signal rather than a scoring tweak.
Do not make it default-on by guess; it's an A/B lever for Phase 0.
