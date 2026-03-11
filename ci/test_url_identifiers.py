"""
Tests for URL identifier helpers and model url_id / url_slug properties.

Covers:
  - lookup_by_identifier(): full UUID, 8-char prefix, short-UUID+slug, invalid
  - lookup_artefact_by_id(): slug, UUID, short-UUID prefix, wrong-item 404
  - Item.url_id: with and without slug
  - Artefact.url_slug: with and without slug
  - Slug auto-generated on item create (via get_or_create_slug)
  - Slug auto-generated on artefact create (via ensure_unique_slug)
  - Sibling artefacts with same label get distinct slugs

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_url_identifiers -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-url-id-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _make_item(db, name='Test Item', platform_id=None, category_id=None):
    from myapp.database import Item
    item = Item(name=name, platform_id=platform_id, category_id=category_id)
    db.session.add(item)
    db.session.commit()
    return item


def _make_artefact(db, item, label='Disc 1'):
    from myapp.database import Artefact
    from shared.enums import ArtefactType
    artefact = Artefact(
        item_id=item.id,
        label=label,
        artefact_type=ArtefactType.UNKNOWN,
        original_filename='test.img',
        storage_path='test.img',
    )
    db.session.add(artefact)
    db.session.commit()
    return artefact


class TestLookupByIdentifier(unittest.TestCase):
    """Tests for lookup_by_identifier() with Item model."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            _db.create_all()
            cls.item = _make_item(_db, name='Lookup Test Item')
            cls.item.slug = 'lookup-test-item'
            _db.session.commit()
            cls.item_uuid = cls.item.uuid
            cls.item_id = cls.item.id

    def _lookup(self, identifier):
        from myapp.database import Item
        from myapp.utils.slugs import lookup_by_identifier
        with self.app.app_context():
            return lookup_by_identifier(Item, identifier)

    def _lookup_404(self, identifier):
        """Assert that lookup raises a 404."""
        from myapp.database import Item
        from myapp.utils.slugs import lookup_by_identifier
        from werkzeug.exceptions import NotFound
        with self.app.app_context():
            with self.assertRaises(NotFound):
                lookup_by_identifier(Item, identifier)

    def test_full_uuid_resolves(self):
        item = self._lookup(self.item_uuid)
        self.assertEqual(item.id, self.item_id)

    def test_8char_prefix_resolves(self):
        prefix = self.item_uuid[:8]
        item = self._lookup(prefix)
        self.assertEqual(item.id, self.item_id)

    def test_short_uuid_plus_slug_resolves(self):
        identifier = f"{self.item_uuid[:8]}-lookup-test-item"
        item = self._lookup(identifier)
        self.assertEqual(item.id, self.item_id)

    def test_unknown_uuid_returns_404(self):
        self._lookup_404('00000000000000000000000000000000')

    def test_unknown_prefix_returns_404(self):
        self._lookup_404('00000000')

    def test_invalid_identifier_returns_404(self):
        self._lookup_404('not-a-valid-id')

    def test_too_short_returns_404(self):
        self._lookup_404('abc')


