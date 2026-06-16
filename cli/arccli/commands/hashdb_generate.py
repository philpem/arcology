"""
arco hashdb generate-riscos — Generate a RISC OS HashDB JSON from items in Arcology.

Scans selected items (by tag, platform, or explicit UUID), fetches their
extracted file listings, identifies RISC OS application directories (those whose
top-level component begins with '!'), parses each application's !Run Obey file to
find the program it launches, and emits HashDB-compatible JSON ready for
`arco hashdb import`.

Classification (Mandatory == is_required, Optional == not is_required):

  * The file(s) launched by !Run — an Absolute/Module run via Run/RMRun/RMLoad,
    or a BASIC image via `BASIC -quit <file>` — are marked **Mandatory**,
    provided the file is *unique*: it appears in only one application across the
    scanned set and is not already present in an active hash database.
  * !Run and !Boot themselves are always **Optional**: their bytes legitimately
    vary (e.g. innoculation against the Extend virus appends a commented 0xFF
    byte at EOF), so they must never gate a product match.
  * Shared files (identical content in more than one application) are added as
    **Optional** everywhere, so they add confidence but never gate a match.
  * If !Run cannot be parsed, a filetype heuristic is used as a fallback to pick
    Mandatory candidates (still gated by uniqueness).

This tool is RISC OS-specific but not collection-specific: use it for the arcarc
archive, a batch of museum disc images, or any other RISC OS material.
"""

import json
import logging
import re
import sys
from datetime import date
from ..client import ArcologyClient

log = logging.getLogger('arco.hashdb.generate-riscos')


# ---------------------------------------------------------------------------
# RISC OS filetype constants for fallback classification
# ---------------------------------------------------------------------------

FT_BASIC = 'ffb'
FT_ABSOLUTE = 'ff8'
FT_TEMPLATE = 'fec'
FT_SPRITE = 'ff9'
FT_MODULE = 'ffa'
FT_UTILITY = 'ffc'

REQUIRED_FILETYPES = {FT_BASIC, FT_ABSOLUTE, FT_TEMPLATE, FT_MODULE, FT_UTILITY}


# ---------------------------------------------------------------------------
# Artefact label parsing (for multi-disc detection / version)
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
    """Parse version and disc number from an artefact label."""
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
    """Group extracted files by application directory (top-level '!' component)."""
    app_dirs: dict[str, list[dict]] = {}
    root_files: list[dict] = []

    for f in files:
        if f.get('is_directory'):
            continue

        path = f.get('path', '')
        parts = path.split('/') if '/' in path else [path]

        if len(parts) > 1 and parts[0].startswith('!'):
            app_dir = parts[0]
            app_dirs.setdefault(app_dir, []).append(f)
        else:
            root_files.append(f)

    if root_files and root_mode != 'skip':
        key = f'[Root] {disc_label}' if root_mode == 'flag' else disc_label
        app_dirs[key] = root_files

    return app_dirs


# ---------------------------------------------------------------------------
# !Run Obey file parsing
# ---------------------------------------------------------------------------

_LAUNCH_CMDS = {'run', 'rmrun', 'rmload'}

# Sentinel standing in for the application (Obey) directory during expansion.
_ROOT = '\x00'

# System variables that always resolve to the application directory.
_OBEY_VARS = {'obey$dir', 'obey$path'}

# Well-known system variables that point *outside* the application; a file
# referenced through one of these is not an in-app file and is ignored.
_EXTERNAL_VARS = {
    'system$dir', 'system$path', 'boot$dir', 'resources$dir',
    'choices$dir', 'choices$write', 'wimp$scrapdir', 'scrap$dir',
}

_SET_RE = re.compile(r'^Set(?:Macro|Eval)?\s+(\S+)\s+(.+)$', re.IGNORECASE)
_VAR_RE = re.compile(r'<([^>]+)>')


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def _build_var_map(lines: list[str]) -> dict[str, str]:
    """Collect `Set <name> <value>` assignments (case-insensitive names)."""
    varmap: dict[str, str] = {}
    for line in lines:
        m = _SET_RE.match(line)
        if m:
            varmap[m.group(1).lower()] = _strip_quotes(m.group(2).strip())
    return varmap


