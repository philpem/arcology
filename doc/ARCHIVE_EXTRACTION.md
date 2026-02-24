# Archive Extraction Implementation

This document describes the design and implementation plan for nested archive and disk image extraction in Arcology.

## Overview

**Status**: Design phase - not yet implemented

Support for extracting nested archives from RISC OS disk images and directly uploaded archive files, with recursive extraction, cycle detection, and depth limits.

## Supported Formats

### RISC OS Formats

| Filetype | Name | Type | Tool | Phase |
|----------|------|------|------|-------|
| &3FB | ArcFS | Archive | riscosarc | 3 |
| &68E | PackDir | Archive | riscosarc | 3 |
| &A91 | ZIP (RISC OS) | Archive | unzip | 3 |
| &DDC | Spark / ZIP (RISC OS)† | Archive | riscosarc / unzip | 3 |
| &B21 | TBAFS | Archive | tbafs-extractor | 3 |
| &D96 | CFS | Compressor* | riscosarc | 3 |
| &FCA | Squash | Compressor* | riscosarc | 3 |
| &FC8 | DOSDisc | Disk Image | sfdisk+7z | 4 |
| &FCD | FCFS | Disk Image | fcfs2raw (✓ installed) | 4 |
| &B23 | X-Files | Archive | custom | 6 (future) |

_†SparkFS filetypes ZIP archives as &DDC. If riscosarc extraction fails, the worker falls back to unzip and reclassifies the archive as ZIP (RISC OS) so that RISC OS `,xxx` filetype suffixes are still parsed from extracted filenames._

_*Compressor = single-file, decompresses to file with same name (not a directory)_

### PC Formats

| Extension | Name | Type | Tool | Phase |
|-----------|------|------|------|-------|
| .zip | ZIP | Archive | unzip/7z | 3 |
| .rar | RAR | Archive | unrar | 3 |
| .7z | 7-Zip | Archive | 7z | 3 |
| .tar | TAR | Archive | tar | 3 |
| .tar.gz/.tgz | TAR+GZIP | Archive | tar | 3 |
| .tar.bz2/.tbz2 | TAR+BZIP2 | Archive | tar | 3 |
| .tar.xz/.txz | TAR+XZ | Archive | tar | 3 |
| .gz | GZIP | Compressor | gzip | 3 |
| .bz2 | BZIP2 | Compressor | bzip2 | 3 |
| .xz | XZ | Compressor | xz | 3 |
| .zst | ZSTD | Compressor | zstd (✓ installed) | 3 |

## Key Features

### 1. Hierarchical Output Structure

```
/data/outputs/
  {item_uuid}_{item_slug}/
    {artefact_uuid}_{artefact_slug}/
      {analysis_uuid}_{analysis_slug}/
        partition_0_system/
          !Boot
          Archive.arc  (marked as archive)
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

### 3. Safety Features

**Depth Limiting:**
- Default maximum depth: 10 levels (configurable via `MAX_ARCHIVE_DEPTH`)
- Tracked in `ExtractedFile.extraction_depth` field
- Shown in UI when limit exceeded

**Cycle Detection:**
- Traverses parent_file_id chain
- Detects circular references
- Prevents infinite loops

**Resource Limits:**
- Each extraction is a separate analysis job (can be monitored/cancelled)
- Failed extractions don't block other processing

### 4. Processing Flow

```
1. User uploads ADFS disk image → Artefact created
2. FILE_EXTRACTION runs → Extracts files, creates ExtractedFile records with risc_os_filetype
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

## Database Schema Changes

### New Fields

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

### New Analysis Types

```python
ARCHIVE_DETECT = "archive_detect"    # Scan for archives
ARCHIVE_EXTRACT = "archive_extract"  # Extract specific archive
```

## Implementation Phases

### Phase 1: Database and Infrastructure
**Status**: Design complete, ready to implement

- Update database models with new fields
- Create slug generation utilities
- Hierarchical output path functions
- Configuration settings

### Phase 2: Archive Detection
**Status**: Design complete

- Add `ARCHIVE_DETECT` to `AnalysisType` enum
- Detection logic by RISC OS filetype
- Automatic queueing of extraction jobs
- Database fields populated

### Phase 3: Archive Extraction Tools
**Status**: Design complete, fcfs2raw already installed

- Add riscosarc to worker Dockerfile
- Add tbafs-extractor to worker Dockerfile
- Extraction wrapper functions
- Error handling and validation

### Phase 4: Archive Extraction Handler
**Status**: Design complete

- Add `ARCHIVE_EXTRACT` to `AnalysisType` enum
- Implement extraction handler
- Depth checking and cycle detection
- Parent-child file relationships
- Recursive processing

### Phase 5: DOS Partition Table UI
**Status**: Design complete

- Store partition detection results
- Partition table visualization in UI
- Support for uploaded DOS disk images
- Support for extracted DOSDisc files

### Phase 6: Long Filename Support (Future)
**Status**: Deferred to later release

- raFS detection and integration
- LongFiles parser
- X-Files format parser and extractor

## Centralized Archive Definitions

**File**: `myapp/archive_formats.py`

Single source of truth for archive format definitions, used by both web UI and worker:

```python
from myapp.archive_formats import (
    ArchiveType,              # Enum of all formats
    ArchiveCategory,          # ARCHIVE, COMPRESS, or DISK_IMAGE
    get_archive_by_filetype,  # '3fb' -> ArchiveType.ARCFS
    get_archive_by_extension, # 'file.zip' -> ArchiveType.ZIP
    is_archive_format,        # Multi-file archive?
    is_compressor_format,     # Single-file compressor?
    is_disk_image_format,     # Nested disk image?
    get_archive_info,         # Get full metadata dict
)
```

## Configuration

```bash
# Worker configuration
MAX_ARCHIVE_DEPTH=10         # Maximum nesting depth (default: 10)
ARCOLOGY_API=http://web:5000/api
UPLOAD_DIR=/data/uploads
OUTPUT_DIR=/data/outputs      # Hierarchical structure created here
```

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
- Multiple concurrent extractions
- UI display of archives and partition tables
- Output directory structure validation

## Next Steps

To implement:

1. **Start with Phase 1** (Database and Infrastructure)
2. **Then Phase 2** (Archive Detection)
3. **Then Phases 3-4** (Extraction)
4. **Finally Phase 5** (DOS Partition Table UI)

**Phase 6** (Long Filename Support) can be deferred.

For detailed implementation examples including code snippets, Docker configurations, and API handlers, see the git history of this file.
