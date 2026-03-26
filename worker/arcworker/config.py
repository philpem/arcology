"""
Worker configuration.

Loads settings from environment variables with sensible defaults.
"""

import os
import sys
import logging
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

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# vim: ts=4 sw=4 et
