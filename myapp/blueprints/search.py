"""
Arcology - Search Blueprint

Global cross-item search using a prefix query syntax.
"""

import re
from flask import Blueprint, abort, render_template, request
from flask_login import current_user
from markupsafe import Markup, escape
from sqlalchemy import String, and_, case, cast, distinct, false, func, literal, or_
from ..database import (
    Artefact,
    ArtefactMastering,
    ArtefactProtection,
    ExtractedFile,
    FilesystemType,
    Item,
    Partition,
    ReplayMovie,
    RiscosModule,
    Tag,
    artefact_tags,
)
from ..extensions import db
from ..permissions import public_readable
from ..riscos_filetypes import lookup_filetype_hex
from ..services.file_metadata import metadata_by_file_id
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
    # Acorn Replay / ARMovie metadata keys (lower-cased by the parser)
    'replaytitle':       'replay_title',
    'replayauthor':      'replay_author',
    'replaycopyright':   'replay_copyright',
    'replayvideoformat': 'replay_vformat',
    'replayvideocodec':  'replay_vformat',   # synonym
    'replaycodec':       'replay_vformat',   # synonym
    'replaysoundformat': 'replay_sformat',
    'replaywidth':       'replay_width',
    'replayheight':      'replay_height',
    'replayframerate':   'replay_framerate',
    'replayduration':    'replay_duration',
}

# Reserved token key under which negated terms are collected.  A negation is a
# keyed term prefixed with '!' (e.g. '!type:Obey').  Stored as a nested
# {key: [values]} dict and only present when at least one negation is parsed.
NOT_KEY = '__not__'

# Canonical search keys recognised by the search engine (after alias resolution).
# Any key not in this set is silently ignored by all sub-searches; we surface
# such keys to the user as warnings so they can spot typos.
KNOWN_KEYS = frozenset({
    'md5', 'sha1', 'sha256',
    'filename', 'path', 'type', 'ext',
    'ident', 'label', 'fs',
    'protection', 'mastering',
    'module', 'command', 'swi',
    'tag',
    'text',
    'replay_title', 'replay_author', 'replay_copyright',
    'replay_vformat', 'replay_sformat',
    'replay_width', 'replay_height', 'replay_framerate', 'replay_duration',
})

# Regex: optional '!' negation prefix on keyed terms, quoted/bare value after a
# colon, or bare word.  Negation is only recognised on key:value forms — a bare
# word beginning with '!' (e.g. a RISC OS filename like '!Boot') is left intact.
_TOKEN_RE = re.compile(
    r'(!?)(\w+):"([^"]+)"'   # [!]key:"quoted value"
    r'|(!?)(\w+):(\S+)'      # [!]key:value
    r'|"([^"]+)"'            # "bare quoted phrase"
    r'|(\S+)',               # bare word
    re.UNICODE,
)

PER_PAGE = 50


def parse_query(raw: str) -> dict:
    """Parse a search query string into a dict of {key: [values]}.

    Keys: md5, sha1, sha256, filename, path, type, ext, ident,
          label, fs, protection, mastering, tag, text (bare words),
          replay_title, replay_author, replay_copyright, replay_vformat,
          replay_sformat, replay_width, replay_height, replay_framerate,
          replay_duration (Acorn Replay / ARMovie metadata).

    Keyed terms may be negated with a leading '!' (e.g. '!type:Obey').  Negated
    terms are collected under the reserved ``NOT_KEY`` as a nested
    {key: [values]} dict; this key is absent when no negations are present.
    """
    tokens: dict[str, list[str]] = {}
    negations: dict[str, list[str]] = {}

    for m in _TOKEN_RE.finditer(raw or ''):
        if m.group(2) is not None:    # [!]key:"quoted value"
            neg, key, val = m.group(1), m.group(2).lower(), m.group(3)
        elif m.group(5) is not None:  # [!]key:value
            neg, key, val = m.group(4), m.group(5).lower(), m.group(6)
        elif m.group(7) is not None:  # "bare quoted phrase"
            neg, key, val = '', 'text', m.group(7)
        else:                          # bare word
            neg, key, val = '', 'text', m.group(8)

        key = _ALIASES.get(key, key)
        target = negations if neg else tokens
        target.setdefault(key, []).append(val)

    if negations:
        tokens[NOT_KEY] = negations

    return tokens


def _neg(tokens: dict, key: str) -> list[str]:
    """Return the list of negated values for *key* (empty if none)."""
    return tokens.get(NOT_KEY, {}).get(key, [])


# =============================================================================
# Routes
# =============================================================================

