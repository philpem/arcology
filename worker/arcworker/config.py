"""
Worker configuration.

Loads settings from environment variables with sensible defaults.
"""

import logging
import os
import sys
from pathlib import Path

# API and directory configuration
ARCOLOGY_API = os.environ.get('ARCOLOGY_API', 'http://host.docker.internal:5000/api')
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', '/data/uploads'))
OUTPUT_DIR = Path(os.environ.get('OUTPUT_DIR', '/data/outputs'))
MAX_POLL = int(os.environ.get('POLL_INTERVAL', '10'))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

# Worker API key for authenticating with the web application
WORKER_API_KEY = os.environ.get('WORKER_API_KEY', '')
if not WORKER_API_KEY:
    logging.critical("WORKER_API_KEY is not set. Exiting.")
    sys.exit(1)

# Subprocess timeout for tool execution and decompression (seconds)
TOOL_TIMEOUT = int(os.environ.get('TOOL_TIMEOUT', '3600'))

# How often (in seconds) the worker polls the API to detect mid-job cancellation.
# Lower values detect cancellation sooner; higher values reduce API load.
CANCEL_CHECK_INTERVAL = int(os.environ.get('CANCEL_CHECK_INTERVAL', '30'))

# Archive extraction configuration
MAX_ARCHIVE_DEPTH = int(os.environ.get('MAX_ARCHIVE_DEPTH', '10'))

# Maximum size of a decompressed file in bytes (default: 10 GiB).
# Prevents decompression-bomb inputs from exhausting disk space.
MAX_DECOMPRESSED_BYTES = int(os.environ.get('MAX_DECOMPRESSED_BYTES', str(10 * 1024 ** 3)))

# Mastering detection: number of trailing tracks to scan
MASTERING_TRACK_SCAN_COUNT = int(os.environ.get('MASTERING_TRACK_SCAN_COUNT', '5'))

# Job-type filter: comma-separated AnalysisType *names* this worker will accept.
# e.g. "FLUX_VISUALISATION,FLUX_DECODE"  — empty string (default) accepts all types.
WORKER_ANALYSIS_TYPES: list[str] = (
    [t.strip() for t in os.environ['WORKER_ANALYSIS_TYPES'].split(',') if t.strip()]
    if os.environ.get('WORKER_ANALYSIS_TYPES', '') else []
)

# Storage backend configuration: 'local' (default) or 's3'
STORAGE_BACKEND = os.environ.get('STORAGE_BACKEND', 'local')
S3_ENDPOINT_URL = os.environ.get('S3_ENDPOINT_URL', '')
S3_BUCKET = os.environ.get('S3_BUCKET', 'arcology')
S3_ACCESS_KEY = os.environ.get('S3_ACCESS_KEY', '')
S3_SECRET_KEY = os.environ.get('S3_SECRET_KEY', '')
S3_REGION = os.environ.get('S3_REGION', 'us-east-1')
S3_PUBLIC_URL = os.environ.get('S3_PUBLIC_URL', '')

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

SENTRY_DSN = os.environ.get('SENTRY_DSN', '')
SENTRY_TRACES_SAMPLE_RATE = float(os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '1.0'))

if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        send_default_pii=True,
    )
    log.info("Sentry initialised")

# vim: ts=4 sw=4 et
