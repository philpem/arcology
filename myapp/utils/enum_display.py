"""Safe enum value access for display and serialisation.

``artefact_type`` and ``analysis_type`` use the ``_TolerantEnum`` column type
(see ``myapp.database``), which yields ``None`` for a DB value absent from the
Python enum — e.g. an orphan row left behind when a feature-branch migration is
downgraded without cleaning up its rows (NSFW_SCAN is the recurring example).

Any code that does ``member.value`` on such a column crashes with
``AttributeError: 'NoneType' object has no attribute 'value'``.  In a request
handler or template that surfaces as a 500.  ``enum_value()`` is the single
guard used by serialisers, view code, and the ``enum_value`` Jinja filter so the
fallback behaviour is consistent everywhere.
"""


def enum_value(member, default=None):
    """Return ``member.value``, or ``default`` when ``member`` is ``None``.

    ``member`` is normally an ``enum.Enum`` instance; ``None`` indicates a DB
    value the Python enum no longer knows about (orphan row).
    """
    return member.value if member is not None else default

# vim: ts=4 sw=4 et
