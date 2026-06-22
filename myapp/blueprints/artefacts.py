"""
Arcology - Artefacts Blueprint

CRUD operations for digital artefacts with file upload and auto-analysis.
"""

import hashlib
import json
import mimetypes
import os
from datetime import datetime, timezone
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from werkzeug.exceptions import NotFound
from wtforms import BooleanField, IntegerField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Optional
from ..database import (
    ANALYSIS_PRIORITY_HIGH,
    Analysis,
    AnalysisStatus,
    AnalysisType,
    Artefact,
    ArtefactRestriction,
    ArtefactType,
    ExtractedFile,
    ExtractedFileRestriction,
    HashDatabase,
    Item,
    KnownFile,
    KnownProduct,
    Partition,
    Platform,
    RecognisedProduct,
    ReplayMovie,
    RestrictionType,
    RiscosModule,
    Tag,
    User,
    UserArtefactBypass,
)
from ..extensions import db
from ..permissions import public_downloadable, public_readable, require_permission
from ..riscos_filetypes import lookup_filetype_hex
from ..services import chunked_upload as _chunked
from ..services.artefact_lifecycle import (
    ArtefactMoveError,
    build_processing_tree,
    cleanup_artefact_outputs,
    cleanup_artefact_outputs_s3,
    collect_output_cleanup_keys,
    delete_artefact_files,
    get_all_derived_artefact_ids,
    move_artefact_to_item,
    queue_storage_cleanup,
    reset_artefact_for_reanalysis,
    validate_artefact_move,
)
from ..services.artefact_storage import (
    compute_file_hashes,
    get_artefact_storage_key,
    resolve_extracted_file_path,
    safe_original_filename,
    save_uploaded_file,
)
from ..services.artefact_types import (
    ANALYSIS_MAP,
    detect_artefact_type,
    queue_analyses_for_artefact,
)
from ..services.downloads import (
    resolve_output_artefact,
    serve_artefact_file,
    serve_extracted_file,
    serve_output_file,
)
from ..services.file_metadata import metadata_by_path
from ..services.restrictions import (
    artefact_contained_file_restrictions,
    collect_all_file_restrictions,
    collect_ancestor_file_restrictions,
    grantable_bypass_rtypes,
)
from ..services.similarity import (
    component_match_counts,
    matches_for_component,
    similar_artefacts,
    similar_components,
    similar_files_by_tlsh,
)
from ..services.upload_pipeline import QUEUE_CHECKSUM_ONLY, QUEUE_FULL, ingest_uploaded_artefact
from ..utils.blobs import artefact_blob_storage_path, assign_blob
from ..utils.config import int_config
from ..utils.enum_display import enum_value
from ..utils.path_nav import build_directory_tree
from ..utils.slugs import ensure_unique_slug, generate_slug, lookup_artefact_by_id, lookup_by_identifier
from ..visibility import (
    artefact_visibility_clause,
    can_change_owner,
    can_contribute_to_item,
    can_curate_item,
    can_download_despite_restrictions,
    can_manage_privacy,
    can_reveal_explicit,
    can_view_artefact,
    can_view_item,
    content_gate_flags,
    output_blocked_for,
)

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='', template_folder='templates')


# Artefact types that have a dedicated viewer page (sprite/draw/text rendering).
VIEWER_ARTEFACT_TYPES = (ArtefactType.ACORN_SPRITE, ArtefactType.ACORN_DRAW, ArtefactType.ACORN_TEXT)


# Human-readable display names for artefact types (used in form dropdowns).
# Falls back to t.value.upper().replace('_', ' ') for any type not listed here.
_ARTEFACT_TYPE_DISPLAY_NAMES = {
    ArtefactType.SCP:          "SuperCard Pro (SCP)",
    ArtefactType.DFI:          "DiscFerret (DFI)",
    ArtefactType.A2R:          "Applesauce A2R",
    ArtefactType.IMD:          "ImageDisk (IMD)",
    ArtefactType.HFE:          "HxC Floppy Emulator (HFE)",
    ArtefactType.RAW_SECTOR:   "Raw Sector Image",
    ArtefactType.ISO:          "ISO 9660 Disc Image",
    ArtefactType.DD_ZST:       "Compressed Raw Sector (zstd)",
    ArtefactType.DD_GZ:        "Compressed Raw Sector (gzip)",
    ArtefactType.DD_BZ2:       "Compressed Raw Sector (bzip2)",
    ArtefactType.PDF:          "PDF Document",
    ArtefactType.ZIP:          "ZIP Archive",
    ArtefactType.TARGZ:        "TAR+GZip Archive",
    ArtefactType.RAR:          "RAR Archive",
    ArtefactType.ARC:          "ArcFS / Spark Archive",
    ArtefactType.TBAFS:        "TBAFS Archive",
    ArtefactType.XFILES:       "X-Files Archive",
    ArtefactType.ACORN_SPRITE: "Acorn Sprite",
    ArtefactType.ACORN_DRAW:   "Acorn Draw",
    ArtefactType.ACORN_TEXT:   "Acorn Text / Script",
    ArtefactType.IMAGE:        "Raster / Vector Image",
    ArtefactType.SIDECAR:      "Sidecar / Companion File",
    ArtefactType.UNKNOWN:      "Unknown",
}


def _type_display_name(t: ArtefactType) -> str:
    """Return a human-readable display name for an ArtefactType."""
    return _ARTEFACT_TYPE_DISPLAY_NAMES.get(t, t.value.upper().replace('_', ' '))



# =============================================================================
# Forms
# =============================================================================

class ArtefactUploadForm(FlaskForm):
    """Form for uploading a new artefact."""
    item_id = SelectField('Item', coerce=int, validators=[DataRequired()])
    file = FileField('File', validators=[FileRequired()])
    label = StringField('Label', validators=[DataRequired()],
                        description='e.g., "Disc 1", "Program Disc", "Manual"')
    platform_id = SelectField('Platform hint', coerce=int, validators=[Optional()],
                               description='Helps analysis tools identify format')
    dfi_clock_mhz = IntegerField('DFI clock frequency (MHz)', validators=[Optional()],
                                  description='Override sample frequency for DFI files recorded at non-standard rates (e.g. 100)')
    artefact_type = SelectField('Type (auto-detected)', coerce=str, validators=[Optional()],
                                 description='Leave as "Auto-detect" unless incorrect')
    description = TextAreaField('Description', validators=[Optional()])
    is_private = BooleanField('Private',
                              description='Visible only to you and administrators.')
    auto_analyse = BooleanField('Run automatic analysis', default=True)
    upload_more = BooleanField('Upload more', default=False)


class ArtefactEditForm(FlaskForm):
    """Form for editing artefact metadata."""
    label = StringField('Label', validators=[DataRequired()])
    artefact_type = SelectField('Type', coerce=str, validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional()])
    tags = StringField('Tags', validators=[Optional()],
                       description='Comma-separated list of tags')
    is_private = BooleanField('Private',
                              description='Visible only to you and administrators.')
    owner_id = SelectField('Owner', coerce=int, validators=[Optional()],
                           description='Reassign this artefact to another user.')


class AnalyseForm(FlaskForm):
    """Form for running analysis with optional hints."""
    platform_id = SelectField('Platform hint', coerce=int, validators=[Optional()],
                               description='Helps analysis tools identify format')
    filesystem_hint = StringField('Filesystem hint', validators=[Optional()],
                                   description='e.g., adfs, fat12, hfs')
    dfi_clock_mhz = IntegerField('DFI clock frequency (MHz)', validators=[Optional()],
                                  description='Override sample frequency for DFI files recorded at non-standard rates (e.g. 100)')
    notes = TextAreaField('Additional notes', validators=[Optional()])


class FileSearchForm(FlaskForm):
    partition_uuid = StringField('Partition UUID', validators=[Optional()])
    filename = StringField('Filename', validators=[Optional()])
    filetype = StringField('Filetype', validators=[Optional()])
    path = StringField('Path/Directory', validators=[Optional()])
    md5 = StringField('MD5 Hash', validators=[Optional()])
    sha1 = StringField('SHA1 Hash', validators=[Optional()])
    hide_known = SelectField('Known files', choices=[
        ('', 'Known: All'),
        ('hide', 'Known: Hide'),
        ('only', 'Known: Only'),
    ], default='', validators=[Optional()])
    filter_products = SelectField('Product matches', choices=[
        ('', 'Products: All'),
        ('hide', 'Products: Hide'),
        ('only', 'Products: Only'),
    ], default='', validators=[Optional()])
    show_directories = BooleanField('Show Dirs', default=False)


# =============================================================================
# Routes
# =============================================================================

def _resolve_artefact(item_id, artefact_id, root_id=None):
    """Lookup helper: resolve item + artefact, validate root_id if nested URL.

    Enforces privacy: a private artefact (or one in a private item) is hidden
    from users who are neither its owner nor an administrator.
    """
    item = lookup_by_identifier(Item, item_id)
    artefact = lookup_artefact_by_id(item, artefact_id)
    if not can_view_artefact(artefact, current_user):
        abort(404)
    if root_id is not None:
        root = lookup_artefact_by_id(item, root_id)
        if artefact.root_artefact.id != root.id:
            abort(404)
    return item, artefact


