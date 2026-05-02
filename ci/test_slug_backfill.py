"""
Tests for `flask backfill-slugs`.

Covers:
- NULL item slug → unique slug assigned
- NULL artefact slug → unique slug assigned, scoped per item
- Collision with an existing slug → suffixed form (-2, -3, …)
- --dry-run touches no rows

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \
        python -m unittest ci.test_slug_backfill -v
"""

import os
import sys
import unittest
from unittest.mock import patch
from io import StringIO

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-backfill-slugs-test-secret')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _make_item(db, name, slug=None):
    from myapp.database import Item
    item = Item(name=name)
    item.slug = slug
    db.session.add(item)
    db.session.commit()
    return item


def _make_artefact(db, item, label, slug=None):
    from myapp.database import Artefact
    from shared.enums import ArtefactType
    art = Artefact(
        item_id=item.id,
        label=label,
        artefact_type=ArtefactType.UNKNOWN,
        original_filename=f'{label}.bin',
        storage_path=f'{label}.bin',
    )
    art.slug = slug
    db.session.add(art)
    db.session.commit()
    return art


class TestBackfillSlugsItems(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        # Each test gets a clean context
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def _run_backfill(self, **kwargs):
        from click.testing import CliRunner
        from myapp.cli.backfill_slugs import backfill_slugs
        runner = CliRunner()
        args = []
        if kwargs.get('dry_run'):
            args.append('--dry-run')
        if 'batch_size' in kwargs:
            args += ['--batch-size', str(kwargs['batch_size'])]
        # Run inside the existing app context by pushing app manually
        with self.app.app_context():
            result = runner.invoke(backfill_slugs, args, catch_exceptions=False)
        return result

    def test_null_item_slug_gets_assigned(self):
        from myapp.database import Item
        item = _make_item(self.db, 'Test Item Alpha')
        self.assertIsNone(item.slug)

        self._run_backfill()

        self.db.session.refresh(item)
        self.assertEqual(item.slug, 'test-item-alpha')

    def test_item_with_existing_slug_is_skipped(self):
        from myapp.database import Item
        item = _make_item(self.db, 'Already Slugged', slug='already-slugged')

        self._run_backfill()

        self.db.session.refresh(item)
        self.assertEqual(item.slug, 'already-slugged')

    def test_item_slug_collision_produces_suffix(self):
        from myapp.database import Item
        # One item already holds the base slug
        existing = _make_item(self.db, 'Duplicate Name', slug='duplicate-name')
        # Second item with the same name but no slug yet
        new_item = _make_item(self.db, 'Duplicate Name')
        self.assertIsNone(new_item.slug)

        self._run_backfill()

        self.db.session.refresh(new_item)
        self.assertEqual(new_item.slug, 'duplicate-name-2')

    def test_dry_run_does_not_write(self):
        from myapp.database import Item
        item = _make_item(self.db, 'Dry Run Item')
        self.assertIsNone(item.slug)

        result = self._run_backfill(dry_run=True)

        self.db.session.refresh(item)
        self.assertIsNone(item.slug)
        self.assertIn('[dry-run]', result.output)
        self.assertIn('would be assigned', result.output)


class TestBackfillSlugsArtefacts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def _run_backfill(self, **kwargs):
        from click.testing import CliRunner
        from myapp.cli.backfill_slugs import backfill_slugs
        runner = CliRunner()
        args = []
        if kwargs.get('dry_run'):
            args.append('--dry-run')
        with self.app.app_context():
            result = runner.invoke(backfill_slugs, args, catch_exceptions=False)
        return result

    def test_null_artefact_slug_gets_assigned(self):
        item = _make_item(self.db, 'Item For Artefact Test', slug='item-for-artefact-test')
        art = _make_artefact(self.db, item, 'Disc 1')
        self.assertIsNone(art.slug)

        self._run_backfill()

        self.db.session.refresh(art)
        self.assertEqual(art.slug, 'disc-1')

    def test_artefact_collision_within_item_produces_suffix(self):
        item = _make_item(self.db, 'Item Collision Test', slug='item-collision-test')
        existing = _make_artefact(self.db, item, 'Side A', slug='side-a')
        new_art = _make_artefact(self.db, item, 'Side A')
        self.assertIsNone(new_art.slug)

        self._run_backfill()

        self.db.session.refresh(new_art)
        self.assertEqual(new_art.slug, 'side-a-2')

    def test_artefact_same_label_different_items_no_suffix(self):
        """Same label in two different items must each get the base slug."""
        item_a = _make_item(self.db, 'Item A Cross', slug='item-a-cross')
        item_b = _make_item(self.db, 'Item B Cross', slug='item-b-cross')
        art_a = _make_artefact(self.db, item_a, 'Disc 1')
        art_b = _make_artefact(self.db, item_b, 'Disc 1')

        self._run_backfill()

        self.db.session.refresh(art_a)
        self.db.session.refresh(art_b)
        self.assertEqual(art_a.slug, 'disc-1')
        self.assertEqual(art_b.slug, 'disc-1')

    def test_dry_run_artefact_untouched(self):
        item = _make_item(self.db, 'Dry Run Art Item', slug='dry-run-art-item')
        art = _make_artefact(self.db, item, 'Track 0')
        self.assertIsNone(art.slug)

        result = self._run_backfill(dry_run=True)

        self.db.session.refresh(art)
        self.assertIsNone(art.slug)
        self.assertIn('[dry-run]', result.output)


class TestGetOrCreateSlugUniqueness(unittest.TestCase):
    """get_or_create_slug now calls ensure_unique_slug, so it can't produce duplicates."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def test_get_or_create_slug_avoids_collision(self):
        from myapp.utils.slugs import get_or_create_slug
        # First item claims the base slug
        item1 = _make_item(self.db, 'Collision Item', slug='collision-item')
        # Second item with the same name, no slug yet
        item2 = _make_item(self.db, 'Collision Item')

        slug = get_or_create_slug(item2, 'name')

        self.assertEqual(slug, 'collision-item-2')
        self.db.session.refresh(item2)
        self.assertEqual(item2.slug, 'collision-item-2')

    def test_get_or_create_slug_returns_existing(self):
        from myapp.utils.slugs import get_or_create_slug
        item = _make_item(self.db, 'Already Has Slug', slug='already-has-slug')
        slug = get_or_create_slug(item, 'name')
        self.assertEqual(slug, 'already-has-slug')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