def _expand(token: str, varmap: dict[str, str], depth: int = 0) -> str:
    """Expand <var> references, anchoring application-directory variables on the
    _ROOT sentinel.  Handles the `Set App$Dir <Obey$Dir>` indirection pattern
    (and chains/subdirectories thereof).
    """
    if depth > 16:  # guard against pathological/circular definitions
        return token

    def repl(m):
        name = m.group(1).lower()
        if name in _OBEY_VARS:
            return _ROOT
        if name in varmap:
            return _expand(varmap[name], varmap, depth + 1)
        if name in _EXTERNAL_VARS:
            return m.group(0)  # external: leave unresolved
        if name.endswith('$dir') or name.endswith('$path'):
            return _ROOT       # unknown directory variable: assume app dir
        return m.group(0)      # unknown: leave unresolved

    return _VAR_RE.sub(repl, token)


def _resolve_obey_path(token: str, varmap: dict[str, str] | None = None) -> str | None:
    """Resolve an Obey file reference to a path relative to the app directory.

    '<Obey$Dir>.!RunImage'                      -> '!RunImage'
    '<App$Dir>.bin.loader'                       -> 'bin/loader'
    '<App$Dir>.loader' with Set App$Dir=<..>.Bin -> 'bin/loader'
    '<System$Dir>.Modules.Foo'                   -> None (external)

    Returns None for tokens that are not in-app file references (options,
    argument placeholders, external/unresolved variables).
    """
    token = _strip_quotes(token).strip()
    if not token or token.startswith('-') or token.startswith('%'):
        return None

    expanded = _expand(token, varmap or {})
    if _ROOT in expanded:
        rest = expanded.split(_ROOT, 1)[1].lstrip('.')
        return rest.replace('.', '/') or None
    if '<' not in token:
        # A bare relative path (no variable) is taken to be app-relative.
        return token.replace('.', '/')
    # An unresolved/external variable reference is not an in-app file.
    return None


def parse_run_obey(text: str) -> list[str]:
    """Parse a RISC OS !Run Obey file, returning the app-relative paths of the
    files it launches (lowercased, '/'-separated).  Best-effort.

    A first pass collects `Set`/`SetMacro` variable assignments so that path
    references built from them (the common `Set App$Dir <Obey$Dir>` idiom, and
    subdirectory/chained variants) resolve to the correct app-relative path.
    """
    cleaned: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('|'):  # blank or comment
            continue
        if line.startswith('*'):              # explicit star command
            line = line[1:].strip()
        if line:
            cleaned.append(line)

    varmap = _build_var_map(cleaned)

    targets: list[str] = []

    def add(token):
        if not token:
            return
        rel = _resolve_obey_path(token, varmap)
        if rel:
            targets.append(rel.lower())

    for line in cleaned:
        tokens = line.split()
        if not tokens:
            continue
        cmd = tokens[0].lower()

        if cmd in _LAUNCH_CMDS:
            if len(tokens) > 1:
                add(tokens[1])
        elif cmd == 'rmensure':
            # RMEnsure <module> <version> <action ...>; capture an RM* target.
            lowered = [t.lower() for t in tokens]
            for kw in ('rmload', 'rmrun'):
                if kw in lowered:
                    idx = lowered.index(kw)
                    if idx + 1 < len(tokens):
                        add(tokens[idx + 1])
        elif cmd == 'basic':
            # BASIC [-quit|-load] <file> ...  (the file arg is the BASIC image)
            lowered = [t.lower() for t in tokens]
            target = None
            for opt in ('-quit', '-load'):
                if opt in lowered:
                    idx = lowered.index(opt)
                    if idx + 1 < len(tokens):
                        target = tokens[idx + 1]
                    break
            if target is None:
                for t in tokens[1:]:
                    if not t.startswith('-'):
                        target = t
                        break
            add(target)

    # De-duplicate, preserving order.
    seen = set()
    result = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Required/Optional classification
# ---------------------------------------------------------------------------

def _app_relative(app_dir_name: str, path: str) -> str:
    """Return *path* relative to *app_dir_name* (or the path itself)."""
    prefix = app_dir_name + '/'
    return path[len(prefix):] if path.startswith(prefix) else path


