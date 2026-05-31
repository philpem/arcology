"""
Arcology - Help Blueprint

Serves in-app help pages rendered from Markdown source files.
All routes are publicly accessible without authentication.
"""

import os
import markdown
from flask import Blueprint, abort, render_template

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/help', template_folder='templates')

# Ordered list of (slug, title) pairs.  Only slugs in this list are served;
# any other slug returns 404.
PAGES = [
    ('index',            'Help Home'),
    ('getting-started',  'Getting Started'),
    ('analysis',         'Analysis Pipeline'),
    ('searching',        'Searching'),
    ('permissions',      'Permissions & Access'),
]

_CONTENT_DIR = os.path.join(os.path.dirname(__file__), '..', 'help_content')

_MD = markdown.Markdown(
    extensions=['extra', 'toc'],
    extension_configs={
        'toc': {'permalink': True, 'permalink_class': 'help-anchor'},
    },
)


def _render_page(slug):
    """Load and render a Markdown help page.  Returns (html, title) or raises 404."""
    titles = dict(PAGES)
    if slug not in titles:
        abort(404)
    path = os.path.join(_CONTENT_DIR, f'{slug}.md')
    try:
        with open(path, encoding='utf-8') as fh:
            source = fh.read()
    except FileNotFoundError:
        abort(404)
    _MD.reset()
    html = _MD.convert(source)
    return html, titles[slug]




@blueprint.route('/')
@blueprint.route('/index')
def index():
    html, title = _render_page('index')
    return render_template('help/page.html',
                           content=html,
                           title=title,
                           current_slug='index',
                           pages=PAGES)


@blueprint.route('/<slug>')
def page(slug):
    html, title = _render_page(slug)
    return render_template('help/page.html',
                           content=html,
                           title=title,
                           current_slug=slug,
                           pages=PAGES)

# vim: ts=4 sw=4 et
