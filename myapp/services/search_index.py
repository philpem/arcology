"""
Arcology - Search index population service

Translates completed analysis results into the structured search-index
tables that power the web search and the analysis detail views:

  ArtefactProtection   ← DISC_PROTECTION_DETECT
  ArtefactMastering    ← DISC_MASTERING_DETECT
  Partition.gnu_file_type ← PARTITION_DETECT
  RiscosModule         ← RISCOS_MODULE_PARSE

The low-level per-type handlers are exposed individually so the
``rebuild-search-index`` CLI command can batch-process all historical
analyses without the per-transaction savepoint overhead.

The high-level ``populate_search_index_from_analysis()`` is called live
by the API when the worker reports a completed analysis; it uses a
savepoint so a DB error in index population never aborts the outer
transaction that records the analysis status.
"""

import json
from flask import current_app
from sqlalchemy import select
from shared.enums import AnalysisType
from ..database import (
    Analysis,
    AnalysisStatus,
    Artefact,
    ArtefactMastering,
    ArtefactProtection,
    Partition,
    RiscosModule,
)
from ..extensions import db
from ..utils.enum_display import enum_value

# =============================================================================
# Per-type handler functions
# =============================================================================
# Each handler receives (analysis, details_dict) and performs the DB writes
# for one completed analysis.  They are side-effect-only (no return value);
# callers decide whether to flush/commit.
# =============================================================================

def handle_protection(analysis: Analysis, details: dict) -> None:
    """Rebuild ArtefactProtection rows from a DISC_PROTECTION_DETECT result."""
    ArtefactProtection.query.filter_by(artefact_id=analysis.artefact_id).delete()
    for ind in details.get('indicators', []):
        db.session.add(ArtefactProtection(
            artefact_id=analysis.artefact_id,
            protection_type=ind.get('type', 'unknown'),
            track=ind.get('track'),
            side=ind.get('side'),
            details=ind.get('sector_id') or ind.get('details'),
        ))


def handle_mastering(analysis: Analysis, details: dict) -> None:
    """Rebuild ArtefactMastering rows from a DISC_MASTERING_DETECT result."""
    ArtefactMastering.query.filter_by(artefact_id=analysis.artefact_id).delete()
    for ind in details.get('indicators', []):
        mtype = ind.get('type', 'unknown')
        if mtype == 'bcd_timestamp':
            mtype = 'formaster'
        db.session.add(ArtefactMastering(
            artefact_id=analysis.artefact_id,
            mastering_type=mtype,
            track=ind.get('track'),
            decoded=ind.get('decoded') or ind.get('data'),
        ))


def handle_partition_detect(analysis: Analysis, details: dict) -> None:
    """Update gnu_file_type on all partitions from a PARTITION_DETECT result."""
    gnu_file_type = details.get('file', {}).get('file_type')
    if gnu_file_type:
        Partition.query.filter_by(artefact_id=analysis.artefact_id).update(
            {'gnu_file_type': gnu_file_type}
        )


def handle_riscos_modules(analysis: Analysis, details: dict,
                           full_rebuild: bool = False) -> None:
    """Rebuild RiscosModule rows from a RISCOS_MODULE_PARSE result.

    ``full_rebuild=True`` deletes *all* modules for the artefact before
    reinserting (used by the rebuild CLI command which processes from a known-
    clean state).  The default (``False``) uses scoped deletion by path_prefix
    so that concurrent nested-archive jobs don't clobber each other's results.
    """
    path_prefix = details.get('path_prefix', '')

    if full_rebuild:
        RiscosModule.query.filter_by(artefact_id=analysis.artefact_id).delete()
    elif path_prefix:
        RiscosModule.query.filter(
            RiscosModule.artefact_id == analysis.artefact_id,
            RiscosModule.file_path.like(path_prefix + '/%'),
        ).delete(synchronize_session=False)
    elif details.get('modules'):
        # Only clear all when a top-level scan actually found modules
        # (disc image case). If 0 modules found with no prefix, preserve
        # rows from nested-archive scans to avoid a race where a re-analysis
        # top-level job runs before nested archives are re-extracted and
        # wipes all previously stored module data.
        RiscosModule.query.filter_by(artefact_id=analysis.artefact_id).delete()

    _title_max = RiscosModule.__table__.c.title_string.type.length
    _help_max = RiscosModule.__table__.c.help_title.type.length
    _path_max = RiscosModule.__table__.c.file_path.type.length

    for mod in details.get('modules', []):
        title = mod.get('title_string', '')
        if not title:
            continue
        help_title = mod.get('help_title')
        file_path = mod.get('file_path', '')
        if (len(title) > _title_max
                or (help_title and len(help_title) > _help_max)
                or len(file_path) > _path_max):
            current_app.logger.warning(
                "Skipping module with oversized string fields "
                f"(title={len(title)}, "
                f"help_title={len(help_title) if help_title is not None else 'None'}, "
                f"file_path={len(file_path)}): {file_path}"
            )
            continue
        commands = mod.get('commands', [])
        commands_json = json.dumps([c['name'] for c in commands]) if commands else None
        raw_swis = mod.get('swi_names')
        if raw_swis and len(raw_swis) > 1:
            swi_names_json = json.dumps([f"{raw_swis[0]}_{s}" for s in raw_swis[1:]])
        else:
            swi_names_json = None
        db.session.add(RiscosModule(
            artefact_id=analysis.artefact_id,
            title_string=title,
            help_title=help_title,
            version=mod.get('version'),
            date=mod.get('date'),
            swi_chunk=mod.get('swi_chunk'),
            file_path=file_path,
            module_hash=mod.get('hash'),
            commands=commands_json,
            swi_names=swi_names_json,
        ))