def _get_artefact_or_404(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Load an artefact from either the nested or legacy route parameters."""
    if uuid is not None:
        artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
        if not can_view_artefact(artefact, current_user):
            abort(404)
        return artefact
    _, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    return artefact


def _require_manage_artefact_content(artefact):
    """Abort if caller may not mutate artefact state.

    Within a private item: editor+ share is required for content changes;
    curator share (or owner/admin) is required to toggle artefact.is_private.
    Within a public item: artefact.is_private can only be toggled by the
    artefact owner or admin.
    """
    if artefact.item.private_effective:
        if not can_contribute_to_item(artefact.item, current_user):
            abort(403)
    elif artefact.is_private and not can_manage_privacy(artefact, current_user):
        abort(403)


def _artefact_view_kwargs(artefact):
    """Return standard kwargs for redirecting to an artefact view."""
    return {
        'item_id': artefact.item.url_id,
        'artefact_id': artefact.url_slug,
    }


def _canonical_redirect(endpoint, item, item_id, artefact, artefact_id, root_id=None):
    """Return 301 to canonical URL if item_id/artefact_id are not in canonical form, else None.

    Preserves the query string.  Only call on GET requests.
    """
    canonical_item_id = item.url_id
    canonical_artefact_id = artefact.url_slug
    if root_id is None:
        if item_id == canonical_item_id and artefact_id == canonical_artefact_id:
            return None
        loc = url_for(endpoint, item_id=canonical_item_id, artefact_id=canonical_artefact_id)
    else:
        canonical_root_id = artefact.root_artefact.url_slug
        if (item_id == canonical_item_id
                and root_id == canonical_root_id
                and artefact_id == canonical_artefact_id):
            return None
        loc = url_for(endpoint,
                      item_id=canonical_item_id,
                      root_id=canonical_root_id,
                      artefact_id=canonical_artefact_id)
    if request.query_string:
        loc += '?' + request.query_string.decode()
    return redirect(loc, 301)


def _redirect_to_artefact_view(artefact):
    """Redirect to the standard artefact view."""
    return redirect(url_for(f'{ROUTENAME}.view', **_artefact_view_kwargs(artefact)))


def _check_download_restrictions(artefact):
    """Return a redirect response when download restrictions block access."""
    # Restrictions on a container artefact cascade to artefacts derived from it.
    restrictions = artefact.effective_restrictions
    if not restrictions:
        return None

    if not can_download_despite_restrictions(current_user, restrictions, artefact):
        categories = ', '.join({r.restriction_type.label for r in restrictions})
        flash(f'Download restricted: {categories}', 'danger')
        return _redirect_to_artefact_view(artefact)

    if not request.args.get('confirm_bypass'):
        flash('This artefact has download restrictions. Use the download override button to confirm.', 'warning')
        return _redirect_to_artefact_view(artefact)

    return None


def _check_file_download_restrictions(ef):
    """Return a redirect when file-level restrictions block an extracted-file download.

    Called after _check_download_restrictions() has cleared artefact-level
    restrictions.  Checks restrictions on ef itself, on any nested descendants
    (so downloading an archive is blocked if any contained file is restricted),
    and on any ancestor archive/directory (so a file inside a restricted archive
    is also blocked).
    """
    all_restrictions = (
        collect_all_file_restrictions(ef) +
        collect_ancestor_file_restrictions(ef)
    )
    if not all_restrictions:
        return None

    if not can_download_despite_restrictions(current_user, all_restrictions, ef.partition.artefact):
        categories = ', '.join({r.restriction_type.label for r in all_restrictions})
        flash(f'File download restricted: {categories}', 'danger')
        return _redirect_to_artefact_view(ef.partition.artefact)

    if not request.args.get('confirm_bypass'):
        flash('This file has download restrictions. Use the download override to confirm.', 'warning')
        return _redirect_to_artefact_view(ef.partition.artefact)

    return None


def _check_artefact_file_restrictions(artefact):
    """Block artefact download when any extracted file within it has restrictions.

    Called after _check_download_restrictions() has cleared artefact-level
    restrictions.  Because ExtractedFileRestriction has .restriction_type, the
    existing can_bypass_all_restrictions() method works on these objects directly.
    """
    file_restrictions = artefact_contained_file_restrictions(artefact)

    if not file_restrictions:
        return None

    if not can_download_despite_restrictions(current_user, file_restrictions, artefact):
        categories = ', '.join({r.restriction_type.label for r in file_restrictions})
        flash(f'Download restricted (artefact contains restricted files): {categories}', 'danger')
        return _redirect_to_artefact_view(artefact)

    if not request.args.get('confirm_bypass'):
        flash('This artefact contains files with download restrictions. Use the download override to confirm.', 'warning')
        return _redirect_to_artefact_view(artefact)

    return None


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>')
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>', endpoint='view_nested')
@public_readable
def view(item_id, artefact_id, root_id=None):
    """View an artefact and its partitions/files."""
    item, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    endpoint = f'{ROUTENAME}.view_nested' if root_id is not None else f'{ROUTENAME}.view'
    redir = _canonical_redirect(endpoint, item, item_id, artefact, artefact_id, root_id)
    if redir:
        return redir
    return _render_artefact_view(artefact)


@blueprint.route('/artefacts/<string:uuid>')
@public_readable
def view_legacy(uuid):
    """Legacy flat-URL compat shim — resolves and renders without redirect."""
    artefact = _get_artefact_or_404(uuid=uuid)
    return _render_artefact_view(artefact)


@blueprint.route('/artefacts/<string:uuid>/analysis-status.json')
@public_readable
def analysis_status_json(uuid):
    """Lightweight JSON endpoint returning analysis status counts for an artefact tree.

    Used by the artefact view page to poll for completion without a full reload.
    Returns: {"pending": N, "running": N, "completed": N, "failed": N, "total": N}
    """
    from flask import jsonify
    artefact = _get_artefact_or_404(uuid=uuid)
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    counts = {s.value: 0 for s in AnalysisStatus}
    rows = (
        db.session.query(Analysis.status, db.func.count(Analysis.id))
        .filter(Analysis.artefact_id.in_(all_ids))
        .group_by(Analysis.status)
        .all()
    )
    for status, n in rows:
        counts[status.value] = n
    counts['total'] = sum(counts.values())
    return jsonify(counts)


@blueprint.route('/artefacts/<string:uuid>/dirtree.html')
@public_readable
def dirtree_html(uuid):
    """AJAX endpoint: returns an HTML fragment containing the full directory tree.

    Called by the "Tree" toggle button in the file listing panel.  Accepts an
    optional ``?partition_uuid=`` query param to restrict the tree to one
    partition (used when the user has already selected a partition filter).
    """
    artefact = _get_artefact_or_404(uuid=uuid)
    all_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)

    all_partitions = (
        Partition.query
        .filter(Partition.artefact_id.in_(all_ids))
        .order_by(Partition.artefact_id, Partition.partition_index)
        .all()
    )
    if not all_partitions:
        return ('', 204)

    # Optional single-partition filter (mirrors the main file listing filter)
    partition_uuid_filter = request.args.get('partition_uuid') or None
    if partition_uuid_filter == 'None':
        partition_uuid_filter = None

    visible_partitions = (
        [p for p in all_partitions if p.uuid == partition_uuid_filter]
        if partition_uuid_filter else all_partitions
    )

    def _base_file_q():
        q = (
            db.session.query(ExtractedFile.path, Partition.uuid)
            .join(Partition)
            .filter(Partition.artefact_id.in_(all_ids))
        )
        if partition_uuid_filter:
            q = q.filter(Partition.uuid == partition_uuid_filter)
        return q

    # Safety cap: directory trees with more paths than this are truncated.
    # Large disk images can have hundreds of thousands of files; loading every
    # path string unbounded would exhaust worker memory under concurrent load.
    _TREE_PATH_LIMIT = 200_000

    # Fetch LIMIT+1 rows so we can tell whether the cap was actually hit
    # (if we get exactly LIMIT+1 back, we truncated; exactly LIMIT means the
    # DB is at or below the cap).  Mirrors the hashdb.py SEARCH_LIMIT pattern.
    path_rows = _base_file_q().filter(ExtractedFile.is_directory == False).limit(_TREE_PATH_LIMIT + 1).all()  # noqa: E712
    dir_rows  = _base_file_q().filter(ExtractedFile.is_directory == True).limit(_TREE_PATH_LIMIT + 1).all()  # noqa: E712

    is_truncated = len(path_rows) > _TREE_PATH_LIMIT or len(dir_rows) > _TREE_PATH_LIMIT
    if len(path_rows) > _TREE_PATH_LIMIT:
        path_rows = path_rows[:_TREE_PATH_LIMIT]
    if len(dir_rows) > _TREE_PATH_LIMIT:
        dir_rows = dir_rows[:_TREE_PATH_LIMIT]

    # Archive paths (for folder-vs-zip icons in the tree).
    # Apply the same partition filter so cross-partition paths don't bleed in.
    arc_base = (
        db.session.query(ExtractedFile.path)
        .join(Partition)
        .filter(
            Partition.artefact_id.in_(all_ids),
            ExtractedFile.is_archive == True,  # noqa: E712
        )
    )
    if partition_uuid_filter:
        arc_base = arc_base.filter(Partition.uuid == partition_uuid_filter)
    archive_paths = {row[0] for row in arc_base.limit(_TREE_PATH_LIMIT).all()}

    # is_directory rows store the directory path itself (e.g. "dir1").
    # Append '/' so _extract_dir_set in build_directory_tree treats them as
    # directories rather than root-level files (which produce no implied dirs).
    synthetic_dir_rows = [(p + '/', p_uuid) for p, p_uuid in dir_rows]

    tree_data = build_directory_tree(
        list(path_rows) + synthetic_dir_rows,
        visible_partitions,
        archive_paths=archive_paths,
    )

    return render_template(
        'artefacts/_dir_tree_panel.html',
        tree=tree_data,
        all_partitions=all_partitions,
        single_partition=len(visible_partitions) == 1,
        is_truncated=is_truncated,
    )


@blueprint.route('/artefacts/<string:uuid>/tree')
@public_readable
def tree(uuid):
    """Processing tree view — shows the full artefact derivation tree with analysis status."""
    artefact = _get_artefact_or_404(uuid=uuid)
    root = artefact.root_artefact
    if root is not artefact:
        return redirect(url_for(f'{ROUTENAME}.tree', uuid=root.uuid))
    tree_data, has_active, status_counts, total_count = build_processing_tree(root)
    return render_template(
        'artefacts/tree.html',
        artefact=root,
        tree=tree_data,
        has_active_analyses=has_active,
        status_counts=status_counts,
        total_count=total_count,
    )


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/viewer')
@public_readable
def viewer(item_id, artefact_id):
    """Viewer page for converted outputs (images, text, etc.)."""
    item, artefact = _resolve_artefact(item_id, artefact_id)
    redir = _canonical_redirect(f'{ROUTENAME}.viewer', item, item_id, artefact, artefact_id)
    if redir:
        return redir
    return _render_viewer(artefact)


@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/viewer')
@public_readable
def viewer_nested(item_id, root_id, artefact_id):
    """Viewer page for converted outputs (nested artefact)."""
    item, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    redir = _canonical_redirect(f'{ROUTENAME}.viewer_nested', item, item_id, artefact, artefact_id, root_id)
    if redir:
        return redir
    return _render_viewer(artefact)


_VALID_VIEWER_COLUMNS = [2, 3, 4, 6, 8]
# Responsive grid classes: phones get 2 cols, tablets get an intermediate
# count, desktops get the user's selection. Values must match the ones in
# viewer.html's custom CSS (.col-custom-N for non-12-divisible counts).
_COLUMN_CLASSES = {
    2: 'col-6',
    3: 'col-6 col-sm-4',
    4: 'col-6 col-sm-4 col-md-3',
    6: 'col-6 col-sm-4 col-md-3 col-lg-2',
    8: 'col-6 col-sm-4 col-md-3 col-lg-2 col-custom-8-xl',
}



def _viewer_make_output_helpers():
    """Shared per-request closures: output restriction gate and text-content enrichment."""
    # Download restrictions gate the original bytes; an analysis output renders
    # the same content, so withhold any output whose source artefact carries a
    # restriction the current user cannot bypass.  Outputs may come from the
    # viewed artefact or any derived artefact (Mode 2), so resolve per output by
    # the artefact UUID embedded in its path (see resolve_output_artefact).
    _restriction_cache: dict[str, bool] = {}

    def _output_blocked(filename) -> bool:
        # Cache by the artefact directory component ({uuid}_{slug}) so many
        # outputs from one source artefact resolve with a single query.
        parts = (filename or '').split('/', 2)
        cache_key = parts[1] if len(parts) >= 2 else filename
        if cache_key not in _restriction_cache:
            src = resolve_output_artefact(filename)
            _restriction_cache[cache_key] = bool(src) and output_blocked_for(current_user, src)
        return _restriction_cache[cache_key]

    def _enrich_outputs(outputs):
        """For text outputs, read file content for inline rendering.

        Skips content whose source artefact has a non-bypassable download
        restriction — the inline text is the restricted content itself.  This is
        defence-in-depth: the per-group ``group['restricted']`` gate (set below)
        is the primary control and stops the template rendering the text at all.
        """
        storage = current_app.storage
        for out in outputs:
            if out.get('type') == 'text':
                if _output_blocked(out.get('filename', '')):
                    out['text_content'] = None
                    continue
                try:
                    key = storage.storage_key('outputs', out['filename'])
                    with storage.open_read(key) as f:
                        out['text_content'] = f.read().decode('utf-8', errors='replace')
                except Exception:
                    out['text_content'] = None
        return outputs
    return _output_blocked, _enrich_outputs


def _viewer_collect_groups(artefact, all_artefact_ids, file_filter, current_path, _enrich_outputs):
    """Output groups for the viewer grid (Mode 1 own outputs / Mode 2 aggregate)."""
    from collections import defaultdict

    _viewable_types = VIEWER_ARTEFACT_TYPES
    output_groups = []
    viewer_status = None  # 'pending', 'failed', 'partial', or None (ready)
    failed_conversion_list = []  # [{source_file, error, analysis_uuid}, ...]
    use_pagination = False  # only paginate Mode 2 aggregate view without ?file=

    # If the viewed artefact itself carries a restriction this user cannot
    # bypass, don't render any outputs (they are renderings of the restricted
    # content) — show a restricted notice instead of broken images.
    viewer_outputs_blocked = output_blocked_for(current_user, artefact)

    if viewer_outputs_blocked:
        viewer_status = 'restricted'
    elif artefact.artefact_type in _viewable_types:
        # Mode 1: Artefact is itself a viewable type — show its own FORMAT_CONVERT output
        conv = Analysis.query.filter_by(
            artefact_id=artefact.id,
            analysis_type=AnalysisType.FORMAT_CONVERT,
        ).order_by(Analysis.id.desc()).first()
        if conv and conv.status == AnalysisStatus.COMPLETED and conv.success:
            try:
                details = json.loads(conv.details or '{}')
            except (json.JSONDecodeError, TypeError):
                details = {}
            outputs = _enrich_outputs(details.get('outputs', []))
            if outputs:
                output_groups.append({
                    'label': artefact.original_filename or artefact.label,
                    'source_file': None,
                    'outputs': outputs,
                })
        elif conv and conv.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING):
            viewer_status = 'pending'
        else:
            viewer_status = 'failed'
            if conv and conv.status == AnalysisStatus.FAILED:
                failed_conversion_list = [{
                    'source_file': artefact.original_filename or artefact.label,
                    'error': conv.error_message or 'Conversion failed',
                    'analysis_uuid': conv.uuid,
                }]
    else:
        # Mode 2: Aggregate outputs from all FORMAT_CONVERT analyses on this artefact
        # and all derived artefacts (e.g. an ISO extracted from a ZIP).
        convs = (
            Analysis.query
            .filter(
                Analysis.artefact_id.in_(all_artefact_ids),
                Analysis.analysis_type == AnalysisType.FORMAT_CONVERT,
            )
            .order_by(Analysis.id)
            .all()
        )

        pending_count = sum(
            1 for c in convs
            if c.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)
        )

        # Collect outputs grouped by source_file (filtered by ?file= if set)
        # and gather any per-file conversion failures recorded in details JSON.
        groups: dict[str, list] = defaultdict(list)
        for conv in convs:
            if conv.status == AnalysisStatus.FAILED:
                if not file_filter:
                    failed_conversion_list.append({
                        'source_file': None,
                        'error': conv.error_message or 'Conversion failed',
                        'analysis_uuid': conv.uuid,
                    })
                continue
            if not (conv.status == AnalysisStatus.COMPLETED and conv.success):
                continue
            try:
                details = json.loads(conv.details or '{}')
            except (json.JSONDecodeError, TypeError):
                continue
            for fc in details.get('failed_conversions', []):
                if file_filter and fc.get('source_file') != file_filter:
                    continue
                failed_conversion_list.append({**fc, 'analysis_uuid': conv.uuid})
            for out in details.get('outputs', []):
                source = out.get('source_file', '')
                if file_filter and source != file_filter:
                    continue
                groups[source].append(out)

        for source_file, outputs in groups.items():
            # NOTE: do NOT call _enrich_outputs here — deferred to current page only
            label = source_file if source_file else (artefact.original_filename or artefact.label)
            output_groups.append({'label': label, 'source_file': source_file, 'outputs': outputs})

        # Sort groups alphabetically by label for stable ordering across pages
        output_groups.sort(key=lambda g: g['label'].lower())

        # Apply subdirectory filter (Mode 2 only).  Matches File Viewer ?path=
        # semantics: include everything under the prefix (current level + deeper).
        if current_path:
            output_groups = [
                g for g in output_groups
                if (g.get('source_file') or '').startswith(current_path)
            ]

        if not output_groups:
            viewer_status = 'pending' if pending_count > 0 else 'failed'
        elif pending_count > 0:
            viewer_status = 'partial'

        # Enable pagination for aggregate view without ?file= filter
        if not file_filter:
            use_pagination = True

    return output_groups, viewer_status, failed_conversion_list, use_pagination


def _viewer_subdirectories(artefact, output_groups, file_filter, current_path, all_partition_ids):
    """Subdirectory navigation chips above the viewer grid (Mode 2)."""
    _viewable_types = VIEWER_ARTEFACT_TYPES
    # ── Subdirectory navigation (Mode 2) ─────────────────────────────────────
    # Compute before the filetype filter so subdirectories stay visible even
    # when the current filetype filter empties the grid.
    subdirectories: list = []
    archive_paths: set = set()
    if not file_filter and artefact.artefact_type not in _viewable_types:
        from ..utils.path_nav import compute_subdirectories
        source_files_in_scope = [
            g.get('source_file') for g in output_groups if g.get('source_file')
        ]
        subdirectories = compute_subdirectories(source_files_in_scope, current_path)
        if subdirectories and all_partition_ids:
            archive_paths = {
                row.path for row in (
                    ExtractedFile.query
                    .filter(
                        ExtractedFile.partition_id.in_(all_partition_ids),
                        ExtractedFile.is_archive == True,
                    )
                    .with_entities(ExtractedFile.path)
                    .all()
                )
            }
    return subdirectories, archive_paths


def _viewer_filename_filter(output_groups, filename_filter, file_filter):
    """?filename= glob filter applied to the viewer grid (Mode 2)."""
    # ── Filename glob filter (Mode 2 only, applied before facet build) ──────
    if filename_filter and not file_filter:
        import fnmatch as _fnmatch
        from posixpath import basename as _basename
        pat = filename_filter.lower()
        if '*' not in pat and '?' not in pat:
            pat = f'*{pat}*'
        output_groups = [
            g for g in output_groups
            if not g.get('source_file') or
               _fnmatch.fnmatch(_basename(g['source_file']).lower(), pat)
        ]
    return output_groups


def _viewer_filetype_facet(output_groups, all_partition_ids):
    """Filetype facet panel: counts, active filters, and toggle URLs."""
    from collections import Counter

    # ── Filetype facet (Mode 2 only, when there are source_file paths) ───────
    # Type keys: RISC OS hex codes (e.g. 'fff') or '.ext' for extension-based
    # types (e.g. '.wmf'). The dot prefix distinguishes them in URLs/filters.
    filetype_facet = []  # [(type_key, count), ...] sorted by count desc
    active_filetypes = set()
    filetype_toggle_urls = {}
    clear_filter_args = {}

    source_paths = [g['source_file'] for g in output_groups if g.get('source_file')]
    if source_paths:
        if all_partition_ids:
            filetype_rows = (
                ExtractedFile.query
                .filter(
                    ExtractedFile.partition_id.in_(all_partition_ids),
                    ExtractedFile.path.in_(source_paths),
                )
                .with_entities(ExtractedFile.path, ExtractedFile.risc_os_filetype,
                                ExtractedFile.extension)
                .all()
            )
            # Prefer risc_os_filetype; fall back to '.ext' for non-RISC OS files.
            path_to_filetype = {}
            for r in filetype_rows:
                if r.risc_os_filetype:
                    path_to_filetype[r.path] = r.risc_os_filetype
                elif r.extension:
                    path_to_filetype[r.path] = f'.{r.extension}'

            # Tag each group with its effective type key
            for group in output_groups:
                group['filetype'] = path_to_filetype.get(group.get('source_file'))

            # Build facet as a list of (type_key, count) sorted by count desc,
            # then key asc as tiebreaker. Template iterates this list directly.
            counts = Counter(
                g['filetype'] for g in output_groups if g.get('filetype')
            )
            filetype_facet = sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )

            # Apply filetype filter from ?filetype=ff9,fff,.wmf
            filetype_param = request.args.get('filetype', '')
            active_filetypes = set(filetype_param.split(',')) - {''}
            if active_filetypes:
                output_groups = [
                    g for g in output_groups
                    if g.get('filetype') in active_filetypes
                ]

            # Build toggle URLs for the template
            base_args = {k: v for k, v in request.args.items()
                         if k not in ('filetype', 'page')}
            for ft, _ in filetype_facet:
                toggled = active_filetypes ^ {ft}
                args = dict(base_args)
                if toggled:
                    args['filetype'] = ','.join(sorted(toggled))
                filetype_toggle_urls[ft] = args
            clear_filter_args = dict(base_args)
    return output_groups, dict(
        filetype_facet=filetype_facet,
        active_filetypes=active_filetypes,
        filetype_toggle_urls=filetype_toggle_urls,
        clear_filter_args=clear_filter_args,
    )


def _viewer_summary_counts(output_groups):
    """Per-type output counts for the viewer summary line."""
    from collections import Counter

    # ── Summary counts (post-filter, pre-pagination) ─────────────────────────
    total_counts = Counter()
    for g in output_groups:
        for out in g['outputs']:
            total_counts[out.get('type', 'unknown')] += 1
    total_groups = len(output_groups)
    return total_counts, total_groups


def _viewer_paginate(output_groups, use_pagination, _enrich_outputs):
    """Pagination panel for the Mode 2 aggregate grid (and arg preservation)."""
    from ..utils.pagination import VALID_PER_PAGE, ListPagination, resolve_per_page

    # ── Pagination (Mode 2 aggregate only, without ?file=) ───────────────────
    # pagination_args is always populated so column/filter URLs preserve state
    # in non-paginated views (Mode 1 or ?file=) too.
    pagination_args = {k: v for k, v in request.args.items() if k != 'page'}
    # Args for the "navigate up to the containing directory" breadcrumb shown
    # when a specific file is opened: same as pagination_args but without the
    # ?file= filter, so following the breadcrumb clears the single-file view.
    # (Computed here rather than in the template — Jinja2 has no dict
    # comprehension, so {k: v for ...} is a syntax error there.)
    file_dir_args = {k: v for k, v in pagination_args.items() if k != 'file'}
    pagination = None
    view_all = False

    if use_pagination and output_groups:
        per_page, page, view_all = resolve_per_page('VIEWER_PER_PAGE', 25)
        pagination = ListPagination(output_groups, page, per_page)
        # Only enrich text content for the current page
        for group in pagination.items:
            _enrich_outputs(group['outputs'])
        output_groups = pagination.items
    else:
        # Mode 1 or ?file= — enrich all (typically small set)
        for group in output_groups:
            _enrich_outputs(group['outputs'])

    return output_groups, dict(
        pagination=pagination,
        pagination_args=pagination_args,
        file_dir_args=file_dir_args,
        view_all=view_all,
        valid_per_page=VALID_PER_PAGE,
    )


def _viewer_preferences():
    """Configurable column-count preference for the viewer grid."""
    # ── Configurable columns ─────────────────────────────────────────────────
    # Preference writes are batched: set_preference marks the session dirty
    # and prefs_dirty triggers ONE commit at the end of the function, instead
    # of a commit per preference inside this GET render path.
    prefs_dirty = False
    columns_param = request.args.get('columns', None, type=int)
    if columns_param in _VALID_VIEWER_COLUMNS:
        viewer_columns = columns_param
        if (current_user.is_authenticated
                and current_user.get_preference('viewer_columns') != columns_param):
            current_user.set_preference('viewer_columns', columns_param)
            prefs_dirty = True
    else:
        saved = None
        if current_user.is_authenticated:
            saved = current_user.get_preference('viewer_columns')
        viewer_columns = saved if saved in _VALID_VIEWER_COLUMNS else 4

    viewer_col_class = _COLUMN_CLASSES[viewer_columns]
    return viewer_columns, viewer_col_class, prefs_dirty


def _viewer_explicit_gate(artefact, output_groups, all_partition_ids, _output_blocked):
    """Explicit/restricted gates and stable IDs stamped onto each group."""
    # ── Explicit-content gate (must run before thumbnail bundling) ───────────
    # group['explicit'] must be set before the thumbnail bundling pass so that
    # explicit groups are not pulled into the unified thumbnail grid.
    explicit_type = RestrictionType.EXPLICIT
    user_can_bypass_explicit = can_reveal_explicit(current_user)

    artefact_is_explicit = any(
        r.restriction_type == explicit_type for r in artefact.restrictions
    )

    explicit_file_paths: set[str] = set()
    if not artefact_is_explicit:
        if all_partition_ids:
            explicit_efs = (
                ExtractedFile.query
                .join(ExtractedFileRestriction,
                      ExtractedFileRestriction.extracted_file_id == ExtractedFile.id)
                .filter(
                    ExtractedFileRestriction.restriction_type == explicit_type,
                    ExtractedFile.partition_id.in_(all_partition_ids),
                )
                .with_entities(ExtractedFile.path)
                .all()
            )
            explicit_file_paths = {row.path for row in explicit_efs}

    for group in output_groups:
        # Replay groups carry their own explicit/restricted/stable_id from
        # content_gate_flags (gated per owning artefact, incl. derived ones);
        # don't overwrite them with the output-path based computation here.
        if group.get('is_replay'):
            continue
        group['explicit'] = (
            artefact_is_explicit or group.get('source_file') in explicit_file_paths
        )
        # Generalise the per-group placeholder to all download restrictions: when
        # a group's source artefact carries a restriction this user cannot bypass
        # (only possible in Mode 2 for a derived artefact — the viewed artefact's
        # own restriction short-circuits to viewer_status='restricted' above), its
        # image/SVG outputs would 403.  Mark the group so the template renders a
        # notice / locked placeholder instead of a broken thumbnail.
        group['restricted'] = any(
            _output_blocked(o.get('filename', '')) for o in group['outputs']
        )
        # Stamp stable_id now so bundle_items (original group dicts pulled into
        # the thumbnail bundle below) carry it for per-thumbnail explicit gates.
        # Bundle wrapper groups receive their own stable_id when constructed.
        key_source = group.get('source_file') or group.get('label') or ''
        group['stable_id'] = hashlib.md5(
            f"{artefact.uuid}:{key_source}".encode()
        ).hexdigest()[:12]
    return user_can_bypass_explicit


def _viewer_thumbnail_bundle(artefact, output_groups, file_filter):
    """Thumbnail-mode bundling of single-image groups (Mode 2 aggregate)."""
    _viewable_types = VIEWER_ARTEFACT_TYPES
    prefs_dirty = False
    # ── Thumbnail mode (Mode 2 aggregate only) ────────────────────────────────
    # Collects all single-image groups from the current page (including
    # explicit ones, which the template wraps in per-thumbnail gates) into a
    # unified grid; sprite/multi-image groups remain as separate labelled
    # sections below.
    is_aggregate_mode = artefact.artefact_type not in _viewable_types

    thumb_param = request.args.get('thumb')
    if thumb_param in ('0', '1'):
        viewer_thumbnail_mode = thumb_param == '1'
        if current_user.is_authenticated:
            saved_thumb = current_user.get_preference('viewer_thumb')
            if saved_thumb != viewer_thumbnail_mode:
                current_user.set_preference('viewer_thumb', viewer_thumbnail_mode)
                prefs_dirty = True
    else:
        viewer_thumbnail_mode = False
        if current_user.is_authenticated:
            saved_thumb = current_user.get_preference('viewer_thumb')
            if saved_thumb is not None:
                viewer_thumbnail_mode = bool(saved_thumb)

    if viewer_thumbnail_mode and is_aggregate_mode and not file_filter:
        from posixpath import dirname as _posix_dirname
        single_img_groups, other_groups = [], []
        for g in output_groups:
            img_count = sum(1 for o in g['outputs'] if o.get('type') == 'image')
            if img_count == 1 or g.get('is_replay'):
                # Single-image groups and Replay movies are single thumbnails, so
                # they join the per-directory bundle grid.  Explicit ones stay in
                # the bundle; the template wraps each with its own gate so the
                # directory grid stays unified.
                single_img_groups.append(g)
            else:
                other_groups.append(g)
        if single_img_groups:
            # Build per-directory bundles so singles stay grouped with their
            # directory's multi-image files rather than all floating to the top.
            dir_singles: dict[str, list] = {}
            for g in single_img_groups:
                d = _posix_dirname(g.get('source_file') or '')
                if d not in dir_singles:
                    dir_singles[d] = []
                dir_singles[d].append(g)

            # Derive directory order from the current page's sorted group list
            dir_order: list[str] = []
            seen_dirs: set = set()
            for g in output_groups:
                d = _posix_dirname(g.get('source_file') or '')
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    dir_order.append(d)

            new_groups: list = []
            for d in dir_order:
                singles = dir_singles.get(d, [])
                if singles:
                    bundle_id = hashlib.md5(
                        f"{artefact.uuid}:thumb:{d}".encode()
                    ).hexdigest()[:12]
                    new_groups.append({
                        'label': d or '',
                        'source_file': None,
                        'is_thumbnail_bundle': True,
                        'bundle_items': singles,
                        'outputs': [],
                        'explicit': False,
                        'restricted': False,
                        'stable_id': bundle_id,
                        'filetype': None,
                    })
                for g in other_groups:
                    if _posix_dirname(g.get('source_file') or '') == d:
                        new_groups.append(g)
            output_groups = new_groups
    return output_groups, viewer_thumbnail_mode, is_aggregate_mode, prefs_dirty


def _viewer_module_detail(file_filter, all_artefact_ids):
    """RISC OS module detail card when ?file= matches a module path."""
    # ── Look up RISC OS module detail when ?file= matches a module path ──────
    module_detail = None
    if file_filter:
        mod_row = RiscosModule.query.filter(
            RiscosModule.artefact_id.in_(all_artefact_ids),
            RiscosModule.file_path == file_filter,
        ).first()
        if mod_row:
            module_detail = {
                'title_string': mod_row.title_string,
                'help_title': mod_row.help_title,
                'version': mod_row.version,
                'date': mod_row.date,
                'swi_chunk': mod_row.swi_chunk,
                'module_hash': mod_row.module_hash,
                'file_path': mod_row.file_path,
                'swi_names': None,
                'module_flags': None,
                'commands': [],
            }
            mod_analysis = Analysis.query.filter(
                Analysis.artefact_id == mod_row.artefact_id,
                Analysis.analysis_type == AnalysisType.RISCOS_MODULE_PARSE,
                Analysis.status == AnalysisStatus.COMPLETED,
            ).order_by(Analysis.id.desc()).first()
            if mod_analysis:
                try:
                    details = json.loads(mod_analysis.details or '{}')
                except (json.JSONDecodeError, TypeError):
                    details = {}
                for m in details.get('modules', []):
                    if m.get('file_path') == file_filter:
                        module_detail['swi_names'] = m.get('swi_names')
                        module_detail['module_flags'] = m.get('module_flags')
                        module_detail['commands'] = m.get('commands', [])
                        break
    return module_detail


def _viewer_replay_detail(file_filter, all_artefact_ids):
    """Acorn Replay / ARMovie detail card when ?file= matches an ARMovie path.

    The ReplayMovie row anchors the lookup (and provides the searchable subset);
    the full 21-line header + derived stats are read from the matching movie
    entry in the latest completed REPLAY_PROCESS analysis details, falling back
    to the row's columns when the analysis details are unavailable.
    """
    if not file_filter:
        return None
    row = ReplayMovie.query.filter(
        ReplayMovie.artefact_id.in_(all_artefact_ids),
        ReplayMovie.file_path == file_filter,
    ).first()
    if not row:
        return None

    # Start from the indexed columns as a fallback.
    detail = {
        'file_path': row.file_path,
        'title': row.title,
        'author': row.author,
        'copyright': row.copyright,
        'video_format': row.video_format,
        'video_label': row.video_label,
        'width': row.width,
        'height': row.height,
        'pixel_depth': row.pixel_depth,
        'frame_rate': row.frame_rate,
        'sound_format': row.sound_format,
        'sound_rate': row.sound_rate,
        'sound_channels': row.sound_channels,
        'sound_precision': row.sound_precision,
        'frames_per_chunk': row.frames_per_chunk,
        'number_of_chunks': row.number_of_chunks,
        'duration_seconds': row.duration_seconds,
    }

    # Merge the full header/stats from the analysis details for this file.
    rp_analysis = Analysis.query.filter(
        Analysis.artefact_id == row.artefact_id,
        Analysis.analysis_type == AnalysisType.REPLAY_PROCESS,
        Analysis.status == AnalysisStatus.COMPLETED,
    ).order_by(Analysis.id.desc()).first()
    if rp_analysis:
        try:
            details = json.loads(rp_analysis.details or '{}')
        except (json.JSONDecodeError, TypeError):
            details = {}
        for m in details.get('movies', []):
            if m.get('file_path') == file_filter:
                detail = dict(m)
                break

    # Transcoded video + poster (populated by a REPLAY_TRANSCODE analysis).
    detail['mp4_url'] = (
        url_for(f'{ROUTENAME}.get_output_file', filename=row.mp4_output_path)
        if row.mp4_output_path else None
    )
    detail['poster_url'] = (
        url_for(f'{ROUTENAME}.get_output_file', filename=row.poster_path)
        if row.poster_path else None
    )

    # Link to download the original ARMovie file (the extracted file the
    # ReplayMovie row was indexed from), gated by the same download route as the
    # file listing.  Matched by path within the viewer's artefact set.
    ef_uuid = (
        db.session.query(ExtractedFile.uuid)
        .join(Partition)
        .filter(
            Partition.artefact_id.in_(all_artefact_ids),
            ExtractedFile.path == file_filter,
            ExtractedFile.is_directory == False,  # noqa: E712
        )
        .limit(1)
        .scalar()
    )
    detail['original_url'] = (
        url_for(f'{ROUTENAME}.download_file', uuid=ef_uuid) if ef_uuid else None
    )

    # Content-gate flags (NSFW/explicit blur + hard-restriction lock), keyed off
    # the movie's owning artefact so a derived/explicit movie is gated even when
    # the viewed root artefact is not.
    owning = db.session.get(Artefact, row.artefact_id)
    restricted, explicit = content_gate_flags(current_user, owning)
    detail['restricted'] = restricted
    detail['explicit'] = explicit
    detail['stable_id'] = hashlib.md5(
        f"replay:{row.artefact_id}:{row.file_path}".encode()
    ).hexdigest()[:12]
    return detail


def _viewer_replay_groups(all_artefact_ids, current_path):
    """Replay (ARMovie) movies as viewer groups, interleaved with converted outputs.

    Each movie becomes a single-thumbnail group keyed by its extracted-file path
    (``source_file``) so it sorts, filters, paginates and thumbnail-bundles
    alongside the other files instead of sitting in a separate trailing section.
    Each links to the Replay detail/player card (viewer?file=<path>), the same
    way converted sprites link to their full image.  Poster images come from a
    completed REPLAY_TRANSCODE analysis; a movie not yet transcoded has no poster
    (the template renders a play/audio placeholder).
    """
    rows = (
        ReplayMovie.query
        .filter(ReplayMovie.artefact_id.in_(all_artefact_ids))
        .order_by(ReplayMovie.file_path)
        .all()
    )
    # Resolve owning artefacts once for the content-gate checks (small N).
    art_ids = {row.artefact_id for row in rows}
    artefacts = (
        {a.id: a for a in Artefact.query.filter(Artefact.id.in_(art_ids)).all()}
        if art_ids else {}
    )
    groups = []
    for row in rows:
        # Honour the subdirectory browse filter (?path=), matching the prefix
        # filter _viewer_collect_groups applies to the converted-output groups.
        if current_path and not (row.file_path or '').startswith(current_path):
            continue
        restricted, explicit = content_gate_flags(
            current_user, artefacts.get(row.artefact_id))
        groups.append({
            'label': row.file_path,
            'source_file': row.file_path,
            'outputs': [],
            'is_replay': True,
            'replay': {
                'file_path': row.file_path,
                'title': row.title,
                'poster_url': (
                    url_for(f'{ROUTENAME}.get_output_file', filename=row.poster_path)
                    if row.poster_path else None
                ),
                'has_mp4': bool(row.mp4_output_path),
                'sound_only': row.video_format == 0,
            },
            'restricted': restricted,
            'explicit': explicit,
            'stable_id': hashlib.md5(
                f"replay:{row.artefact_id}:{row.file_path}".encode()
            ).hexdigest()[:12],
        })
    return groups


def _viewer_stamp_stable_ids(artefact, output_groups):
    """Defensive stable-ID stamping for groups missed upstream."""
    # ── Stable IDs for any group that didn't get one upstream ───────────────
    # Most groups are stamped before thumbnail bundling so bundle_items keep
    # their IDs; bundle wrapper groups already receive a bundle_id at
    # construction.  Anything still missing (defensive) gets one now.
    for group in output_groups:
        if not group.get('stable_id'):
            key_source = group.get('source_file') or group.get('label') or ''
            group['stable_id'] = hashlib.md5(
                f"{artefact.uuid}:{key_source}".encode()
            ).hexdigest()[:12]


def _render_viewer(artefact):
    """Build and render the viewer page for an artefact's converted outputs."""
    _output_blocked, _enrich_outputs = _viewer_make_output_helpers()

    file_filter = request.args.get('file')
    # Subdirectory browse filter — matches the File Viewer's ?path=<dir>/ scheme
    # so selecting a subdirectory there carries through to the Viewer.
    current_path = request.args.get('path', '').strip()
    if current_path and not current_path.endswith('/'):
        current_path += '/'
    # Filename glob filter — applied to the source file's basename in Mode 2.
    filename_filter = request.args.get('filename', '').strip()
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    # Partition IDs across the whole artefact tree, fetched once — the
    # archive-path, filetype-facet, and explicit-content sections below all
    # need the same set (previously three identical queries per request).
    all_partition_ids = [
        row[0] for row in db.session.query(Partition.id)
        .filter(Partition.artefact_id.in_(all_artefact_ids)).all()
    ]

    output_groups, viewer_status, failed_conversion_list, use_pagination = \
        _viewer_collect_groups(artefact, all_artefact_ids, file_filter, current_path, _enrich_outputs)

    # Fold Replay (ARMovie) movies into the unified grid so they interleave with
    # the converted outputs by filename — sharing the same sort, subdirectory
    # nav, filetype facet, pagination and thumbnail bundling — instead of sitting
    # in a separate trailing "Acorn Replay media" section.  Skip when drilled
    # into a single file (?file=): the replay_detail player card handles that.
    replay_present = False
    if not file_filter and viewer_status != 'restricted':
        replay_groups = _viewer_replay_groups(all_artefact_ids, current_path)
        if replay_groups:
            replay_present = True
            output_groups = output_groups + replay_groups
            # Re-sort the merged list (collect already sorted its own groups) so
            # movies and converted outputs interleave alphabetically by path.
            output_groups.sort(key=lambda g: g['label'].lower())

    subdirectories, archive_paths = _viewer_subdirectories(
        artefact, output_groups, file_filter, current_path, all_partition_ids)

    output_groups = _viewer_filename_filter(output_groups, filename_filter, file_filter)

    output_groups, facet_ctx = _viewer_filetype_facet(output_groups, all_partition_ids)

    total_counts, total_groups = _viewer_summary_counts(output_groups)

    output_groups, page_ctx = _viewer_paginate(output_groups, use_pagination, _enrich_outputs)

    viewer_columns, viewer_col_class, prefs_dirty = _viewer_preferences()

    user_can_bypass_explicit = _viewer_explicit_gate(
        artefact, output_groups, all_partition_ids, _output_blocked)

    output_groups, viewer_thumbnail_mode, is_aggregate_mode, thumb_dirty = \
        _viewer_thumbnail_bundle(artefact, output_groups, file_filter)
    prefs_dirty = prefs_dirty or thumb_dirty

    module_detail = _viewer_module_detail(file_filter, all_artefact_ids)
    # Replay player/poster are renderings of the same content as the MP4/poster
    # outputs, so suppress them when the artefact's outputs are restricted —
    # matching how image/Draw outputs are hidden (the bytes are independently
    # gated by get_output_file, but we must not show a broken player either).
    # The interleaved poster thumbnails were already folded into output_groups
    # above; replay_detail covers the single-movie (?file=) player card.
    _outputs_restricted = viewer_status == 'restricted'
    replay_detail = None if _outputs_restricted else _viewer_replay_detail(file_filter, all_artefact_ids)

    _viewer_stamp_stable_ids(artefact, output_groups)

    # Persist any viewer preference changes in one commit (see prefs_dirty above).
    if prefs_dirty:
        db.session.commit()

    ctx = dict(
        artefact=artefact,
        output_groups=output_groups,
        viewer_status=viewer_status,
        failed_conversion_list=failed_conversion_list,
        module_detail=module_detail,
        replay_detail=replay_detail,
        replay_present=replay_present,
        user_can_bypass_explicit=user_can_bypass_explicit,
        total_counts=total_counts,
        total_groups=total_groups,
        viewer_columns=viewer_columns,
        viewer_col_class=viewer_col_class,
        valid_viewer_columns=_VALID_VIEWER_COLUMNS,
        viewer_thumbnail_mode=viewer_thumbnail_mode,
        is_aggregate_mode=is_aggregate_mode,
        current_path=current_path,
        subdirectories=subdirectories,
        archive_paths=archive_paths,
        file_filter=file_filter,
        filename_filter=filename_filter,
        file_list_args={k: v for k, v in [
            ('path', current_path or None),
            ('filename', filename_filter or None),
        ] if v},
        clear_filename_args={k: v for k, v in request.args.items()
                              if k not in ('filename', 'page')},
    )
    ctx.update(facet_ctx)
    ctx.update(page_ctx)
    return render_template('artefacts/viewer.html', **ctx)


def _derived_artefact_url(artefact, endpoint):
    """url_for() helper mirroring the canonical-URL logic from app.artefact_url().

    Used during view-render where we need to materialise nested URLs in Python
    rather than via the Jinja template global.
    """
    root = artefact.root_artefact
    if root is not artefact:
        route = f'{ROUTENAME}.{endpoint}_nested'
        return url_for(route, item_id=artefact.item.url_id,
                       root_id=root.url_slug, artefact_id=artefact.url_slug)
    route = f'{ROUTENAME}.{endpoint}'
    return url_for(route, item_id=artefact.item.url_id, artefact_id=artefact.url_slug)


def _build_derived_entries(artefact):
    """Walk derived_artefacts recursively into a flat list of dicts for the sidebar.

    Each entry carries pre-computed URLs and restriction state so the template
    stays declarative.
    """
    entries = []

    # Thread the ancestor-id set down the recursion (each node's set includes
    # itself) so per-artefact bypass checks need no per-child parent walk.
    def _walk(node, depth, node_ancestor_ids):
        for child in node.derived_artefacts:
            child_ancestor_ids = node_ancestor_ids | {child.id}
            try:
                restricted = not current_user.can_bypass_all_restrictions(
                    child.restrictions, artefact_id=child_ancestor_ids)
            except Exception:
                restricted = bool(child.restrictions)
            has_viewer = child.artefact_type in VIEWER_ARTEFACT_TYPES
            entries.append({
                'artefact': child,
                'depth': depth,
                'download_url': _derived_artefact_url(child, 'download'),
                'viewer_url': _derived_artefact_url(child, 'viewer') if has_viewer else None,
                'restricted': restricted,
                'file_size': child.file_size,
                'mime_type': child.mime_type or '',
            })
            _walk(child, depth + 1, child_ancestor_ids)

    _walk(artefact, 0, artefact.ancestor_ids)
    return entries


def _view_file_form():
    """File-listing filter form bound to request.args."""
    # Only bind to request.args when the user has actively submitted a filter,
    # so that BooleanField defaults (e.g. recursive=True) apply on first load.
    # Without this, WTForms treats missing checkbox keys as False.
    _file_filter_keys = {'partition_uuid', 'filename', 'filetype', 'extension', 'path', 'md5', 'sha1',
                         'hide_known', 'filter_products', 'show_directories'}
    if _file_filter_keys & set(request.args.keys()):
        file_form = FileSearchForm(request.args)
    else:
        file_form = FileSearchForm()
    return file_form


def _view_analysis_summaries(all_artefact_ids):
    """Analyses card: status counts and the recent/active analysis list."""
    # Check if user wants to see all analyses or just the most recent N successful
    show_all_analyses = request.args.get('show_all_analyses', 'false').lower() == 'true'

    # How many recent successful analyses to show in the default (non-show-all) view.
    # Configurable via ANALYSES_SHOWN in myapp.cfg (default: 5).
    analyses_shown_limit = current_app.config.get('ANALYSES_SHOWN', 5)

    # Fetch all related analyses for stats, newest first (eager-load artefact for template).
    # Defer the large `details` JSON column: the analyses list/counts never read it
    # (only analysis_type/status/artefact/uuid are shown). The few detail-bearing
    # analyses surfaced as cards are fetched separately below (#447).
    from sqlalchemy.orm import defer
    from sqlalchemy.orm import joinedload as _jl_a
    all_related_analyses = Analysis.query.filter(
        Analysis.artefact_id.in_(all_artefact_ids)
    ).options(_jl_a(Analysis.artefact), defer(Analysis.details)).order_by(Analysis.id.desc()).all()
    total_analyses_count = len(all_related_analyses)

    # Status breakdown counts (displayed in the card header)
    status_counts = {s.value: 0 for s in AnalysisStatus}
    for a in all_related_analyses:
        status_counts[a.status.value] += 1

    if show_all_analyses:
        analyses = all_related_analyses  # already sorted newest first
    else:
        # Default view: always show active (pending/running) and failed analyses,
        # plus the N most recent completed analyses.
        active = [a for a in all_related_analyses
                  if a.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING,
                                  AnalysisStatus.FAILED)][:analyses_shown_limit]
        completed = [a for a in all_related_analyses
                     if a.status == AnalysisStatus.COMPLETED][:analyses_shown_limit]
        seen_ids = {a.id for a in active + completed}
        analyses = [a for a in all_related_analyses if a.id in seen_ids]
        # all_related_analyses is already newest-first, so analyses inherits that order

    has_hidden_analyses = not show_all_analyses and total_analyses_count > len(analyses)

    return dict(
        analyses=analyses,
        show_all_analyses=show_all_analyses,
        has_hidden_analyses=has_hidden_analyses,
        total_analyses_count=total_analyses_count,
        status_counts=status_counts,
    )


