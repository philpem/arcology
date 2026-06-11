"""
arco bulk-import — Bulk import a file archive into Arcology.

Walks a local directory tree and imports each top-level subdirectory as an
Arcology Item, with all importable files as Artefacts.
"""

import logging
import sys
import tempfile
import zipfile
from pathlib import Path
import requests
from arcology_shared.bundle import BUNDLE_MARKER, is_sidecar_name
from ..client import ArcologyClient, ArcologyError
from ..formatting import format_size

try:
    import zstandard
except ImportError:  # pragma: no cover - zstandard is a declared dependency
    zstandard = None

log = logging.getLogger('arco.bulk-import')

# Raw-sector / disk-image extensions.  Each of these may also appear with a
# trailing compressor suffix (e.g. ``.dd.zst``, ``.img.gz``) — convention is to
# compress the original image immediately after imaging the drive.
RAW_SECTOR_EXTENSIONS = {
    '.adf', '.img', '.ima', '.dsk', '.dd',   # Floppy / generic sector images
    '.raw', '.bin', '.hdd', '.hdf', '.image',  # Hard-disk / generic raw dumps
}

# Compressor suffixes recognised on top of a raw-sector extension, in order of
# preference when several compressed forms of the same image exist.
COMPRESSORS = ('.zst', '.gz', '.bz2')

# Archive containers.  A ``.zip`` / ``.7z`` of a dd image (which may also bundle
# a ddrescue ``.map`` and a readme) is treated as the archived form of the image.
ARCHIVE_EXTENSIONS = {'.zip', '.7z', '.rar', '.tgz'}  # '.tar.gz' handled specially

# Other importable types that are NOT image duplicates (never deduplicated).
OTHER_IMPORTABLE_EXTENSIONS = {
    '.scp',            # Flux images
    '.imd', '.hfe',    # Sector floppy images
    '.iso',            # CD/DVD images
    '.pdf',            # Documents
}

# Dedup ranks: archive beats compressed image beats raw image.
_RANK_ARCHIVE = 3
_RANK_COMPRESSED = 2
_RANK_RAW = 1

# arcarc.nl category name mapping (used by --arcarc preset)
ARCARC_CATEGORY_MAP = {
    'Apps': 'Applications',
    'Games': 'Games',
    'PD': 'Public Domain',
    'Demos': 'Demos',
    'Education': 'Education',
    'Utilities': 'Utilities',
}


# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------

def _make_label_simple(parts: tuple[str, ...]) -> str:
    """Label = path below the category directory."""
    return '/'.join(parts[1:]) if len(parts) > 2 else parts[-1]


def _is_letter_group(s: str) -> bool:
    """True for single-character letter/digit directory groupings (A-Z, 0-9)."""
    return len(s) == 1 and s.isalnum()


def _make_label_arcarc(parts: tuple[str, ...]) -> str:
    """Smart label for arcarc.nl directory structure.

    Strips the category and any single-char letter grouping, then checks
    whether the filename already starts with its parent directory name.
    """
    remaining = parts[1:]

    if len(remaining) > 1 and _is_letter_group(remaining[0]):
        remaining = remaining[1:]

    filename = remaining[-1]
    context = remaining[:-1]

    if not context:
        return filename

    parent_dir = context[-1]
    stem = filename.rsplit('.', 1)[0] if '.' in filename else filename
    if stem.lower().startswith(parent_dir.lower()):
        return filename

    return '/'.join(remaining)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _matched_ext(name: str) -> str | None:
    """Return the importable extension matched at the end of *name* (lowercased).

    Recognises compressed raw-sector images (``foo.dd.zst``), multi-part
    tarballs (``foo.tar.gz``) and any single-suffix importable type.  Returns
    ``None`` if the file is not importable.
    """
    n = name.lower()
    # Compressed raw-sector image: <raw-ext><compressor>
    for raw in RAW_SECTOR_EXTENSIONS:
        for comp in COMPRESSORS:
            if n.endswith(raw + comp):
                return raw + comp
    if n.endswith('.tar.gz'):
        return '.tar.gz'
    suffix = Path(n).suffix
    if (suffix in RAW_SECTOR_EXTENSIONS
            or suffix in ARCHIVE_EXTENSIONS
            or suffix in OTHER_IMPORTABLE_EXTENSIONS):
        return suffix
    return None


