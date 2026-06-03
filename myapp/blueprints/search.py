"""
Arcology - Search Blueprint

Global cross-item search using a prefix query syntax.
"""

import re
from flask import Blueprint, render_template, request
from flask_login import current_user
from sqlalchemy import and_, distinct, func, or_
from ..database import (
    Artefact,
    ArtefactMastering,
    ArtefactProtection,
    ExtractedFile,
    FilesystemType,
    Item,
    Partition,
    RiscosModule,
    Tag,
    artefact_tags,
)
from ..extensions import db
from ..permissions import public_readable
from ..riscos_filetypes import lookup_filetype_hex
from ..utils.pagination import VALID_PER_PAGE, ListPagination, resolve_per_page
from ..visibility import artefact_visibility_clause, item_visibility_clause

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/search', template_folder='templates')


def init_app(app):
    app.add_menu_item("Search", f"{ROUTENAME}.index", 50)


# =============================================================================
# Query parser
# =============================================================================

# Prefix aliases → canonical key
_ALIASES = {
    'file':       'filename',
    'filetype':   'type',
    'disc':       'label',
    'gnu':        'ident',
    'gnufile':    'ident',
    'filesystem': 'fs',
    'prot':       'protection',
}

# Regex: optional-quote value after colon, or bare word
_TOKEN_RE = re.compile(
    r'(\w+):"([^"]+)"'   # key:"quoted value"
    r'|(\w+):(\S+)'      # key:value
    r'|"([^"]+)"'        # "bare quoted phrase"
    r'|(\S+)',            # bare word
    re.UNICODE,
)

PER_PAGE = 50


def parse_query(raw: str) -> dict:
    """Parse a search query string into a dict of {key: [values]}.

    Keys: md5, sha1, sha256, filename, path, type, ext, ident,
          label, fs, protection, mastering, tag, text (bare words).
    """
    tokens: dict[str, list[str]] = {}

    for m in _TOKEN_RE.finditer(raw or ''):
        if m.group(1):   # key:"quoted value"
            key, val = m.group(1).lower(), m.group(2)
        elif m.group(3): # key:value
            key, val = m.group(3).lower(), m.group(4)
        elif m.group(5): # "bare quoted phrase"
            key, val = 'text', m.group(5)
        else:             # bare word
            key, val = 'text', m.group(6)

        key = _ALIASES.get(key, key)
        tokens.setdefault(key, []).append(val)

    return tokens


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/')
@public_readable
def index():
    q = request.args.get('q', '').strip()
    per_page, page, view_all = resolve_per_page('SEARCH_PER_PAGE', PER_PAGE)
    # Clamp page to ≥1 (resolve_per_page already does this via request.args)
    page = max(1, page)
    tokens = parse_query(q)

    results = _run_search(tokens, page=page, per_page=per_page) if q else None
    if results:
        results.pop('has_next', None)
        totals = results.pop('totals', {'files': 0, 'artefacts': 0, 'items': 0})
        max_total = max(totals.values()) if totals else 0
    else:
        totals = {'files': 0, 'artefacts': 0, 'items': 0}
        max_total = 0

    # Build a Pagination-compatible object across the largest result bucket so
    # search shares the common pagination macro.  range() keeps this O(1) — the
    # shim only needs the count, not the rows (those live in `results`).
    pagination = ListPagination(range(max_total), page, per_page)
    pagination_args = {k: v for k, v in request.args.items() if k != 'page'}

    known_protection_types = sorted(
        v for (v,) in db.session.query(distinct(ArtefactProtection.protection_type)).all()
    )
    known_mastering_types = sorted(
        v for (v,) in db.session.query(distinct(ArtefactMastering.mastering_type)).all()
    )

    return render_template(
        'search/index.html',
        q=q,
        tokens=tokens,
        results=results,
        totals=totals,
        pagination=pagination,
        pagination_args=pagination_args,
        valid_per_page=VALID_PER_PAGE,
        view_all=view_all,
        FilesystemType=FilesystemType,
        known_protection_types=known_protection_types,
        known_mastering_types=known_mastering_types,
    )


