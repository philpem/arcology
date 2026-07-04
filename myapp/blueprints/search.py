"""
Arcology - Search Blueprint

Global cross-item search using a prefix query syntax.  This module is the thin
routes + presentation layer; the query parser and all sub-search logic live in
:mod:`myapp.services.search` (``parse_query`` / ``_run_search`` /
``run_duplicate_search`` and friends).
"""

from flask import Blueprint, abort, render_template, request
from sqlalchemy import distinct
from ..database import ArtefactMastering, ArtefactProtection, FilesystemType
from ..extensions import db
from ..permissions import public_readable
from ..services.file_metadata import metadata_by_file_id
from ..services.search import (
    KNOWN_KEYS,
    NOT_KEY,
    PER_PAGE,
    _check_query_warnings,
    _run_search,
    parse_query,
    result_hints,
    run_duplicate_search,
)
from ..utils.pagination import VALID_PER_PAGE, ListPagination, resolve_per_page

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/search', template_folder='templates')


def init_app(app):
    app.add_menu_item("Search", f"{ROUTENAME}.index", 50)


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
    # Hints that depend on the result counts (e.g. an exact filename: that
    # matched nothing) are appended once the search has run.
    query_warnings = query_warnings + result_hints(tokens, results)
    # Real result count drives the pagination.  Each bucket paginates
    # independently but shares one page number, so the number of pages needed to
    # view everything is the largest bucket's page count; _run_search reports
    # that bucket's total under 'total' (see its docstring).
    total = results['total'] if results else 0

    # Build a Pagination-compatible object so search shares the common macro.
    # range() keeps this O(1); the shim only needs the count, not the rows.
    pagination = ListPagination(range(total), page, per_page)
    pagination_args = {k: v for k, v in request.args.items() if k != 'page'}

    # Module / Replay / media viewer icons for the file results (parallel to
    # the artefact file listing).  Keyed by ExtractedFile.id.
    module_info, replay_info, media_info = (
        metadata_by_file_id(results['files']) if results else ({}, {}, {})
    )

    # Per-representative duplicate counts (only present when dedupe collapsed
    # the file bucket).  Keyed by ExtractedFile.id like module_info/replay_info.
    dupe_info = {}
    if results:
        for row in results['files']:
            ef = row[0]
            count = getattr(ef, 'dupe_count', None)
            if count is not None and count > 1:
                dupe_info[ef.id] = {'count': count, 'key': getattr(ef, 'dupe_key', None)}

    # Distinct protection/mastering types only populate the quick-reference cards
    # on the empty landing page (results is None); skip the two DISTINCT scans on
    # every actual search.
    known_protection_types = []
    known_mastering_types = []
    if results is None:
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
        media_info=media_info,
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

    rows, total = run_duplicate_search(key, page=page, per_page=per_page)
    if total == 0:
        abort(404)

    pagination = ListPagination(range(total), page, per_page)
    pagination_args = {k: v for k, v in request.args.items() if k != 'page'}
    module_info, replay_info, media_info = metadata_by_file_id(rows)

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
        media_info=media_info,
    )


# vim: ts=4 sw=4 et