def _view_file_listing(file_form, all_artefact_ids):
    """File listing table: filtered query, sorting, pagination, hash matches."""
    from sqlalchemy.orm import selectinload as _sil
    files_query = ExtractedFile.query.join(Partition).filter(
        Partition.artefact_id.in_(all_artefact_ids)
    ).options(
        _sil(ExtractedFile.partition),
        _sil(ExtractedFile.known_file).selectinload(KnownFile.product),
        _sil(ExtractedFile.known_file).selectinload(KnownFile.database),
        # The file table reads file.restrictions on every row (download icon,
        # restriction badges, manage button). Eager-load it here to avoid an
        # N+1 of one extracted_file_restrictions query per file (issue #447).
        _sil(ExtractedFile.restrictions),
    )

    # Filter by specific partition if requested.
    # Guard against the string "None" which can arrive when Jinja2
    # renders a None value into a URL parameter.
    if file_form.partition_uuid.data in (None, '', 'None'):
        file_form.partition_uuid.data = None
    if file_form.partition_uuid.data:
        files_query = files_query.filter(Partition.uuid == file_form.partition_uuid.data)

    # Show empty directory entries so users can see the full disc structure

    if file_form.filename.data:
        fn = file_form.filename.data
        if '*' in fn or '?' in fn:
            # Glob pattern: escape SQL special chars then convert glob wildcards
            like_pat = fn.replace('%', r'\%').replace('_', r'\_').replace('*', '%').replace('?', '_')
            files_query = files_query.filter(ExtractedFile.filename.ilike(like_pat))
        else:
            # Plain text: substring match (backward-compatible behaviour)
            files_query = files_query.filter(ExtractedFile.filename.ilike(f'%{fn}%'))

    if file_form.filetype.data:
        # Strip a leading '#' or '&' that users might include with the hex value,
        # then resolve either a hex code or a name (e.g. "Drawfile") to a hex code.
        ft_raw = file_form.filetype.data.strip().lstrip('#&')
        ft = lookup_filetype_hex(ft_raw)
        if ft is not None:
            files_query = files_query.filter(ExtractedFile.risc_os_filetype == ft)
        else:
            # Not a RISC OS type — treat as a file extension (e.g. "wmf", ".bmp")
            ext_raw = ft_raw.lstrip('.').lower()
            if ext_raw:
                files_query = files_query.filter(ExtractedFile.extension == ext_raw)
            else:
                from sqlalchemy import false as _false
                files_query = files_query.filter(_false())

    if file_form.path.data:
        path_filter = file_form.path.data.strip()
        if path_filter.endswith('/'):
            # Directory browse: plain prefix match (existing behaviour)
            files_query = files_query.filter(
                ExtractedFile.path.ilike(f'{path_filter}%')
            )
        else:
            # Exact entry (e.g. from search result link) plus all contents
            from sqlalchemy import or_
            files_query = files_query.filter(
                or_(
                    ExtractedFile.path == path_filter,
                    ExtractedFile.path.ilike(f'{path_filter}/%')
                )
            )

    if file_form.md5.data:
        files_query = files_query.filter(ExtractedFile.md5 == file_form.md5.data.lower())
    
    if file_form.sha1.data:
        files_query = files_query.filter(ExtractedFile.sha1 == file_form.sha1.data.lower())
    
    if file_form.hide_known.data == 'hide':
        # Always show archive files even when hiding known files, because
        # archives serve as navigational pseudo-directories in the UI.
        from sqlalchemy import or_
        files_query = files_query.filter(
            or_(ExtractedFile.known_file_id.is_(None), ExtractedFile.is_archive == True)
        )
    elif file_form.hide_known.data == 'only':
        from sqlalchemy import or_
        files_query = files_query.filter(
            or_(ExtractedFile.known_file_id.isnot(None), ExtractedFile.is_archive == True)
        )

    if file_form.filter_products.data == 'hide':
        # Hide files whose primary known_file match has a product association.
        from sqlalchemy import or_ as _or
        files_query = files_query.filter(
            _or(
                ExtractedFile.known_file_id == None,
                ~ExtractedFile.known_file.has(KnownFile.product_id != None),
                ExtractedFile.is_archive == True,
            )
        )
    elif file_form.filter_products.data == 'only':
        from sqlalchemy import or_ as _or
        files_query = files_query.filter(
            _or(
                ExtractedFile.known_file.has(KnownFile.product_id != None),
                ExtractedFile.is_archive == True,
            )
        )
    
    from ..utils.pagination import VALID_PER_PAGE, resolve_per_page
    per_page, page, view_all = resolve_per_page('FILES_PER_PAGE', 100)

    # Column sorting: sort=<col> ascending, sort=-<col> descending
    sort_param = request.args.get('sort', 'path')
    sort_desc = sort_param.startswith('-')
    sort_col = sort_param.lstrip('-')
    from sqlalchemy import desc
    from sqlalchemy import func as _func
    _sort_columns = {
        'path': _func.lower(ExtractedFile.path),
        'size': ExtractedFile.file_size,
        'filetype': ExtractedFile.risc_os_filetype,
        'known': ExtractedFile.is_known,
        'date': ExtractedFile.modified_time,
    }
    sort_expr = _sort_columns.get(sort_col, _func.lower(ExtractedFile.path))
    if sort_desc:
        sort_expr = desc(sort_expr)

    files_pagination = files_query.order_by(sort_expr).paginate(
        page=page, per_page=per_page, max_per_page=per_page
    )

    # Compute letter-to-page mapping for A-Z jump bar (only for path sort).
    # Skip the extra GROUP BY pass when the whole listing fits on one page —
    # the jump bar has nothing to jump to, so its ~16ms query is pure waste
    # for the common small-artefact case (#486).
    if sort_col == 'path' and files_pagination.pages > 1:
        from ..utils.pagination import compute_letter_pages
        letter_pages, current_letter = compute_letter_pages(
            files_query, ExtractedFile.path,
            per_page, current_page=page, descending=sort_desc
        )
    else:
        letter_pages, current_letter = {}, ''

    # Count globally visible instances of each content key on this page.
    # ``file_size is not None`` deliberately includes valid zero-length files.
    duplicate_counts = {}
    duplicate_keys = {
        (f.file_size, f.sha256)
        for f in files_pagination.items
        if not f.is_directory and f.file_size is not None and f.sha256
    }
    if duplicate_keys:
        from sqlalchemy import and_ as _and
        from sqlalchemy import or_ as _or
        duplicate_rows = (
            db.session.query(
                ExtractedFile.file_size,
                ExtractedFile.sha256,
                _func.count(ExtractedFile.id),
            )
            .join(Partition)
            .join(Artefact, Partition.artefact_id == Artefact.id)
            # artefact_visibility_clause references Item columns, so Item must be
            # explicitly joined.  Without it SQLAlchemy adds Item as an implicit
            # cartesian join, multiplying count(ExtractedFile.id) by the number of
            # matching items and inflating the badge above the real instance count
            # shown by the file_duplicates list view (which joins Item correctly).
            .join(Item, Artefact.item_id == Item.id)
            .filter(artefact_visibility_clause(current_user))
            .filter(_or(*[
                _and(
                    ExtractedFile.file_size == size,
                    ExtractedFile.sha256 == sha256,
                )
                for size, sha256 in duplicate_keys
            ]))
            .group_by(ExtractedFile.file_size, ExtractedFile.sha256)
            .all()
        )
        duplicate_counts = {
            (size, sha256): count
            for size, sha256, count in duplicate_rows
            if count > 1
        }

    # Batch-query all matching KnownFiles across active hash databases
    # for the current page of files, so the template can show multiple badges.
    from ..services.hash_rescan import find_all_known_files_batch
    file_known_matches = find_all_known_files_batch(files_pagination.items)

    # Build query args for pagination links, preserving all active filters
    pagination_args = request.args.to_dict()
    pagination_args.pop('page', None)
    # Keep 'mode' in pagination_args so pagination/sort/per-page links
    # preserve hashdb mode.  The toggle button uses hashdb_toggle_args
    # (without mode) so it can toggle freely.
    hashdb_toggle_args = {k: v for k, v in pagination_args.items() if k != 'mode'}
    current_sort = sort_param

    return files_query, files_pagination, dict(
        files=files_pagination.items,
        files_pagination=files_pagination,
        pagination_args=pagination_args,
        hashdb_toggle_args=hashdb_toggle_args,
        valid_per_page=VALID_PER_PAGE,
        view_all=view_all,
        current_sort=current_sort,
        letter_pages=letter_pages,
        current_letter=current_letter,
        file_known_matches=file_known_matches,
        duplicate_counts=duplicate_counts,
    )


