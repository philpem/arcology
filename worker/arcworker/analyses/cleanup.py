"""Storage cleanup analysis handler.

CLEANUP is a maintenance job queued by the web app when an item (and all
its artefacts) is bulk-deleted, or when an artefact is re-analysed and its
previous outputs must go.  The job has no artefact — the hints JSON carries
the storage keys to delete:

    {
        "artefact_keys":       ["uploads/abc.img", ...],   # delete()
        "output_file_keys":    ["outputs/vis.png", ...],   # delete()
        "output_dir_prefixes": ["outputs/item/art/", ...], # delete_prefix()
        "cache_prefixes":      ["outputs/.cache/uuid", ...] # delete_prefix()
    }

Running this through the worker (instead of the web container's old
fire-and-forget daemon threads) means cleanup survives web restarts, is
retryable, and works identically for local and S3 storage backends.
Individual key failures are logged and counted but do not fail the job —
a missing key just means there is nothing left to delete.
"""

import json
from pathlib import Path
from arcology_shared.enums import AnalysisType
from arcology_shared.hints import HintKey
from ..config import log
from ._common import ProgressReporter, analysis_handler

# Emit a progress summary at most this often while deleting, so a large
# cleanup (hundreds of keys/prefixes) shows live progress instead of an
# opaque "In progress…" spinner.
_PROGRESS_INTERVAL_SECONDS = 5.0


@analysis_handler("storage cleanup", AnalysisType.CLEANUP)
def process_cleanup(self, analysis: dict, artefact: dict, work_dir: Path):
    """Delete the storage keys/prefixes listed in the job's hints."""
    analysis_id = analysis['id']

    try:
        hints = json.loads(analysis.get('hints') or '{}')
    except (json.JSONDecodeError, TypeError):
        self.fail_analysis(analysis_id, 'CLEANUP hints are not valid JSON')
        return

    keys = list(hints.get(HintKey.ARTEFACT_KEYS) or []) + \
        list(hints.get(HintKey.OUTPUT_FILE_KEYS) or [])
    prefixes = list(hints.get(HintKey.OUTPUT_DIR_PREFIXES) or []) + \
        list(hints.get(HintKey.CACHE_PREFIXES) or [])

    deleted = 0
    errors = 0
    processed = 0
    total = len(keys) + len(prefixes)
    reporter = ProgressReporter(
        self, analysis_id, total=total,
        min_interval=_PROGRESS_INTERVAL_SECONDS,
        label='Deleting storage objects',
    )

    for key in keys:
        try:
            self.storage.delete(key)
            deleted += 1
        except FileNotFoundError:
            pass  # already gone — nothing left to delete
        except Exception as e:
            errors += 1
            log.warning(f"Cleanup: failed to delete key {key!r}: {e}")
        processed += 1
        reporter.update(processed)

    for prefix in prefixes:
        try:
            self.storage.delete_prefix(prefix)
            deleted += 1
        except FileNotFoundError:
            pass
        except Exception as e:
            errors += 1
            log.warning(f"Cleanup: failed to delete prefix {prefix!r}: {e}")
        processed += 1
        reporter.update(processed)

    summary = f'{deleted} of {len(keys) + len(prefixes)} keys/prefixes deleted'
    if errors:
        summary += f', {errors} failed (see worker log)'

    self.complete_analysis(
        analysis_id,
        summary=summary,
        details=json.dumps({
            'keys': len(keys),
            'prefixes': len(prefixes),
            'deleted': deleted,
            'errors': errors,
        }),
    )

# vim: ts=4 sw=4 et
