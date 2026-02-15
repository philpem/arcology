# Archive Extraction Implementation Design

## Overview

This document describes the implementation of nested archive and disk image extraction for Arcology, addressing issue #34.

## Requirements

1. **Archive Format Support:**
   - ArcFS (filetype &3FB)
   - CFS (filetype &D96)
   - PackDir (filetype &68E)
   - Squash (filetype &FCA)
   - Spark (filetype &DDC)
   - TBAFS (filetype &B21)
   - Zip with RISC OS filetypes (custom Info-Zip build)

2. **Nested Disk Images:**
   - FCFS (filetype &FCD) - convert with fcfs2raw, then extract as ADFS
   - DOSDisc (filetype &FC8) - extract as PC disk image

3. **Long Filename Systems:**
   - raFS (identified by `!Atterer` file)
   - LongFiles (identified by `!ZZ!!Z!LF` file)
   - X-Files (filetype &B23) - requires custom extractor

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

## RISC OS Archive Type Detection

```python
# In worker/arcworker/tools/archives.py

RISC_OS_ARCHIVE_TYPES = {
    '3fb': {
        'name': 'ArcFS',
        'tool': 'riscosarc',
        'description': 'ArcFS archive'
    },
    'd96': {
        'name': 'CFS',
        'tool': 'riscosarc',
        'description': 'Computer Concepts CFS compressed file'
    },
    '68e': {
        'name': 'PackDir',
        'tool': 'riscosarc',
        'description': 'PackDir archive'
    },
    'fca': {
        'name': 'Squash',
        'tool': 'riscosarc',
        'description': 'Squash compressed file'
    },
    'ddc': {
        'name': 'Spark',
        'tool': 'riscosarc',
        'description': 'Spark archive'
    },
    'b21': {
        'name': 'TBAFS',
        'tool': 'tbafs-extractor',
        'description': 'TBAFS archive'
    },
    'fc8': {
        'name': 'DOSDisc',
        'tool': 'sfdisk+7z',
        'description': 'PC hard disk image'
    },
    'fcd': {
        'name': 'FCFS',
        'tool': 'fcfs2raw',
        'description': 'Filecore hard disk image'
    },
    'b23': {
        'name': 'X-Files',
        'tool': 'custom',
        'description': 'X-Files archive'
    },
}

def is_archive_filetype(filetype: str) -> bool:
    """Check if RISC OS filetype is a known archive format."""
    return filetype and filetype.lower() in RISC_OS_ARCHIVE_TYPES

def get_archive_info(filetype: str) -> dict:
    """Get archive format information."""
    return RISC_OS_ARCHIVE_TYPES.get(filetype.lower())
```

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

    Looks for RISC OS filetypes that indicate archives.
    Marks files as archives and queues ARCHIVE_EXTRACT analysis.
    """
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

    for file_data in files:
        filetype = file_data.get('risc_os_filetype')

        if not is_archive_filetype(filetype):
            continue

        archive_info = get_archive_info(filetype)

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

        # Mark as archive
        self.api.mark_file_as_archive(
            file_data['id'],
            is_archive=True,
            archive_format=archive_info['name']
        )
        archive_count += 1

        # Queue extraction
        self.api.queue_analysis(
            artefact['id'],
            AnalysisType.ARCHIVE_EXTRACT.value,
            hints={
                'file_id': file_data['id'],
                'partition_id': partition_id,
                'archive_format': archive_info['name'],
                'extraction_depth': current_depth + 1
            }
        )
        queued_count += 1

    summary = f"Detected {archive_count} archives, queued {queued_count} for extraction"
    if depth_limit_exceeded > 0:
        summary += f", {depth_limit_exceeded} at depth limit"

    self.api.update_analysis(
        analysis_id,
        status='completed',
        success=True,
        summary=summary,
        details=json.dumps({
            'archives_found': archive_count,
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
    Extract a specific archive file.

    Extracts archive contents, registers as ExtractedFile records with
    parent_file_id set, and queues archive_detect for recursive processing.
    """
    analysis_id = analysis['id']
    hints = json.loads(analysis.get('hints') or '{}')

    file_id = hints.get('file_id')
    partition_id = hints.get('partition_id')
    archive_format = hints.get('archive_format')
    extraction_depth = hints.get('extraction_depth', 1)

    # TODO: Implement get_extracted_file_path to retrieve actual file
    # For now, this is a placeholder
    archive_path = self.get_extracted_file_path(file_id, partition_id, artefact)

    if not archive_path or not archive_path.exists():
        self.api.update_analysis(
            analysis_id,
            status='failed',
            success=False,
            error_message=f'Archive file not found: {file_id}'
        )
        return

    output_dir = work_dir / 'archive_contents'

    # Choose extraction method based on format
    if archive_format in ['ArcFS', 'CFS', 'PackDir', 'Squash', 'Spark']:
        result = extract_riscosarc(archive_path, output_dir)
    elif archive_format == 'TBAFS':
        result = extract_tbafs(archive_path, output_dir)
    elif archive_format == 'FCFS':
        # Convert to raw, then extract as ADFS
        raw_path = work_dir / 'converted.img'
        conv_result = convert_fcfs_to_raw(archive_path, raw_path)
        if not conv_result['success']:
            result = conv_result
        else:
            result = list_files_dim(raw_path)  # Reuse existing function
    elif archive_format == 'DOSDisc':
        result = extract_dos_7z(archive_path, output_dir)
    else:
        self.api.update_analysis(
            analysis_id,
            status='failed',
            success=False,
            error_message=f'Unsupported archive format: {archive_format}'
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