# =============================================================================
# Search logic
# =============================================================================


def _ilike(col, val):
    """Case-insensitive substring filter, with * as wildcard."""
    pattern = val.replace('*', '%')
    if '%' not in pattern:
        pattern = f'%{pattern}%'
    return col.ilike(pattern)


def _ilike_path(col, val):
    """Case-insensitive filter for filename/path fields.

    Without a wildcard: exact match (filename:!Run matches only '!Run').
    With a wildcard:    glob match (filename:!Run* matches '!RunImage' etc.).

    This is more intuitive for filesystem names than the default substring
    behaviour of _ilike(), which would make filename:!Run match !RunImage.
    """
    pattern = val.replace('*', '%')
    return col.ilike(pattern)


def _ilike_json(col, val):
    """Like _ilike but always wraps in %..% for JSON text column searches.

    JSON columns store lists as '["Foo", "Bar"]', so even a trailing-wildcard
    pattern like 'Desktop*' needs leading '%' to skip the '["' prefix.
    """
    pattern = val.replace('*', '%')
    if not pattern.startswith('%'):
        pattern = f'%{pattern}'
    if not pattern.endswith('%'):
        pattern = f'{pattern}%'
    return col.ilike(pattern)


def _resolve_riscos_type(val: str):
    """Return an ExtractedFile.risc_os_filetype filter for a type: term.

    Accepts either a 3-digit hex code (e.g. 'fea') or a human-readable RISC OS
    filetype name (e.g. 'Desktop').  Returns a SQLAlchemy column expression, or
    None if the value cannot be resolved to a known hex code.
    """
    hex_code = lookup_filetype_hex(val)
    if hex_code is not None:
        return ExtractedFile.risc_os_filetype == hex_code
    # Unknown name/code — match literally (may simply return no rows)
    return ExtractedFile.risc_os_filetype == val.lower()


def _dedup_by_artefact(rows):
    """Deduplicate query rows by artefact id (second element of each row)."""
    seen = set()
    deduped = []
    for row in rows:
        _, a, i = row
        if a.id not in seen:
            seen.add(a.id)
            deduped.append(row)
    return deduped


def _search_files(tokens, page=1, per_page=PER_PAGE):
    """Search ExtractedFile by hash, filename, path, type, or extension."""
    per_key = {}
    for h in tokens.get('md5', []):
        per_key.setdefault('md5', []).append(ExtractedFile.md5 == h.lower())
    for h in tokens.get('sha1', []):
        per_key.setdefault('sha1', []).append(ExtractedFile.sha1 == h.lower())
    for h in tokens.get('sha256', []):
        per_key.setdefault('sha256', []).append(ExtractedFile.sha256 == h.lower())
    for v in tokens.get('filename', []):
        per_key.setdefault('filename', []).append(_ilike_path(ExtractedFile.filename, v))
    for v in tokens.get('path', []):
        per_key.setdefault('path', []).append(_ilike(ExtractedFile.path, v))
    for v in tokens.get('type', []):
        per_key.setdefault('type', []).append(_resolve_riscos_type(v))
    for v in tokens.get('ext', []):
        per_key.setdefault('ext', []).append(ExtractedFile.extension == v.lower())

    if not per_key:
        return [], 0

    combined = and_(*[or_(*clauses) for clauses in per_key.values()])
    base_q = (
        db.session.query(ExtractedFile, Partition, Artefact, Item)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(combined)
        .filter(ExtractedFile.is_directory == False)
        .filter(artefact_visibility_clause(current_user))
    )
    total = base_q.count()
    q = base_q.order_by(
        func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path)
    ).offset((page - 1) * per_page).limit(per_page).all()
    return q, total


