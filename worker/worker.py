#!/usr/bin/env python3
"""
Arcology Analysis Worker

Runs inside a Docker container with analysis tools installed.
Polls the Arcology API for pending jobs and processes them.

Tools required in container:
- imgviz (Fluxfox) - flux visualisation
- hxcfe (HxC Floppy Emulator) - flux conversion and visualisation
- gw (Greaseweazle) - sector image conversion
- xvfb-run + DiscImageManager - Acorn filesystem extraction
- 7z - DOS/ISO file extraction
- zstd, gzip, bzip2 - decompression

This script is the entry point. The actual implementation is in the
arcworker package which provides a modular, maintainable structure.
"""

import os as _os, sys as _sys
# Ensure the repo root (parent of this file's directory) is on sys.path so
# that 'shared' is importable when running the worker outside Docker.
_repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _repo_root not in _sys.path:
	_sys.path.insert(0, _repo_root)
del _os, _sys, _repo_root

from arcworker import AnalysisWorker, ARCOLOGY_API, UPLOAD_DIR, OUTPUT_DIR, WORKER_API_KEY


def main():
    worker = AnalysisWorker(
        api_url=ARCOLOGY_API,
        upload_dir=UPLOAD_DIR,
        output_dir=OUTPUT_DIR,
        api_key=WORKER_API_KEY
    )
    worker.run()


if __name__ == '__main__':
    main()
