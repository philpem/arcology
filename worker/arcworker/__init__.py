"""
Arcology Analysis Worker Package

Modular worker for processing analysis jobs from the Arcology API.
"""

from shared.enums import AnalysisType, ArtefactType
from .analysis import AnalysisWorker
from .config import (
    ARCOLOGY_API,
    MAX_POLL,
    OUTPUT_DIR,
    S3_ACCESS_KEY,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_PUBLIC_URL,
    S3_REGION,
    S3_SECRET_KEY,
    STORAGE_BACKEND,
    UPLOAD_DIR,
    WORKER_API_KEY,
    log,
)

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
    'S3_PUBLIC_URL',
    'log',
]

# vim: ts=4 sw=4 et
