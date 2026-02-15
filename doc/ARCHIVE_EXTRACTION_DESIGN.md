# Archive Extraction Implementation Design

## Overview

This document describes the implementation of nested archive and disk image extraction for Arcology, addressing issue #34.

## Requirements

1. **RISC OS Archive Format Support:**
   - **Multi-file archives:**
     - ArcFS (filetype &3FB)
     - PackDir (filetype &68E)
     - Spark (filetype &DDC)
     - TBAFS (filetype &B21)
     - X-Files (filetype &B23) - requires custom extractor
   - **Single-file compressors** (decompress to file with same name):
     - CFS (filetype &D96) - Computer Concepts CFS
     - Squash (filetype &FCA) - Squash compressed file

2. **PC Archive Format Support:**
   - ZIP (including RISC OS filetype extensions)
   - RAR
   - 7-Zip (.7z)
   - TAR (including .tar.gz, .tar.bz2, .tar.xz)
   - Single-file compressors: .gz, .bz2, .xz, .zst

3. **Nested Disk Images:**
   - FCFS (filetype &FCD) - convert with fcfs2raw, then extract as ADFS
   - DOSDisc (filetype &FC8) - extract as PC disk image

4. **Long Filename Systems:**
   - raFS (identified by `!Atterer` file)
   - LongFiles (identified by `!ZZ!!Z!LF` file)
   - X-Files (filetype &B23) - archive format with long filename support

5. **Direct Upload Support:**
   - Archives uploaded directly (not from within disk images) should be processed the same way
   - Existing ANALYSIS_MAP infrastructure already handles this

4. **Output Directory Structure:**
   ```
   /data/outputs/
     {item_uuid}_{item_slug}/
       {artefact_uuid}_{artefact_slug}/
         {analysis_uuid}_{analysis_slug}/
           {partition_id}/  (if partitions exist)
             (extracted files)
   ```

5. **Safety Requirements:**
   - Detect and stop on self-referential archives (quines, trojans)
   - Configurable maximum depth (default: 10 levels)
   - Show depth limit exceeded in UI
   - Prevent disk space exhaustion

6. **DOS Partition Table Display:**
   - Show partition table for DOSDisc files (filetype &FC8)
   - Show for uploaded artifacts with DOS partitions
   - Store partition detection results
   - Later extend to Filecore partitioning (ICS, HCCS, Simtec IDE)

## Database Schema Changes

### 1. Add Slugs (Immutable Once Set)

```python
# Add to Item model
class Item(db.Model):
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

# Add to Artefact model
class Artefact(db.Model):
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

# Add to Analysis model
class Analysis(db.Model):
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

# Add to Partition model
class Partition(db.Model):
    slug: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
```

### 2. Archive Support in ExtractedFile

```python
class ExtractedFile(db.Model):
    # Existing fields...

    # New fields for archive support
    parent_file_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("extracted_files.id"), nullable=True, index=True
    )
    is_archive: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    archive_format: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    risc_os_filetype: Mapped[Optional[str]] = mapped_column(String(3), nullable=True, index=True)
    extraction_depth: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    parent_file: Mapped[Optional["ExtractedFile"]] = relationship(
        "ExtractedFile",
        remote_side=[id],
        foreign_keys=[parent_file_id],
        backref="child_files"
    )
```

### 3. Store Partition Detection Results

```python
class Partition(db.Model):
    # Existing fields...

    # New field for partition detection details (JSON)
    detection_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Stores: partition table type (MBR, GPT, Filecore), partition entries, etc.
```

### 4. New Analysis Types

```python
class AnalysisType(enum.Enum):
    # Existing...

    # New archive-related analysis types
    ARCHIVE_DETECT = "archive_detect"      # Scan for archives by filetype
    ARCHIVE_EXTRACT = "archive_extract"    # Extract a specific archive
```

## Slug Generation Algorithm

