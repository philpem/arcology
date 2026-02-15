# Issue #34 Implementation Summary

## Overview

This document summarizes the implementation plan for nested archive and disk image extraction in Arcology.

**Key Updates:**
- ✅ Centralized archive format definitions (`myapp/archive_formats.py`)
- ✅ Distinction between multi-file archives and single-file compressors
- ✅ Support for PC archives (ZIP, RAR, 7z, TAR.*, etc.)
- ✅ Direct upload handling (archives uploaded as Artefacts, not just found in disk images)

## Design Approach

We're implementing **Approach 1 (Post-Extraction Pipeline)** with your requested enhancements:

1. **Queued Analysis Jobs** - Archives are detected and extracted via separate analysis jobs
2. **Hierarchical Output Structure** - Organized by Item → Artefact → Analysis → Partition
3. **Immutable Slugs** - Stored in database, generated once from names/labels
4. **Configurable Depth Limits** - Default 10 levels, configurable via environment variable
5. **Cycle Detection** - Prevents infinite loops from self-referential archives
6. **DOS Partition Table UI** - Display partition tables for DOSDisc files and uploaded images

## Key Features

### 1. Output Directory Structure

```
/data/outputs/
  {item_uuid}_{item_slug}/
    {artefact_uuid}_{artefact_slug}/
      {analysis_uuid}_{analysis_slug}/
        partition_0_system/  (if partitions exist)
          !Boot
          !Run
          Archive.arc  (marked as archive in database)
        partition_1_data/
          (files...)
```

### 2. Slug Generation

- **Lowercase** conversion
- **Field separator normalization** - `/`, `.`, `:`, `;`, `,`, `_`, space → `-`
- **Safe characters only** - `[a-z0-9-]`
- **Collapse multiple dashes** - `---` → `-`
- **Stored in database** - immutable once set
- **Example**: `FBX3-01 KUAI` → `fbx3-01-kuai`

### 3. Archive Format Support

**RISC OS Formats:**

| Filetype | Name | Type | Tool | Status |
|----------|------|------|------|--------|
| &3FB | ArcFS | Archive | riscosarc | Phase 3 |
| &68E | PackDir | Archive | riscosarc | Phase 3 |
| &DDC | Spark | Archive | riscosarc | Phase 3 |
| &B21 | TBAFS | Archive | tbafs-extractor | Phase 3 |
| &D96 | CFS | Compressor* | riscosarc | Phase 3 |
| &FCA | Squash | Compressor* | riscosarc | Phase 3 |
| &FC8 | DOSDisc | Disk Image | sfdisk+7z | Phase 4 |
| &FCD | FCFS | Disk Image | fcfs2raw (✓ installed) | Phase 4 |
| &B23 | X-Files | Archive | custom | Phase 6 (future) |

_*Compressor = single-file, decompresses to file with same name (not a directory)_

**PC Formats:**

| Extension | Name | Type | Tool | Status |
|-----------|------|------|------|--------|
| .zip | ZIP | Archive | unzip/7z | Phase 3 |
| .rar | RAR | Archive | unrar | Phase 3 |
| .7z | 7-Zip | Archive | 7z | Phase 3 |
| .tar | TAR | Archive | tar | Phase 3 |
| .tar.gz/.tgz | TAR+GZIP | Archive | tar | Phase 3 |
| .tar.bz2/.tbz2 | TAR+BZIP2 | Archive | tar | Phase 3 |
| .tar.xz/.txz | TAR+XZ | Archive | tar | Phase 3 |
| .gz | GZIP | Compressor | gzip | Phase 3 |
| .bz2 | BZIP2 | Compressor | bzip2 | Phase 3 |
| .xz | XZ | Compressor | xz | Phase 3 |
| .zst | ZSTD | Compressor | zstd (✓ installed) | Phase 3 |

### 4. Safety Features

**Depth Limiting:**
```python
MAX_ARCHIVE_DEPTH = 10  # Configurable via environment
```
- Tracked in `ExtractedFile.extraction_depth` field
- Shown in UI when limit exceeded
- Prevents runaway extraction

