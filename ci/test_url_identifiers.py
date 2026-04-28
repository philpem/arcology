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
        from werkzeug.exceptions import NotFound

        from myapp.database import Item
        from myapp.utils.slugs import lookup_by_identifier
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
        from myapp.database import Item
        from myapp.utils.slugs import lookup_artefact_by_id
        with self.app.app_context():
            item = Item.query.get(item_id)
            return lookup_artefact_by_id(item, artefact_id)

    def _lookup_404(self, item_id, artefact_id):
        from werkzeug.exceptions import NotFound

        from myapp.database import Item
        from myapp.utils.slugs import lookup_artefact_by_id
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
        from myapp.database import Artefact
        from myapp.extensions import db
        from myapp.utils.slugs import ensure_unique_slug, generate_slug
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
        from myapp.database import Artefact
        from myapp.extensions import db
        from myapp.utils.slugs import ensure_unique_slug, generate_slug
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


class TestRootArtefactProperty(unittest.TestCase):
    """Tests for Artefact.root_artefact and nested URL routing."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            _db.create_all()
            item = _make_item(_db, name='Root Artefact Test Item')
            from myapp.database import Artefact
            from myapp.utils.slugs import ensure_unique_slug, generate_slug

            # root artefact
            root = _make_artefact(_db, item, label='Disc 1')
            root.slug = ensure_unique_slug(generate_slug('Disc 1'), Artefact, scope_filter={'item_id': item.id})
            _db.session.commit()

            # first-level derived artefact
            child = Artefact(
                item_id=item.id,
                label='Decoded Sector Image',
                artefact_type=root.artefact_type,
                original_filename='decoded.img',
                storage_path='decoded.img',
                parent_artefact_id=root.id,
            )
            _db.session.add(child)
            _db.session.commit()
            child.slug = ensure_unique_slug(generate_slug('Decoded Sector Image'), Artefact, scope_filter={'item_id': item.id})
            _db.session.commit()

            # second-level derived artefact (grandchild)
            grandchild = Artefact(
                item_id=item.id,
                label='File Listing',
                artefact_type=root.artefact_type,
                original_filename='files.txt',
                storage_path='files.txt',
                parent_artefact_id=child.id,
            )
            _db.session.add(grandchild)
            _db.session.commit()
            grandchild.slug = ensure_unique_slug(generate_slug('File Listing'), Artefact, scope_filter={'item_id': item.id})
            _db.session.commit()

            cls.item_id = item.id
            cls.item_uuid = item.uuid
            cls.root_id = root.id
            cls.child_id = child.id
            cls.grandchild_id = grandchild.id

    def test_root_artefact_of_root_is_self(self):
        from myapp.database import Artefact
        with self.app.app_context():
            root = Artefact.query.get(self.root_id)
            self.assertIs(root.root_artefact, root)

    def test_root_artefact_of_child(self):
        from myapp.database import Artefact
        with self.app.app_context():
            child = Artefact.query.get(self.child_id)
            root = Artefact.query.get(self.root_id)
            self.assertEqual(child.root_artefact.id, root.id)

    def test_root_artefact_of_grandchild(self):
        from myapp.database import Artefact
        with self.app.app_context():
            grandchild = Artefact.query.get(self.grandchild_id)
            root = Artefact.query.get(self.root_id)
            self.assertEqual(grandchild.root_artefact.id, root.id)

    def test_artefact_url_root_has_no_root_id_segment(self):
        """artefact_url() for a root artefact should NOT include root_id."""
        from myapp.database import Artefact, Item
        with self.app.app_context():
            root = Artefact.query.get(self.root_id)
            item = Item.query.get(self.item_id)
            with self.app.test_request_context():
                from flask import url_for
                expected = url_for('myapp_blueprints_artefacts.view',
                                   item_id=item.url_id, artefact_id=root.url_slug)
                # Call the template global directly via the app
                result = self.app.jinja_env.globals['artefact_url'](root)
                self.assertEqual(result, expected)
                self.assertNotIn('/disc-1/', result)  # no root_id prefix

    def test_artefact_url_derived_includes_root_id_segment(self):
        """artefact_url() for a derived artefact should include the root slug."""
        from myapp.database import Artefact
        with self.app.app_context():
            child = Artefact.query.get(self.child_id)
            root = Artefact.query.get(self.root_id)
            with self.app.test_request_context():
                result = self.app.jinja_env.globals['artefact_url'](child)
                # Should contain the root slug and the child slug as path segments
                self.assertIn(f'/{root.url_slug}/', result)
                self.assertIn(child.url_slug, result)

    def test_artefact_url_grandchild_uses_root_not_parent(self):
        """artefact_url() for a grandchild should use the root (not the immediate parent)."""
        from myapp.database import Artefact
        with self.app.app_context():
            grandchild = Artefact.query.get(self.grandchild_id)
            root = Artefact.query.get(self.root_id)
            child = Artefact.query.get(self.child_id)
            with self.app.test_request_context():
                result = self.app.jinja_env.globals['artefact_url'](grandchild)
                self.assertIn(f'/{root.url_slug}/', result)
                self.assertIn(grandchild.url_slug, result)
                # child slug should NOT be in the URL (root, not intermediate parent)
                self.assertNotIn(f'/{child.url_slug}/', result)


class TestApiSlugGeneration(unittest.TestCase):
    """Derived artefacts created via the API should get slugs."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()

        with cls.app.app_context():
            _db.create_all()
            item = _make_item(_db, name='API Slug Test Item')
            item.slug = 'api-slug-test-item'
            _db.session.commit()
            cls.item_uuid = item.uuid
            cls.item_id = item.id

    def test_api_artefact_gets_slug(self):
        """POST /api/items/<uuid>/artefacts should generate a slug from the label."""
        from myapp.database import Artefact
        headers = {'X-API-Key': self.app.config['WORKER_API_KEY'], 'Content-Type': 'application/json'}
        resp = self.client.post(
            f'/api/items/{self.item_uuid}/artefacts',
            json={
                'label': 'Decoded Sector Image',
                'storage_path': 'decoded.img',
                'original_filename': 'decoded.img',
                'artefact_type': 'raw_sector',
            },
            headers=headers,
        )
        self.assertEqual(resp.status_code, 201)
        artefact_uuid = resp.get_json()['uuid']
        with self.app.app_context():
            artefact = Artefact.query.filter_by(uuid=artefact_uuid).first()
            self.assertIsNotNone(artefact)
            self.assertIsNotNone(artefact.slug)
            self.assertEqual(artefact.slug, 'decoded-sector-image')

    def test_api_artefacts_get_unique_slugs(self):
        """Two API artefacts with the same label under the same item get distinct slugs."""
        from myapp.database import Artefact
        headers = {'X-API-Key': self.app.config['WORKER_API_KEY'], 'Content-Type': 'application/json'}
        r1 = self.client.post(
            f'/api/items/{self.item_uuid}/artefacts',
            json={'label': 'Same Label', 'storage_path': 'a.img', 'original_filename': 'a.img', 'artefact_type': 'unknown'},
            headers=headers,
        )
        r2 = self.client.post(
            f'/api/items/{self.item_uuid}/artefacts',
            json={'label': 'Same Label', 'storage_path': 'b.img', 'original_filename': 'b.img', 'artefact_type': 'unknown'},
            headers=headers,
        )
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        uuid1 = r1.get_json()['uuid']
        uuid2 = r2.get_json()['uuid']
        with self.app.app_context():
            a1 = Artefact.query.filter_by(uuid=uuid1).first()
            a2 = Artefact.query.filter_by(uuid=uuid2).first()
            self.assertIsNotNone(a1.slug)
            self.assertIsNotNone(a2.slug)
            self.assertNotEqual(a1.slug, a2.slug, f'Slugs not unique: {a1.slug!r}')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