```python
import re

def generate_slug(text: str, max_length: int = 200) -> str:
    """
    Generate a URL-safe slug from text.

    - Convert to lowercase
    - Replace common accession number separators with dash
    - Remove unsafe characters
    - Collapse multiple dashes
    - Trim to max_length
    """
    # Convert to lowercase
    slug = text.lower()

    # Common accession number field separators
    separators = ['/', '.', ':', ';', ',', '_', ' ']
    for sep in separators:
        slug = slug.replace(sep, '-')

    # Remove non-alphanumeric except dash
    slug = re.sub(r'[^a-z0-9-]', '', slug)

    # Collapse multiple dashes
    slug = re.sub(r'-+', '-', slug)

    # Strip leading/trailing dashes
    slug = slug.strip('-')

    # Truncate to max length
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip('-')

    return slug or 'untitled'


def get_or_create_slug(obj, text_field: str) -> str:
    """
    Get existing slug or create and save new one.
    Slugs are immutable once set.
    """
    if obj.slug:
        return obj.slug

    text = getattr(obj, text_field)
    slug = generate_slug(text)
    obj.slug = slug
    db.session.commit()
    return slug
```

## Output Directory Structure

```python
from pathlib import Path

def get_output_path(item, artefact, analysis, partition=None) -> Path:
    """
    Generate hierarchical output directory path.

    Returns: /data/outputs/{item_uuid}_{item_slug}/{artefact_uuid}_{artefact_slug}/
                           {analysis_uuid}_{analysis_slug}/{partition_id}/
    """
    from .config import OUTPUT_DIR

    # Get or create slugs (immutable)
    item_slug = get_or_create_slug(item, 'name')
    artefact_slug = get_or_create_slug(artefact, 'label')
    analysis_slug = get_or_create_slug(analysis, 'analysis_type')

    # Build path
    path = OUTPUT_DIR / f"{item.uuid}_{item_slug}"
    path = path / f"{artefact.uuid}_{artefact_slug}"
    path = path / f"{analysis.uuid}_{analysis_slug}"

    if partition:
        partition_slug = get_or_create_slug(partition, 'label') if partition.label else str(partition.partition_index)
        path = path / f"partition_{partition.partition_index}_{partition_slug}"

    path.mkdir(parents=True, exist_ok=True)
    return path
```

## Centralized Archive Format Definitions

**Location:** `myapp/archive_formats.py`

This module provides a single source of truth for all archive format definitions, used by both the web application and worker. Key features:

1. **Archive Categories:**
   - `ARCHIVE` - Multi-file archives that create a directory when extracted
   - `COMPRESS` - Single-file compressors (e.g., CFS, Squash, .gz, .bz2)
   - `DISK_IMAGE` - Nested disk images requiring special handling

2. **Format Metadata:**
   - RISC OS filetype (if applicable)
   - File extensions
   - Extraction tool
   - Whether extraction creates a directory or single file
   - Output filename behavior for compressors

3. **Lookup Functions:**
   ```python
   from myapp.archive_formats import (
       get_archive_by_filetype,   # '3fb' -> ArchiveType.ARCFS
       get_archive_by_extension,  # 'file.zip' -> ArchiveType.ZIP
       is_archive_filetype,        # Check if filetype is archive
       is_archive_format,          # Check if multi-file archive
       is_compressor_format,       # Check if single-file compressor
       is_disk_image_format,       # Check if nested disk image
       get_archive_info,           # Get full format metadata
   )
   ```

4. **UI Integration:**
   - Web templates import from `myapp.archive_formats`
   - Worker imports from `myapp.archive_formats` (shared via volume mount)
   - Single update point for adding new formats

**Example Format Definition:**
```python
ArchiveType.CFS: {
    'name': 'CFS Compressed File',
    'category': ArchiveCategory.COMPRESS,
    'risc_os_filetype': 'd96',
    'extensions': [],
    'tool': 'riscosarc',
    'description': 'Computer Concepts CFS - decompresses to single file with same name',
    'extract_creates_dir': False,
    'output_filename': 'same_as_input',  # Special handling for compressors
}
```

See `myapp/archive_formats.py` for complete definitions.

## Direct Upload Handling

Archives can be uploaded in two ways:

1. **Found within disk images** - extracted during FILE_LISTING/FILE_EXTRACTION
2. **Uploaded directly** - user uploads archive file as an Artefact

Both paths converge on the same processing:

### Direct Upload Flow

```
1. User uploads ZIP file → Artefact created with type=ZIP
2. queue_analyses_for_artefact() checks ANALYSIS_MAP
3. ANALYSIS_MAP[ArtefactType.ZIP] = [AnalysisType.FILE_LISTING]
4. FILE_LISTING runs → extracts files, creates Partition + ExtractedFile records
5. ARCHIVE_DETECT runs → scans for nested archives
6. Process continues recursively
```