def _filetype_mandatory(f: dict) -> bool:
    """Fallback heuristic: is this file an executable that should be Mandatory?"""
    filetype = f.get('risc_os_filetype')
    if filetype in REQUIRED_FILETYPES:
        return True
    filename = f.get('filename', '')
    if filetype == FT_SPRITE and re.match(r'^!Sprites\d*$', filename, re.IGNORECASE):
        return True
    return False


def get_launched_set(client: ArcologyClient, app_files: list[dict],
                     verbose: bool = False) -> set[str]:
    """Locate the application's !Run file, download it, and parse the set of
    app-relative paths (lowercased) that it launches.  Empty set if no !Run.
    """
    run_file = None
    for f in app_files:
        if f.get('filename', '').lower() == '!run' and not f.get('is_directory'):
            run_file = f
            break
    if not run_file or not run_file.get('uuid'):
        return set()

    try:
        data = client.download_extracted_file_bytes(run_file['uuid'])
        launched = set(parse_run_obey(data.decode('latin-1', errors='replace')))
        if verbose:
            log.info('    !Run launches: %s', ', '.join(sorted(launched)) or '(none parsed)')
        return launched
    except Exception as exc:  # noqa: BLE001 - best-effort; fall back to heuristic
        log.debug('    Could not read/parse !Run: %s', exc)
        return set()


def classify_app_files(app_dir_name: str, app_files: list[dict],
                       launched_set: set[str], is_unique,
                       verbose: bool = False) -> list[tuple[dict, bool]]:
    """Classify each file in an application directory as Mandatory or Optional.

    Returns a list of (file_dict, is_required) tuples.  *is_unique* is a callable
    taking a file dict and returning whether the file is unique enough to be
    Mandatory.
    """
    results: list[tuple[dict, bool]] = []
    any_mandatory = False

    for f in app_files:
        if f.get('is_directory'):
            continue
        leaf = f.get('filename', '').lower()
        if leaf in ('!run', '!boot'):
            results.append((f, False))
            continue

        rel = _app_relative(app_dir_name, f.get('path', '')).lower()
        is_launched = rel in launched_set or any(
            t.rsplit('/', 1)[-1] == leaf for t in launched_set
        )
        if is_launched and is_unique(f):
            results.append((f, True))
            any_mandatory = True
        else:
            results.append((f, False))

    # Fallback: nothing was launched/matched -> use the filetype heuristic
    # (still gated by uniqueness), excluding !Run/!Boot.
    if not any_mandatory:
        for i, (f, _req) in enumerate(results):
            leaf = f.get('filename', '').lower()
            if leaf in ('!run', '!boot'):
                continue
            if _filetype_mandatory(f) and is_unique(f):
                results[i] = (f, True)

    return results


def build_product_files(classified: list[tuple[dict, bool]],
                        verbose: bool = False) -> list[dict]:
    """Convert classified files to HashDB KnownFile entries."""
    result = []
    for f, is_req in classified:
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
            continue  # cannot match a file with no hash
        result.append(entry)
        if verbose:
            log.info('    %s %s', 'MANDATORY' if is_req else 'optional',
                     f.get('path', ''))
    return result


# ---------------------------------------------------------------------------
# Product title construction
# ---------------------------------------------------------------------------

def _item_context(item_name: str, version: str | None) -> str:
    """Build the provenance context appended to a product title."""
    ctx = (item_name or '').strip()
    if version and f'v{version}' not in ctx and version not in ctx:
        ctx = f'{ctx} v{version}'.strip() if ctx else f'v{version}'
    return ctx


def build_product_title(app_dir_name: str, context: str | None = None,
                        disc_number: int | None = None) -> str:
    """Compose a product title: app-dir name plus item/version provenance."""
    title = f'{app_dir_name} — {context}' if context else app_dir_name
    if disc_number is not None:
        title += f' (Disk {disc_number})'
    return title


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------

def make_is_unique(client: ArcologyClient, md5_appkeys: dict[str, set],
                   global_check: bool):
    """Build the is_unique() predicate.

    A file is Mandatory-eligible when it is not already in an active hash
    database (is_known) and its content is unique to one application.  Local
    uniqueness is judged from *md5_appkeys* (built across the scanned set); when
    *global_check* is on, /hash-lookup confirms the file does not also appear in
    other items across the whole catalogue.
    """
    def is_unique(f: dict) -> bool:
        if f.get('is_known'):
            return False
        md5 = (f.get('md5') or '').lower()
        if not md5:
            return False
        keys = md5_appkeys.get(md5)
        if keys is None or len(keys) != 1:
            return False
        if global_check:
            try:
                data = client.hash_lookup(md5=f.get('md5'), sha1=f.get('sha1'))
            except Exception:  # noqa: BLE001 - network hiccup: trust local result
                return True
            if data.get('known_file'):
                return False
            item_ids = {fi.get('item_id') for fi in data.get('found_in', [])}
            if len(item_ids) > 1:
                return False
        return True

    return is_unique


