"""
Arcology - Service layer

Business logic shared between the web UI blueprints, the REST API, and the
Flask CLI commands.  Modules here must not depend on request context beyond
``current_app`` (no ``request`` / ``session`` access) so they stay callable
from CLI commands and tests.
"""

# vim: ts=4 sw=4 et