def _is_importable(path: Path) -> bool:
    """Check if a file has an importable extension."""
    return _matched_ext(path.name) is not None


# ---------------------------------------------------------------------------
# Compressed / archived duplicate filtering
# ---------------------------------------------------------------------------

def _form_rank(name: str) -> int | None:
    """Dedup rank for *name*, or ``None`` if it is not an image form.

    Only raw-sector images (raw or compressed) and archive containers take part
    in deduplication; flux/floppy/CD images and documents never do.
    """
    ext = _matched_ext(name)
    if ext is None:
        return None
    if ext == '.tar.gz' or ext in ARCHIVE_EXTENSIONS:
        return _RANK_ARCHIVE
    for raw in RAW_SECTOR_EXTENSIONS:
        for comp in COMPRESSORS:
            if ext == raw + comp:
                return _RANK_COMPRESSED
    if ext in RAW_SECTOR_EXTENSIONS:
        return _RANK_RAW
    return None


def _image_base(name: str) -> str:
    """Filename with its importable extension stripped, lowercased."""
    ext = _matched_ext(name)
    return name[:len(name) - len(ext)].lower() if ext else name.lower()


def _raw_type_of(name: str) -> str | None:
    """The raw-sector extension of *name*, ignoring any compressor suffix.

    ``foo.dd.zst`` and ``foo.dd`` both yield ``.dd``.
    """
    ext = _matched_ext(name)
    if ext is None:
        return None
    for comp in COMPRESSORS:
        if ext.endswith(comp) and ext != comp:
            return ext[:-len(comp)]
    return ext


def _compression_pref(name: str) -> tuple[int, int]:
    """Sort key preferring a compressed form, then zst > gz > bz2, over raw."""
    ext = _matched_ext(name) or ''
    for i, comp in enumerate(COMPRESSORS):
        if ext.endswith(comp) and ext != comp:
            return (0, i)   # compressed (preferred), best compressor first
    return (1, 0)           # raw (least preferred)


def _select_best_forms(group: list[dict]) -> list[dict]:
    """Pick the form(s) to keep from a set of same-base image files.

    Archive containers win over every loose image form.  Otherwise each
    distinct raw-sector type is collapsed to its single best (compressed >
    raw) form, so genuinely different images (e.g. a .dd and a .img) are both
    kept while redundant compressions of one image are dropped.
    """
    archives = [f for f in group if _form_rank(f['filename']) == _RANK_ARCHIVE]
    if archives:
        return sorted(archives, key=lambda f: f['filename'])

    by_type: dict[str, list[dict]] = {}
    for f in group:
        by_type.setdefault(_raw_type_of(f['filename']), []).append(f)
    return [min(fs, key=lambda f: _compression_pref(f['filename']))
            for fs in by_type.values()]


