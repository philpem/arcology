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
`artefact_components`, `component_similarity`). They are populated two ways:

- **Full rebuild** — `flask rebuild-similarity` recomputes everything.
- **Incremental** — after each `FILE_EXTRACTION` / `ARCHIVE_EXTRACT` completes,
  the API refreshes just that artefact's rows (`recompute_for_artefact`). Gated
  by `SIMILARITY_AUTO_REFRESH` (default on); a failure here never fails the
  worker's result post.

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
