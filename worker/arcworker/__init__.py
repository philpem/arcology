"""
Arcology Analysis Worker Package

Modular worker for processing analysis jobs from the Arcology API.
"""

from .config import (
    ARCOLOGY_API, UPLOAD_DIR, OUTPUT_DIR, MAX_POLL, WORKER_API_KEY, log,
    STORAGE_BACKEND, S3_ENDPOINT_URL, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION,
)
from shared.enums import ArtefactType, AnalysisType
from .analysis import AnalysisWorker

__all__ = [
    'AnalysisWorker',
    'ArtefactType',
    'AnalysisType',
    'ARCOLOGY_API',
    'UPLOAD_DIR',
    'OUTPUT_DIR',
    'MAX_POLL',
    'WORKER_API_KEY',
    'STORAGE_BACKEND',
    'S3_ENDPOINT_URL',
    'S3_BUCKET',
    'S3_ACCESS_KEY',
    'S3_SECRET_KEY',
    'S3_REGION',
    'log',
]

# vim: ts=4 sw=4 et