### Existing ANALYSIS_MAP (in myapp/blueprints/artefacts.py)

Already supports direct archive uploads:
```python
ANALYSIS_MAP = {
    ArtefactType.ZIP: [AnalysisType.FILE_LISTING],
    ArtefactType.TARGZ: [AnalysisType.FILE_LISTING],
    ArtefactType.RAR: [AnalysisType.FILE_LISTING],
    # ... other types
}
```

**New ArtefactType entries needed:**
- Add RISC OS archive types (ARCFS, SPARK, PACKDIR, TBAFS, CFS, SQUASH)
- Add FCFS and DOSDISC types
- All map to [AnalysisType.FILE_LISTING] (or FILE_EXTRACTION for compressors)

**Update needed in artefacts.py:**
- Import from `myapp.archive_formats` instead of local definitions
- Auto-queue ARCHIVE_DETECT after FILE_LISTING completes

## Archive Depth Tracking and Limits

```python
# In worker/arcworker/config.py

MAX_ARCHIVE_DEPTH = int(os.environ.get('MAX_ARCHIVE_DEPTH', '10'))


# In archive extraction logic

def check_archive_depth(parent_file_id: int, max_depth: int = MAX_ARCHIVE_DEPTH) -> int:
    """
    Calculate extraction depth by traversing parent chain.
    Raises ValueError if depth exceeds max_depth.
    """
    depth = 0
    current_id = parent_file_id
    visited = set()

    while current_id is not None:
        if current_id in visited:
            raise ValueError(f"Circular archive reference detected: {current_id}")

        visited.add(current_id)
        depth += 1

        if depth > max_depth:
            raise ValueError(f"Archive depth limit exceeded (max: {max_depth})")

        # Get parent of current file
        file = ExtractedFile.query.get(current_id)
        if not file:
            break
        current_id = file.parent_file_id

    return depth
```

## Archive Detection Handler

```python
def process_archive_detect(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Scan partition for archive files and queue extraction.

    Detects both RISC OS archives (by filetype) and PC archives (by extension).
    Handles multi-file archives, single-file compressors, and nested disk images.
    """
    from myapp.archive_formats import (
        get_archive_by_filetype,
        get_archive_by_extension,
        get_archive_info,
        is_compressor_format,
    )

    analysis_id = analysis['id']
    partition_id = analysis.get('hints', {}).get('partition_id')

    if not partition_id:
        self.api.update_analysis(
            analysis_id,
            status='failed',
            success=False,
            error_message='No partition_id in analysis hints'
        )
        return

    # Get all files in partition
    files = self.api.get_partition_files(partition_id)

    archive_count = 0
    queued_count = 0
    depth_limit_exceeded = 0
    compressor_count = 0

    for file_data in files:
        filetype = file_data.get('risc_os_filetype')
        filename = file_data.get('filename', '')

        # Try detecting by RISC OS filetype first
        archive_type = get_archive_by_filetype(filetype) if filetype else None

        # Fall back to extension-based detection (for PC archives)
        if not archive_type:
            archive_type = get_archive_by_extension(filename)

        if not archive_type:
            continue

        archive_info = get_archive_info(archive_type)

        # Check if this is a single-file compressor (CFS, Squash, .gz, etc.)
        is_compressor = is_compressor_format(archive_type)

        # Check depth limit
        current_depth = file_data.get('extraction_depth', 0)
        if current_depth >= MAX_ARCHIVE_DEPTH:
            depth_limit_exceeded += 1
            self.api.mark_file_as_archive(
                file_data['id'],
                is_archive=True,
                archive_format=archive_info['name'],
                depth_exceeded=True
            )
            continue

        # Mark as archive or compressor
        self.api.mark_file_as_archive(
            file_data['id'],
            is_archive=True,
            archive_format=archive_info['name'],
            is_compressor=is_compressor  # Track if single-file compressor
        )
        archive_count += 1
        if is_compressor:
            compressor_count += 1

        # Queue extraction
        self.api.queue_analysis(
            artefact['id'],
            AnalysisType.ARCHIVE_EXTRACT.value,
            hints={
                'file_id': file_data['id'],
                'partition_id': partition_id,
                'archive_type': archive_type.value,  # Pass full type info
                'archive_format': archive_info['name'],
                'is_compressor': is_compressor,
                'extraction_depth': current_depth + 1
            }
        )
        queued_count += 1

    summary = f"Detected {archive_count} archives ({compressor_count} compressors), queued {queued_count} for extraction"
    if depth_limit_exceeded > 0:
        summary += f", {depth_limit_exceeded} at depth limit"

    self.api.update_analysis(
        analysis_id,
        status='completed',
        success=True,
        summary=summary,
        details=json.dumps({
            'archives_found': archive_count,
            'compressors_found': compressor_count,
            'extractions_queued': queued_count,
            'depth_limit_exceeded': depth_limit_exceeded
        })
    )
```