**Cycle Detection:**
- Traverses parent_file_id chain
- Detects circular references
- Raises error to prevent infinite loops

**Resource Limits:**
- Maximum depth prevents disk space exhaustion
- Each archive extraction is a separate analysis job (can be monitored/cancelled)
- Failed extractions don't block other processing

### 5. Database Schema Changes

**New Fields:**

```python
# Item, Artefact, Analysis, Partition models
slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

# ExtractedFile model
parent_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("extracted_files.id"), nullable=True, index=True)
is_archive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
archive_format: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
risc_os_filetype: Mapped[Optional[str]] = mapped_column(String(3), nullable=True, index=True)
extraction_depth: Mapped[int] = mapped_column(Integer, default=0)

# Partition model
detection_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
```

**New Analysis Types:**
```python
ARCHIVE_DETECT = "archive_detect"    # Scan for archives
ARCHIVE_EXTRACT = "archive_extract"  # Extract specific archive
```

### 6. Processing Flow

```
1. User uploads ADFS disk image → Artefact created
2. FILE_LISTING runs → ExtractedFile records with risc_os_filetype
3. ARCHIVE_DETECT runs → Identifies archives, marks is_archive=True
4. For each archive:
   a. Queue ARCHIVE_EXTRACT with file_id, depth
   b. Check depth limit before extraction
   c. Extract using appropriate tool
   d. Register files with parent_file_id set
   e. Queue ARCHIVE_DETECT for newly extracted files
5. Recursion continues until:
   - No more archives found
   - Depth limit reached
   - Circular reference detected
```

### 7. Centralized Archive Definitions

**New File:** `myapp/archive_formats.py`

Single source of truth for all archive format definitions, used by both web UI and worker:

```python
from myapp.archive_formats import (
    ArchiveType,              # Enum of all formats (ARCFS, ZIP, etc.)
    ArchiveCategory,          # ARCHIVE, COMPRESS, or DISK_IMAGE
    get_archive_by_filetype,  # '3fb' -> ArchiveType.ARCFS
    get_archive_by_extension, # 'file.zip' -> ArchiveType.ZIP
    is_archive_format,        # Multi-file archive?
    is_compressor_format,     # Single-file compressor?
    is_disk_image_format,     # Nested disk image?
    get_archive_info,         # Get full metadata dict
)
```

**Benefits:**
- Consistent definitions across web app and worker
- Easy to add new formats (single location)
- Metadata includes: tool, category, extensions, RISC OS filetype, extraction behavior

**Format Metadata Example:**
```python
ArchiveType.CFS: {
    'name': 'CFS Compressed File',
    'category': ArchiveCategory.COMPRESS,
    'risc_os_filetype': 'd96',
    'tool': 'riscosarc',
    'description': 'Decompresses to single file with same name',
    'extract_creates_dir': False,
    'output_filename': 'same_as_input',
}
```

### 8. Direct Upload Handling

Archives can be uploaded two ways, both using the same pipeline:

**Path 1: Found within disk images**
```
Extract ADFS image → FILE_LISTING finds "Archive,3fb"
→ ExtractedFile(risc_os_filetype='3fb')
→ ARCHIVE_DETECT identifies as ArcFS
→ ARCHIVE_EXTRACT processes
→ Recursive scan for nested archives
```

**Path 2: Uploaded directly**
```
User uploads file.zip → Artefact(type=ZIP)
→ ANALYSIS_MAP[ZIP] = [FILE_LISTING]
→ FILE_LISTING extracts → ExtractedFile records
→ ARCHIVE_DETECT scans for nested archives
→ Recursive processing
```

**Implementation:** Existing `ANALYSIS_MAP` in `myapp/blueprints/artefacts.py` already handles ZIP/RAR/TAR. Add new RISC OS archive types:

