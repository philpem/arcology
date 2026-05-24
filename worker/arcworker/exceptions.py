"""
Worker exception types.
"""


class JobCancelledException(Exception):
    """Raised when the monitoring thread detects the current job has been
    cancelled or deleted server-side before the subprocess finished."""

# vim: ts=4 sw=4 et
