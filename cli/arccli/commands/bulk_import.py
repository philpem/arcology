"""
arco bulk-import — Bulk import a file archive into Arcology.

Walks a local directory tree and imports each top-level subdirectory as an
Arcology Item, with all importable files as Artefacts.
"""

import logging
import sys
from pathlib import Path

from ..client import ArcologyClient, ArcologyError

log = logging.getLogger('arco.bulk-import')

# File extensions recognised as importable artefacts
IMPORTABLE_EXTENSIONS = {
    '.adf', '.img', '.ima', '.dsk', '.dd',   # Sector images
    '.scp',                                    # Flux images
    '.imd', '.hfe',                            # Sector floppy images
    '.iso',                                    # CD/DVD images
    '.zip', '.rar',                            # PC archives
    '.tar.gz', '.tgz',                         # Compressed tarballs
    '.dd.zst', '.dd.gz', '.dd.bz2',           # Compressed sector images
    '.pdf',                                    # Documents
}

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

def _is_importable(path: Path) -> bool:
    """Check if a file has an importable extension."""
    name = path.name.lower()
    for ext in ('.tar.gz', '.dd.zst', '.dd.gz', '.dd.bz2'):
        if name.endswith(ext):
            return True
    return path.suffix.lower() in IMPORTABLE_EXTENSIONS


def discover_files(archive_dir: Path, categories: list[str] | None,
                   skip_dirs: set[str] | None,
                   skip_ext: set[str] | None = None,
                   label_fn=_make_label_simple) -> dict[str, list[dict]]:
    """Walk archive directory and find importable files grouped by top-level dir."""
    collections: dict[str, list[dict]] = {}

    if not archive_dir.is_dir():
        log.error('Archive directory does not exist: %s', archive_dir)
        return collections

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
            log.debug('Skipping file in unexpected location: %s', rel)
            continue

        category = parts[0]

        if categories and category not in categories:
            continue

        if skip_dirs:
            if any(part in skip_dirs for part in parts[:-1]):
                continue

        label_path = label_fn(parts)

        if category not in collections:
            collections[category] = []
        collections[category].append({
            'path': file_path,
            'relative_path': str(rel),
            'filename': file_path.name,
            'label': label_path,
        })

    return collections


def discover_files_flat(archive_dir: Path,
                        skip_dirs: set[str] | None,
                        skip_ext: set[str] | None = None) -> list[dict]:
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

    if args.tag is None:
        print('Error: --tag is required (or use --arcarc for arcarc.nl defaults)', file=sys.stderr)
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

    # Discover files
    log.info('Scanning %s ...', archive_dir)
    if args.flat:
        if categories:
            log.warning('--categories is ignored in --flat mode')
        flat_files = discover_files_flat(archive_dir, skip_dirs, skip_ext)
        coll_key = archive_dir.resolve().name
        collections = {coll_key: flat_files} if flat_files else {}
    else:
        collections = discover_files(archive_dir, categories, skip_dirs,
                                     skip_ext, label_fn=label_fn)

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
                    log.info('    %s', f['label'])
        log.info('')
        log.info('Would create %d Item(s) with %d Artefact(s) total',
                 len(collections), total_files)
        return

    # Look up platform (once)
    platform_id = client.lookup_platform(args.platform)

    # Process each collection as one Item
    created_items = 0
    uploaded_files = 0
    skipped_files = 0
    failed_uploads = []

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
            item_data = {
                'name': item_name,
                'tags': [args.tag, coll_name.lower()],
            }
            if platform_id:
                item_data['platform_id'] = platform_id
            if category_id:
                item_data['category_id'] = category_id

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

            if args.resume and filename in existing_filenames:
                log.debug('  [%d/%d] Skipping (exists): %s', j, len(files), filename)
                skipped_files += 1
                continue

            log.info('  [%d/%d] %s', j, len(files), label)
            result = client.upload_artefact_retry(
                item_uuid, str(filepath), label,
                auto_analyse=not args.no_auto_analyse,
            )
            if result and result.get('duplicate'):
                log.debug('    -> skipped (duplicate of %s)', result.get('uuid', '?')[:8])
                skipped_files += 1
            elif result:
                uploaded_files += 1
                artefact_type = result.get('artefact_type', '?')
                log.debug('    -> %s (type: %s)', result.get('uuid', '?')[:8], artefact_type)
            else:
                log.error('    FAILED: %s', filename)
                failed_uploads.append(file_entry['relative_path'])

    # Summary
    log.info('')
    log.info('=== Summary ===')
    log.info('Items created:   %d', created_items)
    log.info('Files uploaded:  %d', uploaded_files)
    log.info('Files skipped:   %d', skipped_files)
    log.info('Files failed:    %d', len(failed_uploads))

    if failed_uploads:
        log.info('')
        log.info('=== Failed uploads ===')
        for path in failed_uploads:
            log.info('  %s', path)

# vim: ts=4 sw=4 et