def _view_subdirectories(file_form, files_query, all_partitions, all_artefact_ids):
    """Directory-browse chips and archive-path icons for the file listing."""
    # Extract subdirectories at the current path level for directory browsing
    current_path = file_form.path.data.strip() if file_form.path.data else ''
    subdirectories: list = []

    # Set of archive file paths so the template can show archive icons for
    # "directories" that are actually archives.
    archive_paths = set()
    if all_partitions:
        from sqlalchemy import or_ as _or_flags
        from ..utils.path_nav import compute_subdirectories

        # Infer subdirectories from file paths (covers non-empty directories).
        all_file_paths = [p for (p,) in files_query.with_entities(ExtractedFile.path).all()]
        subdir_set = set(compute_subdirectories(all_file_paths, current_path))

        # Single scan for the two flag-based path sets (#447):
        #   - is_directory entries → empty directories excluded from files_query
        #     when filters suppress directory rows (honours the partition filter).
        #   - is_archive entries → archive_paths (intentionally unfiltered by
        #     partition, matching prior behaviour).
        _sel_partition = file_form.partition_uuid.data
        flag_rows = (
            db.session.query(
                ExtractedFile.path,
                ExtractedFile.is_directory,
                ExtractedFile.is_archive,
                Partition.uuid,
            )
            .join(Partition)
            .filter(
                Partition.artefact_id.in_(all_artefact_ids),
                _or_flags(
                    ExtractedFile.is_directory == True,
                    ExtractedFile.is_archive == True,
                ),
            )
            .all()
        )
        for dir_path, is_dir, is_arc, p_uuid in flag_rows:
            if is_arc:
                archive_paths.add(dir_path)
            if not is_dir:
                continue
            if _sel_partition and p_uuid != _sel_partition:
                continue
            if current_path:
                if not dir_path.startswith(current_path):
                    continue
                relative_path = dir_path[len(current_path):]
            else:
                relative_path = dir_path
            # Only add direct children (no slash = not a deeper descendant)
            if relative_path and '/' not in relative_path:
                subdir_set.add(relative_path)

        from natsort import natsorted, ns
        subdirectories = natsorted(subdir_set, alg=ns.IGNORECASE)
    return current_path, subdirectories, archive_paths


def _view_archive_banner(artefact, current_path, all_partitions, all_artefact_ids):
    """Archive comment banner above the file listing."""
    # Header banner: when browsing inside an archive (or at the root of a
    # top-level archive), surface its archive_comment above the file list.
    # - At the root, a Partition with filesystem='archive' may carry the
    #   comment (top-level archive uploads use this path).
    # - Below the root, a nested archive's outer ExtractedFile carries the
    #   comment so we can find it by exact path match.
    archive_comment_banner = None
    archive_comment_label = None
    if current_path:
        path_match = current_path.rstrip('/')
        if path_match:
            ef = (
                ExtractedFile.query.join(Partition)
                .filter(
                    Partition.artefact_id.in_(all_artefact_ids),
                    ExtractedFile.path == path_match,
                    ExtractedFile.is_archive == True,
                    ExtractedFile.archive_comment.isnot(None),
                )
                .with_entities(
                    ExtractedFile.archive_comment,
                    ExtractedFile.archive_format,
                    ExtractedFile.filename,
                )
                .first()
            )
            if ef:
                archive_comment_banner = ef.archive_comment
                archive_comment_label = (
                    f"{ef.filename} ({ef.archive_format})" if ef.archive_format else ef.filename
                )
    elif all_partitions:
        for p in all_partitions:
            if p.archive_comment:
                archive_comment_banner = p.archive_comment
                archive_comment_label = (
                    f"{artefact.label} ({p.container_format})" if p.container_format else artefact.label
                )
                break
    return dict(
        archive_comment_banner=archive_comment_banner,
        archive_comment_label=archive_comment_label,
    )