@blueprint.route('/')
@public_readable
def index():
    q = request.args.get('q', '').strip()
    dedupe = request.args.get('dedupe', '').lower() in ('1', 'true', 'on', 'yes')
    per_page, page, view_all = resolve_per_page('SEARCH_PER_PAGE', PER_PAGE)
    # Clamp page to ≥1: resolve_per_page passes the raw ?page= value through,
    # and a negative page would produce a negative OFFSET in the sub-searches.
    page = max(1, page)
    tokens = parse_query(q)

    # Warn about keys the search engine doesn't recognise (typos / wrong syntax).
    # Aliases are already resolved by parse_query, so only truly unknown keys appear.
    _all_used_keys = (set(tokens) - {NOT_KEY}) | set(tokens.get(NOT_KEY, {}))
    unknown_keys = sorted(_all_used_keys - KNOWN_KEYS)

    # A query made up entirely of negations has nothing to match against — every
    # sub-search needs at least one positive term to seed its result set.
    has_positive = any(k != NOT_KEY for k in tokens)
    query_error = None
    if q and NOT_KEY in tokens and not has_positive:
        query_error = "A search must include at least one term that is not negated."

    query_warnings = _check_query_warnings(tokens)
    run = bool(q) and query_error is None
    results = _run_search(tokens, page=page, per_page=per_page, dedupe=dedupe) if run else None
    # Real result count drives the pagination.  Each bucket paginates
    # independently but shares one page number, so the number of pages needed to
    # view everything is the largest bucket's page count; _run_search reports
    # that bucket's total under 'total' (see its docstring).
    total = results['total'] if results else 0

    # Build a Pagination-compatible object so search shares the common macro.
    # range() keeps this O(1); the shim only needs the count, not the rows.
    pagination = ListPagination(range(total), page, per_page)
    pagination_args = {k: v for k, v in request.args.items() if k != 'page'}

    # Module / Replay viewer icons for the file results (parallel to the
    # artefact file listing).  Keyed by ExtractedFile.id.
    module_info, replay_info = metadata_by_file_id(results['files']) if results else ({}, {})

    # Per-representative duplicate counts (only present when dedupe collapsed
    # the file bucket).  Keyed by ExtractedFile.id like module_info/replay_info.
    dupe_info = {}
    if results:
        for row in results['files']:
            ef = row[0]
            count = getattr(ef, 'dupe_count', None)
            if count is not None and count > 1:
                dupe_info[ef.id] = {'count': count, 'key': getattr(ef, 'dupe_key', None)}

    known_protection_types = sorted(
        v for (v,) in db.session.query(distinct(ArtefactProtection.protection_type)).all()
    )
    known_mastering_types = sorted(
        v for (v,) in db.session.query(distinct(ArtefactMastering.mastering_type)).all()
    )

    return render_template(
        'search/index.html',
        q=q,
        dedupe=dedupe,
        tokens=tokens,
        query_error=query_error,
        unknown_keys=unknown_keys,
        query_warnings=query_warnings,
        results=results,
        pagination=pagination,
        pagination_args=pagination_args,
        valid_per_page=VALID_PER_PAGE,
        view_all=view_all,
        FilesystemType=FilesystemType,
        known_protection_types=known_protection_types,
        known_mastering_types=known_mastering_types,
        module_info=module_info,
        replay_info=replay_info,
        dupe_info=dupe_info,
    )


@blueprint.route('/help')
@public_readable
def help():
    """Search syntax reference page."""
    return render_template('search/help.html')


