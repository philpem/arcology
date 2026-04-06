"""
arco hashdb generate-arcarc — Generate HashDB JSON from arcarc items in Arcology.

Queries the Arcology API for items tagged with a given tag, fetches their
extracted file listings, identifies RISC OS application directories,
classifies files as Required/Optional, and outputs HashDB-compatible JSON.
"""

import json
import logging
import re
import sys
from datetime import date

from ..client import ArcologyClient

log = logging.getLogger('arco.hashdb.generate-arcarc')


# ---------------------------------------------------------------------------
# RISC OS filetype constants for classification
# ---------------------------------------------------------------------------

FT_BASIC = 'ffb'
FT_ABSOLUTE = 'ff8'
FT_TEMPLATE = 'fec'
FT_SPRITE = 'ff9'
FT_MODULE = 'ffa'
FT_UTILITY = 'ffc'

REQUIRED_FILETYPES = {FT_BASIC, FT_ABSOLUTE, FT_TEMPLATE, FT_MODULE, FT_UTILITY}


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
    """Group extracted files by application directory."""
    app_dirs: dict[str, list[dict]] = {}
    root_files: list[dict] = []

    for f in files:
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
    """Determine if a file should be Required (True) or Optional (False)."""
    filename = f.get('filename', '')
    filetype = f.get('risc_os_filetype')

    if filetype in REQUIRED_FILETYPES:
        if verbose:
            log.info('    REQUIRED (filetype %s): %s', filetype, f.get('path', ''))
        return True

    if filetype == FT_SPRITE and re.match(r'^!Sprites\d*$', filename, re.IGNORECASE):
        if verbose:
            log.info('    REQUIRED (app sprites): %s', f.get('path', ''))
        return True

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
        if f.get('md5'):
            entry['md5'] = f['md5']
        if f.get('sha1'):
            entry['sha1'] = f['sha1']

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

def _process_item(client: ArcologyClient, item: dict, args) -> list[dict]:
    """Process a single Arcology Item and return product dicts for HashDB."""
    products = []
    item_name = item['name']
    category = ''
    for tag in item.get('tags', []):
        if tag != args.tag:
            category = tag
            break

    item_detail = client.get_item(item['uuid'])
    artefacts = item_detail.get('artefacts', [])

    if not artefacts:
        log.debug('  No artefacts for item: %s', item_name)
        return products

    artefact_results = []

    for art in artefacts:
        art_uuid = art['uuid']
        art_label = art.get('label', art.get('original_filename', ''))

        art_detail = client.get_artefact(art_uuid)
        partitions = art_detail.get('partitions', [])

        if not partitions:
            log.debug('  No partitions for artefact: %s', art_label)
            continue

        parsed = parse_artefact_label(art_label)

        all_files = []
        for part in partitions:
            part_uuid = part['uuid']
            files = client.get_partition_files_all(part_uuid, show_known='true')
            all_files.extend(files)

        if not all_files:
            continue

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
        merged: dict[str, list[dict]] = {}
        for ar in artefact_results:
            disc_num = ar['disc_number']
            for app_dir_name, app_files in ar['app_dirs'].items():
                if app_dir_name not in merged:
                    merged[app_dir_name] = []
                for f in app_files:
                    if is_multi_disc and disc_num is not None:
                        f = dict(f)
                        f['path'] = f'Disk {disc_num}/{f.get("path", "")}'
                    merged[app_dir_name].append(f)

        for app_dir_name, all_files in merged.items():
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


def cmd_hashdb_generate_arcarc(client: ArcologyClient, args):
    """Generate HashDB JSON from arcarc items in Arcology."""
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(message)s', stream=sys.stderr)

    log.info('Fetching items tagged "%s"...', args.tag)
    items = client.list_items_all(tag=args.tag)

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

    all_products = []
    items_with_files = 0
    items_without_files = 0

    for i, item in enumerate(items, 1):
        log.info('[%d/%d] %s', i, len(items), item['name'])
        products = _process_item(client, item, args)

        if products:
            all_products.extend(products)
            items_with_files += 1
        else:
            items_without_files += 1
            log.debug('  No products generated')

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

    total_files = sum(len(p['files']) for p in all_products)
    required_files = sum(
        sum(1 for f in p['files'] if f['is_required'])
        for p in all_products
    )
    optional_files = total_files - required_files

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
    log.info('Import with:  arco hashdb import %s', args.output)

# vim: ts=4 sw=4 et