def _view_analysis_detail_cards(all_artefact_ids):
    """Detail cards/badges: mastering, protection, partitions, ARMlock, flux, density."""
    # Extract completed analysis results for display.
    # These are surfaced as badges + cards directly on the artefact view page.
    mastering_analysis = None
    protection_analysis = None
    partition_detect_details = None
    armlock_analysis = None
    flux_visualisation_analysis = None
    density_detect_analysis = None
    # Load `details` only for the analysis types surfaced as cards/badges, newest
    # first, rather than carrying details for every analysis (#447). Picking the
    # first match per type preserves the previous "most recent completed" behaviour.
    _detail_types = (
        AnalysisType.DISC_MASTERING_DETECT, AnalysisType.DISC_PROTECTION_DETECT,
        AnalysisType.PARTITION_DETECT, AnalysisType.ARMLOCK_REMOVE,
        AnalysisType.FLUX_VISUALISATION, AnalysisType.DETECT_TRACK_DENSITY,
    )
    _detail_analyses = Analysis.query.filter(
        Analysis.artefact_id.in_(all_artefact_ids),
        Analysis.status == AnalysisStatus.COMPLETED,
        Analysis.analysis_type.in_(_detail_types),
        Analysis.details.isnot(None),
    ).order_by(Analysis.id.desc()).all()
    for a in _detail_analyses:
        if a.status == AnalysisStatus.COMPLETED and a.details:
            if mastering_analysis is None and a.analysis_type == AnalysisType.DISC_MASTERING_DETECT:
                try:
                    mastering_analysis = json.loads(a.details)
                    mastering_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse mastering analysis details for {a.uuid}: {e}")
            elif protection_analysis is None and a.analysis_type == AnalysisType.DISC_PROTECTION_DETECT:
                try:
                    protection_analysis = json.loads(a.details)
                    protection_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse protection analysis details for {a.uuid}: {e}")
            elif partition_detect_details is None and a.analysis_type == AnalysisType.PARTITION_DETECT:
                try:
                    partition_detect_details = json.loads(a.details)
                    partition_detect_details['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse partition detect details for {a.uuid}: {e}")
            elif armlock_analysis is None and a.analysis_type == AnalysisType.ARMLOCK_REMOVE:
                try:
                    armlock_analysis = json.loads(a.details)
                    armlock_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse ARMlock analysis details for {a.uuid}: {e}")
            elif flux_visualisation_analysis is None and a.analysis_type == AnalysisType.FLUX_VISUALISATION:
                try:
                    flux_visualisation_analysis = json.loads(a.details)
                    flux_visualisation_analysis['_analysis_uuid'] = a.uuid
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse flux visualisation analysis details for {a.uuid}: {e}")
            elif density_detect_analysis is None and a.analysis_type == AnalysisType.DETECT_TRACK_DENSITY:
                try:
                    det_details = json.loads(a.details)
                    detection = det_details.get('detection', {})
                    if detection.get('detected'):
                        density_detect_analysis = {**detection, '_analysis_uuid': a.uuid}
                except (json.JSONDecodeError, TypeError) as e:
                    current_app.logger.warning(f"Failed to parse density detection details for {a.uuid}: {e}")
        if (mastering_analysis is not None and protection_analysis is not None
                and partition_detect_details is not None
                and flux_visualisation_analysis is not None
                and armlock_analysis is not None
                and density_detect_analysis is not None):
            break

    return dict(
        mastering_analysis=mastering_analysis,
        protection_analysis=protection_analysis,
        partition_detect_details=partition_detect_details,
        armlock_analysis=armlock_analysis,
        flux_visualisation_analysis=flux_visualisation_analysis,
        density_detect_analysis=density_detect_analysis,
    )


def _view_iso_metadata(artefact):
    """ISO 9660 metadata card parsed from artefact.media_metadata."""
    # Parse format-specific metadata (populated by METADATA_EXTRACT for ISO
    # images and potentially other formats).  Only the ``iso9660`` section is
    # surfaced today; the template hides its card when the section is absent.
    iso9660_metadata = None
    if artefact.media_metadata:
        try:
            _mm = json.loads(artefact.media_metadata)
        except (json.JSONDecodeError, TypeError) as e:
            current_app.logger.warning(
                f"Failed to parse media_metadata for artefact {artefact.uuid}: {e}"
            )
            _mm = None
        if isinstance(_mm, dict):
            iso9660 = _mm.get('iso9660')
            if isinstance(iso9660, dict) and iso9660:
                iso9660_metadata = iso9660
    return iso9660_metadata


def _view_partition_metadata(partition_detect_details):
    """Per-partition metadata shown inline in the Partitions table."""
    # Build a lookup of per-partition metadata from PARTITION_DETECT, keyed by
    # partition index, so the template can display disc names, passwords,
    # protection levels, and flags inline in the Partitions table.
    partition_metadata = {}
    if partition_detect_details:
        for p in partition_detect_details.get('partitions', []):
            idx = p.get('index')
            if idx is not None:
                partition_metadata[idx] = p
    return partition_metadata


def _view_conversion_status(artefact, all_artefact_ids):
    """Viewable/failed-conversion markers for file rows and the View button state."""
    # Build viewable_filenames and failed_conversion_info by scanning all
    # FORMAT_CONVERT analyses for this artefact tree.
    #   viewable_filenames    — set of file.path strings with completed outputs
    #   failed_conversion_info — dict file.path → {error, analysis_uuid, kind}
    #     kind='conversion'  for per-file FORMAT_CONVERT failures
    #     kind='extraction'  for ARCHIVE_EXTRACT failures (see below)
    _viewable_types = VIEWER_ARTEFACT_TYPES
    viewable_filenames = set()
    failed_conversion_info = {}
    has_converted_outputs = artefact.artefact_type in _viewable_types
    if artefact.artefact_type not in _viewable_types:
        convs = (
            Analysis.query
            .filter(
                Analysis.artefact_id.in_(all_artefact_ids),
                Analysis.analysis_type == AnalysisType.FORMAT_CONVERT,
            )
            .all()
        )
        for conv in convs:
            if not (conv.status == AnalysisStatus.COMPLETED and conv.success):
                continue
            try:
                details = json.loads(conv.details or '{}')
            except (json.JSONDecodeError, TypeError):
                continue
            for out in details.get('outputs', []):
                sf = out.get('source_file')
                if sf:
                    viewable_filenames.add(sf)
            for fc in details.get('failed_conversions', []):
                sf = fc.get('source_file')
                if sf and sf not in failed_conversion_info:
                    failed_conversion_info[sf] = {
                        'error': fc.get('error', 'Conversion failed'),
                        'analysis_uuid': conv.uuid,
                        'kind': 'conversion',
                    }

        # Collect ARCHIVE_EXTRACT failures and map back to the archive file's
        # path via the file_id stored in hints.
        # Defer the large `details` JSON: this loop only reads hints,
        # error_message, and uuid, so loading details is wasted I/O (#486).
        from sqlalchemy.orm import defer
        failed_extractions = (
            Analysis.query
            .filter(
                Analysis.artefact_id.in_(all_artefact_ids),
                Analysis.analysis_type == AnalysisType.ARCHIVE_EXTRACT,
                Analysis.status == AnalysisStatus.FAILED,
            )
            .options(defer(Analysis.details))
            .all()
        )
        if failed_extractions:
            failed_file_ids = {}
            for ae in failed_extractions:
                try:
                    hints = json.loads(ae.hints or '{}')
                except (json.JSONDecodeError, TypeError):
                    hints = {}
                file_id = hints.get('file_id')
                if file_id:
                    failed_file_ids[int(file_id)] = {
                        'error': ae.error_message or 'Extraction failed',
                        'analysis_uuid': ae.uuid,
                        'kind': 'extraction',
                    }
            if failed_file_ids:
                efs = ExtractedFile.query.filter(
                    ExtractedFile.id.in_(failed_file_ids.keys())
                ).all()
                for ef in efs:
                    failed_conversion_info[ef.path] = failed_file_ids[ef.id]

        # "View" button: show if artefact is viewable type, or has any FORMAT_CONVERT.
        # When not a viewable type we already fetched every FORMAT_CONVERT analysis
        # into `convs` above (same guard), so reuse it rather than issuing another
        # existence query (#447).
        has_converted_outputs = bool(convs)

    return viewable_filenames, failed_conversion_info, has_converted_outputs


def _view_recognised_products(all_partitions):
    """Recognised products card and folder badges."""
    # Recognised products for all partitions of this artefact tree
    recognised_products = []
    if all_partitions:
        partition_ids = [p.id for p in all_partitions]
        from sqlalchemy.orm import joinedload as _jl
        recognised_products = (
            RecognisedProduct.query
            .join(RecognisedProduct.partition)
            .filter(RecognisedProduct.partition_id.in_(partition_ids))
            .options(_jl(RecognisedProduct.product).joinedload(KnownProduct.database))
            .order_by(Partition.partition_index, RecognisedProduct.folder_path)
            .all()
        )

    # Build a set of folder paths that have a recognised product (for directory row badges)
    recognised_folder_paths = {rp.folder_path: rp for rp in recognised_products}

    return recognised_products, recognised_folder_paths


def _view_hashdb_context(hashdb_mode):
    """Hash-database list and product cache for the Add to Hash DB modal."""
    # Hash databases for the "Add to Hash DB" modal (with products pre-loaded).
    # hashdb_product_cache is the JSON payload for the modal's product
    # dropdown: {database_id: [{id, title}, ...]} sorted by title.
    hashdb_product_cache = {}
    if hashdb_mode:
        from sqlalchemy.orm import joinedload as _jl2
        hash_databases = (
            HashDatabase.query
            .filter(HashDatabase.is_deleting.is_(False))
            .options(_jl2(HashDatabase.known_products))
            .order_by(HashDatabase.name)
            .all()
        )
        hashdb_product_cache = {
            hdb.id: [
                {'id': p.id, 'title': p.title}
                for p in sorted(hdb.known_products, key=lambda p: p.title.lower())
            ]
            for hdb in hash_databases
        }
    else:
        hash_databases = []

    return dict(
        hash_databases=hash_databases,
        hashdb_product_cache=hashdb_product_cache,
    )


def _view_restriction_maps(all_artefact_ids, files_pagination):
    """Direct/ancestor/descendant restriction maps for the file listing."""
    # File-level restrictions on any extracted file within this artefact tree.
    # Used to adjust the download button state when the artefact itself is
    # unrestricted but contains restricted extracted files.
    from sqlalchemy.orm import joinedload as _jl_efr
    artefact_file_restrictions = (
        ExtractedFileRestriction.query
        .options(_jl_efr(ExtractedFileRestriction.extracted_file))
        .join(ExtractedFile, ExtractedFileRestriction.extracted_file_id == ExtractedFile.id)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .filter(Partition.artefact_id.in_(all_artefact_ids))
        .all()
    )

    # Build two mappings for non-direct restriction display in the file listing:
    #
    #   file_ancestor_restrictions {file_id: [restrictions]}
    #     A file inside a restricted archive — the restriction comes from above.
    #
    #   file_descendant_restrictions {file_id: [restrictions]}
    #     An archive whose contents include a restricted file — the restriction
    #     originates from below.
    #
    # Strategy: one query for the parent_id map of all files in the artefact
    # tree, then two in-memory passes.
    file_ancestor_restrictions: dict[int, list] = {}
    file_descendant_restrictions: dict[int, list] = {}
    if artefact_file_restrictions:
        # direct map: file_id -> [restriction objects]
        _direct_map: dict[int, list] = {}
        for r in artefact_file_restrictions:
            _direct_map.setdefault(r.extracted_file_id, []).append(r)

        # parent map: file_id -> parent_file_id (None for top-level)
        _parent_rows = (
            ExtractedFile.query
            .join(Partition, ExtractedFile.partition_id == Partition.id)
            .filter(Partition.artefact_id.in_(all_artefact_ids))
            .with_entities(ExtractedFile.id, ExtractedFile.parent_file_id)
            .all()
        )
        _parent_map: dict[int, int | None] = {row.id: row.parent_file_id for row in _parent_rows}

        # Pass 1 — upward: for every directly restricted file, mark all of
        # its ancestor archives as having a restriction originating from below.
        for restricted_id, restr_list in _direct_map.items():
            pid = _parent_map.get(restricted_id)
            while pid is not None:
                file_descendant_restrictions.setdefault(pid, []).extend(restr_list)
                pid = _parent_map.get(pid)

        # Pass 2 — downward (current page only): for files on this page that
        # have no direct restrictions, check whether any enclosing archive is
        # restricted and propagate that restriction down to them.
        for f in files_pagination.items:
            if f.id in _direct_map:
                continue  # has direct restrictions — handled by file.restrictions in template
            inherited = []
            pid = _parent_map.get(f.id)
            while pid is not None:
                if pid in _direct_map:
                    inherited.extend(_direct_map[pid])
                pid = _parent_map.get(pid)
            if inherited:
                file_ancestor_restrictions.setdefault(f.id, []).extend(inherited)

    def _dedup_by_type(rlist):
        """Return rlist with duplicate restriction_type entries removed (keeps first)."""
        seen: set = set()
        result = []
        for r in rlist:
            if r.restriction_type not in seen:
                seen.add(r.restriction_type)
                result.append(r)
        return result

    # Deduplicate each per-file list so that e.g. an archive containing five
    # MALWARE-restricted files doesn't show the badge five times.
    file_ancestor_restrictions = {
        fid: _dedup_by_type(rlist)
        for fid, rlist in file_ancestor_restrictions.items()
    }
    file_descendant_restrictions = {
        fid: _dedup_by_type(rlist)
        for fid, rlist in file_descendant_restrictions.items()
    }

    # Legacy alias used by the download-button logic in the template — the
    # effective non-direct restrictions are the union of both directions.
    file_inherited_restrictions = {
        fid: _dedup_by_type(
            file_ancestor_restrictions.get(fid, []) + file_descendant_restrictions.get(fid, [])
        )
        for fid in set(file_ancestor_restrictions) | set(file_descendant_restrictions)
    }

    return dict(
        artefact_file_restrictions=artefact_file_restrictions,
        file_inherited_restrictions=file_inherited_restrictions,
        file_ancestor_restrictions=file_ancestor_restrictions,
        file_descendant_restrictions=file_descendant_restrictions,
    )


def _view_derived_entries(artefact):
    """Derived-artefact and sidecar sections in the sidebar."""
    _all_derived = _build_derived_entries(artefact)
    # Sidecar/companion files (a disk image's ddrescue .map, readme, checksums)
    # are shown in their own section, not mixed into the derived-artefact tree.
    sidecar_entries = [e for e in _all_derived
                       if e['artefact'].artefact_type == ArtefactType.SIDECAR]
    derived_entries = [e for e in _all_derived
                       if e['artefact'].artefact_type != ArtefactType.SIDECAR]
    return derived_entries, sidecar_entries


def _view_admin_bypass(artefact, all_artefact_ids=None):
    """Per-artefact restriction-bypass admin panel data.

    *all_artefact_ids* (current + derived IDs) is threaded in from the caller so
    ``grantable_bypass_rtypes`` reuses it instead of re-running the derived-IDs
    recursive CTE (#486).
    """
    # Per-artefact bypass data: only loaded for admins to avoid unnecessary queries.
    if current_user.is_authenticated and current_user.is_admin:
        artefact_user_bypasses = (
            UserArtefactBypass.query
            .filter_by(artefact_id=artefact.id)
            .order_by(UserArtefactBypass.restriction_type)
            .all()
        )
        # NOTE (#486): loads the entire user table for the bypass-grant owner
        # dropdown. Fine at current scale but grows linearly with user count;
        # revisit with a typeahead/paginated lookup if the table gets large.
        bypass_eligible_users = (
            User.query.order_by(User.username).all()
        )
        bypass_grantable_rtypes = sorted(
            grantable_bypass_rtypes(artefact, all_artefact_ids), key=lambda rt: rt.label
        )
    else:
        artefact_user_bypasses = []
        bypass_eligible_users = []
        bypass_grantable_rtypes = []

    return dict(
        artefact_user_bypasses=artefact_user_bypasses,
        bypass_eligible_users=bypass_eligible_users,
        bypass_grantable_rtypes=bypass_grantable_rtypes,
    )


# Number of similar artefacts shown in the sidebar preview card before the
# "Show all" link to the full similarity page.
SIMILAR_SIDEBAR_LIMIT = 5


def _render_artefact_view(artefact):
    """Render the artefact detail page from per-panel helper contexts."""
    file_form = _view_file_form()

    # Collect all artefact IDs: current + all derived (recursively).
    # Used for both partitions/files and analyses so that follow-on jobs
    # queued against derived partition artefacts are visible here.
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)

    analyses_ctx = _view_analysis_summaries(all_artefact_ids)

    # Query partitions from all artefacts (for display)
    all_partitions = Partition.query.filter(
        Partition.artefact_id.in_(all_artefact_ids)
    ).order_by(Partition.artefact_id, Partition.partition_index).all()

    files_query, files_pagination, files_ctx = _view_file_listing(file_form, all_artefact_ids)
    current_path, subdirectories, archive_paths = _view_subdirectories(
        file_form, files_query, all_partitions, all_artefact_ids)
    banner_ctx = _view_archive_banner(artefact, current_path, all_partitions, all_artefact_ids)
    detail_ctx = _view_analysis_detail_cards(all_artefact_ids)
    iso9660_metadata = _view_iso_metadata(artefact)
    partition_metadata = _view_partition_metadata(detail_ctx['partition_detect_details'])

    hashdb_mode = request.args.get('mode') == 'hashdb'

    viewable_filenames, failed_conversion_info, has_converted_outputs = \
        _view_conversion_status(artefact, all_artefact_ids)
    module_info, replay_info = metadata_by_path(all_artefact_ids)
    recognised_products, recognised_folder_paths = _view_recognised_products(all_partitions)
    hashdb_ctx = _view_hashdb_context(hashdb_mode)
    restriction_ctx = _view_restriction_maps(all_artefact_ids, files_pagination)
    derived_entries, sidecar_entries = _view_derived_entries(artefact)
    bypass_ctx = _view_admin_bypass(artefact, all_artefact_ids)
    similar_preview = similar_artefacts(artefact, current_user, limit=SIMILAR_SIDEBAR_LIMIT)
    similar_folder_counts = component_match_counts(all_artefact_ids, current_user)

    ctx = dict(
        artefact=artefact,
        file_form=file_form,
        all_partitions=all_partitions,
        subdirectories=subdirectories,
        current_path=current_path,
        archive_paths=archive_paths,
        partition_metadata=partition_metadata,
        iso9660_metadata=iso9660_metadata,
        hashdb_mode=hashdb_mode,
        RestrictionType=RestrictionType,
        viewable_filenames=viewable_filenames,
        failed_conversion_info=failed_conversion_info,
        has_converted_outputs=has_converted_outputs,
        module_info=module_info,
        replay_info=replay_info,
        recognised_products=recognised_products,
        recognised_folder_paths=recognised_folder_paths,
        derived_entries=derived_entries,
        sidecar_entries=sidecar_entries,
        similar_preview=similar_preview,
        similar_folder_counts=similar_folder_counts,
        move_item_choices=_move_item_choices(artefact),
    )
    ctx.update(analyses_ctx)
    ctx.update(files_ctx)
    ctx.update(banner_ctx)
    ctx.update(detail_ctx)
    ctx.update(hashdb_ctx)
    ctx.update(restriction_ctx)
    ctx.update(bypass_ctx)
    return render_template('artefacts/view.html', **ctx)