# ---------------------------------------------------------------------------
# Item gathering and product building
# ---------------------------------------------------------------------------

def _gather_item(client: ArcologyClient, item: dict, filter_tags: list[str],
                 root_files: str) -> dict:
    """Fetch an item's artefacts/partitions/files and group by application dir."""
    item_name = item['name']
    category = ''
    for tag in item.get('tags', []):
        if tag not in filter_tags:
            category = tag
            break

    item_detail = client.get_item(item['uuid'])
    artefacts = item_detail.get('artefacts', [])

    artefact_results = []
    for art in artefacts:
        art_label = art.get('label', art.get('original_filename', ''))
        art_detail = client.get_artefact(art['uuid'])
        partitions = art_detail.get('partitions', [])
        if not partitions:
            continue

        all_files = []
        for part in partitions:
            all_files.extend(
                client.get_partition_files_all(part['uuid'], show_known='true')
            )
        if not all_files:
            continue

        parsed = parse_artefact_label(art_label)
        artefact_results.append({
            'label': art_label,
            'disc_number': parsed['disc_number'],
            'version': parsed['version'],
            'app_dirs': find_app_directories(all_files, root_files, art_label),
        })

    version = next((ar['version'] for ar in artefact_results if ar['version']), None)
    return {
        'item': item,
        'item_name': item_name,
        'category': category,
        'version': version,
        'artefact_results': artefact_results,
    }


def _build_products(client: ArcologyClient, g: dict, args, is_unique) -> list[dict]:
    """Build HashDB product dicts for one gathered item."""
    products = []
    description = g['category'].title() if g['category'] else ''
    artefact_results = g['artefact_results']
    is_multi_disc = len(artefact_results) > 1
    context = _item_context(g['item_name'], g['version'])

    def make_product(app_dir_name, app_files, disc_number, suffix=''):
        launched = get_launched_set(client, app_files, verbose=args.verbose)
        classified = classify_app_files(app_dir_name, app_files, launched,
                                        is_unique, verbose=args.verbose)
        pfiles = build_product_files(classified, verbose=args.verbose)
        if not pfiles:
            return None
        return {
            'title': build_product_title(app_dir_name, context, disc_number) + suffix,
            'description': description,
            'path_match_enabled': True,
            'files': pfiles,
        }

    if args.multi_disc in ('separate', 'both'):
        for ar in artefact_results:
            disc_num = ar['disc_number'] if is_multi_disc else None
            for app_dir_name, app_files in ar['app_dirs'].items():
                p = make_product(app_dir_name, app_files, disc_num)
                if p:
                    products.append(p)

    if args.multi_disc in ('merge', 'both'):
        merged: dict[str, list[dict]] = {}
        for ar in artefact_results:
            disc_num = ar['disc_number']
            for app_dir_name, app_files in ar['app_dirs'].items():
                bucket = merged.setdefault(app_dir_name, [])
                for f in app_files:
                    if is_multi_disc and disc_num is not None:
                        f = dict(f)
                        f['path'] = f'Disk {disc_num}/{f.get("path", "")}'
                    bucket.append(f)

        for app_dir_name, all_files in merged.items():
            seen = set()
            deduped = []
            for f in all_files:
                key = (f.get('md5', ''), f.get('path', ''))
                if key not in seen:
                    seen.add(key)
                    deduped.append(f)
            suffix = ' [All Discs]' if (args.multi_disc == 'both' and is_multi_disc) else ''
            p = make_product(app_dir_name, deduped, None, suffix=suffix)
            if p:
                products.append(p)

    return products


# ---------------------------------------------------------------------------
# Item selection
# ---------------------------------------------------------------------------