# =============================================================================
# High-level entry point (used by the API on analysis completion)
# =============================================================================

_HANDLER_MAP = {
    AnalysisType.DISC_PROTECTION_DETECT: handle_protection,
    AnalysisType.DISC_MASTERING_DETECT:  handle_mastering,
    AnalysisType.PARTITION_DETECT:       handle_partition_detect,
    AnalysisType.RISCOS_MODULE_PARSE:    handle_riscos_modules,
}


def populate_search_index_from_analysis(analysis: Analysis) -> None:
    """Update search-index tables from a completed analysis result (API path).

    Uses a savepoint so that a DB error during index population never aborts
    the outer transaction that records the analysis status.  Flushes the
    status update *before* opening the savepoint so a savepoint rollback
    cannot undo it.

    A row-level lock (SELECT … FOR UPDATE) on the artefact serialises
    concurrent completions of the same analysis type so they don't clobber
    each other's freshly-inserted rows.
    """
    if not analysis.details:
        return
    handler = _HANDLER_MAP.get(analysis.analysis_type)
    if handler is None:
        return

    try:
        details = json.loads(analysis.details)
    except (ValueError, TypeError):
        current_app.logger.warning(
            f"Could not parse details JSON for analysis {analysis.uuid} "
            f"({enum_value(analysis.analysis_type)}) — skipping search index update"
        )
        return

    try:
        db.session.flush()
        with db.session.begin_nested():
            db.session.execute(
                select(Artefact).where(Artefact.id == analysis.artefact_id).with_for_update()
            )
            handler(analysis, details)
            db.session.flush()
    except Exception:
        try:
            import sentry_sdk
            sentry_sdk.capture_exception()
        except ImportError:
            pass
        current_app.logger.exception(
            f"Error populating search index for analysis {analysis.uuid} "
            f"({enum_value(analysis.analysis_type)})"
        )


# =============================================================================
# Batch rebuild helpers (used by the rebuild-search-index CLI command)
# =============================================================================

def rebuild_all(echo=None) -> dict:
    """Process all completed analyses and rebuild the full search index.

    ``echo`` is an optional callable(message) for progress output (e.g.
    ``click.echo``).  Returns a dict of counts by analysis type name.

    Callers are responsible for calling ``db.session.commit()`` afterwards.
    """
    if echo is None:
        echo = lambda _: None  # noqa: E731

    counts = {}
    for analysis_type, handler in _HANDLER_MAP.items():
        analyses = (
            Analysis.query
            .filter_by(
                analysis_type=analysis_type,
                status=AnalysisStatus.COMPLETED,
                success=True,
            )
            .all()
        )
        echo(f"Processing {len(analyses)} {analysis_type.name} analyses...")
        count = 0
        for analysis in analyses:
            if not analysis.details:
                continue
            try:
                details = json.loads(analysis.details)
            except (ValueError, TypeError):
                echo(f"  WARNING: could not parse details for analysis {analysis.uuid}")
                continue
            # full_rebuild=True for the RISC OS modules handler so the CLI's
            # batch pass doesn't need path-prefix logic (it rebuilds from scratch).
            if analysis_type == AnalysisType.RISCOS_MODULE_PARSE:
                handler(analysis, details, full_rebuild=True)
            else:
                handler(analysis, details)
            count += 1
        counts[analysis_type.name] = count

    return counts

# vim: ts=4 sw=4 et