def _move_item_choices(artefact):
    """Build item selector choices for the move-artefact form.

    Returns an empty list when the artefact is derived (cannot be moved).
    Items are shown with depth-based indentation to reflect hierarchy.
    """
    if artefact.parent_artefact_id is not None:
        return []

    # NOTE (#486): indented_item_choices loads the entire items table (ORDER BY
    # name) to build the move-target dropdown. Fine at current scale but grows
    # linearly with item count; revisit with a typeahead if the table gets large.
    from ..utils.item_helpers import indented_item_choices
    return indented_item_choices(
        value_fn=lambda item: item.url_id,
        exclude_ids={artefact.item_id},
    )


@blueprint.route('/<string:uuid>/add-to-hashdb', methods=['POST'])
@login_required
@require_permission('read_write')
def add_to_hashdb(uuid):
    """Add selected extracted files to a hash database."""
    artefact = _get_artefact_or_404(uuid=uuid)
    _require_manage_artefact_content(artefact)

    file_ids = request.form.getlist('file_ids', type=int)
    raw_db_id = request.form.get('database_id', '').strip()
    product_id = request.form.get('product_id', type=int)
    new_product_title = request.form.get('new_product_title', '').strip()
    new_product_description = request.form.get('new_product_description', '').strip()
    is_required = request.form.get('is_required', '1') == '1'
    base_path = request.form.get('base_path', '').strip()

    # Preserve directory navigation state across the redirect
    nav_partition_uuid = request.form.get('partition_uuid', '').strip() or None
    nav_path = request.form.get('nav_path', '').strip() or None
    redirect_kwargs = dict(item_id=artefact.item.url_id, artefact_id=artefact.url_slug, mode='hashdb')
    if nav_partition_uuid:
        redirect_kwargs['partition_uuid'] = nav_partition_uuid
    if nav_path:
        redirect_kwargs['path'] = nav_path

    if not file_ids:
        flash('No files selected.', 'warning')
        return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))

    if raw_db_id == 'new':
        new_db_name = request.form.get('new_database_name', '').strip()
        if not new_db_name:
            flash('Provide a name for the new hash database.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))
        database = HashDatabase(name=new_db_name)
        db.session.add(database)
        db.session.flush()
    else:
        try:
            database_id = int(raw_db_id)
        except (ValueError, TypeError):
            flash('Select a hash database.', 'danger')
            return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))
        database = db.get_or_404(HashDatabase, database_id)

    # Create or fetch the product
    if product_id:
        product = KnownProduct.query.filter_by(id=product_id, database_id=database.id).first_or_404()
    elif new_product_title:
        product = KnownProduct(
            database_id=database.id,
            title=new_product_title,
            description=new_product_description or None,
        )
        db.session.add(product)
        db.session.flush()  # get product.id
    else:
        flash('Select a product or provide a new product title.', 'danger')
        return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))

    # Get OUTPUT_FOLDER for on-demand hash computation
    output_folder = current_app.config.get('OUTPUT_FOLDER', '')
    if not os.path.isabs(output_folder):
        output_folder = os.path.join(current_app.instance_path, output_folder)

    added = 0
    new_kfs = []
    skipped_no_hash = []
    skipped_no_file = []

    # Compute valid artefact IDs once with a single recursive CTE query instead
    # of re-walking the derivation tree once per submitted file.
    valid_artefact_ids = {artefact.id} | set(get_all_derived_artefact_ids(artefact))

    for file_id in file_ids:
        ef = db.session.get(ExtractedFile, file_id)
        if ef is None or ef.partition.artefact_id not in valid_artefact_ids:
            continue
        if ef.is_directory:
            continue

        md5 = ef.md5
        sha1 = ef.sha1
        sha256 = ef.sha256

        # Compute hashes on demand if missing
        if not md5:
            file_path_on_disk = resolve_extracted_file_path(ef)
            if not file_path_on_disk:
                skipped_no_file.append(ef.path)
                continue
            try:
                md5_h = hashlib.md5()
                sha1_h = hashlib.sha1()
                sha256_h = hashlib.sha256()
                with open(file_path_on_disk, 'rb') as fh:
                    for chunk in iter(lambda: fh.read(65536), b''):
                        md5_h.update(chunk)
                        sha1_h.update(chunk)
                        sha256_h.update(chunk)
                md5 = md5_h.hexdigest()
                sha1 = sha1_h.hexdigest()
                sha256 = sha256_h.hexdigest()
                # Persist back to ExtractedFile
                ef.md5 = md5
                ef.sha1 = sha1
                ef.sha256 = sha256
            except OSError:
                skipped_no_file.append(ef.path)
                continue

        # Deduplicate: skip if this md5 already exists in this product
        if KnownFile.query.filter_by(database_id=database.id, product_id=product.id, md5=md5).first():
            continue

        kf = KnownFile(
            database_id=database.id,
            product_id=product.id,
            filename=ef.filename,
            file_size=ef.file_size,
            md5=md5,
            sha1=sha1,
            sha256=sha256,
            is_required=is_required,
            relative_path=(ef.path[len(base_path):] if base_path and ef.path and ef.path.startswith(base_path) else ef.path) or None,
        )
        db.session.add(kf)
        new_kfs.append(kf)
        added += 1

    db.session.commit()

    # Linking the freshly-added KnownFiles against the whole extracted-file
    # corpus (and the recognition backfill) is unbounded work: for a bulk
    # selection of hundreds of files it routinely blows past the reverse
    # proxy's timeout, so the user sees a 504 even though the rows committed
    # above.  Hand it to the task runner instead — it relinks in bounded
    # chunks and queues recognition itself — exactly as the web DB-import
    # route does (see hashdb._post_known_file_changes).
    linking_queued = False
    if added and database.is_active:
        from ..services.hash_rescan import queue_hashdb_link_job
        _, linking_queued = queue_hashdb_link_job(database.id)

    if added:
        msg = f'Added {added} file(s) to "{product.title}" in "{database.name}".'
        if linking_queued:
            msg += ' Matching against the collection is running in the background.'
        flash(msg, 'success')
    if skipped_no_hash:
        flash(f'{len(skipped_no_hash)} file(s) skipped — no hash available and extraction analysis not found. Re-run FILE_EXTRACTION first.', 'warning')
    if skipped_no_file:
        flash(f'{len(skipped_no_file)} file(s) skipped — extracted files no longer on disk.', 'warning')
    if not added and not skipped_no_hash and not skipped_no_file:
        flash('All selected files already exist in this hash database.', 'info')

    return redirect(url_for(f'{ROUTENAME}.view', **redirect_kwargs))


@blueprint.route('/items/<string:item_id>/artefacts/upload', methods=['GET', 'POST'])
@login_required
@require_permission('read_write')
def upload(item_id):
    """Upload a new artefact."""
    item = lookup_by_identifier(Item, item_id)
    if not can_view_item(item, current_user):
        abort(404)
    if item.private_effective and not can_contribute_to_item(item, current_user):
        abort(403)
    form = ArtefactUploadForm()

    # Build item choices
    from ..utils.item_helpers import indented_item_choices
    form.item_id.choices = [(0, '-- Select item --')] + indented_item_choices(viewer=current_user)

    # Build type choices with auto-detect as default
    type_choices = [('auto', '-- Auto-detect --')]
    type_choices.extend([(t.value, _type_display_name(t)) for t in ArtefactType if t != ArtefactType.UNKNOWN])
    form.artefact_type.choices = type_choices

    # Build platform choices
    platforms = Platform.query.order_by(Platform.name).all()
    form.platform_id.choices = [(0, '-- No hint --')] + [(p.id, p.name) for p in platforms]

    if form.validate_on_submit():
        target_item = db.session.get(Item, form.item_id.data) or item
        if not can_view_item(target_item, current_user):
            abort(404)
        if target_item.private_effective and not can_contribute_to_item(target_item, current_user):
            abort(403)

        file = form.file.data
        original_filename = safe_original_filename(file.filename)

        # Detect or use specified type
        if form.artefact_type.data == 'auto':
            artefact_type = detect_artefact_type(original_filename)
            type_overridden = False
        else:
            artefact_type = ArtefactType(form.artefact_type.data)
            type_overridden = True

        # Save file
        storage_path, file_size = save_uploaded_file(file)

        # Compute hashes and check for duplicates
        storage_key = current_app.storage.storage_key('uploads', storage_path)
        try:
            md5, sha256 = compute_file_hashes(storage_key, use_storage=True)
        except OSError:
            md5, sha256 = None, None

        # Detect MIME type
        mime_type, _ = mimetypes.guess_type(original_filename)

        # Analysis hints from the form fields
        hints = {}
        if form.platform_id.data and form.platform_id.data != 0:
            platform = db.session.get(Platform, form.platform_id.data)
            if platform:
                hints['platform'] = platform.name
        if form.dfi_clock_mhz.data:
            hints['dfi_clock_mhz'] = form.dfi_clock_mhz.data
        web_priority = current_app.config.get('WEB_UI_ANALYSIS_PRIORITY', ANALYSIS_PRIORITY_HIGH)

        # Create the artefact, slug, and analysis queue entries atomically.
        # Checksum computation is always queued; type-specific analyses only
        # when the user requested auto-analyse.
        outcome = ingest_uploaded_artefact(
            target_item,
            label=form.label.data,
            artefact_type=artefact_type,
            type_overridden=type_overridden,
            original_filename=original_filename,
            storage_name=storage_path,
            file_size=file_size,
            md5=md5,
            sha256=sha256,
            description=form.description.data,
            mime_type=mime_type,
            owner_id=current_user.id,
            is_private=form.is_private.data,
            hints=hints if hints else None,
            queue=QUEUE_FULL if form.auto_analyse.data else QUEUE_CHECKSUM_ONLY,
            priority=web_priority,
        )
        artefact = outcome.artefact

        if form.auto_analyse.data:
            flash(f'Artefact "{artefact.label}" uploaded. Analysis queued.', 'success')
        else:
            flash(f'Artefact "{artefact.label}" uploaded.', 'success')

        if form.upload_more.data:
            return redirect(url_for(f'{ROUTENAME}.upload', item_id=item.url_id, upload_more=1))
        return redirect(url_for(f'{ROUTENAME}.view', item_id=target_item.url_id, artefact_id=artefact.url_slug))

    if request.args.get('upload_more') == '1':
        form.upload_more.data = True
    form.item_id.data = item.id
    return render_template(
        'artefacts/upload.html', form=form, item=item,
        chunk_threshold=_chunk_threshold(), chunk_size=_chunk_size())


# Default trigger levels for the browser-side chunked uploader.  Files at or
# above CHUNKED_UPLOAD_THRESHOLD bytes are uploaded in CHUNKED_UPLOAD_CHUNK_SIZE
# chunks; smaller files use the plain multipart form POST.  Both are overridable
# via myapp.cfg or environment variables.
_DEFAULT_CHUNK_THRESHOLD = 100 * 1024 * 1024  # 100 MB
_DEFAULT_CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB


def _chunk_threshold() -> int:
    """Configured size at/above which web uploads use the chunked path."""
    return int_config('CHUNKED_UPLOAD_THRESHOLD', _DEFAULT_CHUNK_THRESHOLD)


def _chunk_size() -> int:
    """Configured size of each upload chunk, in bytes."""
    return int_config('CHUNKED_UPLOAD_CHUNK_SIZE', _DEFAULT_CHUNK_SIZE)


def _chunk_error(message, code=400):
    """JSON error response for the session-authenticated chunked routes."""
    return jsonify({'error': message}), code


def _resolve_chunk_target_item(pk_value, url_identifier=None):
    """Resolve and authorise a chunked-upload target item.

    *pk_value* is the integer DB id the item ``<select>`` posts; *url_identifier*
    is a URL/meta fallback (an item's url_id or full UUID, resolved with the same
    rules as the rest of the app).  Returns a JSON error rather than aborting, so
    the AJAX caller gets a structured response.  Mirrors the web upload view's
    checks: 404 when the item is not visible, 403 when visible but not
    contributable.
    """
    item = None
    if pk_value is not None and str(pk_value).isdigit() and int(pk_value) > 0:
        item = db.session.get(Item, int(pk_value))
    elif url_identifier:
        try:
            item = lookup_by_identifier(Item, str(url_identifier))
        except NotFound:
            item = None
    if item is None or not can_view_item(item, current_user):
        return None, _chunk_error('Item not found', 404)
    if item.private_effective and not can_contribute_to_item(item, current_user):
        return None, _chunk_error('Not permitted to add to this item', 403)
    return item, None


