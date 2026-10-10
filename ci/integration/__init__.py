# Worker analysis-pipeline integration tests.
#
# These tests run the *real* AnalysisWorker handlers with the real external
# tools against small committed fixtures, with only the HTTP API boundary
# replaced by an in-memory fake server.  They are intentionally NOT named
# ``test_*`` so the app-tests ``unittest discover -p 'test_*.py'`` job never
# picks them up — they require the worker container's tools and run via
# ``run_integration.py`` instead.

# vim: ts=4 sw=4 et
