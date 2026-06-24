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
from arcology_shared.enums import AnalysisType
from ..database import (
    Analysis,
    AnalysisStatus,
    Artefact,
    ArtefactMastering,
    ArtefactProtection,
    MediaFile,
    OutputBlob,
    Partition,
    ReplayMovie,
    RiscosModule,
    StorageDirectory,
)
from ..extensions import db
from ..utils.blobs import get_or_create_blob
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


def handle_replay_movies(analysis: Analysis, details: dict,
                          full_rebuild: bool = False) -> None:
    """Rebuild ReplayMovie rows from a REPLAY_PROCESS result.

    Same scoped-deletion semantics as ``handle_riscos_modules`` (see there for
    the path_prefix / full_rebuild race-avoidance rationale).
    """
    path_prefix = details.get('path_prefix', '')

    if full_rebuild:
        ReplayMovie.query.filter_by(artefact_id=analysis.artefact_id).delete()
    elif path_prefix:
        ReplayMovie.query.filter(
            ReplayMovie.artefact_id == analysis.artefact_id,
            ReplayMovie.file_path.like(path_prefix + '/%'),
        ).delete(synchronize_session=False)
    elif details.get('movies'):
        ReplayMovie.query.filter_by(artefact_id=analysis.artefact_id).delete()

    _title_max = ReplayMovie.__table__.c.title.type.length
    _author_max = ReplayMovie.__table__.c.author.type.length
    _copyright_max = ReplayMovie.__table__.c.copyright.type.length
    _path_max = ReplayMovie.__table__.c.file_path.type.length
    _vlabel_max = ReplayMovie.__table__.c.video_label.type.length

    def _truncate(value, limit):
        if value is None:
            return None
        return value[:limit]

    for mov in details.get('movies', []):
        file_path = mov.get('file_path', '')
        if file_path and len(file_path) > _path_max:
            current_app.logger.warning(
                f"Skipping ARMovie with oversized file_path ({len(file_path)}): {file_path}"
            )
            continue
        # ARMovie titles may legitimately be empty — index the row regardless.
        db.session.add(ReplayMovie(
            artefact_id=analysis.artefact_id,
            file_path=file_path,
            title=_truncate(mov.get('title'), _title_max),
            author=_truncate(mov.get('author'), _author_max),
            copyright=_truncate(mov.get('copyright'), _copyright_max),
            video_format=mov.get('video_format'),
            video_label=_truncate(mov.get('video_label'), _vlabel_max),
            width=mov.get('width'),
            height=mov.get('height'),
            pixel_depth=mov.get('pixel_depth'),
            frame_rate=mov.get('frame_rate'),
            sound_format=mov.get('sound_format'),
            sound_rate=mov.get('sound_rate'),
            sound_channels=mov.get('sound_channels'),
            sound_precision=mov.get('sound_precision'),
            frames_per_chunk=mov.get('frames_per_chunk'),
            number_of_chunks=mov.get('number_of_chunks'),
            duration_seconds=mov.get('duration_seconds'),
        ))


def _link_transcode_blobs(entry: dict) -> tuple[int | None, int | None]:
    """Return ``(mp4_output_blob_id, poster_blob_id)`` for a transcode entry.

    Content-keyed media transcoding stores its MP4/poster under a shared,
    content-addressed path; these refcounting ``OutputBlob`` rows are what let
    the storage GC reclaim the bytes only once nothing references them.  On a
    cache hit the blob already exists and is found by its (unique) storage path;
    on a miss it is created from the worker-reported output hash.  Legacy entries
    that predate content-addressed transcoding carry no ``input_sha256`` and are
    left unlinked — they keep their artefact-scoped path string and per-prefix GC.
    """
    if not entry.get('input_sha256'):
        return None, None

    def _blob_id(path, file_size, sha256):
        if not path:
            return None
        blob = OutputBlob.query.filter_by(storage_path=path).first()
        if blob is None and file_size is not None and sha256:
            blob, _ = get_or_create_blob(
                StorageDirectory.OUTPUTS, path, file_size, sha256)
        return blob.id if blob is not None else None

    mp4_id = _blob_id(entry.get('mp4_output_path'),
                      entry.get('mp4_file_size'), entry.get('mp4_sha256'))
    poster_id = _blob_id(entry.get('poster_path'),
                         entry.get('poster_file_size'), entry.get('poster_sha256'))
    return mp4_id, poster_id


