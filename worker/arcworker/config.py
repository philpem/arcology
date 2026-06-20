"""
Worker configuration.

Loads settings from environment variables with sensible defaults.
"""

import logging
import os
from pathlib import Path


def _int_env(name: str, default: str) -> int:
    """Parse an integer environment variable with an actionable error.

    A bare int() raises ``ValueError: invalid literal ...`` with no hint
    of which variable was malformed.
    """
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"Environment variable {name} must be an integer, got {raw!r}"
        ) from None


def validate_config() -> None:
    """Validate required settings; called from the worker entry point.

    Kept out of module import so that importing arcworker (e.g. from
    tests or tools that only want a submodule) does not kill the process
    when WORKER_API_KEY is unset.
    """
    if not WORKER_API_KEY:
        raise SystemExit("WORKER_API_KEY is not set. Exiting.")


# API and directory configuration
ARCOLOGY_API = os.environ.get('ARCOLOGY_API', 'http://host.docker.internal:5000/api')
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', '/data/uploads'))
OUTPUT_DIR = Path(os.environ.get('OUTPUT_DIR', '/data/outputs'))
# Poll backoff bounds.  When idle, the worker sleeps between API polls and
# doubles the delay up to POLL_BACKOFF_CEILING; a successful claim resets it to
# POLL_BACKOFF_FLOOR.  POLL_INTERVAL names the *ceiling* of that backoff (kept
# for backwards compatibility with existing deployments / the Dockerfile).
POLL_BACKOFF_CEILING = _int_env('POLL_INTERVAL', '10')
POLL_BACKOFF_FLOOR = float(os.environ.get('POLL_BACKOFF_FLOOR', '0.5'))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

# Worker API key for authenticating with the web application.
# Checked by validate_config() at worker startup, not at import time.
WORKER_API_KEY = os.environ.get('WORKER_API_KEY', '')

# Subprocess timeout for tool execution and decompression (seconds)
TOOL_TIMEOUT = _int_env('TOOL_TIMEOUT', '3600')

# Per-request timeout for calls to the web API (seconds)
API_TIMEOUT = _int_env('API_TIMEOUT', '30')

# Transparent retries for idempotent (GET) API requests on connection
# errors and 5xx responses.  Mutating requests are never retried at this
# layer — duplicate-side-effect protection belongs to the caller/server.
API_RETRIES = _int_env('API_RETRIES', '3')

# How often (in seconds) the worker polls the API to detect mid-job cancellation.
# Lower values detect cancellation sooner; higher values reduce API load.
CANCEL_CHECK_INTERVAL = _int_env('CANCEL_CHECK_INTERVAL', '30')

# Upper bound (in seconds) on how long the cancellation-monitor thread keeps
# sending liveness heartbeats for a single job.  The monitor's heartbeat keeps a
# RUNNING job from being treated as stale (see STALE_JOB_TIMEOUT_SECONDS), which
# is what we want for a job that is genuinely making progress.  But the monitor
# only knows the worker *process* is alive, not that the *handler* is making
# forward progress — so a handler wedged on an unbounded call (a network/storage
# read with no timeout, an infinite loop) would otherwise be kept "fresh"
# forever and never recovered.  Capping the heartbeat means a job that stops
# reporting real progress (handlers bump the timestamp directly via
# ProgressReporter, independent of this cap) becomes eligible for stale reset
# roughly HEARTBEAT_MAX_SECONDS + STALE_JOB_TIMEOUT_SECONDS after it started.
# Set generously above the longest expected *silent* (no item-progress) phase.
HEARTBEAT_MAX_SECONDS = _int_env('HEARTBEAT_MAX_SECONDS', '21600')  # 6 hours

# How often (in seconds) the worker asks the server to re-queue stale RUNNING
# jobs (those orphaned by a crash or a SIGKILL past the stop grace period).
# The server only re-queues jobs older than STALE_JOB_TIMEOUT_SECONDS, so this
# is cheap and safe to run on every live worker; it just bounds how long an
# orphaned job waits past that timeout before being retried. Set to 0 to
# disable the periodic check (recovery then only happens on worker startup).
STALE_RESET_INTERVAL = _int_env('STALE_RESET_INTERVAL', '300')

# Acorn Replay / ARMovie transcoding (REPLAY_TRANSCODE analysis).
# Directory containing RISC OS Replay decompressor modules (Decomp*/Decompress,ffd),
# passed to scotch's replay-transcode as --modules-dir.  Compressed Replay codecs
# (Moving Lines, Moving Blocks, Super Moving Blocks, …) need the original Acorn
# decompressor module to decode; those modules are proprietary and not shipped, so
# operators mount their own.  Unset (default) means only codecs that need no module
# (e.g. type 23 raw, uncompressed) can be transcoded.
REPLAY_MODULES_DIR = os.environ.get('REPLAY_MODULES_DIR', '')

# Archive extraction configuration
MAX_ARCHIVE_DEPTH = _int_env('MAX_ARCHIVE_DEPTH', '10')

# Maximum size of a decompressed file in bytes (default: 10 GiB).
# Prevents decompression-bomb inputs from exhausting disk space.
MAX_DECOMPRESSED_BYTES = _int_env('MAX_DECOMPRESSED_BYTES', str(10 * 1024 ** 3))

# Mastering detection: number of trailing tracks to scan
MASTERING_TRACK_SCAN_COUNT = _int_env('MASTERING_TRACK_SCAN_COUNT', '5')

# Job-type filter: comma-separated AnalysisType *names* this worker will accept.
# e.g. "FLUX_VISUALISATION,FLUX_DECODE"  — empty string (default) accepts all types.
WORKER_ANALYSIS_TYPES: list[str] = (
    [t.strip() for t in os.environ['WORKER_ANALYSIS_TYPES'].split(',') if t.strip()]
    if os.environ.get('WORKER_ANALYSIS_TYPES', '') else []
)

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

SENTRY_DSN = os.environ.get('SENTRY_WORKER_DSN') or os.environ.get('SENTRY_DSN', '')
SENTRY_TRACES_SAMPLE_RATE = float(
    os.environ.get('SENTRY_WORKER_TRACES_SAMPLE_RATE')
    or os.environ.get('SENTRY_TRACES_SAMPLE_RATE', '1.0')
)

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