@blueprint.route('/items/<string:item_id>/artefacts/chunked/init', methods=['POST'])
@login_required
@require_permission('read_write')
def chunked_upload_init(item_id):
    """Initialise a session-authenticated chunked upload from the web UI."""
    data = request.get_json(silent=True) or {}

    filename = (data.get('filename') or '').strip()
    if not filename:
        return _chunk_error('filename is required')

    try:
        total_chunks = int(data['total_chunks'])
        if total_chunks < 1:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        return _chunk_error('total_chunks must be a positive integer')
    if total_chunks > _chunked.MAX_TOTAL_CHUNKS:
        return _chunk_error(
            f'total_chunks exceeds the maximum of {_chunked.MAX_TOTAL_CHUNKS}')

    # Reject up front when the client already declares an over-size upload; the
    # assembled size is re-checked authoritatively in /complete.
    max_size = _chunked.max_upload_size()
    total_size = data.get('total_size')
    if total_size is not None:
        try:
            if int(total_size) > max_size:
                return _chunk_error(
                    f'Upload exceeds the maximum size of {max_size} bytes', 413)
        except (ValueError, TypeError):
            return _chunk_error('total_size must be an integer')

    # The target item comes from the form's item select (its integer DB id in the
    # JSON body); fall back to the page's item from the URL so the upload page can
    # still redirect an artefact to a different item.
    item, error = _resolve_chunk_target_item(data.get('item_id'), item_id)
    if error:
        return error

    label = (data.get('label') or '').strip()
    if not label:
        return _chunk_error('label is required')

    hints = data.get('hints')
    if hints is not None and not isinstance(hints, dict):
        return _chunk_error('hints must be a JSON object')

    meta = {
        'filename': filename,
        'total_chunks': total_chunks,
        'total_size': total_size,
        'item_uuid': item.uuid,
        'label': label,
        'artefact_type': data.get('artefact_type', 'auto'),
        'description': data.get('description'),
        'is_private': bool(data.get('is_private', False)),
        'auto_analyse': bool(data.get('auto_analyse', True)),
        'hints': hints,
        'creator_user_id': current_user.id,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    upload_uuid = _chunked.init_chunk_session(meta)
    return jsonify({'upload_uuid': upload_uuid}), 201


def _load_owned_session(upload_uuid):
    """Load a chunked session's meta, enforcing existence and ownership.

    Returns (meta, None) on success or (None, error_response) otherwise.  A
    session created by a different user is reported as 404 so it is
    indistinguishable from a nonexistent one.
    """
    try:
        meta = _chunked.read_meta(upload_uuid)
    except _chunked.ChunkSessionCorrupt:
        return None, _chunk_error('Upload session corrupt', 500)
    if meta is None or meta.get('creator_user_id') != current_user.id:
        return None, _chunk_error('Upload session not found', 404)
    return meta, None


@blueprint.route('/items/<string:item_id>/artefacts/chunked/<string:upload_uuid>/chunk/<int:chunk_index>',
                 methods=['POST'])
@login_required
@require_permission('read_write')
def chunked_upload_chunk(item_id, upload_uuid, chunk_index):
    """Receive a single chunk (raw octet-stream body, CSRF via X-CSRFToken)."""
    meta, error = _load_owned_session(upload_uuid)
    if error:
        return error
    # Once finalise has been claimed the session is immutable.
    if meta.get('finalize_state') is not None:
        return _chunk_error('Upload is being finalised; no more chunks accepted', 409)
    if chunk_index < 0 or chunk_index >= meta['total_chunks']:
        return _chunk_error('chunk_index out of range', 400)
    _chunked.write_chunk(upload_uuid, chunk_index, request.data)
    return jsonify({'received': True, 'chunk': chunk_index})


@blueprint.route('/items/<string:item_id>/artefacts/chunked/<string:upload_uuid>/status',
                 methods=['GET'])
@login_required
@require_permission('read_write')
def chunked_upload_status(item_id, upload_uuid):
    """Report which chunks have arrived so the client can resume."""
    meta, error = _load_owned_session(upload_uuid)
    if error:
        return error
    return jsonify({
        'upload_uuid': upload_uuid,
        'total_chunks': meta['total_chunks'],
        'received_chunks': _chunked.received_chunks(upload_uuid),
    })


def _resolve_chunk_artefact_type(meta):
    """Resolve (artefact_type, type_overridden, original_filename) from meta.

    Returns ((…tuple…), None) or (None, error_response) on an invalid stored
    artefact_type override.  Shared by the sync and async /complete paths and
    by /complete/status (re-drive).
    """
    original_filename = safe_original_filename(meta['filename']) or 'unnamed'
    type_override = meta.get('artefact_type', 'auto')
    type_overridden = False
    if type_override and type_override != 'auto':
        try:
            artefact_type = ArtefactType(type_override)
            type_overridden = True
        except ValueError:
            return None, _chunk_error(f'Invalid artefact_type: {type_override}')
    else:
        artefact_type = detect_artefact_type(original_filename)
    return (artefact_type, type_overridden, original_filename), None


def _build_chunk_finalize_fn(meta, artefact_type, type_overridden, original_filename):
    """Build the ingest closure run off-thread by the async finalise runner.

    Captures only primitives (the request's authorisation has already happened
    on the request thread) so it is safe to run in the pool thread under a fresh
    app context; re-resolves the Item there and returns the artefact's UUID.
    Mirrors the synchronous ingest below.
    """
    item_uuid = meta['item_uuid']
    label = meta['label']
    description = meta.get('description')
    hints = meta.get('hints') or None
    owner_id = meta.get('creator_user_id')
    is_private = bool(meta.get('is_private', False))
    auto_analyse = bool(meta.get('auto_analyse', True))
    mime_type, _ = mimetypes.guess_type(original_filename)
    web_priority = current_app.config.get('WEB_UI_ANALYSIS_PRIORITY', ANALYSIS_PRIORITY_HIGH)

    def _finalize(assembled):
        item = db.session.scalar(db.select(Item).filter_by(uuid=item_uuid))
        if item is None:
            raise RuntimeError(f'Item {item_uuid} no longer exists')
        outcome = ingest_uploaded_artefact(
            item,
            label=label,
            artefact_type=artefact_type,
            type_overridden=type_overridden,
            original_filename=original_filename,
            storage_name=assembled.storage_name,
            file_size=assembled.file_size,
            md5=assembled.md5,
            sha256=assembled.sha256,
            description=description,
            mime_type=mime_type,
            owner_id=owner_id,
            is_private=is_private,
            hints=hints,
            queue=QUEUE_FULL if auto_analyse else QUEUE_CHECKSUM_ONLY,
            priority=web_priority,
        )
        return outcome.artefact.uuid

    return _finalize


@blueprint.route('/items/<string:item_id>/artefacts/chunked/<string:upload_uuid>/complete',
                 methods=['POST'])
@login_required
@require_permission('read_write')
def chunked_upload_complete(item_id, upload_uuid):
    """Assemble the chunks and create the artefact.

    Synchronously by default (returns a redirect URL with 201); when the request
    body sets ``"async": true`` the assembly runs on the background pool and the
    endpoint returns 202 with a status_url the client polls via
    /complete/status, keeping a large assembly off the request thread.
    """
    meta, error = _load_owned_session(upload_uuid)
    if error:
        return error

    total_chunks = meta['total_chunks']
    if not isinstance(total_chunks, int) or total_chunks < 1 or total_chunks > _chunked.MAX_TOTAL_CHUNKS:
        return _chunk_error('Upload session corrupt', 400)
    missing = _chunked.missing_chunks(upload_uuid, total_chunks)
    if missing:
        return _chunk_error(f'Missing chunks: {missing}', 400)

    # Re-resolve and re-authorise the target item at completion time.
    item, error = _resolve_chunk_target_item(None, meta.get('item_uuid'))
    if error:
        return error

    resolved, error = _resolve_chunk_artefact_type(meta)
    if error:
        return error
    artefact_type, type_overridden, original_filename = resolved

    data = request.get_json(silent=True) or {}
    if data.get('async'):
        finalize_fn = _build_chunk_finalize_fn(
            meta, artefact_type, type_overridden, original_filename)
        if _chunked.claim_finalize(upload_uuid):
            _chunked.submit_finalize(upload_uuid, finalize_fn)
        status = _chunked.finalize_status(upload_uuid) or {'state': 'pending'}
        return jsonify({
            'upload_uuid': upload_uuid,
            'state': status['state'],
            'status_url': request.path + '/status',
        }), 202

    try:
        assembled = _chunked.assemble_to_storage(
            upload_uuid, original_filename,
            total_chunks=total_chunks, max_size=_chunked.max_upload_size())
    except _chunked.UploadTooLarge as exc:
        return _chunk_error(str(exc), 413)
    except _chunked.StorageUnavailable as exc:
        return _chunk_error(f'Storage backend unavailable: {exc}', 503)

    mime_type, _ = mimetypes.guess_type(original_filename)
    auto_analyse = bool(meta.get('auto_analyse', True))
    web_priority = current_app.config.get('WEB_UI_ANALYSIS_PRIORITY', ANALYSIS_PRIORITY_HIGH)

    outcome = ingest_uploaded_artefact(
        item,
        label=meta['label'],
        artefact_type=artefact_type,
        type_overridden=type_overridden,
        original_filename=original_filename,
        storage_name=assembled.storage_name,
        file_size=assembled.file_size,
        md5=assembled.md5,
        sha256=assembled.sha256,
        description=meta.get('description'),
        mime_type=mime_type,
        owner_id=meta.get('creator_user_id'),
        is_private=bool(meta.get('is_private', False)),
        hints=meta.get('hints') or None,
        queue=QUEUE_FULL if auto_analyse else QUEUE_CHECKSUM_ONLY,
        priority=web_priority,
    )
    artefact = outcome.artefact
    redirect_url = url_for(
        f'{ROUTENAME}.view', item_id=item.url_id, artefact_id=artefact.url_slug)
    return jsonify({'redirect': redirect_url}), 201


@blueprint.route('/items/<string:item_id>/artefacts/chunked/<string:upload_uuid>/complete/status',
                 methods=['GET'])
@login_required
@require_permission('read_write')
def chunked_upload_complete_status(item_id, upload_uuid):
    """Poll the result of an asynchronous finalise.

    Returns 202 {state: pending|assembling} while working, 200 {state: done,
    redirect} once the artefact exists, or 200 {state: failed, error}.  A stale
    (orphaned) assembly is re-driven here so an upload never gets stuck.
    """
    meta, error = _load_owned_session(upload_uuid)
    if error:
        return error
    status = _chunked.finalize_status(upload_uuid)
    if status is None:
        return _chunk_error('Upload session not found', 404)

    state = status['state']
    if state == _chunked.FINALIZE_DONE:
        artefact = db.session.scalar(
            db.select(Artefact).filter_by(uuid=status.get('artefact_uuid')))
        if artefact is None:
            return _chunk_error('Finalised artefact not found', 404)
        item = artefact.item
        redirect_url = url_for(
            f'{ROUTENAME}.view', item_id=item.url_id, artefact_id=artefact.url_slug)
        return jsonify({'state': 'done', 'redirect': redirect_url}), 200
    if state == _chunked.FINALIZE_FAILED:
        return jsonify({
            'state': 'failed',
            'error': status.get('error'),
            'error_code': status.get('error_code'),
        }), 200

    # pending or assembling: re-drive a stale (orphaned) assembly.  Re-check
    # contribution rights first — re-drive ingests the artefact just as /complete
    # does, so it must enforce the same authorisation rather than trusting the
    # session-ownership check alone (the item's visibility or the user's rights
    # may have changed since /complete).
    if state == _chunked.FINALIZE_ASSEMBLING and _chunked.finalize_is_stale(upload_uuid):
        item, auth_error = _resolve_chunk_target_item(None, meta.get('item_uuid'))
        if auth_error:
            return auth_error
        resolved, type_error = _resolve_chunk_artefact_type(meta)
        if not type_error and _chunked.claim_finalize(upload_uuid):
            artefact_type, type_overridden, original_filename = resolved
            _chunked.submit_finalize(
                upload_uuid,
                _build_chunk_finalize_fn(
                    meta, artefact_type, type_overridden, original_filename))
    return jsonify({'upload_uuid': upload_uuid, 'state': state}), 202


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/edit', methods=['GET', 'POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/edit', methods=['GET', 'POST'], endpoint='edit_nested')
@blueprint.route('/artefacts/<string:uuid>/edit', methods=['GET', 'POST'], endpoint='edit_legacy')
@login_required
@require_permission('read_write')
def edit(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Edit artefact metadata."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    _require_manage_artefact_content(artefact)

    if request.method == 'GET' and uuid is None and item_id is not None:
        endpoint = f'{ROUTENAME}.edit_nested' if root_id is not None else f'{ROUTENAME}.edit'
        redir = _canonical_redirect(endpoint, artefact.item, item_id, artefact, artefact_id, root_id)
        if redir:
            return redir

    form = ArtefactEditForm(obj=artefact)

    # Build type choices with auto-detect as the first option so an override
    # can be reverted back to filename-based detection.
    type_choices = [('auto', '-- Auto-detect --')]
    type_choices.extend([(t.value, _type_display_name(t)) for t in ArtefactType])
    form.artefact_type.choices = type_choices

    # Curators on the parent item can also toggle artefact privacy.
    can_priv = can_manage_privacy(artefact, current_user) or (
        artefact.item is not None and can_curate_item(artefact.item, current_user)
    )
    can_own = can_change_owner(artefact, current_user)
    if can_own:
        form.owner_id.choices = [(0, '-- No owner --')] + [
            (u.id, u.username) for u in User.query.order_by(User.username).all()
        ]
    else:
        del form['owner_id']

    if request.method == 'GET':
        form.artefact_type.data = (
            enum_value(artefact.artefact_type, 'auto') if artefact.type_overridden else 'auto'
        )
        form.tags.data = ', '.join(t.name for t in artefact.tags)
        form.is_private.data = artefact.is_private
        if can_own:
            form.owner_id.data = artefact.owner_id or 0

    if form.validate_on_submit():
        artefact.label = form.label.data
        artefact.slug = ensure_unique_slug(
            generate_slug(artefact.label), Artefact,
            existing_id=artefact.id, scope_filter={'item_id': artefact.item_id},
        )
        if form.artefact_type.data == 'auto':
            # Revert to filename-based detection and clear the override flag.
            new_type = detect_artefact_type(artefact.original_filename)
            artefact.artefact_type = new_type
            artefact.type_overridden = False
        else:
            new_type = ArtefactType(form.artefact_type.data)
            if new_type != artefact.artefact_type:
                artefact.artefact_type = new_type
                artefact.type_overridden = True
        artefact.description = form.description.data

        # Owner reassignment (owner or admin only), applied before the privacy
        # claim so an explicit choice wins over auto-claim.
        if can_own:
            artefact.owner_id = form.owner_id.data or None

        # Privacy toggle (owner, admin, or anyone claiming an unowned artefact).
        if can_priv:
            if form.is_private.data and artefact.owner_id is None:
                artefact.owner_id = current_user.id
            artefact.is_private = form.is_private.data

        artefact.tags.clear()
        if form.tags.data:
            tag_names = [t.strip() for t in form.tags.data.split(',') if t.strip()]
            existing = {t.name: t for t in Tag.query.filter(Tag.name.in_(tag_names)).all()}
            for tag_name in tag_names:
                tag = existing.get(tag_name)
                if not tag:
                    tag = Tag(name=tag_name)
                    db.session.add(tag)
                artefact.tags.append(tag)

        db.session.commit()

        flash(f'Artefact "{artefact.label}" updated.', 'success')
        return _redirect_to_artefact_view(artefact)

    return render_template('artefacts/edit.html',
                           form=form,
                           artefact=artefact,
                           item=artefact.item,
                           all_tags=Tag.all_for_picker(),
                           can_set_private=can_priv)




@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/move', methods=['POST'])
@login_required
@require_permission('read_write')
def move(item_id=None, artefact_id=None):
    """Move a root artefact to a different item."""
    artefact = _get_artefact_or_404(item_id, artefact_id)

    target_uuid = request.form.get('target_item_uuid')
    if not target_uuid:
        flash('No target item selected.', 'danger')
        return _redirect_to_artefact_view(artefact)

    target_item = lookup_by_identifier(Item, target_uuid)
    if not can_view_item(target_item, current_user):
        flash('Target item not found.', 'danger')
        return _redirect_to_artefact_view(artefact)

    try:
        validate_artefact_move(artefact, target_item, current_user)
    except ArtefactMoveError as e:
        if e.code == 'source_forbidden':
            abort(403)
        flash(str(e), 'warning' if e.code == 'same_item' else 'danger')
        return _redirect_to_artefact_view(artefact)

    old_item_name = artefact.item.name
    move_artefact_to_item(artefact, target_item)

    flash(f'Artefact "{artefact.label}" moved from "{old_item_name}" to "{target_item.name}".', 'success')
    return _redirect_to_artefact_view(artefact)




@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/delete', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/delete', methods=['POST'], endpoint='delete_nested')
@blueprint.route('/artefacts/<string:uuid>/delete', methods=['POST'], endpoint='delete_legacy')
@login_required
@require_permission('read_write')
def delete(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Delete an artefact and its file."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    if artefact.item.private_effective and not can_change_owner(artefact.item, current_user):
        abort(403)
    item_url_id = artefact.item.url_id
    label = artefact.label

    # Delete files for this artefact and all derived artefacts.
    delete_artefact_files(artefact)

    # Clean up analysis outputs (extraction trees, visualisations, cache).
    from arcology_shared.storage import S3Storage
    storage = current_app.storage
    if isinstance(storage, S3Storage):
        cleanup_artefact_outputs_s3(artefact, storage)
    else:
        cleanup_artefact_outputs(artefact, current_app.logger)

    db.session.delete(artefact)
    db.session.commit()

    flash(f'Artefact "{label}" deleted.', 'success')
    return redirect(url_for('myapp_blueprints_items.view', uuid=item_url_id))


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/download')
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/download', endpoint='download_nested')
@blueprint.route('/artefacts/<string:uuid>/download', endpoint='download_legacy')
@public_downloadable
def download(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Download the artefact file.  Blocked when the artefact itself is
    restricted, or when any extracted file within it carries a restriction the
    user cannot bypass."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)

    restriction_redirect = _check_download_restrictions(artefact)
    if restriction_redirect:
        return restriction_redirect

    restriction_redirect = _check_artefact_file_restrictions(artefact)
    if restriction_redirect:
        return restriction_redirect

    response = serve_artefact_file(artefact)
    if response is None:
        abort(404, description='File not found')
    return response


@blueprint.route('/files/<string:uuid>/download', endpoint='download_file')
@public_downloadable
def download_file(uuid):
    """Download an individual extracted file from a partition.

    Honours artefact-level restrictions first, then file-level restrictions
    (including any restrictions on nested descendants of the file).
    """
    ef = ExtractedFile.query.filter_by(uuid=uuid).first_or_404()
    artefact = ef.partition.artefact
    if not can_view_artefact(artefact, current_user):
        abort(404)

    restriction_redirect = _check_download_restrictions(artefact)
    if restriction_redirect:
        return restriction_redirect

    restriction_redirect = _check_file_download_restrictions(ef)
    if restriction_redirect:
        return restriction_redirect

    response = serve_extracted_file(ef)
    if response is None:
        abort(404, description='Extracted file not found on disk')
    return response


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/bypass', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/bypass', methods=['POST'], endpoint='grant_bypass_nested')
@login_required
def grant_bypass(item_id=None, artefact_id=None, root_id=None):
    """Grant a per-artefact restriction bypass to a user (admin only)."""
    if not current_user.is_admin:
        abort(403)
    _, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    user_id = request.form.get('user_id', type=int)
    rtype_value = request.form.get('restriction_type', '')
    reason = request.form.get('reason', '').strip() or None
    if not user_id or not rtype_value:
        flash('User and restriction type are required.', 'danger')
        return _redirect_to_artefact_view(artefact)
    try:
        rtype = RestrictionType(rtype_value)
    except ValueError:
        flash('Invalid restriction type.', 'danger')
        return _redirect_to_artefact_view(artefact)
    # A bypass only makes sense for a restriction the artefact actually carries,
    # either at artefact level or on one of its extracted files.
    if rtype not in grantable_bypass_rtypes(artefact):
        flash(f'This artefact has no {rtype.label} restriction to bypass.', 'danger')
        return _redirect_to_artefact_view(artefact)
    target_user = db.get_or_404(User, user_id)
    existing = UserArtefactBypass.query.filter_by(
        user_id=target_user.id, artefact_id=artefact.id, restriction_type=rtype
    ).first()
    if existing:
        flash(f'{target_user.username} already has a bypass for {rtype.label} on this artefact.', 'warning')
    else:
        db.session.add(UserArtefactBypass(
            user_id=target_user.id,
            artefact_id=artefact.id,
            restriction_type=rtype,
            reason=reason,
            granted_by_id=current_user.id,
        ))
        db.session.commit()
        flash(f'Granted {target_user.username} download access ({rtype.label}) for this artefact.', 'success')
    return _redirect_to_artefact_view(artefact)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/bypass/<int:bypass_id>/revoke', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/bypass/<int:bypass_id>/revoke', methods=['POST'], endpoint='revoke_bypass_nested')
@login_required
def revoke_bypass(item_id=None, artefact_id=None, root_id=None, bypass_id=None):
    """Revoke a per-artefact restriction bypass (admin only)."""
    if not current_user.is_admin:
        abort(403)
    _, artefact = _resolve_artefact(item_id, artefact_id, root_id)
    bypass = UserArtefactBypass.query.filter_by(id=bypass_id, artefact_id=artefact.id).first_or_404()
    username = bypass.user.username
    rtype_label = bypass.restriction_type.label
    db.session.delete(bypass)
    db.session.commit()
    flash(f'Revoked {username}\'s {rtype_label} bypass for this artefact.', 'success')
    return _redirect_to_artefact_view(artefact)


def _apply_restriction_action(model, fk_filter: dict, noun: str) -> None:
    """Apply an add/remove/update restriction action from the submitted form.

    Shared state machine for artefact-level (ArtefactRestriction) and
    file-level (ExtractedFileRestriction) restrictions: *model* is the
    restriction class, *fk_filter* the owning-row filter (e.g.
    ``{'artefact_id': artefact.id}``), and *noun* the flash-message prefix
    ('Restriction' / 'File restriction').

    Policy enforced here, in one place for both kinds: non-admins may only
    remove or edit restrictions they added themselves, and a duplicate
    restriction type on the same owner is rejected.

    Flashes the outcome; the caller is responsible for the redirect.
    """
    action = request.form.get('action', '')
    category = request.form.get('category', '')
    reason = request.form.get('reason', '').strip() or None

    try:
        rtype = RestrictionType(category)
    except (ValueError, KeyError):
        flash(f'Invalid restriction type: {category}', 'danger')
        return

    existing = model.query.filter_by(restriction_type=rtype, **fk_filter).first()

    if action == 'add':
        if not existing:
            db.session.add(model(
                restriction_type=rtype,
                reason=reason,
                added_by_id=current_user.id,
                **fk_filter,
            ))
            db.session.commit()
            flash(f'{noun} added: {rtype.label}', 'success')
        else:
            flash(f'{noun} already exists: {rtype.label}', 'info')
    elif action == 'remove':
        if existing:
            # Non-admins can only remove restrictions they added themselves
            if not current_user.is_admin and existing.added_by_id != current_user.id:
                flash('Only administrators can remove restrictions added by other users.', 'danger')
            else:
                db.session.delete(existing)
                db.session.commit()
                flash(f'{noun} removed: {rtype.label}', 'success')
        else:
            flash(f'{noun} not found: {rtype.label}', 'warning')
    elif action == 'update':
        new_category = request.form.get('new_category', '').strip()
        try:
            new_rtype = RestrictionType(new_category) if new_category else rtype
        except (ValueError, KeyError):
            flash(f'Invalid restriction type: {new_category}', 'danger')
            return
        if not existing:
            flash(f'{noun} not found: {rtype.label}', 'warning')
        elif not current_user.is_admin and existing.added_by_id != current_user.id:
            flash('Only administrators can edit restrictions added by other users.', 'danger')
        elif new_rtype != rtype and model.query.filter_by(
            restriction_type=new_rtype, **fk_filter
        ).first():
            flash(f'A {new_rtype.label} restriction already exists.', 'danger')
        else:
            existing.restriction_type = new_rtype
            existing.reason = reason
            db.session.commit()
            flash(f'{noun} updated.', 'success')
    else:
        flash(f'Invalid action: {action}', 'danger')


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/restrictions', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/restrictions', methods=['POST'], endpoint='manage_restrictions_nested')
@login_required
@require_permission('read_write')
def manage_restrictions(item_id=None, artefact_id=None, root_id=None):
    """Add or remove a download restriction on an artefact."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id)
    _require_manage_artefact_content(artefact)

    _apply_restriction_action(
        ArtefactRestriction, {'artefact_id': artefact.id}, 'Restriction')
    return _redirect_to_artefact_view(artefact)


@blueprint.route('/files/<string:uuid>/restrictions', methods=['POST'], endpoint='manage_file_restrictions')
@login_required
@require_permission('read_write')
def manage_file_restrictions(uuid):
    """Add or remove a restriction on an individual extracted file."""
    ef = ExtractedFile.query.filter_by(uuid=uuid).first_or_404()
    artefact = ef.partition.artefact
    if not can_view_artefact(artefact, current_user):
        abort(404)
    _require_manage_artefact_content(artefact)

    _apply_restriction_action(
        ExtractedFileRestriction, {'extracted_file_id': ef.id}, 'File restriction')
    # Always redirect to the root artefact so the user lands on the page that
    # shows the global restriction, even when the file belongs to a derived
    # artefact whose partition is displayed inline on the root's view.
    return _redirect_to_artefact_view(artefact.root_artefact)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/analyse', methods=['GET', 'POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/analyse', methods=['GET', 'POST'], endpoint='analyse_nested')
@blueprint.route('/artefacts/<string:uuid>/analyse', methods=['GET', 'POST'], endpoint='analyse_legacy')
@login_required
@require_permission('read_write')
def analyse(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Re-run analysis on an artefact, clearing all previous results first."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    _require_manage_artefact_content(artefact)
    form = AnalyseForm()

    # Platform choices for hints
    platforms = Platform.query.order_by(Platform.name).all()
    form.platform_id.choices = [(0, '-- No hint --')] + [
        (p.id, p.name) for p in platforms
    ]

    if form.validate_on_submit():
        hints = {}
        if form.platform_id.data and form.platform_id.data != 0:
            platform = db.session.get(Platform, form.platform_id.data)
            if platform:
                hints['platform'] = platform.name
        if form.filesystem_hint.data:
            hints['filesystem'] = form.filesystem_hint.data
        if form.dfi_clock_mhz.data:
            hints['dfi_clock_mhz'] = form.dfi_clock_mhz.data
        if form.notes.data:
            hints['notes'] = form.notes.data

        # Collect the previous run's output keys BEFORE the reset deletes the
        # Analysis rows they are derived from, then queue a worker-side
        # CLEANUP job for them (replaces the old fire-and-forget daemon
        # thread, which died silently on web-container restart and only
        # handled local storage).
        cleanup_keys = collect_output_cleanup_keys(artefact)
        reset_artefact_for_reanalysis(artefact)
        web_priority = current_app.config.get('WEB_UI_ANALYSIS_PRIORITY', ANALYSIS_PRIORITY_HIGH)
        # Queue the cleanup and the replacement analyses in one transaction so
        # the dispatch barrier (api.get_pending_analyses) and the jobs it guards
        # become visible together.  The barrier holds back the artefact's new
        # analyses while this CLEANUP is PENDING/RUNNING — the hard guarantee
        # that the previous run's output (notably the shared
        # outputs/.cache/<uuid> partition cache) is deleted before the new run
        # produces any.  The +1 priority keeps the cleanup near the queue front
        # so the barrier lifts promptly; it is a latency hint, not the guarantee.
        queue_storage_cleanup(cleanup_keys, artefact_id=artefact.id, commit=False,
                              priority=web_priority + 1)
        queue_analyses_for_artefact(artefact, hints if hints else None, priority=web_priority)

        flash('Re-analysis queued. Previous results have been cleared.', 'success')
        return _redirect_to_artefact_view(artefact)

    # Pre-populate form with hints from the most recent analysis that had hints.
    if request.method == 'GET':
        last_with_hints = Analysis.query.filter(
            Analysis.artefact_id == artefact.id,
            Analysis.hints.isnot(None)
        ).order_by(Analysis.id.desc()).first()
        if last_with_hints:
            try:
                last_hints = json.loads(last_with_hints.hints)
                if 'platform' in last_hints:
                    platform = Platform.query.filter_by(name=last_hints['platform']).first()
                    if platform:
                        form.platform_id.data = platform.id
                if 'filesystem' in last_hints:
                    form.filesystem_hint.data = last_hints['filesystem']
                if 'dfi_clock_mhz' in last_hints:
                    form.dfi_clock_mhz.data = last_hints['dfi_clock_mhz']
                if 'notes' in last_hints:
                    form.notes.data = last_hints['notes']
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    # Show what analyses will be queued
    pending_types = ANALYSIS_MAP.get(artefact.artefact_type, [AnalysisType.FORMAT_IDENTIFY])

    return render_template('artefacts/analyse.html',
                           form=form,
                           artefact=artefact,
                           pending_types=pending_types)


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/compute-hashes', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/compute-hashes', methods=['POST'], endpoint='compute_hashes_route_nested')
@blueprint.route('/artefacts/<string:uuid>/compute-hashes', methods=['POST'], endpoint='compute_hashes_legacy')
@login_required
@require_permission('read_write')
def compute_hashes_route(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Compute file hashes for an artefact."""
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    _require_manage_artefact_content(artefact)

    if not artefact.storage_path:
        flash('File not found — artefact has no stored file.', 'error')
        return _redirect_to_artefact_view(artefact)

    key = get_artefact_storage_key(artefact)

    try:
        md5, sha256 = compute_file_hashes(key, use_storage=True)
        obsolete_storage_paths = []
        assign_blob(
            artefact,
            artefact.storage_directory,
            artefact_blob_storage_path(artefact),
            artefact.file_size,
            sha256,
            md5,
            logical_storage_path=artefact.storage_path,
            obsolete_storage_paths=obsolete_storage_paths,
        )
        db.session.commit()
        for storage_path in obsolete_storage_paths:
            try:
                current_app.storage.delete(current_app.storage.storage_key(
                    artefact.storage_directory.value, storage_path
                ))
            except Exception:
                current_app.logger.warning(
                    "Failed to remove obsolete blob object %s/%s",
                    artefact.storage_directory.value,
                    storage_path,
                )
        flash('Hashes computed successfully.', 'success')
    except Exception as e:
        flash(f'Error computing hashes: {e}', 'error')

    return _redirect_to_artefact_view(artefact)


