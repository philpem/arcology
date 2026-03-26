"""Shared web form helpers for blueprint handlers."""

from flask import flash, redirect, url_for


def flash_form_errors(form, category: str = 'error'):
    """Flash all validation errors from a WTForms form."""
    for errors in form.errors.values():
        for error in errors:
            flash(f'{error}', category)


def redirect_local(route_name: str, endpoint: str, **values):
    """Redirect to an endpoint inside a named blueprint."""
    return redirect(url_for(f'{route_name}.{endpoint}', **values))
