# Hash Databases

A *hash database* is a collection of known file hashes (MD5, SHA-1 and/or SHA-256) used to identify the files extracted from your discs and archives.  Each database can also describe *products* — named software titles made up of one or more known files — so Arcology can recognise, for example, "this folder contains !ArcFS 1.30" rather than just listing anonymous files.

---

## What hash databases do

There are two related features:

* **File linking.**  Every extracted file's hash is compared against the active hash databases.  A match links the file to its known-file record, so you can see at a glance which files are already catalogued and which are unknown.
* **Product recognition.**  For databases with *product recognition* enabled, Arcology additionally checks whether a folder contains the files that make up a known product, and records a *recognised product* against that folder.

Recognition is optional and is turned on per database with the **Enable product recognition** toggle.

---

## How product recognition matches

A product is described by a set of *known files*, each marked **required** or **optional**:

* **All required files must be present** in a single folder for the product to be recognised.
* **Optional files are counted** towards a confidence score but are not mandatory.
* A product that has **only optional files** is recognised when **at least one** of them is present.
* When **path matching** is enabled for a product, a file must also appear at the expected location (the folder prefix is stripped, then the relative path is compared) — not merely somewhere in the folder.

Recognition runs automatically after a disc or archive is extracted, and again across your whole collection whenever a database's contents change.  It is processed in the background, so freshly imported databases may show a *pending* recognition state until the worker catches up.

---

## Which hash is used for matching {#best-hash}

A known file may carry any combination of MD5, SHA-1 and SHA-256.  Arcology matches on the **best hash available** for each known file, in this order:

1. **SHA-256** — used whenever the known-file record has one.
2. **SHA-1** — used when there is no SHA-256.
3. **MD5** — used only when neither SHA-256 nor SHA-1 is present.

This means a database that records strong SHA-256 hashes gets exact, collision-resistant matching, while older hash sets that only have MD5 still work.  Note that when a known file specifies a SHA-256, the extracted file must *also* have a SHA-256 that matches — a weaker MD5 match is not accepted as a substitute.  Extracted files are hashed with all three algorithms during analysis, so this only affects what the *database* stores.

---

## Importing and maintaining databases

Databases are normally populated with the `arco` command-line tool, which can import large hash sets in batches.  After an import (or any bulk change) Arcology queues a single background job to re-link files and refresh product recognition for that database, rather than re-scanning every disc individually.

If you import a new hash database and want existing extractions re-checked without re-running their analysis, an administrator can use the `flask rescan-hashes` maintenance command (see the admin documentation).