class TestArtefactLookup(unittest.TestCase):
    """Tests for lookup_artefact_by_id()."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            _db.create_all()
            cls.item = _make_item(_db, name='Artefact Lookup Item')
            cls.other_item = _make_item(_db, name='Other Item')
            cls.artefact = _make_artefact(_db, cls.item, label='Disc 1')
            cls.artefact.slug = 'disc-1'
            _db.session.commit()
            cls.artefact_uuid = cls.artefact.uuid
            cls.artefact_id = cls.artefact.id
            cls.item_id = cls.item.id
            cls.other_item_id = cls.other_item.id

    def _lookup(self, item_id, artefact_id):
        from myapp.database import Item, Artefact
        from myapp.utils.slugs import lookup_artefact_by_id
        with self.app.app_context():
            item = Item.query.get(item_id)
            return lookup_artefact_by_id(item, artefact_id)

    def _lookup_404(self, item_id, artefact_id):
        from myapp.database import Item
        from myapp.utils.slugs import lookup_artefact_by_id
        from werkzeug.exceptions import NotFound
        with self.app.app_context():
            item = Item.query.get(item_id)
            with self.assertRaises(NotFound):
                lookup_artefact_by_id(item, artefact_id)

    def test_find_by_slug(self):
        artefact = self._lookup(self.item_id, 'disc-1')
        self.assertEqual(artefact.id, self.artefact_id)

    def test_find_by_full_uuid(self):
        artefact = self._lookup(self.item_id, self.artefact_uuid)
        self.assertEqual(artefact.id, self.artefact_id)

    def test_find_by_short_uuid_prefix(self):
        artefact = self._lookup(self.item_id, self.artefact_uuid[:8])
        self.assertEqual(artefact.id, self.artefact_id)

    def test_find_by_short_uuid_plus_slug(self):
        identifier = f"{self.artefact_uuid[:8]}-disc-1"
        artefact = self._lookup(self.item_id, identifier)
        self.assertEqual(artefact.id, self.artefact_id)

    def test_wrong_item_slug_returns_404(self):
        self._lookup_404(self.other_item_id, 'disc-1')

    def test_invalid_identifier_returns_404(self):
        self._lookup_404(self.item_id, 'not-a-slug-or-uuid')

    def test_unknown_slug_returns_404(self):
        self._lookup_404(self.item_id, 'nonexistent-slug')


class TestUrlIdProperty(unittest.TestCase):
    """Tests for Item.url_id and Artefact.url_slug properties."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            _db.create_all()
            cls.item_with_slug = _make_item(_db, name='Slug Item')
            cls.item_with_slug.slug = 'slug-item'
            cls.item_no_slug = _make_item(_db, name='No Slug Item')
            _db.session.commit()
            cls.item_with_slug_uuid = cls.item_with_slug.uuid
            cls.item_with_slug_id = cls.item_with_slug.id
            cls.item_no_slug_uuid = cls.item_no_slug.uuid
            cls.item_no_slug_id = cls.item_no_slug.id

            cls.art_with_slug = _make_artefact(_db, cls.item_with_slug, 'Art With Slug')
            cls.art_with_slug.slug = 'art-with-slug'
            cls.art_no_slug = _make_artefact(_db, cls.item_with_slug, 'Art No Slug')
            _db.session.commit()
            cls.art_with_slug_uuid = cls.art_with_slug.uuid
            cls.art_with_slug_id = cls.art_with_slug.id
            cls.art_no_slug_uuid = cls.art_no_slug.uuid
            cls.art_no_slug_id = cls.art_no_slug.id

    def test_item_url_id_with_slug(self):
        with self.app.app_context():
            from myapp.database import Item
            item = Item.query.get(self.item_with_slug_id)
            expected = f"{self.item_with_slug_uuid[:8]}-slug-item"
            self.assertEqual(item.url_id, expected)

    def test_item_url_id_without_slug(self):
        with self.app.app_context():
            from myapp.database import Item
            item = Item.query.get(self.item_no_slug_id)
            self.assertEqual(item.url_id, self.item_no_slug_uuid[:8])

    def test_artefact_url_slug_with_slug(self):
        with self.app.app_context():
            from myapp.database import Artefact
            art = Artefact.query.get(self.art_with_slug_id)
            self.assertEqual(art.url_slug, 'art-with-slug')

    def test_artefact_url_slug_without_slug(self):
        with self.app.app_context():
            from myapp.database import Artefact
            art = Artefact.query.get(self.art_no_slug_id)
            self.assertEqual(art.url_slug, self.art_no_slug_uuid[:8])


class TestSlugGeneration(unittest.TestCase):
    """Tests for automatic slug generation on create."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            _db.create_all()

    def test_item_slug_generated_from_name(self):
        from myapp.extensions import db
        from myapp.utils.slugs import get_or_create_slug
        with self.app.app_context():
            item = _make_item(db, name='Elite BBC Micro')
            get_or_create_slug(item, 'name')
            self.assertEqual(item.slug, 'elite-bbc-micro')

    def test_item_slug_is_immutable(self):
        from myapp.extensions import db
        from myapp.utils.slugs import get_or_create_slug
        with self.app.app_context():
            item = _make_item(db, name='Original Name')
            get_or_create_slug(item, 'name')
            original_slug = item.slug
            item.name = 'Changed Name'
            db.session.commit()
            # Calling again should not change slug
            get_or_create_slug(item, 'name')
            self.assertEqual(item.slug, original_slug)

    def test_artefact_sibling_slugs_unique(self):
        from myapp.extensions import db
        from myapp.utils.slugs import generate_slug, ensure_unique_slug
        from myapp.database import Artefact
        with self.app.app_context():
            item = _make_item(db, name='Sibling Test Item')
            art1 = _make_artefact(db, item, label='Disc 1')
            base = generate_slug(art1.label)
            art1.slug = ensure_unique_slug(base, Artefact, scope_filter={'item_id': item.id})
            db.session.commit()

            art2 = _make_artefact(db, item, label='Disc 1')
            base = generate_slug(art2.label)
            art2.slug = ensure_unique_slug(base, Artefact, scope_filter={'item_id': item.id})
            db.session.commit()

            self.assertEqual(art1.slug, 'disc-1')
            self.assertEqual(art2.slug, 'disc-1-2')

    def test_artefact_slug_scoped_to_item(self):
        """Same slug can exist in different items."""
        from myapp.extensions import db
        from myapp.utils.slugs import generate_slug, ensure_unique_slug
        from myapp.database import Artefact
        with self.app.app_context():
            item_a = _make_item(db, name='Item A For Scope Test')
            item_b = _make_item(db, name='Item B For Scope Test')
            art_a = _make_artefact(db, item_a, label='Disc 1')
            art_a.slug = ensure_unique_slug(
                generate_slug(art_a.label), Artefact, scope_filter={'item_id': item_a.id}
            )
            art_b = _make_artefact(db, item_b, label='Disc 1')
            art_b.slug = ensure_unique_slug(
                generate_slug(art_b.label), Artefact, scope_filter={'item_id': item_b.id}
            )
            db.session.commit()
            # Both can have slug 'disc-1' since they belong to different items
            self.assertEqual(art_a.slug, 'disc-1')
            self.assertEqual(art_b.slug, 'disc-1')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