## Archive Extraction Tools

### riscosarc Installation (Dockerfile)

```dockerfile
# Build riscosarc
FROM debian:bookworm-slim AS build-riscosarc

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/mjwoodcock/riscosarc.git /src/riscosarc && \
    cd /src/riscosarc && \
    make

# Copy to final image
COPY --from=build-riscosarc /src/riscosarc/riscosarc /usr/local/bin/
```

### TBAFS Extractor Installation

```dockerfile
# Build tbafs-extractor
FROM debian:bookworm-slim AS build-tbafs

RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/mattgodbolt/tbafs.git /src/tbafs && \
    cd /src/tbafs && \
    make

COPY --from=build-tbafs /src/tbafs/tbafs /usr/local/bin/tbafs-extractor
```

### Extraction Functions

```python
# In worker/arcworker/tools/archives.py

def extract_riscosarc(input_path: Path, output_dir: Path) -> dict:
    """
    Extract archive using riscosarc.
    Supports: ArcFS, CFS, PackDir, Squash, Spark.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = ['riscosarc', '-x', str(input_path)]
    result = subprocess.run(
        cmd,
        cwd=str(output_dir),
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'riscosarc failed: {result.stderr}',
            'tool': 'riscosarc'
        }

    # Count extracted files
    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': 'riscosarc',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files'
    }


def extract_tbafs(input_path: Path, output_dir: Path) -> dict:
    """Extract TBAFS archive."""
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = ['tbafs-extractor', str(input_path), str(output_dir)]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'tbafs-extractor failed: {result.stderr}',
            'tool': 'tbafs-extractor'
        }

    file_count = sum(1 for _ in output_dir.rglob('*') if _.is_file())

    return {
        'success': True,
        'tool': 'tbafs-extractor',
        'file_count': file_count,
        'summary': f'Extracted {file_count} files'
    }


def convert_fcfs_to_raw(input_path: Path, output_path: Path) -> dict:
    """Convert FCFS image to raw sector image."""
    cmd = ['fcfs2raw', '-v', str(input_path), str(output_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return {
            'success': False,
            'error': f'fcfs2raw failed: {result.stderr}',
            'tool': 'fcfs2raw'
        }

    return {
        'success': True,
        'tool': 'fcfs2raw',
        'output_path': str(output_path),
        'summary': 'FCFS converted to raw image'
    }
```

## Archive Extraction Handler