def _dedupe_image_forms(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Filter redundant raw/compressed/archived forms of the same image.

    Files are grouped by (containing directory, base name) so that different
    drives that happen to share a base name in different folders are never
    collapsed together.  Returns ``(kept, dropped)``.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    passthrough: list[dict] = []
    for f in files:
        if _form_rank(f['filename']) is None:
            passthrough.append(f)
            continue
        parent = str(Path(f['relative_path']).parent)
        groups.setdefault((parent, _image_base(f['filename'])), []).append(f)

    kept = list(passthrough)
    dropped: list[dict] = []
    for group in groups.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        survivors = _select_best_forms(group)
        survivor_ids = {id(s) for s in survivors}
        kept.extend(survivors)
        dropped.extend(f for f in group if id(f) not in survivor_ids)
    return kept, dropped


# ---------------------------------------------------------------------------
# Sidecar bundling (--bundle-sidecars)
# ---------------------------------------------------------------------------

def _bundle_eligible(filename: str) -> bool:
    """True for raw-sector images (raw or compressed) that may carry sidecars.

    Archive containers and non-image types (iso/scp/pdf/…) are never bundled.
    """
    return _form_rank(filename) in (_RANK_RAW, _RANK_COMPRESSED)


def _is_sidecar(entry: Path, base: str) -> bool:
    """Whether *entry* should be bundled with an image whose base name is *base*."""
    if not entry.is_file() or _is_importable(entry):
        return False
    return is_sidecar_name(entry.name, base)


def _find_sidecars(image_path: Path, base: str) -> list[Path]:
    """Loose sidecar files in *image_path*'s directory to bundle with it."""
    try:
        siblings = sorted(image_path.parent.iterdir())
    except OSError:
        return []
    return [e for e in siblings if e != image_path and _is_sidecar(e, base)]


def _image_is_precompressed(filename: str) -> bool:
    """True if the image is already a compressed/archived form not worth recompressing."""
    ext = _matched_ext(filename) or ''
    if _form_rank(filename) == _RANK_ARCHIVE:
        return True
    return any(ext.endswith(c) and ext != c for c in COMPRESSORS)


# Fast zstd: level 3 (zstd's default — fast, good ratio).  The worker
# decompresses any level with `zstd -d`, so this is a client-side speed/size
# choice only.
_ZSTD_LEVEL = 3

# Errors that may be raised while building a bundle (file IO or compression).
_BUNDLE_ERRORS = (OSError,) if zstandard is None else (OSError, zstandard.ZstdError)


def _add_compressed_image(zf: zipfile.ZipFile, image_path: Path) -> None:
    """Add a raw, uncompressed image to *zf*, compressing it for transfer.

    Preferred path: stream a Zstandard-compressed ``.zst`` member, *stored* in
    the zip — the worker's ``unzip`` extracts the `.zst` and its existing `.zst`
    handling decompresses it.  Streaming avoids writing a separate scratch file.
    Falls back to fast Deflate (zlib level 1, the ``gzip --fast`` equivalent)
    when the ``zstandard`` library is unavailable.
    """
    if zstandard is None:
        # A bundle's image must end up as a recognised compressed disk image
        # (e.g. .dd.zst); a plain DEFLATE of the raw .dd would not be detected as
        # a bundle by the worker.  zstandard is a declared dependency, so this is
        # a guard against a broken install rather than an expected path.
        raise RuntimeError(
            'the zstandard library is required for --bundle-sidecars '
            '(pip install zstandard)')
    info = zipfile.ZipInfo(image_path.name + '.zst')
    info.compress_type = zipfile.ZIP_STORED
    cctx = zstandard.ZstdCompressor(level=_ZSTD_LEVEL, threads=-1)
    # Pass the source size so the content size is written into the zstd frame
    # header, making the frame self-describing for any decompressor.
    size = image_path.stat().st_size
    with open(image_path, 'rb') as src, zf.open(info, 'w') as dst:
        cctx.copy_stream(src, dst, size=size)


def _build_sidecar_bundle(image_path: Path, sidecars: list[Path],
                          base: str, tmp_dir: str) -> Path:
    """Write a zip of the image plus its text sidecars.

    The zip container itself only ever uses STORED or DEFLATE so the worker's
    Info-ZIP ``unzip`` can extract it.  An already-compressed image (`.dd.zst`,
    archive) is stored verbatim; a raw uncompressed image is zstd-compressed (see
    :func:`_add_compressed_image`).  Text sidecars are lightly deflated.  Returns
    the path to the created zip.
    """
    zip_path = Path(tmp_dir) / f'{base}.zip'
    with zipfile.ZipFile(zip_path, 'w') as zf:
        # Mark this as an Arcology disk-image bundle so the worker stores the
        # image as a single artefact instead of extracting a generic archive.
        zf.comment = BUNDLE_MARKER.encode('cp437')
        if _image_is_precompressed(image_path.name):
            zf.write(image_path, arcname=image_path.name,
                     compress_type=zipfile.ZIP_STORED)
        else:
            _add_compressed_image(zf, image_path)
        for sidecar in sidecars:
            zf.write(sidecar, arcname=sidecar.name,
                     compress_type=zipfile.ZIP_DEFLATED, compresslevel=1)
    return zip_path


# ---------------------------------------------------------------------------
# Size limit (--max-size)
# ---------------------------------------------------------------------------

_SIZE_UNITS = {'': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}


def _parse_size(text: str) -> int:
    """Parse a human-readable size (e.g. '50G', '500M', '1048576') into bytes.

    Suffixes K/M/G/T are binary multiples (1024-based); a trailing 'B' is
    allowed (e.g. '50GB').  Raises ValueError on malformed input.
    """
    t = text.strip().upper()
    if t.endswith('B'):
        t = t[:-1]
    if not t:
        raise ValueError(f'invalid size: {text!r}')
    unit = t[-1] if t[-1] in _SIZE_UNITS else ''
    number = t[:-1] if unit else t
    if not number:
        raise ValueError(f'invalid size: {text!r}')
    try:
        value = float(number)
    except ValueError:
        raise ValueError(f'invalid size: {text!r}') from None
    if value < 0:
        raise ValueError(f'invalid size: {text!r}')
    return int(value * _SIZE_UNITS[unit])


def _file_size(path: Path) -> int:
    """On-disk size of *path* in bytes, or 0 if it cannot be stat()ed."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _upload_progress(label: str):
    """Return a progress callback for chunked uploads, or None.

    Only large files (> CHUNKED_THRESHOLD) report progress, and a live updating
    bar is only drawn on an interactive terminal — when output is redirected we
    stay silent to avoid flooding the log with carriage-return spam.
    """
    if not sys.stderr.isatty():
        return None

    def report(done: int, total: int):
        pct = (done / total * 100) if total else 100.0
        sys.stderr.write(f'\r      uploading {label}: {pct:5.1f}% '
                         f'({done}/{total} chunks)')
        if done >= total:
            sys.stderr.write('\n')
        sys.stderr.flush()

    return report


def _apply_dedupe(files: list[dict], label: str) -> list[dict]:
    """Filter redundant image forms from *files*, logging what was dropped."""
    kept, dropped = _dedupe_image_forms(files)
    kept.sort(key=lambda f: f['relative_path'])
    for f in dropped:
        log.debug('  Filtering redundant image form: %s', f['relative_path'])
    if dropped:
        log.info('  %s: dropped %d redundant image form(s) '
                 '(use --keep-compressed-duplicates to keep them)',
                 label, len(dropped))
    return kept


def discover_files(archive_dir: Path, categories: list[str] | None,
                   skip_dirs: set[str] | None,
                   skip_ext: set[str] | None = None,
                   label_fn=_make_label_simple,
                   dedupe: bool = True) -> dict[str, list[dict]]:
    """Walk archive directory and find importable files grouped by top-level dir.

    Files sitting directly in *archive_dir* (one level deep) are grouped into a
    collection named after the archive directory itself rather than skipped.
    """
    collections: dict[str, list[dict]] = {}

    if not archive_dir.is_dir():
        log.error('Archive directory does not exist: %s', archive_dir)
        return collections

    root_name = archive_dir.resolve().name

    for file_path in sorted(archive_dir.rglob('*')):
        if not file_path.is_file():
            continue
        if not _is_importable(file_path):
            continue
        if skip_ext and file_path.suffix.lower() in skip_ext:
            continue

        try:
            rel = file_path.relative_to(archive_dir)
        except ValueError:
            continue

        parts = rel.parts
        if len(parts) < 2:
            # Depth-1: file directly in the archive root.  Group it under a
            # collection named after the archive directory itself.
            category = root_name
            label_path = parts[-1]
        else:
            category = parts[0]
            if skip_dirs and any(part in skip_dirs for part in parts[:-1]):
                continue
            label_path = label_fn(parts)

        if categories and category not in categories:
            continue

        collections.setdefault(category, []).append({
            'path': file_path,
            'relative_path': str(rel),
            'filename': file_path.name,
            'label': label_path,
        })

    if dedupe:
        for category in list(collections):
            collections[category] = _apply_dedupe(collections[category], category)

    return collections


def discover_files_flat(archive_dir: Path,
                        skip_dirs: set[str] | None,
                        skip_ext: set[str] | None = None,
                        dedupe: bool = True) -> list[dict]:
    """Walk archive directory as a flat list (all files in one collection)."""
    files: list[dict] = []

    if not archive_dir.is_dir():
        log.error('Archive directory does not exist: %s', archive_dir)
        return files

    for file_path in sorted(archive_dir.rglob('*')):
        if not file_path.is_file():
            continue
        if not _is_importable(file_path):
            continue
        if skip_ext and file_path.suffix.lower() in skip_ext:
            continue

        try:
            rel = file_path.relative_to(archive_dir)
        except ValueError:
            continue

        parts = rel.parts

        if skip_dirs and len(parts) > 1:
            if any(part in skip_dirs for part in parts[:-1]):
                continue

        label = str(rel) if len(parts) > 1 else parts[0]

        files.append({
            'path': file_path,
            'relative_path': str(rel),
            'filename': file_path.name,
            'label': label,
        })

    if dedupe:
        files = _apply_dedupe(files, archive_dir.resolve().name)

    return files


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_bulk_import_purge(client: ArcologyClient, args):
    """Delete all items with the given tag."""
    log.info('Fetching items tagged "%s"...', args.tag)
    items = client.list_items_all(tag=args.tag)

    if not items:
        log.info('No items found with tag "%s". Nothing to delete.', args.tag)
        return

    log.info('Found %d item(s) to delete:', len(items))
    for item in items:
        artefact_count = item.get('artefact_count', 0)
        log.info('  %s  (%d artefact(s))  %s', item['uuid'][:8], artefact_count, item['name'])

    if not args.yes:
        try:
            answer = input(f'\nDelete these {len(items)} item(s) and all their artefacts? [y/N] ')
        except (EOFError, KeyboardInterrupt):
            print()
            log.info('Aborted.')
            return
        if answer.strip().lower() != 'y':
            log.info('Aborted.')
            return

    deleted = 0
    failed = 0
    for item in items:
        log.info('Deleting: %s ...', item['name'])
        try:
            client.delete_item(item['uuid'])
            deleted += 1
        except ArcologyError as e:
            log.error('  Failed: %s', e)
            failed += 1

    log.info('')
    log.info('Deleted %d item(s), %d failed.', deleted, failed)


def cmd_bulk_import(client: ArcologyClient, args):
    """Main bulk-import command."""
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(message)s', stream=sys.stderr)

    # --arcarc preset
    if args.arcarc:
        if args.tag is None:
            args.tag = 'arcarc'
        if args.name_prefix is None:
            args.name_prefix = 'Arcarc'
        args.smart_labels = True

    # --tag is optional for importing, but required for --purge: without it
    # the purge would match (and delete) every item in the catalogue.
    if args.purge and args.tag is None:
        print('Error: --tag is required with --purge', file=sys.stderr)
        sys.exit(1)

    # Bundling compresses raw images to .zst so the worker recognises them; fail
    # fast (rather than per-file) if the zstandard dependency is missing.
    if args.bundle_sidecars and zstandard is None:
        print('Error: --bundle-sidecars requires the zstandard library '
              '(pip install zstandard)', file=sys.stderr)
        sys.exit(1)

    if args.name_prefix is None:
        args.name_prefix = ''

    # Purge mode
    if args.purge:
        cmd_bulk_import_purge(client, args)
        return

    if not args.archive_dir:
        print('Error: --archive-dir is required (unless --purge)', file=sys.stderr)
        sys.exit(1)

    archive_dir = Path(args.archive_dir)

    categories = None
    if args.categories:
        categories = [c.strip() for c in args.categories.split(',')]

    skip_dirs = None
    if args.skip_dirs:
        skip_dirs = {d.strip() for d in args.skip_dirs.split(',')}

    skip_ext = None
    if args.skip_ext:
        skip_ext = {e.strip().lower() if e.strip().startswith('.') else f'.{e.strip().lower()}'
                    for e in args.skip_ext.split(',')}

    # Build category map
    if args.category_map:
        category_map = {}
        for pair in args.category_map.split(','):
            k, _, v = pair.partition('=')
            if v:
                category_map[k.strip()] = v.strip().strip('"').strip("'")
    elif args.arcarc:
        category_map = ARCARC_CATEGORY_MAP
    else:
        category_map = {}

    label_fn = _make_label_arcarc if args.smart_labels else _make_label_simple

    max_size = None
    if args.max_size:
        try:
            max_size = _parse_size(args.max_size)
        except ValueError:
            print(f"Error: invalid --max-size value '{args.max_size}' "
                  "(use e.g. 50G, 500M)", file=sys.stderr)
            sys.exit(1)

    # Discover files
    dedupe = not args.keep_compressed_duplicates
    log.info('Scanning %s ...', archive_dir)
    if args.flat:
        if categories:
            log.warning('--categories is ignored in --flat mode')
        flat_files = discover_files_flat(archive_dir, skip_dirs, skip_ext,
                                         dedupe=dedupe)
        coll_key = archive_dir.resolve().name
        collections = {coll_key: flat_files} if flat_files else {}
    else:
        collections = discover_files(archive_dir, categories, skip_dirs,
                                     skip_ext, label_fn=label_fn, dedupe=dedupe)

    if not collections:
        log.error('No importable files found.')
        sys.exit(1)

    total_files = sum(len(files) for files in collections.values())
    if args.flat:
        log.info('Found %d file(s)', total_files)
    else:
        log.info('Found %d file(s) in %d collection(s): %s',
                 total_files, len(collections),
                 ', '.join(f'{k} ({len(v)})' for k, v in sorted(collections.items())))

    if skip_dirs:
        log.info('Skipping directories: %s', ', '.join(sorted(skip_dirs)))

    if args.dry_run:
        log.info('')
        log.info('=== DRY RUN ===')
        for coll_name, files in sorted(collections.items()):
            log.info('')
            log.info('  Collection: %s (%d file(s))', coll_name, len(files))
            if args.verbose:
                for f in files:
                    if max_size is not None and _file_size(f['path']) > max_size:
                        log.info('    %s  (TOO BIG — will skip)', f['label'])
                        continue
                    sidecars = []
                    if args.bundle_sidecars and _bundle_eligible(f['filename']):
                        sidecars = _find_sidecars(f['path'], _image_base(f['filename']))
                    if sidecars:
                        log.info('    %s  (bundle +%d: %s)', f['label'], len(sidecars),
                                 ', '.join(s.name for s in sidecars))
                    else:
                        log.info('    %s', f['label'])
        log.info('')
        log.info('Would create %d Item(s) with %d Artefact(s) total',
                 len(collections), total_files)
        return

    # Validate the parent item up front so we fail fast rather than on every
    # collection.  The GET endpoint accepts slug-style identifiers, but the
    # create-item endpoint matches on the raw UUID only — so resolve to the
    # canonical UUID here and pass that to create_item().
    parent_uuid = None
    if args.parent:
        try:
            parent = client.get_item(args.parent)
        except ArcologyError as e:
            print(f'Error: parent item "{args.parent}" not found: {e}', file=sys.stderr)
            sys.exit(1)
        parent_uuid = parent.get('uuid', args.parent)
        log.info('Nesting created items under parent: %s (%s)',
                 parent_uuid[:8], parent.get('name', '?'))

    # Look up platform (once)
    platform_id = client.lookup_platform(args.platform)

    # Process each collection as one Item
    created_items = 0
    uploaded_files = 0
    skipped_files = 0
    failed_uploads = []
    oversized_files = []

    for coll_name, files in sorted(collections.items()):
        if args.name_prefix:
            item_name = f'{args.name_prefix}: {coll_name}'
        else:
            item_name = coll_name
        category_name = category_map.get(coll_name, coll_name)
        category_id = client.lookup_category(category_name)

        log.info('')
        log.info('=== %s (%d file(s)) ===', item_name, len(files))

        # Find or create the Item
        existing_item = client.find_item(item_name, args.tag)

        if existing_item:
            item_uuid = existing_item['uuid']
            log.info('Using existing item: %s', item_uuid[:8])
        else:
            tags = [coll_name.lower()]
            if args.tag:
                tags.insert(0, args.tag)
            item_data = {
                'name': item_name,
                'tags': tags,
            }
            if platform_id:
                item_data['platform_id'] = platform_id
            if category_id:
                item_data['category_id'] = category_id
            if parent_uuid:
                item_data['parent_uuid'] = parent_uuid

            try:
                result = client.create_item(**item_data)
                item_uuid = result['uuid']
                created_items += 1
                log.info('Created item: %s', item_uuid[:8])
            except ArcologyError as e:
                log.error('Failed to create item "%s": %s', item_name, e)
                for f in files:
                    failed_uploads.append(f['relative_path'])
                continue

        # In resume mode, fetch existing filenames once
        existing_filenames = set()
        if args.resume:
            existing_filenames = client.get_item_filenames(item_uuid)
            log.info('  %d artefact(s) already on this item', len(existing_filenames))

        # Upload each file
        for j, file_entry in enumerate(files, 1):
            filepath = file_entry['path']
            filename = file_entry['filename']
            label = file_entry['label']

            # Skip files larger than the configured limit (measured on the
            # source file, before any bundling/compression).
            source_size = _file_size(filepath)
            if max_size is not None and source_size > max_size:
                log.warning('  [%d/%d] SKIPPED — %s exceeds --max-size (%s): %s',
                            j, len(files), format_size(source_size),
                            format_size(max_size), label)
                oversized_files.append(file_entry['relative_path'])
                continue

            # Decide whether this image is bundled with loose sidecars, and
            # what filename the upload will actually carry (a bundle uploads as
            # <base>.zip, which matters for the resume check below).
            sidecars = []
            if args.bundle_sidecars and _bundle_eligible(filename):
                sidecars = _find_sidecars(filepath, _image_base(filename))
            upload_name = f'{_image_base(filename)}.zip' if sidecars else filename

            if args.resume and upload_name in existing_filenames:
                log.debug('  [%d/%d] Skipping (exists): %s', j, len(files), upload_name)
                skipped_files += 1
                continue

            tmp_ctx = None
            upload_path = str(filepath)
            try:
                if sidecars:
                    # Log before building — compressing a large image can take a
                    # while, so the user sees activity rather than a silent hang.
                    log.info('  [%d/%d] %s  (bundling %s image + %d sidecar(s): %s...)',
                             j, len(files), label, format_size(source_size),
                             len(sidecars), ', '.join(s.name for s in sidecars))
                    tmp_ctx = tempfile.TemporaryDirectory(dir=args.bundle_tmpdir or None)
                    bundle = _build_sidecar_bundle(filepath, sidecars,
                                                   _image_base(filename), tmp_ctx.name)
                    upload_path = str(bundle)
                else:
                    log.info('  [%d/%d] %s', j, len(files), label)

                result = client.upload_artefact_retry(
                    item_uuid, upload_path, label,
                    auto_analyse=not args.no_auto_analyse,
                    progress_cb=_upload_progress(label),
                )
            except _BUNDLE_ERRORS as e:
                log.error('    FAILED to bundle/upload %s: %s', filename, e)
                failed_uploads.append(file_entry['relative_path'])
                continue
            except (ArcologyError, requests.ConnectionError) as e:
                log.error('    FAILED: %s: %s', filename, e)
                failed_uploads.append(file_entry['relative_path'])
                continue
            finally:
                if tmp_ctx is not None:
                    tmp_ctx.cleanup()
            uploaded_files += 1
            artefact_type = result.get('artefact_type', '?')
            log.debug('    -> %s (type: %s)', result.get('uuid', '?')[:8], artefact_type)

    # Summary
    log.info('')
    log.info('=== Summary ===')
    log.info('Items created:   %d', created_items)
    log.info('Files uploaded:  %d', uploaded_files)
    log.info('Files skipped:   %d', skipped_files)
    log.info('Files too big:   %d', len(oversized_files))
    log.info('Files failed:    %d', len(failed_uploads))

    if oversized_files:
        log.info('')
        log.info('=== Skipped (exceeded --max-size) ===')
        for path in oversized_files:
            log.info('  %s', path)

    if failed_uploads:
        log.info('')
        log.info('=== Failed uploads ===')
        for path in failed_uploads:
            log.info('  %s', path)

# vim: ts=4 sw=4 et
