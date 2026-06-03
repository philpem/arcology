"""
Template syntax gate.

Compiles (parses) every Jinja2 template under myapp/templates/ using the
*application's* Jinja environment, so the check sees the same custom filters,
globals, extensions and parser options (trim_blocks/lstrip_blocks) that real
rendering uses.

This catches template syntax errors at CI time instead of as a 500 in
production.  The original motivation was a Python dict comprehension that had
crept into viewer.html:

    {% set _dir_args = {k: v for k, v in pagination_args.items()
                        if k != 'file'} %}

Jinja2 supports neither dict nor list comprehensions, so that raised
TemplateSyntaxError ("expected token ',', got 'for'") on every viewer request
for an opened file — but only at render time, which unit tests covering other
code paths never exercised.

Note: env.parse() validates *syntax* only.  It does not evaluate the template,
so it won't catch undefined variables, bad filter arguments, or runtime logic
errors — only things the Jinja compiler rejects (mismatched/unknown block tags,
comprehensions, malformed expressions, etc.).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_templates -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-template-syntax-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_TEMPLATES_DIR = os.path.join(_REPO_ROOT, 'myapp', 'templates')


def _iter_template_files():
    """Yield (relative_name, absolute_path) for every .html template."""
    for dirpath, _dirs, files in os.walk(_TEMPLATES_DIR):
        for fname in files:
            if fname.endswith('.html'):
                abspath = os.path.join(dirpath, fname)
                relname = os.path.relpath(abspath, _TEMPLATES_DIR)
                yield relname, abspath


class TestTemplateSyntax(unittest.TestCase):
    """Every template under myapp/templates/ must compile cleanly."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app

        cls.app = create_app()
        cls.env = cls.app.jinja_env

    def test_all_templates_parse(self):
        from jinja2 import TemplateSyntaxError

        failures = []
        checked = 0
        for relname, abspath in sorted(_iter_template_files()):
            checked += 1
            with open(abspath, encoding='utf-8') as f:
                source = f.read()
            try:
                self.env.parse(source, name=relname, filename=abspath)
            except TemplateSyntaxError as exc:
                failures.append(f'{relname}:{exc.lineno}: {exc.message}')

        # Guard against the walk silently finding nothing (e.g. moved dir).
        self.assertGreater(
            checked, 0, f'No templates found under {_TEMPLATES_DIR}'
        )
        self.assertEqual(
            failures, [],
            'Jinja2 template syntax error(s):\n  ' + '\n  '.join(failures),
        )


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