def _select_items(client: ArcologyClient, args) -> list[dict]:
    """Select items by explicit UUID, or by tag(s) and/or platform."""
    if getattr(args, 'item', None):
        return [client.get_item(uuid) for uuid in args.item]

    platform_id = None
    if getattr(args, 'platform', None):
        platform_id = client.lookup_platform(args.platform)
        if platform_id is None:
            log.error('Platform not found: %s', args.platform)
            sys.exit(1)

    tags = args.tag or [None]
    seen = set()
    result = []
    for t in tags:
        params = {}
        if t:
            params['tag'] = t
        if platform_id is not None:
            params['platform_id'] = platform_id
        for it in client.list_items_all(**params):
            if it['uuid'] not in seen:
                seen.add(it['uuid'])
                result.append(it)
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cmd_hashdb_generate_riscos(client: ArcologyClient, args):
    """Generate RISC OS HashDB JSON from items in Arcology."""
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format='%(message)s', stream=sys.stderr)

    if not (getattr(args, 'tag', None) or getattr(args, 'item', None)
            or getattr(args, 'platform', None)):
        log.error('Select items with at least one of --tag, --item or --platform.')
        sys.exit(1)

    filter_tags = list(args.tag or [])

    items = _select_items(client, args)
    if not items:
        log.error('No items matched the selection.')
        sys.exit(1)
    log.info('Selected %d item(s)', len(items))

    if args.dry_run:
        log.info('')
        log.info('=== DRY RUN ===')
        for item in items:
            tags = ', '.join(item.get('tags', []))
            log.info('  %s [%s] (%s artefact(s))', item['name'], tags,
                     item.get('artefact_count', '?'))
        if getattr(args, 'json', False):
            from ..formatting import print_json
            print_json({
                'dry_run': True,
                'items': [
                    {'name': it['name'], 'tags': it.get('tags', []),
                     'artefact_count': it.get('artefact_count', 0)}
                    for it in items
                ],
            })
        return

    # Pass 1: gather every item's application files.
    gathered = []
    for i, item in enumerate(items, 1):
        log.info('[%d/%d] %s', i, len(items), item['name'])
        g = _gather_item(client, item, filter_tags, args.root_files)
        if g['artefact_results']:
            gathered.append(g)

    # Build the cross-collection uniqueness map: md5 -> {(item_uuid, app_dir)}.
    md5_appkeys: dict[str, set] = {}
    for g in gathered:
        item_uuid = g['item']['uuid']
        for ar in g['artefact_results']:
            for app_dir_name, files in ar['app_dirs'].items():
                key = (item_uuid, app_dir_name)
                for f in files:
                    md5 = (f.get('md5') or '').lower()
                    if md5:
                        md5_appkeys.setdefault(md5, set()).add(key)

    is_unique = make_is_unique(client, md5_appkeys, args.global_check)

    # Pass 2: build products.
    all_products = []
    items_with_products = 0
    for g in gathered:
        products = _build_products(client, g, args, is_unique)
        if products:
            all_products.extend(products)
            items_with_products += 1

    output_data = {
        'schema_version': 1,
        'database': {
            'name': args.db_name,
            'description': args.db_description or '',
            'version': args.db_version or date.today().isoformat(),
        },
        'products': all_products,
    }
    if args.source_url:
        output_data['database']['source_url'] = args.source_url

    total_files = sum(len(p['files']) for p in all_products)
    mandatory_files = sum(sum(1 for f in p['files'] if f['is_required'])
                          for p in all_products)
    optional_files = total_files - mandatory_files

    with open(args.output, 'w', encoding='utf-8') as fh:
        json.dump(output_data, fh, indent=2, default=str)

    log.info('')
    log.info('=== Summary ===')
    log.info('Items processed:  %d (%d produced products)',
             len(items), items_with_products)
    log.info('Products:         %d', len(all_products))
    log.info('Files:            %d (%d mandatory, %d optional)',
             total_files, mandatory_files, optional_files)
    log.info('Output:           %s', args.output)
    log.info('')
    log.info('Import with:  arco hashdb import %s', args.output)

    if getattr(args, 'json', False):
        from ..formatting import print_json
        print_json({
            'output': args.output,
            'items_processed': len(items),
            'items_with_products': items_with_products,
            'products': len(all_products),
            'files': total_files,
            'mandatory_files': mandatory_files,
            'optional_files': optional_files,
        })

# vim: ts=4 sw=4 et
