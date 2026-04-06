#!/usr/bin/env python3
"""
import-arcarc-hashdb — Generate HashDB JSON from arcarc items already imported
into Arcology.

Queries the Arcology API for items tagged with a given tag (default: "arcarc"),
fetches their extracted file listings, identifies RISC OS application directories,
classifies files as Required/Optional, and outputs HashDB-compatible JSON.

The output can be imported into Arcology's hash database system via:
    python devtools/arcology-hashdb.py import <output-file>

Prerequisites:
    Items must have been imported into Arcology (e.g. via import-arcarc.py) and
    the worker must have completed file extraction analyses.

Usage:
    import-arcarc-hashdb.py [options]

Options:
    --api-url URL         Arcology API URL (default: $ARCOLOGY_API or http://localhost:5000/api)
    --api-key KEY         API key (default: $WORKER_API_KEY)
    --output PATH         Output JSON file (default: arcarc-hashdb.json)
    --tag TAG             Filter items by tag (default: arcarc)
    --multi-disc MODE     merge | separate | both (default: separate)
    --root-files MODE     include | skip | flag (default: include)
    --db-name NAME        HashDB name (default: "Arcarc RISC OS Archive")
    --verbose / -v        Verbose logging
    --dry-run             Scan only, report what would be included

Examples:
    # Generate HashDB from all arcarc items:
    import-arcarc-hashdb.py --output arcarc-hashdb.json

    # Merge multi-disc applications, skip root files:
    import-arcarc-hashdb.py --multi-disc merge --root-files skip

    # Import the result:
    python devtools/arcology-hashdb.py import arcarc-hashdb.json
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import date

import requests

log = logging.getLogger('import-arcarc-hashdb')


# ---------------------------------------------------------------------------
# RISC OS filetype constants for classification
# ---------------------------------------------------------------------------

FT_BASIC = 'ffb'
FT_ABSOLUTE = 'ff8'
FT_TEMPLATE = 'fec'
FT_SPRITE = 'ff9'
FT_MODULE = 'ffa'
FT_UTILITY = 'ffc'

# Filetypes that indicate code / binary — always Required
REQUIRED_FILETYPES = {FT_BASIC, FT_ABSOLUTE, FT_TEMPLATE, FT_MODULE, FT_UTILITY}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def build_client(api_url: str, api_key: str):
    """Create an authenticated requests session."""
    session = requests.Session()
    session.headers.update({
        'X-API-Key': api_key,
        'Authorization': f'Bearer {api_key}',
    })
    return session, api_url.rstrip('/')


def fetch_all_items(session, api_url: str, tag: str) -> list[dict]:
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


def fetch_item_detail(session, api_url: str, uuid: str) -> dict:
    """Fetch full item detail including artefacts."""
    resp = session.get(f'{api_url}/items/{uuid}')
    resp.raise_for_status()
    return resp.json()


def fetch_artefact_detail(session, api_url: str, uuid: str) -> dict:
    """Fetch artefact detail including partitions."""
    resp = session.get(f'{api_url}/artefacts/{uuid}')
    resp.raise_for_status()
    return resp.json()


def fetch_partition_files(session, api_url: str, partition_uuid: str) -> list[dict]:
    """Fetch all files in a partition, handling pagination."""
    files = []
    page = 1
    while True:
        resp = session.get(f'{api_url}/partitions/{partition_uuid}/files', params={
            'show_known': 'true',
            'per_page': 500,
            'page': page,
        })
        resp.raise_for_status()
        data = resp.json()
        files.extend(data.get('files', []))
        if page >= data.get('pages', 1):
            break
        page += 1
    return files


# ---------------------------------------------------------------------------
# ADF filename parsing (for multi-disc detection)
# ---------------------------------------------------------------------------

_DISC_RE = re.compile(
    r'(?:\s*\(Disk\s+(\d+)\s+of\s+(\d+)\))',
    re.IGNORECASE,
)

_VERSION_RE = re.compile(
    r'\(v([^)]+)\)',
    re.IGNORECASE,
)


def parse_artefact_label(label: str) -> dict:
    """Parse version, disc number from an artefact label."""
    result = {'disc_number': None, 'disc_total': None, 'version': None}

    m = _DISC_RE.search(label)
    if m:
        result['disc_number'] = int(m.group(1))
        result['disc_total'] = int(m.group(2))

    m = _VERSION_RE.search(label)
    if m:
        result['version'] = m.group(1)

    return result


# ---------------------------------------------------------------------------
# Application directory identification
# ---------------------------------------------------------------------------

def find_app_directories(files: list[dict], root_mode: str,
                         disc_label: str) -> dict[str, list[dict]]:
    """
    Group extracted files by application directory.

    Files inside !-prefixed directories are grouped by that directory name.
    Root-level files are handled according to root_mode.
    """
    app_dirs: dict[str, list[dict]] = {}
    root_files: list[dict] = []

    for f in files:
        # Skip directory entries themselves
        if f.get('is_directory'):
            continue

        path = f.get('path', '')
        parts = path.split('/') if '/' in path else [path]

        if len(parts) > 1 and parts[0].startswith('!'):
            app_dir = parts[0]
            if app_dir not in app_dirs:
                app_dirs[app_dir] = []
            app_dirs[app_dir].append(f)
        else:
            root_files.append(f)

    # Handle root files
    if root_files and root_mode != 'skip':
        if root_mode == 'flag':
            key = f'[Root] {disc_label}'
        else:
            key = disc_label
        app_dirs[key] = root_files

    return app_dirs


# ---------------------------------------------------------------------------
# Required/Optional classification
# ---------------------------------------------------------------------------

def classify_file(f: dict, verbose: bool = False) -> bool:
    """
    Determine if a file should be Required (True) or Optional (False).

    Classification is based on RISC OS filetype and filename conventions.
    """
    filename = f.get('filename', '')
    filetype = f.get('risc_os_filetype')

    # Code/binary filetypes are always Required
    if filetype in REQUIRED_FILETYPES:
        if verbose:
            log.info('    REQUIRED (filetype %s): %s', filetype, f.get('path', ''))
        return True

    # !Sprites or !SpritesNN with sprite filetype
    if filetype == FT_SPRITE and re.match(r'^!Sprites\d*$', filename, re.IGNORECASE):
        if verbose:
            log.info('    REQUIRED (app sprites): %s', f.get('path', ''))
        return True

    # !Boot and !Run are Optional by default when using API approach
    # (we can't inspect file content to detect boilerplate)
    if verbose:
        log.info('    OPTIONAL: %s', f.get('path', ''))
    return False


# ---------------------------------------------------------------------------
# Product building
# ---------------------------------------------------------------------------

def build_product_files(app_files: list[dict], verbose: bool = False) -> list[dict]:
    """Convert API file entries to HashDB KnownFile format."""
    result = []
    for f in app_files:
        is_req = classify_file(f, verbose=verbose)
        entry = {
            'filename': f.get('filename', ''),
            'file_size': f.get('file_size'),
            'is_required': is_req,
            'relative_path': f.get('path', ''),
        }
        # Include available hashes
        if f.get('md5'):
            entry['md5'] = f['md5']
        if f.get('sha1'):
            entry['sha1'] = f['sha1']

        # Skip entries with no hashes (can't identify them)
        if not entry.get('md5') and not entry.get('sha1'):
            continue

        result.append(entry)
    return result


def build_product_title(name: str, version: str | None = None,
                        disc_number: int | None = None) -> str:
    """Build a product title from name, version, and optional disc number."""
    title = name
    if version:
        title += f' v{version}'
    if disc_number is not None:
        title += f' (Disk {disc_number})'
    return title


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process_item(session, api_url: str, item: dict, args) -> list[dict]:
    """
    Process a single Arcology Item and return product dicts for HashDB.
    """
    products = []
    item_name = item['name']
    category = ''
    for tag in item.get('tags', []):
        if tag != args.tag:
            category = tag
            break

    # Fetch full item detail with artefacts
    item_detail = fetch_item_detail(session, api_url, item['uuid'])
    artefacts = item_detail.get('artefacts', [])

    if not artefacts:
        log.debug('  No artefacts for item: %s', item_name)
        return products

    # Collect per-artefact results
    artefact_results = []

    for art in artefacts:
        art_uuid = art['uuid']
        art_label = art.get('label', art.get('original_filename', ''))

        # Fetch artefact detail with partitions
        art_detail = fetch_artefact_detail(session, api_url, art_uuid)
        partitions = art_detail.get('partitions', [])

        if not partitions:
            log.debug('  No partitions for artefact: %s', art_label)
            continue

        # Parse disc number from label
        parsed = parse_artefact_label(art_label)

        all_files = []
        for part in partitions:
            part_uuid = part['uuid']
            files = fetch_partition_files(session, api_url, part_uuid)
            all_files.extend(files)

        if not all_files:
            continue

        # Identify application directories
        app_dirs = find_app_directories(all_files, args.root_files, art_label)

        artefact_results.append({
            'label': art_label,
            'disc_number': parsed['disc_number'],
            'disc_total': parsed['disc_total'],
            'version': parsed['version'],
            'app_dirs': app_dirs,
        })

    if not artefact_results:
        return products

    # Determine version from first artefact that has one
    version = None
    for ar in artefact_results:
        if ar['version']:
            version = ar['version']
            break

    description = category.title() if category else ''
    is_multi_disc = len(artefact_results) > 1

    if args.multi_disc in ('separate', 'both'):
        for ar in artefact_results:
            disc_num = ar['disc_number']
            for app_dir_name, app_files in ar['app_dirs'].items():
                title = build_product_title(
                    app_dir_name, version,
                    disc_num if is_multi_disc else None,
                )
                pfiles = build_product_files(app_files, verbose=args.verbose)
                if pfiles:
                    products.append({
                        'title': title,
                        'description': description,
                        'path_match_enabled': True,
                        'files': pfiles,
                    })

    if args.multi_disc in ('merge', 'both'):
        # Merge all artefacts — group by app directory across discs
        merged: dict[str, list[dict]] = {}
        for ar in artefact_results:
            disc_num = ar['disc_number']
            for app_dir_name, app_files in ar['app_dirs'].items():
                if app_dir_name not in merged:
                    merged[app_dir_name] = []
                for f in app_files:
                    # Prefix path for multi-disc disambiguation
                    if is_multi_disc and disc_num is not None:
                        f = dict(f)
                        f['path'] = f'Disk {disc_num}/{f.get("path", "")}'
                    merged[app_dir_name].append(f)

        for app_dir_name, all_files in merged.items():
            # Deduplicate by md5 + path
            seen = set()
            deduped = []
            for f in all_files:
                key = (f.get('md5', ''), f.get('path', ''))
                if key not in seen:
                    seen.add(key)
                    deduped.append(f)

            suffix = ' [All Discs]' if (args.multi_disc == 'both' and is_multi_disc) else ''
            title = build_product_title(app_dir_name, version) + suffix
            pfiles = build_product_files(deduped, verbose=args.verbose)
            if pfiles:
                products.append({
                    'title': title,
                    'description': description,
                    'path_match_enabled': True,
                    'files': pfiles,
                })

    return products


def main():
    parser = argparse.ArgumentParser(
        description='Generate HashDB JSON from arcarc items in Arcology',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--api-url',
                        default=os.environ.get('ARCOLOGY_API', 'http://localhost:5000/api'),
                        help='Arcology API URL')
    parser.add_argument('--api-key',
                        default=os.environ.get('WORKER_API_KEY', ''),
                        help='API key')
    parser.add_argument('--output', default='arcarc-hashdb.json', type=str,
                        help='Output JSON file (default: arcarc-hashdb.json)')
    parser.add_argument('--tag', default='arcarc',
                        help='Filter items by tag (default: arcarc)')
    parser.add_argument('--multi-disc', choices=['merge', 'separate', 'both'],
                        default='separate',
                        help='Multi-disc handling (default: separate)')
    parser.add_argument('--root-files', choices=['include', 'skip', 'flag'],
                        default='include',
                        help='Root-level file handling (default: include)')
    parser.add_argument('--db-name', default='Arcarc RISC OS Archive',
                        help='HashDB name')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--dry-run', action='store_true',
                        help='Scan only, report what would be included')

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(message)s', stream=sys.stderr)

    if not args.api_key:
        log.error('No API key set. Use --api-key or $WORKER_API_KEY.')
        sys.exit(1)

    session, api_url = build_client(args.api_url, args.api_key)

    # Fetch all items with the target tag
    log.info('Fetching items tagged "%s"...', args.tag)
    items = fetch_all_items(session, api_url, args.tag)

    if not items:
        log.error('No items found with tag "%s"', args.tag)
        sys.exit(1)

    log.info('Found %d item(s)', len(items))

    if args.dry_run:
        log.info('')
        log.info('=== DRY RUN ===')
        for item in items:
            tags = ', '.join(item.get('tags', []))
            log.info('  %s [%s] (%d artefact(s))',
                     item['name'], tags, item.get('artefact_count', 0))
        return

    # Process each item
    all_products = []
    items_with_files = 0
    items_without_files = 0

    for i, item in enumerate(items, 1):
        log.info('[%d/%d] %s', i, len(items), item['name'])
        products = process_item(session, api_url, item, args)

        if products:
            all_products.extend(products)
            items_with_files += 1
        else:
            items_without_files += 1
            log.debug('  No products generated')

    # Generate output JSON
    output_data = {
        'schema_version': 1,
        'database': {
            'name': args.db_name,
            'description': 'Known files from the arcarc.nl RISC OS disc image archive',
            'version': date.today().isoformat(),
            'source_url': 'https://arcarc.nl/archive/',
        },
        'products': all_products,
    }

    # Stats
    total_files = sum(len(p['files']) for p in all_products)
    required_files = sum(
        sum(1 for f in p['files'] if f['is_required'])
        for p in all_products
    )
    optional_files = total_files - required_files

    # Write output
    with open(args.output, 'w', encoding='utf-8') as fh:
        json.dump(output_data, fh, indent=2, default=str)

    log.info('')
    log.info('=== Summary ===')
    log.info('Items processed:  %d (%d with files, %d without)',
             len(items), items_with_files, items_without_files)
    log.info('Products:         %d', len(all_products))
    log.info('Files:            %d (%d required, %d optional)',
             total_files, required_files, optional_files)
    log.info('Output:           %s', args.output)
    log.info('')
    log.info('Import with:  python devtools/arcology-hashdb.py import %s', args.output)


if __name__ == '__main__':
    main()

# vim: ts=4 sw=4 et