```python
def process_archive_extract(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Extract a specific archive file or decompress a compressed file.

    Handles:
    - Multi-file archives (creates directory with contents)
    - Single-file compressors (creates single decompressed file)
    - Nested disk images (converts then extracts)

    Registers extracted files with parent_file_id set, and queues
    archive_detect for recursive processing.
    """
    from myapp.archive_formats import (
        ArchiveType,
        get_archive_info,
        is_compressor_format,
    )

    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')

    file_id = hints.get('file_id')
    partition_id = hints.get('partition_id')
    archive_type_str = hints.get('archive_type')
    is_compressor = hints.get('is_compressor', False)
    extraction_depth = hints.get('extraction_depth', 1)

    # Get ArchiveType enum from string
    try:
        archive_type = ArchiveType(archive_type_str)
        archive_info = get_archive_info(archive_type)
    except (ValueError, KeyError):
        self.api.update_analysis(
            analysis_id,
            status='failed',
            success=False,
            error_message=f'Unknown archive type: {archive_type_str}'
        )
        return

    # Get the actual file path
    # TODO: Implement get_extracted_file_path to retrieve actual file
    archive_path = self.get_extracted_file_path(file_id, partition_id, artefact)

    if not archive_path or not archive_path.exists():
        self.api.update_analysis(
            analysis_id,
            status='failed',
            success=False,
            error_message=f'Archive file not found: {file_id}'
        )
        return

    # Choose output location based on type
    if is_compressor:
        # Compressors create a single file, not a directory
        output_file = work_dir / archive_path.stem  # Remove compression extension
    else:
        # Archives create a directory
        output_dir = work_dir / 'archive_contents'

    # Choose extraction method based on archive type
    if archive_type in [ArchiveType.ARCFS, ArchiveType.PACKDIR,
                        ArchiveType.SPARK, ArchiveType.CFS, ArchiveType.SQUASH]:
        # All handled by riscosarc
        if is_compressor:
            result = extract_riscosarc_single(archive_path, output_file)
        else:
            result = extract_riscosarc(archive_path, output_dir)

    elif archive_type == ArchiveType.TBAFS:
        result = extract_tbafs(archive_path, output_dir)

    elif archive_type == ArchiveType.FCFS:
        # Convert to raw, then extract as ADFS
        raw_path = work_dir / 'converted.img'
        conv_result = convert_fcfs_to_raw(archive_path, raw_path)
        if not conv_result['success']:
            result = conv_result
        else:
            result = list_files_dim(raw_path)  # Reuse existing function

    elif archive_type == ArchiveType.DOSDISC:
        result = extract_dos_7z(archive_path, output_dir)

    elif archive_type == ArchiveType.ZIP:
        result = extract_zip(archive_path, output_dir)

    elif archive_type in [ArchiveType.TAR, ArchiveType.TARGZ,
                          ArchiveType.TARBZ2, ArchiveType.TARXZ]:
        result = extract_tar(archive_path, output_dir, archive_type)

    elif archive_type == ArchiveType.RAR:
        result = extract_rar(archive_path, output_dir)

    elif archive_type == ArchiveType.SEVENZ:
        result = extract_7z(archive_path, output_dir)

    elif archive_type in [ArchiveType.GZIP, ArchiveType.BZIP2,
                          ArchiveType.XZ, ArchiveType.ZSTD]:
        # Single-file compressors
        result = decompress_single_file(archive_path, output_file, archive_type)

    else:
        self.api.update_analysis(
            analysis_id,
            status='failed',
            success=False,
            error_message=f'Unsupported archive type: {archive_type.value}'
        )
        return

    if not result['success']:
        self.api.update_analysis(
            analysis_id,
            status='failed',
            success=False,
            error_message=result.get('error', 'Extraction failed'),
            tool_name=result.get('tool')
        )
        return

    # Scan extracted files and register with parent_file_id
    files = self.scan_directory_for_files(output_dir)

    # Add parent_file_id and extraction_depth to all files
    for file_data in files:
        file_data['parent_file_id'] = file_id
        file_data['extraction_depth'] = extraction_depth

    # Register files
    self.api.register_file_listing(
        partition_id=partition_id,
        files=files
    )

    # Queue archive_detect for nested archives (if under depth limit)
    if extraction_depth < MAX_ARCHIVE_DEPTH:
        self.api.queue_analysis(
            artefact['id'],
            AnalysisType.ARCHIVE_DETECT.value,
            hints={'partition_id': partition_id}
        )

    self.api.update_analysis(
        analysis_id,
        status='completed',
        success=True,
        tool_name=result['tool'],
        summary=result['summary'],
        details=json.dumps({
            'file_count': result.get('file_count'),
            'extraction_depth': extraction_depth
        })
    )
```

## DOS Partition Table Storage