@blueprint.route('/files/<string:uuid>/duplicates')
@public_readable
def file_duplicates(uuid):
    """List visible extracted-file instances with identical content."""
    source = (
        ExtractedFile.query
        .join(Partition)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        # Item must be joined: artefact_visibility_clause references Item
        # columns.  Without the join SQLAlchemy adds Item as a cartesian product,
        # so the uuid-matched row passes the filter whenever *any* item is
        # visible — leaking files inside private artefacts the user cannot see.
        .join(Item, Artefact.item_id == Item.id)
        .filter(ExtractedFile.uuid == uuid)
        .filter(artefact_visibility_clause(current_user))
        .first_or_404()
    )
    if source.file_size is None or not source.sha256 or source.is_directory:
        abort(404)

    instances = (
        db.session.query(ExtractedFile, Partition, Artefact, Item)
        .join(Partition, ExtractedFile.partition_id == Partition.id)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(
            ExtractedFile.file_size == source.file_size,
            ExtractedFile.sha256 == source.sha256,
            artefact_visibility_clause(current_user),
        )
        .order_by(Item.name, Artefact.label, Partition.partition_index, ExtractedFile.path)
        .all()
    )
    return render_template(
        'artefacts/file_duplicates.html',
        source=source,
        instances=instances,
    )


@blueprint.route('/files/<string:uuid>/near-duplicates')
@public_readable
def file_near_duplicates(uuid):
    """List extracted files whose content is *near* (not exactly) identical.

    Uses the TLSH fuzzy hash to find files within a small edit distance — the
    "which one file changed?" view that complements exact /duplicates.
    """
    source = (
        ExtractedFile.query
        .join(Partition)
        .join(Artefact, Partition.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(ExtractedFile.uuid == uuid)
        .filter(artefact_visibility_clause(current_user))
        .first_or_404()
    )
    matches = similar_files_by_tlsh(source, current_user)
    return render_template(
        'artefacts/file_near_duplicates.html',
        source=source,
        matches=matches,
    )


@blueprint.route('/components/<string:uuid>/similar')
@public_readable
def component_similar(uuid):
    """Artefacts whose directory subtree (component) matches this one."""
    from ..database import ArtefactComponent
    component, artefact = (
        db.session.query(ArtefactComponent, Artefact)
        .join(Artefact, ArtefactComponent.artefact_id == Artefact.id)
        .join(Item, Artefact.item_id == Item.id)
        .filter(ArtefactComponent.uuid == uuid)
        .filter(artefact_visibility_clause(current_user))
        .first_or_404()
    )
    matches = matches_for_component(component, current_user)
    return render_template(
        'artefacts/component_similar.html',
        component=component,
        artefact=artefact,
        matches=matches,
    )


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/similar')
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/similar', endpoint='similar_nested')
@blueprint.route('/artefacts/<string:uuid>/similar', endpoint='similar_legacy')
@public_readable
def similar(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Full listing of artefacts (and shared components) similar to this one.

    Accepts the nested ``/items/<item>/artefacts/<artefact>/similar`` form (slug
    or UUID, matching the artefact view URL) as well as the flat
    ``/artefacts/<uuid>/similar``.
    """
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    matches = similar_artefacts(artefact, current_user)
    components = similar_components(artefact, current_user)
    return render_template(
        'artefacts/similar.html',
        artefact=artefact,
        matches=matches,
        components=components,
    )


@blueprint.route('/items/<string:item_id>/artefacts/<string:artefact_id>/rescan-hashes', methods=['POST'])
@blueprint.route('/items/<string:item_id>/artefacts/<string:root_id>/<string:artefact_id>/rescan-hashes', methods=['POST'], endpoint='rescan_hashes_route_nested')
@blueprint.route('/artefacts/<string:uuid>/rescan-hashes', methods=['POST'], endpoint='rescan_hashes_legacy')
@login_required
@require_permission('read_write')
def rescan_hashes_route(item_id=None, artefact_id=None, root_id=None, uuid=None):
    """Re-link extracted files to active hash databases without re-analysing."""
    from ..services.hash_rescan import rescan_hashes_for_artefact
    artefact = _get_artefact_or_404(item_id, artefact_id, root_id, uuid)
    _require_manage_artefact_content(artefact)
    updated, total = rescan_hashes_for_artefact(artefact)
    # Clear any stale FAILED HASH_RESCAN analysis rows so the status lozenge
    # reflects the successful rescan rather than the previous failure.
    Analysis.query.filter(
        Analysis.artefact_id == artefact.id,
        Analysis.analysis_type == AnalysisType.HASH_RESCAN,
        Analysis.status == AnalysisStatus.FAILED,
    ).delete(synchronize_session=False)
    db.session.commit()
    flash(f'Hash rescan complete: {updated} of {total} files updated.', 'success')
    return _redirect_to_artefact_view(artefact)


@blueprint.route('/<string:uuid>/rerun-product-recognition', methods=['POST'])
@login_required
@require_permission('read_write')
def rerun_product_recognition_route(uuid):
    """Queue PRODUCT_RECOGNITION for all partitions of an artefact without re-analysing."""
    from ..services.hash_rescan import queue_product_recognition_for_partitions
    artefact = Artefact.query.filter_by(uuid=uuid).first_or_404()
    if not can_view_artefact(artefact, current_user):
        abort(404)
    _require_manage_artefact_content(artefact)
    all_artefact_ids = [artefact.id] + get_all_derived_artefact_ids(artefact)
    partition_ids = [
        p.id for p in Partition.query.filter(
            Partition.artefact_id.in_(all_artefact_ids),
            Partition.total_files > 0,
        ).all()
    ]
    if not partition_ids:
        flash('No partitions with extracted files found.', 'warning')
        return _redirect_to_artefact_view(artefact)
    queued = queue_product_recognition_for_partitions(partition_ids)
    if queued:
        flash(f'Queued product recognition for {queued} partition(s).', 'success')
    else:
        flash('Product recognition already pending or running — nothing new queued.', 'info')
    return _redirect_to_artefact_view(artefact)


@blueprint.route('/outputs/<path:filename>')
@public_downloadable
def get_output_file(filename):
    """Serve an analysis output file (visualisation, etc.).

    Enforces artefact visibility (private artefacts' outputs must not be
    exposed) and delegates the serving to the shared downloads service.
    """
    artefact_for_check = resolve_output_artefact(filename)
    if artefact_for_check is None or not can_view_artefact(artefact_for_check, current_user):
        abort(404)

    # Download restrictions gate the original bytes; analysis outputs are a
    # rendering of the same content (e.g. a Sprite/Draw image, a text
    # conversion), so a caller who cannot bypass the artefact's restrictions
    # must not be able to read its outputs either.
    if output_blocked_for(current_user, artefact_for_check):
        abort(403)

    response = serve_output_file(filename)
    if response is None:
        abort(404)
    return response


# vim: ts=4 sw=4 et