@blueprint.route('/duplicates')
@public_readable
def duplicates():
    """List every visible copy of one content hash (the dedupe "×N" target).

    ``key`` is the COALESCE(sha256, sha1, md5) group key produced by the
    collapsed file search.  Results are visibility-filtered, so a user only ever
    sees (and counts) the copies they are allowed to see.
    """
    key = request.args.get('key', '').strip()
    if not key:
        abort(404)

    per_page, page, view_all = resolve_per_page('SEARCH_PER_PAGE', PER_PAGE)
    page = max(1, page)

    hashkey = _file_hashkey()
    q = (
        db.session.query(ExtractedFile, Partition, Artefact, Item)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(hashkey == key)
        .filter(ExtractedFile.is_directory == False)
        .filter(artefact_visibility_clause(current_user))
    )
    total = _count_distinct(q, ExtractedFile.id)
    rows = (
        q.order_by(func.lower(Item.name), func.lower(Artefact.label), func.lower(ExtractedFile.path))
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    if total == 0:
        abort(404)

    pagination = ListPagination(range(total), page, per_page)
    pagination_args = {k: v for k, v in request.args.items() if k != 'page'}
    module_info, replay_info = metadata_by_file_id(rows)

    return render_template(
        'search/duplicates.html',
        key=key,
        rows=rows,
        total=total,
        representative=rows[0][0] if rows else None,
        pagination=pagination,
        pagination_args=pagination_args,
        valid_per_page=VALID_PER_PAGE,
        view_all=view_all,
        module_info=module_info,
        replay_info=replay_info,
    )


# =============================================================================
# Search logic
# =============================================================================


def _hash_filter(col, val, full_len: int):
    """Match a hash column: exact equality at full length, prefix ILIKE otherwise.

    A value shorter than *full_len* is treated as a prefix — e.g. md5:deadbeef
    (8 chars) matches any row whose md5 starts with 'deadbeef'.  Values at the
    full expected length use strict equality.
    """
    v = val.lower()
    if len(v) < full_len:
        return col.ilike(f'{v}%')
    return col == v


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


def _negate(clause):
    """Null-safe negation: TRUE for rows where *clause* is not TRUE.

    A plain ``~clause`` drops rows where the underlying column is NULL, because
    a NULL comparison yields NULL (not TRUE), and ``NOT NULL`` is still NULL.
    That would wrongly exclude e.g. a file with no RISC OS filetype from a
    ``!type:Obey`` term.  A CASE routes both non-matching and NULL rows to the
    ELSE branch so they are kept.
    """
    return case((clause, False), else_=True)


def _negated_clauses(tokens: dict, builders: dict) -> list:
    """Build null-safe NOT clauses for any negated terms among *builders* keys.

    *builders* maps a token key to a callable turning one value into the same
    column expression used for the positive match; each negated value is wrapped
    in :func:`_negate` so matching rows are excluded.
    """
    out = []
    for key, builder in builders.items():
        for val in _neg(tokens, key):
            out.append(_negate(builder(val)))
    return out


def _numeric_filter(col, val: str, *, is_float: bool = False):
    """Build a numeric column filter supporting exact match or a range.

    Supported value forms (general — usable by any numeric search key):
      ``160``            exact match
      ``>=160`` / ``>160`` / ``<=300`` / ``<300``   comparison
      ``15..25``         inclusive range (15 ≤ col ≤ 25)
      ``15..``           lower bound only (col ≥ 15)
      ``..23``           upper bound only (col ≤ 23)

    An unparseable value yields a clause that matches nothing (rather than
    raising), so a malformed term simply returns no rows.
    """
    cast = float if is_float else int

    def _num(s):
        try:
            return cast(s)
        except (ValueError, TypeError):
            return None

    val = val.strip()

    # Range form: "lo..hi", "lo..", "..hi"
    if '..' in val:
        lo_s, hi_s = val.split('..', 1)
        clauses = []
        if lo_s != '':
            lo = _num(lo_s)
            if lo is None:
                return false()
            clauses.append(col >= lo)
        if hi_s != '':
            hi = _num(hi_s)
            if hi is None:
                return false()
            clauses.append(col <= hi)
        if not clauses:
            return false()
        return and_(*clauses)

    # Comparison operators
    for op_str, op in (('>=', '>='), ('<=', '<='), ('>', '>'), ('<', '<')):
        if val.startswith(op_str):
            n = _num(val[len(op_str):])
            if n is None:
                return false()
            if op == '>=':
                return col >= n
            if op == '<=':
                return col <= n
            if op == '>':
                return col > n
            return col < n

    # Bare value → exact match
    n = _num(val)
    if n is None:
        return false()
    return col == n


def _file_hashkey():
    """Group key collapsing byte-identical extracted files.

    Identical content shares the same md5/sha1/sha256, so
    ``COALESCE(sha256, sha1, md5)`` groups every physical copy of one file.  A
    file with no hash at all falls back to an ``id:<id>`` sentinel so it never
    merges with another unhashed file (the ``id:`` prefix cannot collide with a
    hex hash).
    """
    return func.coalesce(
        ExtractedFile.sha256,
        ExtractedFile.sha1,
        ExtractedFile.md5,
        literal('id:') + cast(ExtractedFile.id, String),
    )


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


def _count_rows(query) -> int:
    """Total number of rows the (unordered) *query* would return."""
    return query.order_by(None).count()


def _count_distinct(query, col) -> int:
    """Number of distinct *col* values in the (unordered) *query*.

    Use for sub-searches whose result rows are later deduplicated on *col*
    (e.g. file buckets deduped by ExtractedFile.id, or artefact buckets deduped
    by Artefact.id), so the reported total matches what the user can page
    through rather than the inflated joined-row count.
    """
    return query.order_by(None).with_entities(func.count(distinct(col))).scalar() or 0


def _file_order():
    """Canonical sort for file-result rows: item → artefact → path (A→Z)."""
    return (
        func.lower(Item.name),
        func.lower(Artefact.label),
        func.lower(ExtractedFile.path),
    )


def _paginate_file_query(q, page, per_page, dedupe=False):
    """Order, paginate and (optionally) consolidate a file-result query.

    *q* must select ``(ExtractedFile, Partition, Artefact, Item)`` and already
    carry its filters plus the visibility clause.  With *dedupe* set,
    byte-identical files collapse to one representative (see
    :func:`_dedupe_file_rows`); otherwise raw rows are returned.  Shared by every
    sub-search that feeds the file bucket (hash/name/path/type/ext, module,
    command, SWI, Replay).  Returns ``(rows, has_more, total)``.
    """
    if dedupe:
        return _dedupe_file_rows(q, page=page, per_page=per_page)
    total = _count_distinct(q, ExtractedFile.id)
    fetched = (
        q.order_by(*_file_order())
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    return fetched[:per_page], has_more, total


def _search_files(tokens, page=1, per_page=PER_PAGE, dedupe=False):
    """Search ExtractedFile by hash, filename, path, type, or extension.

    When *dedupe* is set, byte-identical files (same content hash) collapse to a
    single representative row — the first in the location sort order — and the
    representative carries transient ``dupe_count`` / ``dupe_key`` attributes so
    the caller can render a "×N copies" badge linking to the copies listing.
    Pagination then operates over distinct content groups, not raw rows.
    """
    per_key = {}
    for h in tokens.get('md5', []):
        per_key.setdefault('md5', []).append(_hash_filter(ExtractedFile.md5, h, 32))
    for h in tokens.get('sha1', []):
        per_key.setdefault('sha1', []).append(_hash_filter(ExtractedFile.sha1, h, 40))
    for h in tokens.get('sha256', []):
        per_key.setdefault('sha256', []).append(_hash_filter(ExtractedFile.sha256, h, 64))
    for v in tokens.get('filename', []):
        per_key.setdefault('filename', []).append(_ilike_path(ExtractedFile.filename, v))
    for v in tokens.get('path', []):
        per_key.setdefault('path', []).append(_ilike(ExtractedFile.path, v))
    for v in tokens.get('type', []):
        per_key.setdefault('type', []).append(_resolve_riscos_type(v))
    for v in tokens.get('ext', []):
        per_key.setdefault('ext', []).append(ExtractedFile.extension == v.lower())

    if not per_key:
        return [], False, 0

    neg = _negated_clauses(tokens, {
        'md5':      lambda v: _hash_filter(ExtractedFile.md5, v, 32),
        'sha1':     lambda v: _hash_filter(ExtractedFile.sha1, v, 40),
        'sha256':   lambda v: _hash_filter(ExtractedFile.sha256, v, 64),
        'filename': lambda v: _ilike_path(ExtractedFile.filename, v),
        'path':     lambda v: _ilike(ExtractedFile.path, v),
        'type':     _resolve_riscos_type,
        'ext':      lambda v: ExtractedFile.extension == v.lower(),
    })
    combined = and_(*[or_(*clauses) for clauses in per_key.values()], *neg)
    q = (
        db.session.query(ExtractedFile, Partition, Artefact, Item)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(combined)
        .filter(ExtractedFile.is_directory == False)
        .filter(artefact_visibility_clause(current_user))
    )
    return _paginate_file_query(q, page, per_page, dedupe)


def _dedupe_file_rows(q, page=1, per_page=PER_PAGE):
    """Collapse a file-result query by content hash.

    *q* is a ``(ExtractedFile, Partition, Artefact, Item)`` query already
    filtered (including the visibility clause, so counts never leak private
    copies).  Returns ``(rows, has_more, total)`` where *total* is the number of
    distinct content groups and each representative ExtractedFile carries
    ``dupe_count`` / ``dupe_key`` attributes.
    """
    hashkey = _file_hashkey()
    o_item, o_art, o_path = _file_order()
    # Distinct (ef.id, hashkey, sort-cols) base.  DISTINCT collapses any JOIN
    # fan-out — a module/Replay query joins a metadata row per file and could
    # otherwise emit a file more than once — so the per-group COUNT is a true
    # copy count and ranking stays stable.
    base = (
        q.order_by(None)
        .with_entities(
            ExtractedFile.id.label('ef_id'),
            hashkey.label('hkey'),
            o_item.label('o_item'),
            o_art.label('o_art'),
            o_path.label('o_path'),
        )
        .distinct()
        .subquery()
    )
    total = db.session.query(func.count(distinct(base.c.hkey))).scalar() or 0

    # Rank rows within each content group and count the group size, then keep
    # only the first row of each group as its representative.
    ranked = (
        db.session.query(
            base.c.ef_id,
            base.c.hkey,
            func.count().over(partition_by=base.c.hkey).label('copies'),
            func.row_number().over(
                partition_by=base.c.hkey,
                order_by=(base.c.o_item, base.c.o_art, base.c.o_path),
            ).label('rn'),
            base.c.o_item,
            base.c.o_art,
            base.c.o_path,
        )
        .subquery()
    )
    reps = (
        db.session.query(ranked.c.ef_id, ranked.c.hkey, ranked.c.copies)
        .filter(ranked.c.rn == 1)
        .order_by(ranked.c.o_item, ranked.c.o_art, ranked.c.o_path)
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(reps) > per_page
    reps = reps[:per_page]
    if not reps:
        return [], has_more, total

    counts = {r.ef_id: r.copies for r in reps}
    keys = {r.ef_id: r.hkey for r in reps}
    loaded = {
        row[0].id: row
        for row in (
            db.session.query(ExtractedFile, Partition, Artefact, Item)
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .join(Artefact, Partition.artefact_id == Artefact.id)
            .join(Item, Artefact.item_id == Item.id)
            .filter(ExtractedFile.id.in_(list(counts)))
            .all()
        )
    }
    fetched = []
    for r in reps:  # preserve the paginated location order
        row = loaded.get(r.ef_id)
        if row is None:
            continue
        row[0].dupe_count = counts[r.ef_id]
        row[0].dupe_key = keys[r.ef_id]
        fetched.append(row)
    return fetched, has_more, total


def _search_partitions(tokens, page=1, per_page=PER_PAGE):
    """Search Partitions by label, ident, or filesystem type."""
    per_key = {}
    for v in tokens.get('label', []):
        per_key.setdefault('label', []).append(_ilike(Partition.label, v))
    for v in tokens.get('ident', []):
        per_key.setdefault('ident', []).append(_ilike(Partition.gnu_file_type, v))
    def _fs_clause(v):
        try:
            return Partition.filesystem == FilesystemType(v.lower())
        except ValueError:
            return _ilike(Partition.container_format, v)

    for v in tokens.get('fs', []):
        per_key.setdefault('fs', []).append(_fs_clause(v))

    if not per_key:
        return [], False, 0

    neg = _negated_clauses(tokens, {
        'label': lambda v: _ilike(Partition.label, v),
        'ident': lambda v: _ilike(Partition.gnu_file_type, v),
        'fs':    _fs_clause,
    })
    combined = and_(*[or_(*clauses) for clauses in per_key.values()], *neg)
    q = (
        db.session.query(Partition, Artefact, Item)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(combined)
        .filter(artefact_visibility_clause(current_user))
    )
    total = _count_rows(q)
    fetched = (
        q.order_by(func.lower(Item.name), func.lower(Artefact.label), Partition.partition_index)
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    return fetched[:per_page], has_more, total


def _search_protection(tokens, page=1, per_page=PER_PAGE):
    """Search ArtefactProtection by protection type."""
    values = [v.lower() for v in tokens.get('protection', [])]
    if not values:
        return [], False, 0

    filters = [ArtefactProtection.protection_type.in_(values)]
    neg_values = [v.lower() for v in _neg(tokens, 'protection')]
    if neg_values:
        filters.append(_negate(ArtefactProtection.protection_type.in_(neg_values)))

    q = (
        db.session.query(ArtefactProtection, Artefact, Item)
        .join(Artefact, ArtefactProtection.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(and_(*filters))
        .filter(artefact_visibility_clause(current_user))
    )
    total = _count_distinct(q, Artefact.id)
    fetched = (
        q.order_by(func.lower(Item.name), func.lower(Artefact.label))
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    deduped = _dedup_by_artefact(fetched[:per_page])
    return [
        {'type': 'protection', 'protection_type': p.protection_type, 'artefact': a, 'item': i}
        for p, a, i in deduped
    ], has_more, total


def _search_mastering(tokens, page=1, per_page=PER_PAGE):
    """Search ArtefactMastering by mastering type."""
    values = [v.lower() for v in tokens.get('mastering', [])]
    if not values:
        return [], False, 0

    filters = [ArtefactMastering.mastering_type.in_(values)]
    neg_values = [v.lower() for v in _neg(tokens, 'mastering')]
    if neg_values:
        filters.append(_negate(ArtefactMastering.mastering_type.in_(neg_values)))

    q = (
        db.session.query(ArtefactMastering, Artefact, Item)
        .join(Artefact, ArtefactMastering.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(and_(*filters))
        .filter(artefact_visibility_clause(current_user))
    )
    total = _count_distinct(q, Artefact.id)
    fetched = (
        q.order_by(func.lower(Item.name), func.lower(Artefact.label))
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    deduped = _dedup_by_artefact(fetched[:per_page])
    return [
        {'type': 'mastering', 'mastering_type': m.mastering_type, 'artefact': a, 'item': i}
        for m, a, i in deduped
    ], has_more, total


def _search_riscos_module_files(tokens, key, clause_for, page=1, per_page=PER_PAGE, dedupe=False):
    """Shared RiscosModule → ExtractedFile search for module/command/swi terms.

    *clause_for* builds the per-value RiscosModule filter; values for the same
    key are ORed together so the whole key is one query with one count and one
    offset/limit (correct pagination regardless of how many values were given).
    """
    values = tokens.get(key, [])
    if not values:
        return [], False, 0

    module_filter = [or_(*[clause_for(v) for v in values])]
    module_filter += [_negate(clause_for(v)) for v in _neg(tokens, key)]

    q = (
        db.session.query(ExtractedFile, Partition, Artefact, Item)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .join(RiscosModule, and_(
            RiscosModule.artefact_id == Artefact.id,
            RiscosModule.file_path == ExtractedFile.path))
        .filter(and_(*module_filter))
        .filter(ExtractedFile.is_directory == False)
        .filter(artefact_visibility_clause(current_user))
    )
    return _paginate_file_query(q, page, per_page, dedupe)


def _search_modules(tokens, page=1, per_page=PER_PAGE, dedupe=False):
    """Search RiscosModule by title_string or help_title, returning file tuples."""
    return _search_riscos_module_files(
        tokens, 'module',
        lambda v: or_(_ilike(RiscosModule.title_string, v),
                      _ilike(RiscosModule.help_title, v)),
        page=page, per_page=per_page, dedupe=dedupe,
    )


def _search_commands(tokens, page=1, per_page=PER_PAGE, dedupe=False):
    """Search RiscosModule by star command name, returning file tuples."""
    return _search_riscos_module_files(
        tokens, 'command',
        lambda v: and_(RiscosModule.commands.isnot(None),
                       _ilike_json(RiscosModule.commands, v)),
        page=page, per_page=per_page, dedupe=dedupe,
    )


def _search_swis(tokens, page=1, per_page=PER_PAGE, dedupe=False):
    """Search RiscosModule by SWI name, returning file tuples."""
    return _search_riscos_module_files(
        tokens, 'swi',
        lambda v: and_(RiscosModule.swi_names.isnot(None),
                       _ilike_json(RiscosModule.swi_names, v)),
        page=page, per_page=per_page, dedupe=dedupe,
    )


def _search_replay_movies(tokens, page=1, per_page=PER_PAGE, dedupe=False):
    """Search ReplayMovie metadata, returning ExtractedFile tuples.

    Mirrors _search_riscos_module_files: joins ReplayMovie to its ExtractedFile
    by (artefact_id, file_path) so results render in the existing file bucket.
    Text keys use substring match; numeric keys support exact-or-range via
    _numeric_filter.  Different keys are ANDed; multiple values for one key are
    ORed.
    """
    # (token key, ReplayMovie column, clause builder)
    _text_keys = (
        ('replay_title', ReplayMovie.title),
        ('replay_author', ReplayMovie.author),
        ('replay_copyright', ReplayMovie.copyright),
    )
    _int_keys = (
        ('replay_vformat', ReplayMovie.video_format),
        ('replay_sformat', ReplayMovie.sound_format),
        ('replay_width', ReplayMovie.width),
        ('replay_height', ReplayMovie.height),
    )
    _float_keys = (
        ('replay_framerate', ReplayMovie.frame_rate),
        ('replay_duration', ReplayMovie.duration_seconds),
    )

    per_key = {}
    for key, col in _text_keys:
        for v in tokens.get(key, []):
            per_key.setdefault(key, []).append(_ilike(col, v))
    for key, col in _int_keys:
        for v in tokens.get(key, []):
            per_key.setdefault(key, []).append(_numeric_filter(col, v))
    for key, col in _float_keys:
        for v in tokens.get(key, []):
            per_key.setdefault(key, []).append(_numeric_filter(col, v, is_float=True))

    if not per_key:
        return [], False, 0

    combined = and_(*[or_(*clauses) for clauses in per_key.values()])
    q = (
        db.session.query(ExtractedFile, Partition, Artefact, Item)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .join(ReplayMovie, and_(
            ReplayMovie.artefact_id == Artefact.id,
            ReplayMovie.file_path == ExtractedFile.path))
        .filter(combined)
        .filter(ExtractedFile.is_directory == False)
        .filter(artefact_visibility_clause(current_user))
    )
    return _paginate_file_query(q, page, per_page, dedupe)


def _search_tags(tokens, page=1, per_page=PER_PAGE):
    """Search artefacts by tag name."""
    values = tokens.get('tag', [])
    if not values:
        return [], False, 0

    tag_filter = [or_(*[_ilike(Tag.name, v) for v in values])]
    tag_filter += [_negate(_ilike(Tag.name, v)) for v in _neg(tokens, 'tag')]

    q = (
        db.session.query(Artefact, Item, Tag.name)
        .join(Item, Artefact.item_id == Item.id)
        .join(artefact_tags, artefact_tags.c.artefact_id == Artefact.id)
        .join(Tag, artefact_tags.c.tag_id == Tag.id)
        .filter(and_(*tag_filter))
        .filter(artefact_visibility_clause(current_user))
    )
    total = _count_rows(q)
    fetched = (
        q.order_by(func.lower(Item.name), func.lower(Artefact.label))
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    return [
        {'type': 'tag', 'tag_name': tag_name, 'artefact': a, 'item': i}
        for a, i, tag_name in fetched[:per_page]
    ], has_more, total


def _search_artefact_hashes(tokens, page=1, per_page=PER_PAGE):
    """Search artefact-level hashes (md5, sha256)."""
    art_filters = []
    for h in tokens.get('md5', []):
        art_filters.append(_hash_filter(Artefact.md5, h, 32))
    for h in tokens.get('sha256', []):
        art_filters.append(_hash_filter(Artefact.sha256, h, 64))

    if not art_filters:
        return [], False, 0

    hash_filter = [or_(*art_filters)]
    hash_filter += _negated_clauses(tokens, {
        'md5':    lambda v: _hash_filter(Artefact.md5, v, 32),
        'sha256': lambda v: _hash_filter(Artefact.sha256, v, 64),
    })

    q = (
        db.session.query(Artefact, Item)
        .join(Item, Artefact.item_id == Item.id)
        .filter(and_(*hash_filter))
        .filter(artefact_visibility_clause(current_user))
    )
    total = _count_rows(q)
    fetched = (
        q.order_by(func.lower(Item.name), func.lower(Artefact.label))
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    return [
        {'type': 'artefact_hash', 'artefact': a, 'item': i}
        for a, i in fetched[:per_page]
    ], has_more, total


def _search_text_items(tokens, page=1, per_page=PER_PAGE):
    """Free-text search on item name/description."""
    text_filters = []
    for v in tokens.get('text', []):
        pattern = f'%{v}%'
        text_filters.append(Item.name.ilike(pattern))
        text_filters.append(Item.description.ilike(pattern))

    if not text_filters:
        return [], False, 0

    item_filter = [or_(*text_filters)]
    for v in _neg(tokens, 'text'):
        pattern = f'%{v}%'
        item_filter.append(_negate(or_(
            Item.name.ilike(pattern), Item.description.ilike(pattern))))

    q = (
        Item.query
        .filter(and_(*item_filter))
        .filter(item_visibility_clause(current_user))
    )
    total = _count_rows(q)
    fetched = (
        q.order_by(func.lower(Item.name))
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    return fetched[:per_page], has_more, total


def _search_text_artefacts(tokens, page=1, per_page=PER_PAGE):
    """Free-text search on artefact label/description."""
    art_text_filters = []
    for v in tokens.get('text', []):
        pattern = f'%{v}%'
        art_text_filters.append(Artefact.label.ilike(pattern))
        art_text_filters.append(Artefact.description.ilike(pattern))

    if not art_text_filters:
        return [], False, 0

    art_filter = [or_(*art_text_filters)]
    for v in _neg(tokens, 'text'):
        pattern = f'%{v}%'
        art_filter.append(_negate(or_(
            Artefact.label.ilike(pattern), Artefact.description.ilike(pattern))))

    q = (
        db.session.query(Artefact, Item)
        .join(Item, Artefact.item_id == Item.id)
        .filter(and_(*art_filter))
        .filter(artefact_visibility_clause(current_user))
    )
    total = _count_rows(q)
    fetched = (
        q.order_by(func.lower(Item.name), func.lower(Artefact.label))
        .offset((page - 1) * per_page)
        .limit(per_page + 1)
        .all()
    )
    has_more = len(fetched) > per_page
    return [
        {'type': 'artefact_text', 'artefact': a, 'item': i}
        for a, i in fetched[:per_page]
    ], has_more, total


def _check_query_warnings(tokens: dict) -> list:
    """Return a list of safe Markup warning strings for questionable query constructs.

    Catches common mistakes that silently return wrong or empty results:
    - Negated key whose sub-search won't activate (no positive activator present)
    - Unknown RISC OS filetype name/code in a type: or !type: term
    - Wildcard in a hash search (hashes are matched exactly, not as patterns)
    - Hash value that is the wrong length or contains non-hex characters
    """
    warnings = []
    positive_keys = set(tokens) - {NOT_KEY}
    negated = tokens.get(NOT_KEY, {})

    # Search-function activation groups.  Each sub-search only runs when at
    # least one of its "activating" positive keys is present.  A negated key
    # is "orphaned" — will silently have no effect — when every group it
    # belongs to is inactive.
    _file_keys     = frozenset({'md5', 'sha1', 'sha256', 'filename', 'path', 'type', 'ext'})
    _disc_keys     = frozenset({'label', 'ident', 'fs'})
    _replay_keys   = frozenset(k for k in KNOWN_KEYS if k.startswith('replay_'))
    _art_hash_keys = frozenset({'md5', 'sha256'})
    _solo_keys     = frozenset({'protection', 'mastering', 'module', 'command', 'swi', 'tag', 'text'})

    def _group_active(neg_key):
        if neg_key in _file_keys and positive_keys & _file_keys:
            return True
        if neg_key in _disc_keys and positive_keys & _disc_keys:
            return True
        if neg_key in _art_hash_keys and positive_keys & _art_hash_keys:
            return True
        if neg_key in _replay_keys and positive_keys & _replay_keys:
            return True
        if neg_key in _solo_keys and neg_key in positive_keys:
            return True
        return False

    for neg_key in negated:
        if neg_key not in KNOWN_KEYS:
            continue  # already flagged by unknown_keys check
        if not _group_active(neg_key):
            k = escape(neg_key)
            if neg_key in _file_keys:
                hint = Markup("add a positive file term such as <code>filename:</code>, <code>type:</code>, or <code>ext:</code>")
            elif neg_key in _disc_keys:
                hint = Markup("add a positive disc/partition term: <code>label:</code>, <code>ident:</code>, or <code>fs:</code>")
            elif neg_key in _replay_keys:
                hint = Markup("add a positive Replay term such as <code>replay_title:</code>")
            else:
                hint = Markup(f"add a positive <code>{k}:</code> term")
            warnings.append(Markup(
                f"<code>!{k}:</code> has no effect — {hint} to activate that search."
            ))

    # Invalid RISC OS filetype name or code
    for v, neg in [(v, False) for v in tokens.get('type', [])] + \
                  [(v, True)  for v in negated.get('type', [])]:
        if lookup_filetype_hex(v) is None:
            vesc = escape(v)
            pfx = Markup(f"!type:{vesc}") if neg else Markup(f"type:{vesc}")
            warnings.append(Markup(
                f"<code>{pfx}</code>: unknown RISC OS filetype — use a 3-digit hex code "
                "(e.g. <code>fff</code>) or a known name like "
                "<code>Text</code>, <code>BASIC</code>, <code>Absolute</code>."
            ))

    # Wildcards in hash searches — hashes are compared exactly, * never matches
    for hkey in ('md5', 'sha1', 'sha256'):
        for v in tokens.get(hkey, []) + negated.get(hkey, []):
            if '*' in v or '%' in v:
                warnings.append(Markup(
                    f"<code>{escape(hkey)}:</code> matches the full hash exactly — "
                    f"<code>{escape(v)}</code> will never match anything."
                ))

    # Hash value format / length validation.
    # Shorter-than-full values are valid prefix matches (e.g. md5:deadbeef).
    # Warn only when the value is longer than the full hash or contains non-hex chars.
    _hash_lengths = {'md5': 32, 'sha1': 40, 'sha256': 64}
    _hex_re = re.compile(r'^[0-9a-f]+$', re.IGNORECASE)
    for hkey, expected in _hash_lengths.items():
        for v in tokens.get(hkey, []) + negated.get(hkey, []):
            if '*' in v or '%' in v:
                continue  # already warned above
            if not _hex_re.match(v):
                warnings.append(Markup(
                    f"<code>{escape(hkey)}:{escape(v)}</code>: "
                    "hash values must be hexadecimal characters only."
                ))
            elif len(v) > expected:
                warnings.append(Markup(
                    f"<code>{escape(hkey)}:{escape(v)}</code>: "
                    f"too long — {escape(hkey)} hashes are {expected} hex characters."
                ))

    return warnings


def _run_search(tokens: dict, page: int = 1, per_page: int = PER_PAGE, dedupe: bool = False) -> dict:
    """Execute queries and return result buckets.

    Each result bucket (files / artefacts / catalogue_items) is paginated
    independently but the three share a single page number, so the number of
    pages needed to view everything is the largest bucket's page count.  Because
    ceil() is monotonic, ``max(ceil(bucket_total / per_page))`` equals
    ``ceil(max(bucket_total) / per_page)`` — so ``results['total']`` reports the
    largest bucket total and the caller derives the real page count from it
    (replacing the old next-page-only sentinel).

    Bucket totals are the sum of the contributing sub-searches' real counts.
    When several sub-searches feed the same bucket *and* match overlapping rows
    (rare — only multi-key queries), this is a slight over-count, which can show
    a sparse final page but never hides results.

    With *dedupe*, every file-producing sub-search (hash/name/path/type/ext,
    module, command, SWI, Replay) consolidates byte-identical files to one
    representative.  Consolidation is per-sub-search; combining file *and* module
    keys in one query (rare) consolidates each independently, so the same content
    matched by two different keys may still appear twice — the same best-effort
    property as the bucket-total over-count above.
    """
    results = {
        'files':           [],
        'artefacts':       [],
        'catalogue_items': [],
        'has_next':        False,
    }
    bucket_totals = {'files': 0, 'artefacts': 0, 'catalogue_items': 0}

    has_file_terms = any(k in tokens for k in ('md5', 'sha1', 'sha256', 'filename', 'path', 'type', 'ext'))
    has_disc_terms = any(k in tokens for k in ('label', 'ident', 'fs'))
    has_text = 'text' in tokens

    def _add(bucket, rows, has_more, total):
        if rows:
            results[bucket].extend(rows)
        if has_more:
            results['has_next'] = True
        bucket_totals[bucket] += total

    # File search
    if has_file_terms:
        files, has_more, total = _search_files(tokens, page=page, per_page=per_page, dedupe=dedupe)
        _add('files', files, has_more, total)

    # Disc/partition search
    if has_disc_terms:
        partitions, has_more, total = _search_partitions(tokens, page=page, per_page=per_page)
        _add('artefacts', [
            {'type': 'partition', 'partition': p, 'artefact': a, 'item': i}
            for p, a, i in partitions
        ], has_more, total)

    # Protection indicator search
    if 'protection' in tokens:
        prot_results, has_more, total = _search_protection(tokens, page=page, per_page=per_page)
        _add('artefacts', prot_results, has_more, total)

    # Mastering indicator search
    if 'mastering' in tokens:
        mast_results, has_more, total = _search_mastering(tokens, page=page, per_page=per_page)
        _add('artefacts', mast_results, has_more, total)

    # RISC OS module search
    if 'module' in tokens:
        mod_results, has_more, total = _search_modules(tokens, page=page, per_page=per_page, dedupe=dedupe)
        _add('files', mod_results, has_more, total)

    # Star command search
    if 'command' in tokens:
        cmd_results, has_more, total = _search_commands(tokens, page=page, per_page=per_page, dedupe=dedupe)
        _add('files', cmd_results, has_more, total)

    # SWI name search
    if 'swi' in tokens:
        swi_results, has_more, total = _search_swis(tokens, page=page, per_page=per_page, dedupe=dedupe)
        _add('files', swi_results, has_more, total)

    # Acorn Replay / ARMovie metadata search
    if any(k.startswith('replay_') for k in tokens):
        replay_results, has_more, total = _search_replay_movies(tokens, page=page, per_page=per_page, dedupe=dedupe)
        _add('files', replay_results, has_more, total)

    # Tag search
    if 'tag' in tokens:
        tag_results, has_more, total = _search_tags(tokens, page=page, per_page=per_page)
        _add('artefacts', tag_results, has_more, total)

    # Artefact hash search
    if any(k in tokens for k in ('md5', 'sha256')):
        hash_results, has_more, total = _search_artefact_hashes(tokens, page=page, per_page=per_page)
        _add('artefacts', hash_results, has_more, total)

    # Free-text search
    if has_text:
        items, has_more, total = _search_text_items(tokens, page=page, per_page=per_page)
        _add('catalogue_items', items, has_more, total)

        art_text_results, has_more, total = _search_text_artefacts(tokens, page=page, per_page=per_page)
        _add('artefacts', art_text_results, has_more, total)

    # Deduplicate file results
    seen_file_ids = set()
    deduped_files = []
    for row in results['files']:
        ef = row[0]
        if ef.id not in seen_file_ids:
            seen_file_ids.add(ef.id)
            deduped_files.append(row)
    results['files'] = deduped_files

    # Largest bucket drives the page count (see docstring).
    results['total'] = max(bucket_totals.values())

    return results


# vim: ts=4 sw=4 et