```python
# In worker/arcworker/tools/partition.py

def detect_partitions_sfdisk(image_path: Path) -> dict:
    """
    Detect partitions using sfdisk.
    Returns partition table details for storage in database.
    """
    cmd = ['sfdisk', '-J', str(image_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        return {
            'success': False,
            'error': result.stderr
        }

    try:
        partition_data = json.loads(result.stdout)
        return {
            'success': True,
            'partition_table': partition_data,
            'table_type': partition_data.get('partitiontable', {}).get('label'),
            'partitions': partition_data.get('partitiontable', {}).get('partitions', [])
        }
    except json.JSONDecodeError as e:
        return {
            'success': False,
            'error': f'Failed to parse sfdisk output: {e}'
        }


# In process_partition_detect handler, store results:
def process_partition_detect(self, analysis: dict, artefact: dict, work_dir: Path):
    # ... existing code ...

    # Store partition detection details
    if partition_result['success']:
        self.api.store_partition_detection_results(
            artefact_id=artefact['id'],
            detection_data=partition_result['partition_table']
        )
```

## UI Changes

### 1. Partition Table Display

```html
<!-- In artefact detail page -->
{% if artefact.partition_detection_results %}
<div class="partition-table">
    <h3>Partition Table</h3>
    <table class="table">
        <thead>
            <tr>
                <th>Index</th>
                <th>Type</th>
                <th>Start</th>
                <th>Size</th>
                <th>Filesystem</th>
            </tr>
        </thead>
        <tbody>
            {% for partition in artefact.partition_detection_results.partitions %}
            <tr>
                <td>{{ partition.node }}</td>
                <td>{{ partition.type }}</td>
                <td>{{ partition.start }}</td>
                <td>{{ partition.size }}</td>
                <td>{{ partition.filesystem or 'Unknown' }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>
{% endif %}
```

### 2. Archive File Display

```html
<!-- In file listing -->
<tr class="{% if file.is_archive %}archive-file{% endif %} {% if file.extraction_depth > 0 %}nested-file depth-{{ file.extraction_depth }}{% endif %}">
    <td>
        {% if file.is_archive %}
            <i class="bi bi-archive"></i>
        {% endif %}
        {{ file.filename }}
    </td>
    <td>{{ file.archive_format or file.risc_os_filetype }}</td>
    <td>{{ file.file_size | filesizeformat }}</td>
    <td>
        {% if file.extraction_depth >= max_archive_depth %}
            <span class="badge bg-warning">Depth Limit Reached</span>
        {% endif %}
    </td>
</tr>
```

## Implementation Phases

### Phase 1: Database and Infrastructure
1. Create database migration for slugs
2. Create database migration for archive support in ExtractedFile
3. Implement slug generation functions
4. Implement hierarchical output directory structure
5. Add MAX_ARCHIVE_DEPTH config setting

### Phase 2: Archive Detection
1. Add ARCHIVE_DETECT analysis type
2. Implement process_archive_detect handler
3. Update file_listing to store risc_os_filetype
4. Auto-queue archive_detect after file_listing

### Phase 3: Archive Extraction Tools
1. Add riscosarc to Docker image
2. Add tbafs-extractor to Docker image
3. Implement extract_riscosarc function
4. Implement extract_tbafs function
5. Implement FCFS conversion (already have fcfs2raw)

### Phase 4: Archive Extraction Handler
1. Add ARCHIVE_EXTRACT analysis type
2. Implement process_archive_extract handler
3. Implement recursive archive detection
4. Add cycle detection
5. Add depth limit checking

### Phase 5: DOS Partition Table
1. Store partition detection results in database
2. Create UI component to display partition tables
3. Extend to DOSDisc files (filetype &FC8)

### Phase 6: Long Filename Support (Future)
1. Implement raFS detection and conversion
2. Implement LongFiles detection and parsing
3. Implement X-Files parser and extractor

## Testing Plan

1. **Basic Archive Extraction:**
   - Upload ArcFS archive → verify extraction
   - Upload Spark archive → verify extraction
   - Check file listing shows correct filetypes

2. **Nested Archives:**
   - Create archive containing archive → verify recursive extraction
   - Verify depth tracking
   - Verify depth limit enforcement

3. **Cycle Detection:**
   - Create self-referential archive (archive containing itself)
   - Verify detection and graceful failure

4. **FCFS/DOSDisc:**
   - Extract ADFS image with FCFS file → verify conversion and extraction
   - Extract ADFS image with DOSDisc file → verify extraction

5. **Output Paths:**
   - Verify hierarchical directory structure
   - Verify slugs are generated and stored
   - Verify slugs are immutable

6. **DOS Partition Table:**
   - Upload disk image with DOS partitions
   - Verify partition table is displayed in UI
