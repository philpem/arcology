"""Shared web form helpers for blueprint handlers."""

from flask import flash, redirect, url_for
from wtforms.validators import ValidationError
from .urls import safe_external_url


class SafeExternalUrl:
    """WTForms validator rejecting a value with a dangerous URL scheme.

    Permits empty, http/https and relative/scheme-relative URLs; rejects
    ``javascript:``/``data:``/``vbscript:`` etc. so a value that would be
    stripped at render time (see ``safe_external_url``) is caught with feedback
    at entry instead of silently failing to render as a link.
    """

    def __init__(self, message: str | None = None):
        self.message = message or 'Enter an http(s) or relative URL.'

    def __call__(self, form, field):
        if field.data and safe_external_url(field.data) is None:
            raise ValidationError(self.message)


def flash_form_errors(form, category: str = 'error'):
    """Flash all validation errors from a WTForms form."""
    for errors in form.errors.values():
        for error in errors:
            flash(f'{error}', category)


def redirect_local(route_name: str, endpoint: str, **values):
    """Redirect to an endpoint inside a named blueprint."""
    return redirect(url_for(f'{route_name}.{endpoint}', **values))