```python
ANALYSIS_MAP = {
    # Existing
    ArtefactType.ZIP: [AnalysisType.FILE_LISTING],
    ArtefactType.TARGZ: [AnalysisType.FILE_LISTING],
    ArtefactType.RAR: [AnalysisType.FILE_LISTING],

    # Add RISC OS types
    ArtefactType.ARCFS: [AnalysisType.FILE_LISTING],
    ArtefactType.SPARK: [AnalysisType.FILE_LISTING],
    ArtefactType.FCFS: [AnalysisType.FILE_EXTRACTION],  # Disk image
    # ... etc
}
```

### 9. DOS Partition Table Display

**Storage:**
- `Partition.detection_details` stores JSON from sfdisk
- Includes: partition table type (MBR/GPT), partition entries, start/size

**UI Component:**
```html
<div class="partition-table">
  <h3>Partition Table (MBR)</h3>
  <table>
    <tr>
      <th>Index</th><th>Type</th><th>Start</th><th>Size</th><th>Filesystem</th>
    </tr>
    <!-- Populated from detection_details JSON -->
  </table>
</div>
```

**Future Extensions:**
- Filecore partition schemes (ICS, HCCS, Simtec IDE)
- Custom partition table parsers for proprietary formats

## Implementation Phases

### Phase 1: Database and Infrastructure ✓
**Status:** Design complete, ready to implement

**Tasks:**
1. Update `database.py` models with new fields
2. Create `myapp/utils/slugs.py` with generation functions
3. Update `worker/arcworker/config.py` with MAX_ARCHIVE_DEPTH
4. Create `worker/arcworker/utils/paths.py` for output directory structure
5. Test database changes with `install.py`

**Deliverables:**
- Modified database models
- Slug generation utilities
- Hierarchical output path functions
- Configuration settings

---

### Phase 2: Archive Detection
**Status:** Design complete

**Tasks:**
1. Add `ARCHIVE_DETECT` to `AnalysisType` enum
2. Create `worker/arcworker/tools/archives.py` with detection logic
3. Implement `process_archive_detect` handler in `analysis.py`
4. Update `list_files_dim` to store `risc_os_filetype` in database
5. Auto-queue `archive_detect` after `file_listing` completes

**Deliverables:**
- Archive type detection by RISC OS filetype
- Automatic queueing of archive extraction jobs
- Database fields populated (`is_archive`, `archive_format`, `risc_os_filetype`)

---

### Phase 3: Archive Extraction Tools
**Status:** Design complete, fcfs2raw already installed

**Tasks:**
1. Add riscosarc to `worker/Dockerfile`
2. Add tbafs-extractor to `worker/Dockerfile`
3. Create extraction functions in `worker/arcworker/tools/archives.py`:
   - `extract_riscosarc()` - ArcFS, CFS, PackDir, Squash, Spark
   - `extract_tbafs()` - TBAFS archives
   - `convert_fcfs_to_raw()` - FCFS → raw sector image (wrapper for existing fcfs2raw)
4. Test each tool independently

**Deliverables:**
- Compiled binaries in worker container
- Extraction wrapper functions
- Error handling and validation

---

### Phase 4: Archive Extraction Handler
**Status:** Design complete

**Tasks:**
1. Add `ARCHIVE_EXTRACT` to `AnalysisType` enum
2. Implement `process_archive_extract` in `analysis.py`
3. Implement file storage tracking (map ExtractedFile to actual file path)
4. Add depth checking with `check_archive_depth()`
5. Add cycle detection logic
6. Register extracted files with `parent_file_id`
7. Queue recursive `archive_detect` for nested archives

**Deliverables:**
- Fully functional archive extraction
- Recursive processing up to depth limit
- Parent-child file relationships in database
- Cycle detection and graceful failures

---

### Phase 5: DOS Partition Table UI
**Status:** Design complete

**Tasks:**
1. Update `process_partition_detect` to store results in `detection_details`
2. Create API endpoint: `GET /api/partitions/{id}/detection_details`
3. Create template component: `templates/partitions/partition_table.html`
4. Add partition table display to artefact detail page
5. Extend to DOSDisc files (filetype &FC8)

