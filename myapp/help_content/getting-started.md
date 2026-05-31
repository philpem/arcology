# Getting Started

## The Three-Layer Model

Everything in Arcology is organised in three layers:

```
Items  →  Artefacts  →  Analysis results
```

**Items** represent the things in your collection — a specific piece of software, a game, a utilities disk, a hardware manual.  An item is purely metadata: a name, description, platform, category, and tags.  Items can be nested inside other items to reflect a collection hierarchy (a publisher → a series → individual titles, for example).

**Artefacts** are the physical files attached to an item — a SuperCard Pro flux dump, a disc image, a ZIP archive, a PDF scan.  One item can have many artefacts (an original, a backup, a converted copy).

**Analysis results** are produced automatically by the worker after you upload an artefact.  Arcology extracts the files inside the disc image, identifies them, hashes them, checks for copy protection, and builds a searchable record of everything it found.

---

## Step 1: Create an Item

Go to **Items** in the navigation bar and click **New Item**.

- **Name** is the only required field.  Use a name that will make sense to you and your colleagues later — full title, version, platform where relevant (e.g. "Elite (BBC Micro, 1984)").
- **Parent item** lets you nest this item inside an existing one.  Leave it blank for top-level items.
- **Platform** and **Category** come from the Taxonomy you configure.  They are optional and can be set later.
- **Tags** are free-text, comma-separated.  They can be added and removed at any time.
- **Private** hides this item from other users (unless you share it with them).  Private status is inherited by child items and artefacts.

After saving, you land on the item view page.  The item has no artefacts yet.

---

## Step 2: Upload an Artefact

From the item view, click **Upload Artefact**.

- **File** — select the file to upload.  Arcology detects the type automatically from the extension.  If the detection is wrong (or the file has no recognisable extension), use the **Type override** field.
- **Label** — a short name for this artefact within the item.  Defaults to the filename if left blank.  Useful when you have multiple images of the same disc.
- **Platform hint** — type the platform name (e.g. `bbc_micro`, `commodore_64`).  Optional, but helps some analyses choose the right decoder.
- **Auto-analyse** — leave this ticked.  It queues all appropriate analyses for the file type automatically.

> **Note:** Files compressed with `.gz`, `.bz2`, or `.zst` are decompressed automatically on upload.  The artefact type is detected from the inner filename (e.g. `disc.scp.gz` → SCP).

Click **Upload**.  Arcology saves the file, computes its hash, and redirects you to the artefact detail page.

---

## Step 3: Watch the Analysis

After upload, analysis jobs are queued for the worker to process.  The artefact detail page shows the results of completed analyses as they accumulate — file listings, protection indicators, flux visualisations, recognised products.

If you want to watch the queue in real time, go to **Analysis** → **Queue** to see what is pending and running.

> **Note:** For SuperCard Pro (SCP) flux images, you will notice that flux analysis does not appear in the queue immediately after upload.  A *track density detection* job runs first.  If it finds a 40-track disc that was imaged in an 80-track drive, it corrects the track layout before queuing the flux decode.  This is normal behaviour — the flux analyses will appear once track detection completes.  See the [Analysis Pipeline](analysis) page for more detail.

---

## The Dashboard

The **Dashboard** is your starting point.  It shows:

- **Collection statistics** — total items, artefacts, and pending/running analyses.
- **Recent items** — the last few items added or updated.
- **Recent analyses** — the most recently completed analysis jobs.

---

## Item and Artefact Organisation

Items can be nested to any depth.  In the Items list, click the **Tree** button to see the full hierarchy.  Use the **Filter** bar to narrow by platform, category, tag, or name.

Within an item, artefacts are listed in the lower section.  Each artefact shows its type, size, label, and a summary of its most recent analysis.

---

## Artefact Types Supported

| Type | Description |
|------|-------------|
| **SCP** | SuperCard Pro flux image |
| **DFI** | DiscFerret flux image |
| **HFE** | HxC HFE disc image |
| **RAW\_SECTOR** | Raw sector dump |
| **ISO** | ISO 9660 CD-ROM image |
| **ZIP** | ZIP archive |
| **ARC** | Acorn ARC archive |
| **TAR.GZ / TAR.BZ2 / TAR.ZST** | Compressed tar archives |
| **A2R** | Apple II flux image |
| **PDF** | PDF document |
| **Acorn Sprite / Draw / Text** | RISC OS file formats — rendered for in-browser viewing |

---

## API Keys

If you want to use the `arco` command-line tool, or access the REST API directly, you need an API key.

Go to **Profile** (your username in the top-right corner) and click **Generate API Key**.  Give the key a name, choose the permission level, and submit.  The raw key is shown **once** — copy it before navigating away.  Only a hash is stored after that.

Your key's permission level is capped by your account's permission tier.  A READ\_ONLY account cannot generate a READ\_WRITE key.

See [Permissions & Access](permissions) for details on what each level can do.