def _search_partitions(tokens, page=1, per_page=PER_PAGE):
    """Search Partitions by label, ident, or filesystem type."""
    per_key = {}
    for v in tokens.get('label', []):
        per_key.setdefault('label', []).append(_ilike(Partition.label, v))
    for v in tokens.get('ident', []):
        per_key.setdefault('ident', []).append(_ilike(Partition.gnu_file_type, v))
    for v in tokens.get('fs', []):
        try:
            fs_val = FilesystemType(v.lower())
            per_key.setdefault('fs', []).append(Partition.filesystem == fs_val)
        except ValueError:
            per_key.setdefault('fs', []).append(_ilike(Partition.container_format, v))

    if not per_key:
        return [], 0

    combined = and_(*[or_(*clauses) for clauses in per_key.values()])
    base_q = (
        db.session.query(Partition, Artefact, Item)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(combined)
        .filter(artefact_visibility_clause(current_user))
    )
    total = base_q.count()
    q = base_q.order_by(
        func.lower(Item.name), func.lower(Artefact.label), Partition.partition_index
    ).offset((page - 1) * per_page).limit(per_page).all()
    return q, total


def _search_protection(tokens, page=1, per_page=PER_PAGE):
    """Search ArtefactProtection by protection type."""
    all_results = []
    total = 0
    for prot_type in tokens.get('protection', []):
        base_q = (
            db.session.query(ArtefactProtection, Artefact, Item)
            .join(Artefact, ArtefactProtection.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .filter(ArtefactProtection.protection_type == prot_type.lower())
            .filter(artefact_visibility_clause(current_user))
        )
        total += base_q.count()
        q = base_q.order_by(func.lower(Item.name), func.lower(Artefact.label)) \
                  .offset((page - 1) * per_page).limit(per_page).all()
        deduped = _dedup_by_artefact(q)
        all_results.extend([
            {'type': 'protection', 'protection_type': prot_type, 'artefact': a, 'item': i}
            for _, a, i in deduped
        ])
    return all_results, total


def _search_mastering(tokens, page=1, per_page=PER_PAGE):
    """Search ArtefactMastering by mastering type."""
    all_results = []
    total = 0
    for mast_type in tokens.get('mastering', []):
        base_q = (
            db.session.query(ArtefactMastering, Artefact, Item)
            .join(Artefact, ArtefactMastering.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .filter(ArtefactMastering.mastering_type == mast_type.lower())
            .filter(artefact_visibility_clause(current_user))
        )
        total += base_q.count()
        q = base_q.order_by(func.lower(Item.name), func.lower(Artefact.label)) \
                  .offset((page - 1) * per_page).limit(per_page).all()
        deduped = _dedup_by_artefact(q)
        all_results.extend([
            {'type': 'mastering', 'mastering_type': mast_type, 'artefact': a, 'item': i}
            for _, a, i in deduped
        ])
    return all_results, total


def _search_modules(tokens, page=1, per_page=PER_PAGE):
    """Search RiscosModule by title_string or help_title, returning file tuples."""
    all_results = []
    total = 0
    for mod_val in tokens.get('module', []):
        base_q = (
            db.session.query(ExtractedFile, Partition, Artefact, Item)
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .join(Artefact, Partition.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .join(RiscosModule, and_(
                RiscosModule.artefact_id == Artefact.id,
                RiscosModule.file_path == ExtractedFile.path))
            .filter(or_(
                _ilike(RiscosModule.title_string, mod_val),
                _ilike(RiscosModule.help_title, mod_val)))
            .filter(ExtractedFile.is_directory == False)
            .filter(artefact_visibility_clause(current_user))
        )
        total += base_q.count()
        q = base_q.order_by(
            func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path)
        ).offset((page - 1) * per_page).limit(per_page).all()
        all_results.extend(q)
    return all_results, total


def _search_commands(tokens, page=1, per_page=PER_PAGE):
    """Search RiscosModule by star command name, returning file tuples."""
    all_results = []
    total = 0
    for cmd_val in tokens.get('command', []):
        base_q = (
            db.session.query(ExtractedFile, Partition, Artefact, Item)
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .join(Artefact, Partition.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .join(RiscosModule, and_(
                RiscosModule.artefact_id == Artefact.id,
                RiscosModule.file_path == ExtractedFile.path))
            .filter(RiscosModule.commands.isnot(None))
            .filter(_ilike_json(RiscosModule.commands, cmd_val))
            .filter(ExtractedFile.is_directory == False)
            .filter(artefact_visibility_clause(current_user))
        )
        total += base_q.count()
        q = base_q.order_by(
            func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path)
        ).offset((page - 1) * per_page).limit(per_page).all()
        all_results.extend(q)
    return all_results, total


def _search_swis(tokens, page=1, per_page=PER_PAGE):
    """Search RiscosModule by SWI name, returning file tuples."""
    all_results = []
    total = 0
    for swi_val in tokens.get('swi', []):
        base_q = (
            db.session.query(ExtractedFile, Partition, Artefact, Item)
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .join(Artefact, Partition.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .join(RiscosModule, and_(
                RiscosModule.artefact_id == Artefact.id,
                RiscosModule.file_path == ExtractedFile.path))
            .filter(RiscosModule.swi_names.isnot(None))
            .filter(_ilike_json(RiscosModule.swi_names, swi_val))
            .filter(ExtractedFile.is_directory == False)
            .filter(artefact_visibility_clause(current_user))
        )
        total += base_q.count()
        q = base_q.order_by(
            func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path)
        ).offset((page - 1) * per_page).limit(per_page).all()
        all_results.extend(q)
    return all_results, total


def _search_tags(tokens, page=1, per_page=PER_PAGE):
    """Search artefacts by tag name."""
    all_results = []
    total = 0
    for tag_val in tokens.get('tag', []):
        base_q = (
            db.session.query(Artefact, Item)
            .join(Item, Artefact.item_id == Item.id)
            .join(artefact_tags, artefact_tags.c.artefact_id == Artefact.id)
            .join(Tag, artefact_tags.c.tag_id == Tag.id)
            .filter(_ilike(Tag.name, tag_val))
            .filter(artefact_visibility_clause(current_user))
        )
        total += base_q.count()
        q = base_q.order_by(func.lower(Item.name), func.lower(Artefact.label)) \
                  .offset((page - 1) * per_page).limit(per_page).all()
        all_results.extend([
            {'type': 'tag', 'tag_name': tag_val, 'artefact': a, 'item': i}
            for a, i in q
        ])
    return all_results, total


def _search_artefact_hashes(tokens, page=1, per_page=PER_PAGE):
    """Search artefact-level hashes (md5, sha256)."""
    art_filters = []
    for h in tokens.get('md5', []):
        art_filters.append(Artefact.md5 == h.lower())
    for h in tokens.get('sha256', []):
        art_filters.append(Artefact.sha256 == h.lower())

    if not art_filters:
        return [], 0

    base_q = (
        db.session.query(Artefact, Item)
        .join(Item, Artefact.item_id == Item.id)
        .filter(or_(*art_filters))
        .filter(artefact_visibility_clause(current_user))
    )
    total = base_q.count()
    q = base_q.order_by(func.lower(Item.name), func.lower(Artefact.label)) \
              .offset((page - 1) * per_page).limit(per_page).all()
    return [
        {'type': 'artefact_hash', 'artefact': a, 'item': i}
        for a, i in q
    ], total


def _search_text_items(tokens, page=1, per_page=PER_PAGE):
    """Free-text search on item name/description."""
    text_filters = []
    for v in tokens.get('text', []):
        pattern = f'%{v}%'
        text_filters.append(Item.name.ilike(pattern))
        text_filters.append(Item.description.ilike(pattern))

    if not text_filters:
        return [], 0

    base_q = (
        Item.query
        .filter(or_(*text_filters))
        .filter(item_visibility_clause(current_user))
    )
    total = base_q.count()
    q = base_q.order_by(func.lower(Item.name)) \
              .offset((page - 1) * per_page).limit(per_page).all()
    return q, total


def _search_text_artefacts(tokens, page=1, per_page=PER_PAGE):
    """Free-text search on artefact label/description."""
    art_text_filters = []
    for v in tokens.get('text', []):
        pattern = f'%{v}%'
        art_text_filters.append(Artefact.label.ilike(pattern))
        art_text_filters.append(Artefact.description.ilike(pattern))

    if not art_text_filters:
        return [], 0

    base_q = (
        db.session.query(Artefact, Item)
        .join(Item, Artefact.item_id == Item.id)
        .filter(or_(*art_text_filters))
        .filter(artefact_visibility_clause(current_user))
    )
    total = base_q.count()
    q = base_q.order_by(func.lower(Item.name), func.lower(Artefact.label)) \
              .offset((page - 1) * per_page).limit(per_page).all()
    return [
        {'type': 'artefact_text', 'artefact': a, 'item': i}
        for a, i in q
    ], total


def _run_search(tokens: dict, page: int = 1, per_page: int = PER_PAGE) -> dict:
    """Execute queries and return result buckets with per-bucket totals."""
    results = {
        'files':           [],
        'artefacts':       [],
        'catalogue_items': [],
        'totals':          {'files': 0, 'artefacts': 0, 'items': 0},
        'has_next':        False,
    }

    has_file_terms = any(k in tokens for k in ('md5', 'sha1', 'sha256', 'filename', 'path', 'type', 'ext'))
    has_disc_terms = any(k in tokens for k in ('label', 'ident', 'fs'))
    has_text = 'text' in tokens

    def _add(bucket, rows, total):
        results[bucket] = results[bucket] + rows if rows else results[bucket]
        results['totals'][bucket] += total
        if total > page * per_page:
            results['has_next'] = True

    # File search
    if has_file_terms or any(k in tokens for k in ('md5', 'sha1', 'sha256')):
        files, total = _search_files(tokens, page=page, per_page=per_page)
        results['files'] = files
        results['totals']['files'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Disc/partition search
    if has_disc_terms:
        partitions, total = _search_partitions(tokens, page=page, per_page=per_page)
        results['artefacts'].extend([
            {'type': 'partition', 'partition': p, 'artefact': a, 'item': i}
            for p, a, i in partitions
        ])
        results['totals']['artefacts'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Protection indicator search
    if 'protection' in tokens:
        prot_results, total = _search_protection(tokens, page=page, per_page=per_page)
        results['artefacts'].extend(prot_results)
        results['totals']['artefacts'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Mastering indicator search
    if 'mastering' in tokens:
        mast_results, total = _search_mastering(tokens, page=page, per_page=per_page)
        results['artefacts'].extend(mast_results)
        results['totals']['artefacts'] += total
        if total > page * per_page:
            results['has_next'] = True

    # RISC OS module search
    if 'module' in tokens:
        mod_results, total = _search_modules(tokens, page=page, per_page=per_page)
        results['files'].extend(mod_results)
        results['totals']['files'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Star command search
    if 'command' in tokens:
        cmd_results, total = _search_commands(tokens, page=page, per_page=per_page)
        results['files'].extend(cmd_results)
        results['totals']['files'] += total
        if total > page * per_page:
            results['has_next'] = True

    # SWI name search
    if 'swi' in tokens:
        swi_results, total = _search_swis(tokens, page=page, per_page=per_page)
        results['files'].extend(swi_results)
        results['totals']['files'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Tag search
    if 'tag' in tokens:
        tag_results, total = _search_tags(tokens, page=page, per_page=per_page)
        results['artefacts'].extend(tag_results)
        results['totals']['artefacts'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Artefact hash search
    if any(k in tokens for k in ('md5', 'sha256')):
        hash_results, total = _search_artefact_hashes(tokens, page=page, per_page=per_page)
        results['artefacts'].extend(hash_results)
        results['totals']['artefacts'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Free-text search
    if has_text:
        items, total = _search_text_items(tokens, page=page, per_page=per_page)
        results['catalogue_items'] = items
        results['totals']['items'] += total
        if total > page * per_page:
            results['has_next'] = True

        art_text_results, total = _search_text_artefacts(tokens, page=page, per_page=per_page)
        results['artefacts'].extend(art_text_results)
        results['totals']['artefacts'] += total
        if total > page * per_page:
            results['has_next'] = True

    # Deduplicate file results
    seen_file_ids = set()
    deduped_files = []
    for row in results['files']:
        ef = row[0]
        if ef.id not in seen_file_ids:
            seen_file_ids.add(ef.id)
            deduped_files.append(row)
    results['files'] = deduped_files

    return results


# vim: ts=4 sw=4 et
