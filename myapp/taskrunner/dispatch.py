"""Dispatch table mapping each control-plane AnalysisType to its in-process job.

Each handler takes the claimed ``Analysis`` row plus ``heartbeat`` /
``check_cancelled`` callbacks and returns a result dict containing a
``'summary'`` string (mirroring the worker's complete_analysis contract).  The
runner records that summary and the full dict as the analysis result.
"""

import json
from arcology_shared.enums import CONTROL_PLANE_ANALYSIS_TYPES
from ..database import AnalysisType, Artefact, Partition
from ..extensions import db
from ..services.hashdb_jobs import (
    run_hash_rescan_job,
    run_hashdb_delete_job,
    run_hashdb_link_job,
    run_hashdb_recognition_job,
    run_partition_recognition_job,
)


def _hints(analysis):
    return json.loads(analysis.hints or '{}')


def _dispatch_hash_rescan(analysis, *, heartbeat, check_cancelled):
    artefact = db.session.get(Artefact, analysis.artefact_id)
    if artefact is None:
        raise ValueError('artefact no longer exists')
    return run_hash_rescan_job(
        artefact, heartbeat=heartbeat, check_cancelled=check_cancelled)


def _dispatch_product_recognition(analysis, *, heartbeat, check_cancelled):
    partition_uuid = _hints(analysis).get('partition_uuid')
    if not partition_uuid:
        raise ValueError('no partition_uuid in analysis hints')
    partition = Partition.query.filter_by(uuid=partition_uuid).first()
    if partition is None:
        raise ValueError(f'partition {partition_uuid} no longer exists')
    return run_partition_recognition_job(
        partition, heartbeat=heartbeat, check_cancelled=check_cancelled)


def _database_id(analysis):
    db_id = _hints(analysis).get('database_id')
    if not db_id:
        raise ValueError('no database_id in analysis hints')
    return db_id


def _dispatch_hashdb_link(analysis, *, heartbeat, check_cancelled):
    return run_hashdb_link_job(
        _database_id(analysis), heartbeat=heartbeat, check_cancelled=check_cancelled)


def _dispatch_hashdb_delete(analysis, *, heartbeat, check_cancelled):
    return run_hashdb_delete_job(
        _database_id(analysis), heartbeat=heartbeat, check_cancelled=check_cancelled)


def _dispatch_hashdb_recognition(analysis, *, heartbeat, check_cancelled):
    return run_hashdb_recognition_job(
        _database_id(analysis), heartbeat=heartbeat, check_cancelled=check_cancelled)


DISPATCH = {
    AnalysisType.HASH_RESCAN: _dispatch_hash_rescan,
    AnalysisType.PRODUCT_RECOGNITION: _dispatch_product_recognition,
    AnalysisType.HASHDB_LINK: _dispatch_hashdb_link,
    AnalysisType.HASHDB_DELETE: _dispatch_hashdb_delete,
    AnalysisType.HASHDB_RECOGNITION: _dispatch_hashdb_recognition,
}

# Fail fast at import time if a control-plane type has no handler (a forgotten
# entry would otherwise silently leave its jobs stuck PENDING forever).
assert set(DISPATCH) == set(CONTROL_PLANE_ANALYSIS_TYPES), (
    'DISPATCH must cover exactly CONTROL_PLANE_ANALYSIS_TYPES; '
    f'missing={set(CONTROL_PLANE_ANALYSIS_TYPES) - set(DISPATCH)} '
    f'extra={set(DISPATCH) - set(CONTROL_PLANE_ANALYSIS_TYPES)}'
)

# vim: ts=4 sw=4 et
