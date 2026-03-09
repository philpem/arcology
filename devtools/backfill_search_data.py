#!/usr/bin/env python3
"""
Backfill search index tables from existing completed analysis results.

This is a convenience wrapper for local development. The same logic runs
automatically in Docker via the entrypoint: flask backfill-search

Usage:
    python devtools/backfill_search_data.py

The SQLALCHEMY_DATABASE_URI environment variable must be set, or a
myapp/myapp.cfg must exist with the database URI configured.
"""

import os
import sys
import subprocess

if __name__ == '__main__':
	os.chdir(os.path.join(os.path.dirname(__file__), '..'))
	sys.exit(subprocess.call([sys.executable, '-m', 'flask', 'backfill-search']))