def handle_replay_transcode(analysis: Analysis, details: dict,
                            full_rebuild: bool = False) -> None:
    """Attach transcoded MP4 / poster paths to existing ReplayMovie rows.

    Update-only (never inserts or deletes): the rows are created by the
    REPLAY_PROCESS analysis, which always completes — and has its result
    indexed — before the REPLAY_TRANSCODE job it queues can run.  In the
    ``rebuild_all`` batch pass, REPLAY_PROCESS precedes REPLAY_TRANSCODE in
    ``_HANDLER_MAP`` order, so the rows likewise exist by the time this runs.
    A movie with no matching row (transcode without a recorded parse) is simply
    skipped — re-running rebuild-search-index repairs it.
    """
    _mp4_max = ReplayMovie.__table__.c.mp4_output_path.type.length
    _poster_max = ReplayMovie.__table__.c.poster_path.type.length

    def _truncate(value, limit):
        return value[:limit] if value else None

    for entry in details.get('transcoded', []):
        file_path = entry.get('file_path')
        if not file_path:
            continue
        mp4_blob_id, poster_blob_id = _link_transcode_blobs(entry)
        ReplayMovie.query.filter_by(
            artefact_id=analysis.artefact_id,
            file_path=file_path,
        ).update(
            {
                'mp4_output_path': _truncate(entry.get('mp4_output_path'), _mp4_max),
                'poster_path': _truncate(entry.get('poster_path'), _poster_max),
                'mp4_output_blob_id': mp4_blob_id,
                'poster_blob_id': poster_blob_id,
            },
            synchronize_session=False,
        )


def handle_media_transcode(analysis: Analysis, details: dict,
                           full_rebuild: bool = False) -> None:
    """Rebuild MediaFile rows from a MEDIA_TRANSCODE result.

    Unlike Replay (where REPLAY_PROCESS creates the rows and REPLAY_TRANSCODE
    only updates them), MEDIA_TRANSCODE is the *only* analysis touching
    media_files, so this handler inserts the rows.  Same scoped-deletion
    semantics as ``handle_replay_movies``.

    Entries with no ``file_path`` come from a *direct* media upload (the
    artefact is itself the media); they are indexed with ``file_path = NULL`` so
    the viewer can offer an artefact-level player.  Native uploads transcode to
    nothing (``transcoded`` empty) and produce no rows — they stream directly.
    """
    path_prefix = details.get('path_prefix', '')
    transcoded = details.get('transcoded', [])

    if full_rebuild:
        MediaFile.query.filter_by(artefact_id=analysis.artefact_id).delete()
    elif path_prefix:
        MediaFile.query.filter(
            MediaFile.artefact_id == analysis.artefact_id,
            MediaFile.file_path.like(path_prefix + '/%'),
        ).delete(synchronize_session=False)
    elif transcoded:
        MediaFile.query.filter_by(artefact_id=analysis.artefact_id).delete()

    cols = MediaFile.__table__.c
    _path_max = cols.file_path.type.length
    _kind_max = cols.media_kind.type.length
    _container_max = cols.container_format.type.length
    _vcodec_max = cols.video_codec.type.length
    _acodec_max = cols.audio_codec.type.length
    _out_max = cols.mp4_output_path.type.length
    _poster_max = cols.poster_path.type.length

    def _truncate(value, limit):
        return value[:limit] if value else None

    for entry in transcoded:
        file_path = entry.get('file_path')
        if file_path and len(file_path) > _path_max:
            current_app.logger.warning(
                f"Skipping media file with oversized file_path ({len(file_path)}): {file_path}"
            )
            continue
        mp4_blob_id, poster_blob_id = _link_transcode_blobs(entry)
        db.session.add(MediaFile(
            artefact_id=analysis.artefact_id,
            file_path=file_path,
            media_kind=_truncate(entry.get('media_kind'), _kind_max),
            container_format=_truncate(entry.get('container_format'), _container_max),
            video_codec=_truncate(entry.get('video_codec'), _vcodec_max),
            width=entry.get('width'),
            height=entry.get('height'),
            frame_rate=entry.get('frame_rate'),
            audio_codec=_truncate(entry.get('audio_codec'), _acodec_max),
            sample_rate=entry.get('sample_rate'),
            channels=entry.get('channels'),
            has_audio=entry.get('has_audio'),
            duration_seconds=entry.get('duration_seconds'),
            mp4_output_path=_truncate(entry.get('mp4_output_path'), _out_max),
            poster_path=_truncate(entry.get('poster_path'), _poster_max),
            mp4_output_blob_id=mp4_blob_id,
            poster_blob_id=poster_blob_id,
        ))


# =============================================================================
# High-level entry point (used by the API on analysis completion)
# =============================================================================

_HANDLER_MAP = {
    AnalysisType.DISC_PROTECTION_DETECT: handle_protection,
    AnalysisType.DISC_MASTERING_DETECT:  handle_mastering,
    AnalysisType.PARTITION_DETECT:       handle_partition_detect,
    AnalysisType.RISCOS_MODULE_PARSE:    handle_riscos_modules,
    AnalysisType.REPLAY_PROCESS:         handle_replay_movies,
    # Must come after REPLAY_PROCESS — it updates the rows that handler creates.
    AnalysisType.REPLAY_TRANSCODE:       handle_replay_transcode,
    AnalysisType.MEDIA_TRANSCODE:        handle_media_transcode,
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
            # full_rebuild=True for the per-file handlers so the CLI's batch
            # pass doesn't need path-prefix logic (it rebuilds from scratch).
            if analysis_type in (AnalysisType.RISCOS_MODULE_PARSE,
                                 AnalysisType.REPLAY_PROCESS,
                                 AnalysisType.MEDIA_TRANSCODE):
                handler(analysis, details, full_rebuild=True)
            else:
                handler(analysis, details)
            count += 1
        counts[analysis_type.name] = count

    return counts

# vim: ts=4 sw=4 et