**Deliverables:**
- Partition table visualization in UI
- JSON storage of sfdisk output
- Support for uploaded DOS disk images
- Support for extracted DOSDisc files

---

### Phase 6: Long Filename Support (Future)
**Status:** Deferred to later release

**Tasks:**
1. Implement raFS detection and `rafsln` integration
2. Implement LongFiles parser for `!ZZ!!Z!LF` files
3. Write X-Files format parser and extractor (based on source code in issue)
4. Integrate into extraction pipeline as preprocessing step

**Note:** This phase can be implemented independently after Phases 1-5 are complete.

---

## Testing Strategy

### Unit Tests
- Slug generation edge cases
- Depth limit enforcement
- Cycle detection
- Path generation

### Integration Tests
- Single archive extraction (each format)
- Nested archives (2-3 levels deep)
- Depth limit (archives nested to 11+ levels)
- Self-referential archive (cycle detection)
- FCFS/DOSDisc nested in ADFS image

### System Tests
- Full pipeline: upload → listing → detection → extraction
- Multiple concurrent archive extractions
- UI display of archives and partition tables
- Output directory structure validation

## Configuration

### Environment Variables

```bash
# Worker configuration
MAX_ARCHIVE_DEPTH=10         # Maximum archive nesting depth (default: 10)
ARCOLOGY_API=http://web:5000/api
UPLOAD_DIR=/data/uploads
OUTPUT_DIR=/data/outputs      # Hierarchical structure will be created here
POLL_INTERVAL=30
LOG_LEVEL=INFO
```

### File Locations

```
/home/user/arcology/
├── doc/
│   ├── ARCHIVE_EXTRACTION_DESIGN.md     # Detailed design (this is complete)
│   └── ISSUE_34_IMPLEMENTATION_SUMMARY.md  # This file
├── myapp/
│   ├── database.py                      # Models (needs updates)
│   └── utils/
│       └── slugs.py                     # New: slug generation
├── worker/
│   ├── Dockerfile                       # Needs: riscosarc, tbafs-extractor
│   ├── arcworker/
│   │   ├── config.py                    # Needs: MAX_ARCHIVE_DEPTH
│   │   ├── analysis.py                  # Needs: new handlers
│   │   ├── tools/
│   │   │   ├── archives.py              # New: archive detection & extraction
│   │   │   └── extraction.py            # Update: store risc_os_filetype
│   │   └── utils/
│   │       └── paths.py                 # New: hierarchical output paths
└── data/outputs/                        # New structure will be created here
```

## Next Steps

To proceed with implementation, I recommend:

1. **Start with Phase 1** (Database and Infrastructure)
   - Low risk, establishes foundation
   - Can be tested immediately with `install.py`
   - Enables hierarchical output directory structure

2. **Then Phase 2** (Archive Detection)
   - Leverages existing filetype parsing
   - No new tools needed
   - Provides visibility into archives without extraction

3. **Then Phases 3-4** (Extraction)
   - Requires Docker rebuild (new tools)
   - Most complex code changes
   - Highest value to users

4. **Finally Phase 5** (DOS Partition Table UI)
   - Primarily UI work
   - Low complexity
   - Nice-to-have feature

**Phase 6** (Long Filename Support) can be deferred or implemented independently.

## Questions for Consideration

Before starting implementation:

1. **Migration Strategy:**
   - Current database uses `db.create_all()` (no migrations)
   - Should we set up Flask-Migrate for this change?
   - Or continue with `db.create_all()` and manual upgrades?

2. **Existing Data:**
   - What happens to existing ExtractedFile records without slugs?
   - Should we backfill slugs for existing data?

3. **File Storage:**
   - Where are extracted files currently stored on disk?
   - Do we need to migrate existing extracted files to new structure?
   - Or only apply new structure to future extractions?

4. **Priority:**
   - Which archive formats are most important? (prioritize Docker builds)
   - Is FCFS/DOSDisc higher priority than general archives?

5. **Testing:**
   - Do you have sample files for each archive format for testing?
   - Should I create test fixtures?

Let me know your preferences and I can proceed with implementation!
