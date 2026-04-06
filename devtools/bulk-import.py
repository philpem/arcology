#!/usr/bin/env python3
"""
bulk-import — Bulk import a file archive into Arcology.

Walks a local directory tree and imports each top-level subdirectory as an
Arcology Item, with all importable files as Artefacts.  The Arcology worker
pipeline handles extraction, hashing, archive detection, file listing, and
product recognition automatically.

Structure:
    Each top-level directory becomes one Arcology Item.  Every importable file
    within that directory becomes an Artefact on that Item, labelled with its
    path below the category directory.

Usage:
    bulk-import.py --archive-dir /path/to/archive --tag TAG [options]

Options:
    --archive-dir PATH    Local mirror root (required unless --purge)
    --api-url URL         Arcology API URL (default: $ARCOLOGY_API or http://localhost:5000/api)
    --api-key KEY         API key (default: $WORKER_API_KEY)
    --tag TAG             Tag for all imported items (required unless --arcarc)
    --categories LIST     Filter by top-level directory (e.g. --categories Apps)
    --skip-dirs LIST      Comma-separated directory names to skip
    --skip-ext LIST       Comma-separated extensions to skip (e.g. .pdf,.txt)
    --platform NAME       Platform name to assign (looked up from Arcology)
    --name-prefix PREFIX  Prefix for item names (e.g. --name-prefix Source)
    --category-map K=V,.. Map directory names to Arcology categories
    --no-auto-analyse     Upload without triggering analysis
    --flat                Treat archive-dir as one collection (one Item, all files)
    --smart-labels        Use smart label heuristic (see below)
    --arcarc              Preset for arcarc.nl (sets tag, prefix, categories, smart labels)
    --verbose / -v        Verbose logging
    --dry-run             Scan only, do not import
    --resume              Skip artefacts whose filename already exists on the Item
    --purge               Delete all items with the given tag (cleanup)
    --yes / -y            Skip confirmation prompt for --purge

Smart labels (--smart-labels):
    By default, the artefact label is the full path below the top-level
    directory.  With --smart-labels, the tool applies two heuristics:

    1. Single-character directory groupings (A-Z, 0-9) are stripped, since
       they are just alphabetical index directories.
    2. If the filename already starts with its parent directory name, only
       the filename is used (the parent path is redundant).

    Without --smart-labels:
        Apps/A/ArcFS 2/ArcFS 2 (1995)(VTI).zip  ->  A/ArcFS 2/ArcFS 2 (1995)(VTI).zip
        Apps/G/GCC (FR)/gcc-2.4.5/g++lib.spk     ->  G/GCC (FR)/gcc-2.4.5/g++lib.spk

    With --smart-labels:
        Apps/A/ArcFS 2/ArcFS 2 (1995)(VTI).zip  ->  ArcFS 2 (1995)(VTI).zip
        Apps/G/GCC (FR)/gcc-2.4.5/g++lib.spk     ->  GCC (FR)/gcc-2.4.5/g++lib.spk

    --arcarc enables --smart-labels automatically.

Examples:
    # Import a directory tree:
    bulk-import.py --archive-dir ~/my-archive --tag myimport

    # Import a flat directory of disc images as a single Item:
    bulk-import.py --archive-dir ~/my-discs --tag myimport --flat

    # Import with a name prefix and platform:
    bulk-import.py --archive-dir ~/discs --tag acorn --name-prefix "Acorn" \\
        --platform "Acorn Archimedes"

    # Import arcarc.nl archive (preset sets tag, prefix, category map):
    bulk-import.py --archive-dir ~/arcarc/archive --arcarc

    # Arcarc: just the Apps subdirectory, dry run:
    bulk-import.py --archive-dir ~/arcarc/archive --arcarc --categories Apps --dry-run

    # Resume after interruption:
    bulk-import.py --archive-dir ~/my-archive --tag myimport --resume

    # Delete all items with a given tag:
    bulk-import.py --purge --tag myimport --api-key KEY
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger('bulk-import')

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


def build_client(api_url: str, api_key: str):
    """Create an authenticated requests session."""
    session = requests.Session()
    session.headers.update({
        'X-API-Key': api_key,
        'Authorization': f'Bearer {api_key}',
    })
    return session, api_url.rstrip('/')


def fetch_all_items_by_tag(session, api_url: str, tag: str) -> list[dict]:
    """Fetch all items with the given tag, handling pagination."""
    items = []
    page = 1
    while True:
        resp = session.get(f'{api_url}/items', params={
            'tag': tag,
            'per_page': 100,
            'page': page,
        })
        resp.raise_for_status()
        data = resp.json()
        page_items = data.get('items', [])
        items.extend(page_items)
        if page >= data.get('pages', 1):
            break
        page += 1
    return items


def cmd_purge(args):
    """Delete all items with the given tag."""
    session, api_url = build_client(args.api_url, args.api_key)

    log.info('Fetching items tagged "%s"...', args.tag)
    items = fetch_all_items_by_tag(session, api_url, args.tag)

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
        resp = session.delete(f'{api_url}/items/{item["uuid"]}')
        if resp.status_code == 204:
            deleted += 1
        else:
            log.error('  Failed (HTTP %d): %s', resp.status_code, resp.text[:200])
            failed += 1

    log.info('')
    log.info('Deleted %d item(s), %d failed.', deleted, failed)


# ---------------------------------------------------------------------------
# Label generation
# ---------------------------------------------------------------------------

def _make_label_simple(parts: tuple[str, ...]) -> str:
    """Label = path below the category directory.

    For a file at Category/sub/dir/file.adf the label is sub/dir/file.adf.
    If there is only one level (Category/file.adf) the label is just the
    filename.
    """
    return '/'.join(parts[1:]) if len(parts) > 2 else parts[-1]


def _is_letter_group(s: str) -> bool:
    """True for single-character letter/digit directory groupings (A-Z, 0-9)."""
    return len(s) == 1 and s.isalnum()


def _make_label_arcarc(parts: tuple[str, ...]) -> str:
    """Smart label for arcarc.nl directory structure.

    Strips the category (parts[0]) and any single-char letter grouping, then
    checks whether the filename already starts with its parent directory name.
    If so, the filename alone is sufficient; otherwise the remaining context
    path is included for disambiguation.
    """
    # Strip category (already captured in the Item name)
    remaining = parts[1:]

    # Strip single-char letter grouping if present (e.g. 'A', '0')
    if len(remaining) > 1 and _is_letter_group(remaining[0]):
        remaining = remaining[1:]

    filename = remaining[-1]
    context = remaining[:-1]

    if not context:
        return filename

    # Check if filename is self-describing: starts with the parent dir name
    parent_dir = context[-1]
    stem = filename.rsplit('.', 1)[0] if '.' in filename else filename
    if stem.lower().startswith(parent_dir.lower()):
        return filename

    # Not self-describing — include context path for disambiguation
    return '/'.join(remaining)


def _is_importable(path: Path) -> bool:
    """Check if a file has an importable extension."""
    name = path.name.lower()
    # Check compound extensions first
    for ext in ('.tar.gz', '.dd.zst', '.dd.gz', '.dd.bz2'):
        if name.endswith(ext):
            return True
    return path.suffix.lower() in IMPORTABLE_EXTENSIONS


def discover_files(archive_dir: Path, categories: list[str] | None,
                   skip_dirs: set[str] | None,
                   skip_ext: set[str] | None = None,
                   label_fn=_make_label_simple) -> dict[str, list[dict]]:
    """
    Walk the archive directory and find all importable files.

    Expected structure: archive_dir/{TopLevel}/{...}/{filename}

    Returns dict mapping top-level directory name -> list of file entries.
    """
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

        # Filter by --categories
        if categories and category not in categories:
            continue

        # Filter by --skip-dirs: skip if any path component matches
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
    """
    Walk the archive directory and find all importable files as a flat list.

    All files go into a single collection.  The label is the relative path
    for files in subdirectories, or just the filename for top-level files.

    Returns list of file entries (not grouped by category).
    """
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

        # Filter by --skip-dirs: skip if any path component matches
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


def _api_get(session, url, **kwargs):
    """GET with a friendly error on connection failure."""
    try:
        return session.get(url, **kwargs)
    except requests.ConnectionError as exc:
        log.error('')
        log.error('ERROR: Cannot connect to Arcology at %s', url.split('/api')[0] + '/api')
        log.error('  Check that the server is running and reachable.')
        log.error('  Details: %s', exc)
        sys.exit(1)


def _api_post(session, url, **kwargs):
    """POST with a friendly error on connection failure."""
    try:
        return session.post(url, **kwargs)
    except requests.ConnectionError as exc:
        log.error('')
        log.error('ERROR: Cannot connect to Arcology at %s', url.split('/api')[0] + '/api')
        log.error('  Check that the server is running and reachable.')
        log.error('  Details: %s', exc)
        sys.exit(1)


def _extract_error(resp) -> str:
    """Return a human-readable error from an API response (JSON or truncated HTML)."""
    try:
        return resp.json().get('error', resp.text[:200])
    except Exception:
        text = resp.text.strip()
        if text.startswith('<'):
            return f'(server returned HTML {resp.status_code} — check server logs)'
        return text[:200]


def lookup_platform(session, api_url: str, platform_name: str) -> int | None:
    """Look up a platform by name, return its ID or None."""
    if not platform_name:
        return None
    resp = _api_get(session, f'{api_url}/platforms')
    resp.raise_for_status()
    for p in resp.json().get('platforms', []):
        if p['name'].lower() == platform_name.lower():
            return p['id']
    log.warning('Platform "%s" not found in Arcology', platform_name)
    return None


def lookup_category(session, api_url: str, category_name: str) -> int | None:
    """Look up a category by name, return its ID or None."""
    if not category_name:
        return None
    resp = _api_get(session, f'{api_url}/categories')
    resp.raise_for_status()
    for c in resp.json().get('categories', []):
        if c['name'].lower() == category_name.lower():
            return c['id']
    log.debug('Category "%s" not found', category_name)
    return None


def find_existing_item(session, api_url: str, name: str, tag: str) -> dict | None:
    """Find an existing item by name and tag."""
    resp = _api_get(session, f'{api_url}/items', params={
        'q': name,
        'tag': tag,
        'per_page': 50,
    })
    resp.raise_for_status()
    for item in resp.json().get('items', []):
        if item['name'] == name:
            return item
    return None


def get_existing_filenames(session, api_url: str, item_uuid: str) -> set[str]:
    """Get the set of original_filename values already on an Item."""
    resp = _api_get(session, f'{api_url}/items/{item_uuid}')
    resp.raise_for_status()
    item_data = resp.json()
    return {
        art.get('original_filename', '')
        for art in item_data.get('artefacts', [])
    }


def upload_with_retry(session, api_url: str, item_uuid: str, filepath: str,
                      label: str, auto_analyse: bool = True,
                      max_retries: int = 3) -> dict | None:
    """Upload a file with retry on failure."""
    for attempt in range(max_retries):
        try:
            with open(filepath, 'rb') as f:
                files = {'file': (os.path.basename(filepath), f)}
                data = {'label': label}
                if auto_analyse:
                    data['auto_analyse'] = 'true'
                resp = session.post(
                    f'{api_url}/items/{item_uuid}/artefacts/upload',
                    files=files,
                    data=data,
                )
            if resp.status_code == 201:
                return resp.json()
            log.warning('Upload failed (HTTP %d): %s', resp.status_code, _extract_error(resp))
        except requests.ConnectionError as e:
            log.warning('Connection error on attempt %d: %s', attempt + 1, e)

        if attempt < max_retries - 1:
            wait = 2 ** (attempt + 1)
            log.info('  Retrying in %ds...', wait)
            time.sleep(wait)

    return None


def main():
    parser = argparse.ArgumentParser(
        description='Bulk import a file archive into Arcology',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--archive-dir', type=Path, default=None,
                        help='Local mirror root (required unless --purge)')
    parser.add_argument('--api-url',
                        default=os.environ.get('ARCOLOGY_API', 'http://localhost:5000/api'),
                        help='Arcology API URL')
    parser.add_argument('--api-key',
                        default=os.environ.get('WORKER_API_KEY', ''),
                        help='API key')
    parser.add_argument('--categories', default=None,
                        help='Filter by top-level directory (e.g. --categories Apps imports only Apps/)')
    parser.add_argument('--skip-dirs', default=None,
                        help='Comma-separated directory names to skip (e.g. Source,Acorn)')
    parser.add_argument('--platform', default=None,
                        help='Platform name to assign')
    parser.add_argument('--tag', default=None,
                        help='Tag for imported items (required unless --arcarc)')
    parser.add_argument('--name-prefix', default=None,
                        help='Prefix for item names (e.g. --name-prefix Source)')
    parser.add_argument('--category-map', default=None,
                        help='Directory-to-category mapping as K=V,... (e.g. Apps=Applications,PD="Public Domain")')
    parser.add_argument('--skip-ext', default=None,
                        help='Comma-separated extensions to skip (e.g. .pdf,.txt)')
    parser.add_argument('--no-auto-analyse', action='store_true',
                        help='Upload without triggering automatic analysis')
    parser.add_argument('--smart-labels', action='store_true',
                        help='Strip single-char letter groupings and use filename alone when self-describing')
    parser.add_argument('--flat', action='store_true',
                        help='Treat archive-dir as a single collection (one Item, all files as artefacts)')
    parser.add_argument('--arcarc', action='store_true',
                        help='Preset for arcarc.nl: sets tag, prefix, category map, and smart labels')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--resume', action='store_true',
                        help='Skip artefacts whose filename already exists on the Item')
    parser.add_argument('--purge', action='store_true',
                        help='Delete all items with the given tag instead of importing')
    parser.add_argument('--yes', '-y', action='store_true',
                        help='Skip confirmation prompt for --purge')

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(message)s', stream=sys.stderr)

    # --arcarc preset: fill in defaults for any flag the user didn't set
    if args.arcarc:
        if args.tag is None:
            args.tag = 'arcarc'
        if args.name_prefix is None:
            args.name_prefix = 'Arcarc'
        args.smart_labels = True

    # Require --tag for all modes except --purge (which validates separately)
    if not args.purge and args.tag is None:
        parser.error('--tag is required (or use --arcarc for arcarc.nl defaults)')

    # Default name_prefix to empty string if not set by user or preset
    if args.name_prefix is None:
        args.name_prefix = ''

    # Purge mode — no archive-dir needed
    if args.purge:
        if not args.tag:
            parser.error('--tag is required for --purge')
        if not args.api_key:
            log.error('No API key set. Use --api-key or $WORKER_API_KEY.')
            sys.exit(1)
        cmd_purge(args)
        return

    if not args.archive_dir:
        parser.error('--archive-dir is required (unless --purge)')

    if not args.api_key and not args.dry_run:
        log.error('No API key set. Use --api-key or $WORKER_API_KEY.')
        sys.exit(1)

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

    # Build category map: CLI override > arcarc preset > none
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

    # Select label strategy
    label_fn = _make_label_arcarc if args.smart_labels else _make_label_simple

    # Discover files
    log.info('Scanning %s ...', args.archive_dir)
    if args.flat:
        if categories:
            log.warning('--categories is ignored in --flat mode')
        flat_files = discover_files_flat(args.archive_dir, skip_dirs, skip_ext)
        # Use the directory name as the single collection name
        coll_key = args.archive_dir.resolve().name
        collections = {coll_key: flat_files} if flat_files else {}
    else:
        collections = discover_files(args.archive_dir, categories, skip_dirs,
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

    session, api_url = build_client(args.api_url, args.api_key)

    # Look up platform (once)
    platform_id = lookup_platform(session, api_url, args.platform)

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
        category_id = lookup_category(session, api_url, category_name)

        log.info('')
        log.info('=== %s (%d file(s)) ===', item_name, len(files))

        # Find or create the Item
        existing_item = find_existing_item(session, api_url, item_name, args.tag)

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

            resp = _api_post(session, f'{api_url}/items', json=item_data)
            if resp.status_code == 201:
                item_uuid = resp.json()['uuid']
                created_items += 1
                log.info('Created item: %s', item_uuid[:8])
            else:
                log.error('Failed to create item "%s": %s', item_name, _extract_error(resp))
                for f in files:
                    failed_uploads.append(f['relative_path'])
                continue

        # In resume mode, fetch existing filenames once to avoid per-file API calls
        existing_filenames = set()
        if args.resume:
            existing_filenames = get_existing_filenames(session, api_url, item_uuid)
            log.info('  %d artefact(s) already on this item', len(existing_filenames))

        # Upload each file as an Artefact
        for j, file_entry in enumerate(files, 1):
            filepath = file_entry['path']
            filename = file_entry['filename']
            label = file_entry['label']

            # Skip if already uploaded (resume mode)
            if args.resume and filename in existing_filenames:
                log.debug('  [%d/%d] Skipping (exists): %s', j, len(files), filename)
                skipped_files += 1
                continue

            log.info('  [%d/%d] %s', j, len(files), label)
            result = upload_with_retry(session, api_url, item_uuid, str(filepath), label,
                                      auto_analyse=not args.no_auto_analyse)
            if result:
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


if __name__ == '__main__':
    main()

# vim: ts=4 sw=4 et
