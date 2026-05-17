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

# Archive extraction configuration
MAX_ARCHIVE_DEPTH = int(os.environ.get('MAX_ARCHIVE_DEPTH', '10'))

# Maximum size of a decompressed file in bytes (default: 10 GiB).
# Prevents decompression-bomb inputs from exhausting disk space.
MAX_DECOMPRESSED_BYTES = int(os.environ.get('MAX_DECOMPRESSED_BYTES', str(10 * 1024 ** 3)))

# Mastering detection: number of trailing tracks to scan
MASTERING_TRACK_SCAN_COUNT = int(os.environ.get('MASTERING_TRACK_SCAN_COUNT', '5'))

# Explicit-content moderation (NSFW classification)
# Set NSFW_ENABLED=false to disable all NSFW_SCAN jobs.
NSFW_ENABLED = os.environ.get('NSFW_ENABLED', 'true').lower() not in ('false', '0', 'no')
# Path to directory containing ONNX model files and *_meta.json sidecars.
NSFW_MODEL_DIR = Path(os.environ.get('NSFW_MODEL_DIR', '/opt/nsfw_models'))
# Use INT8-quantized models (smaller/faster; negligible accuracy difference).
NSFW_QUANTIZE = os.environ.get('NSFW_QUANTIZE', 'true').lower() not in ('false', '0', 'no')
# Stage-1 thresholds: score >= HIGH → explicit immediately; score <= LOW → not explicit immediately.
NSFW_HIGH = float(os.environ.get('NSFW_HIGH', '0.90'))
NSFW_LOW  = float(os.environ.get('NSFW_LOW',  '0.20'))
# Stage-2 conviction thresholds.
# NSFW_S2_THRESHOLD:   stage-2 score must reach this to convict (default 0.70 > hard-coded 0.50
#                      reduces false positives from stock/artistic photography).
# NSFW_S1_MIN_EXPLICIT: stage-1 score must reach this for stage-2's explicit verdict to count.
#                      Images where stage-1 is sceptical (< 0.40) are not convicted by stage-2
#                      alone, even if stage-2 is highly confident.
NSFW_S2_THRESHOLD    = float(os.environ.get('NSFW_S2_THRESHOLD',    '0.70'))
NSFW_S1_MIN_EXPLICIT = float(os.environ.get('NSFW_S1_MIN_EXPLICIT', '0.40'))
# Agreement conviction path: both s1 and s2 above this threshold → explicit.
# Catches images where both models are moderately confident but neither alone
# reaches the primary thresholds.  Default 0.55 (just above 0.5 for a two-class model).
NSFW_AGREE_THRESHOLD = float(os.environ.get('NSFW_AGREE_THRESHOLD', '0.55'))
# Per-crop co-located agreement gate.  All stage-2 conviction paths require at least one
# crop to satisfy ``min(s1[crop], s2[crop]) >= NSFW_COLOCATED``.  Suppresses false positives
# where the two models flag different unrelated patches of the same image.  Default 0.45.
NSFW_COLOCATED       = float(os.environ.get('NSFW_COLOCATED',       '0.45'))
# Minimum image area (pixels); images with w×h below this are skipped.
# Default 16384 ≈ 128×128.  Area-based test avoids dropping valid wide frames.
NSFW_MIN_PIXELS = int(os.environ.get('NSFW_MIN_PIXELS', '16384'))

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

# vim: ts=4 sw=4 et
