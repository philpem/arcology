"""
Worker configuration.

Loads settings from environment variables with sensible defaults.
"""

import os
import logging
from pathlib import Path

# API and directory configuration
ARCOLOGY_API = os.environ.get('ARCOLOGY_API', 'http://host.docker.internal:5000/api')
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', '/data/uploads'))
OUTPUT_DIR = Path(os.environ.get('OUTPUT_DIR', '/data/outputs'))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '30'))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

# Archive extraction configuration
MAX_ARCHIVE_DEPTH = int(os.environ.get('MAX_ARCHIVE_DEPTH', '10'))

# Mastering detection: number of trailing tracks to scan
MASTERING_TRACK_SCAN_COUNT = int(os.environ.get('MASTERING_TRACK_SCAN_COUNT', '5'))

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)
