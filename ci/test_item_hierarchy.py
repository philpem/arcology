"""
Item hierarchy tests.

Covers:
  - Parent/child creation and relationship traversal
  - ancestors, breadcrumb_path, effective_platform, effective_category
  - is_ancestor_of (cycle detection helper)
  - Cascade delete: deleting a parent removes all descendants and their artefacts
  - API: create/update with parent_uuid, parent_uuid filter on list
  - Move item to new parent via API (including cycle rejection)
  - Migration: parent_id column exists and is nullable

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_item_hierarchy -v
"""

import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-hierarchy-test-secret')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _create_app_and_db():
    from myapp.app import create_app
    from myapp.extensions import db as _db

    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _db.create_all()
    return app, _db


# =============================================================================
# Model-level hierarchy tests
# =============================================================================

class TestItemHierarchyModel(unittest.TestCase):
    """Test Item model hierarchy properties."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def setUp(self):
        from myapp.database import Item, Platform, Category
        with self.app.app_context():
            self.db.session.query(Item).delete()
            self.db.session.query(Platform).delete()
            self.db.session.query(Category).delete()
            self.db.session.commit()

    def _make_item(self, name, parent=None):
        from myapp.database import Item
        item = Item(name=name, parent_id=parent.id if parent else None)
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def test_root_item_has_no_parent(self):
        with self.app.app_context():
            root = self._make_item('Root')
            self.assertIsNone(root.parent_id)
            self.assertIsNone(root.parent)

    def test_child_item_parent_relationship(self):
        with self.app.app_context():
            root = self._make_item('Root')
            child = self._make_item('Child', parent=root)
            self.assertEqual(child.parent_id, root.id)
            self.assertEqual(child.parent.name, 'Root')
            self.assertIn(child.id, [c.id for c in root.children])

    def test_ancestors_three_levels(self):
        with self.app.app_context():
            root = self._make_item('Root')
            mid = self._make_item('Mid', parent=root)
            leaf = self._make_item('Leaf', parent=mid)

            ancestors = leaf.ancestors
            self.assertEqual(len(ancestors), 2)
            self.assertEqual(ancestors[0].name, 'Root')
            self.assertEqual(ancestors[1].name, 'Mid')

    def test_ancestors_root_item_is_empty(self):
        with self.app.app_context():
            root = self._make_item('Root')
            self.assertEqual(root.ancestors, [])

    def test_breadcrumb_path_includes_self(self):
        with self.app.app_context():
            root = self._make_item('Root')
            child = self._make_item('Child', parent=root)
            path = child.breadcrumb_path
            self.assertEqual([i.name for i in path], ['Root', 'Child'])

    def test_effective_platform_own(self):
        from myapp.database import Item, Platform
        with self.app.app_context():
            plat = Platform(name='TestPlatform')
            self.db.session.add(plat)
            self.db.session.commit()
            root = self._make_item('Root')
            root.platform_id = plat.id
            self.db.session.commit()
            child = self._make_item('Child', parent=root)
            # Child has no own platform — should inherit
            self.assertIsNone(child.platform)
            self.assertEqual(child.effective_platform.name, 'TestPlatform')

    def test_effective_platform_own_overrides_parent(self):
        from myapp.database import Item, Platform
        with self.app.app_context():
            plat_parent = Platform(name='ParentPlatform')
            plat_child = Platform(name='ChildPlatform')
            self.db.session.add_all([plat_parent, plat_child])
            self.db.session.commit()
            root = self._make_item('Root')
            root.platform_id = plat_parent.id
            self.db.session.commit()
            child = self._make_item('Child', parent=root)
            child.platform_id = plat_child.id
            self.db.session.commit()
            self.assertEqual(child.effective_platform.name, 'ChildPlatform')

    def test_effective_category_inherits(self):
        from myapp.database import Item, Category
        with self.app.app_context():
            cat = Category(name='TestCategory')
            self.db.session.add(cat)
            self.db.session.commit()
            root = self._make_item('Root')
            root.category_id = cat.id
            self.db.session.commit()
            grandchild = self._make_item('Child', parent=root)
            gc = self._make_item('Grandchild', parent=grandchild)
            self.assertEqual(gc.effective_category.name, 'TestCategory')

    def test_is_ancestor_of(self):
        with self.app.app_context():
            root = self._make_item('Root')
            mid = self._make_item('Mid', parent=root)
            leaf = self._make_item('Leaf', parent=mid)
            unrelated = self._make_item('Unrelated')

            self.assertTrue(root.is_ancestor_of(leaf))
            self.assertTrue(root.is_ancestor_of(mid))
            self.assertTrue(mid.is_ancestor_of(leaf))
            self.assertFalse(leaf.is_ancestor_of(root))
            self.assertFalse(root.is_ancestor_of(unrelated))

    def test_cascade_delete_removes_children(self):
        from myapp.database import Item
        with self.app.app_context():
            root = self._make_item('Root')
            child = self._make_item('Child', parent=root)
            grandchild = self._make_item('Grandchild', parent=child)
            root_id = root.id
            child_id = child.id
            grandchild_id = grandchild.id

            self.db.session.delete(root)
            self.db.session.commit()

            self.assertIsNone(Item.query.get(root_id))
            self.assertIsNone(Item.query.get(child_id))
            self.assertIsNone(Item.query.get(grandchild_id))


# =============================================================================
# API-level hierarchy tests
# =============================================================================

class TestItemHierarchyAPI(unittest.TestCase):
    """Test item hierarchy via REST API."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()
        _worker_key = os.environ['WORKER_API_KEY']
        cls.headers = {'X-API-Key': _worker_key, 'Content-Type': 'application/json'}

    def setUp(self):
        from myapp.database import Item
        with self.app.app_context():
            # Clean up items between tests
            self.db.session.query(Item).filter(Item.name.like('test-%')).delete()
            self.db.session.commit()

    def _post(self, url, data):
        return self.client.post(url, data=json.dumps(data), headers=self.headers)

    def _put(self, url, data):
        return self.client.put(url, data=json.dumps(data), headers=self.headers)

    def _get(self, url, params=None):
        return self.client.get(url, query_string=params, headers=self.headers)

    def test_create_root_item(self):
        resp = self._post('/api/items', {'name': 'test-root'})
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.data)
        self.assertIsNone(data['parent_uuid'])
        self.assertEqual(data['path'], [])

    def test_create_child_item(self):
        resp = self._post('/api/items', {'name': 'test-parent'})
        parent_uuid = json.loads(resp.data)['uuid']

        resp = self._post('/api/items', {'name': 'test-child', 'parent_uuid': parent_uuid})
        self.assertEqual(resp.status_code, 201)
        data = json.loads(resp.data)
        self.assertEqual(data['parent_uuid'], parent_uuid)
        self.assertEqual(len(data['path']), 1)
        self.assertEqual(data['path'][0]['uuid'], parent_uuid)

    def test_create_with_invalid_parent(self):
        resp = self._post('/api/items', {'name': 'test-orphan',
                                          'parent_uuid': 'deadbeefdeadbeefdeadbeefdeadbeef'})
        self.assertEqual(resp.status_code, 404)

    def test_move_item_to_new_parent(self):
        r1 = self._post('/api/items', {'name': 'test-parent-a'})
        r2 = self._post('/api/items', {'name': 'test-parent-b'})
        uuid_a = json.loads(r1.data)['uuid']
        uuid_b = json.loads(r2.data)['uuid']

        # Create child under A
        rc = self._post('/api/items', {'name': 'test-movable', 'parent_uuid': uuid_a})
        child_uuid = json.loads(rc.data)['uuid']

        # Move child to B
        resp = self._put(f'/api/items/{child_uuid}', {'parent_uuid': uuid_b})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data['parent_uuid'], uuid_b)

    def test_move_item_to_root(self):
        rp = self._post('/api/items', {'name': 'test-parent-c'})
        parent_uuid = json.loads(rp.data)['uuid']
        rc = self._post('/api/items', {'name': 'test-unparent', 'parent_uuid': parent_uuid})
        child_uuid = json.loads(rc.data)['uuid']

        resp = self._put(f'/api/items/{child_uuid}', {'parent_uuid': None})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsNone(data['parent_uuid'])

    def test_move_rejects_cycle_ancestor_to_descendant(self):
        rroot = self._post('/api/items', {'name': 'test-cycle-root'})
        root_uuid = json.loads(rroot.data)['uuid']
        rchild = self._post('/api/items', {'name': 'test-cycle-child', 'parent_uuid': root_uuid})
        child_uuid = json.loads(rchild.data)['uuid']

        # Attempt to make root a child of its own child (cycle)
        resp = self._put(f'/api/items/{root_uuid}', {'parent_uuid': child_uuid})
        self.assertEqual(resp.status_code, 400)

    def test_move_rejects_self_as_parent(self):
        rroot = self._post('/api/items', {'name': 'test-self-parent'})
        root_uuid = json.loads(rroot.data)['uuid']
        resp = self._put(f'/api/items/{root_uuid}', {'parent_uuid': root_uuid})
        self.assertEqual(resp.status_code, 400)

    def test_list_filter_by_parent_uuid(self):
        rp = self._post('/api/items', {'name': 'test-list-parent'})
        parent_uuid = json.loads(rp.data)['uuid']
        self._post('/api/items', {'name': 'test-list-child-1', 'parent_uuid': parent_uuid})
        self._post('/api/items', {'name': 'test-list-child-2', 'parent_uuid': parent_uuid})

        resp = self._get('/api/items', {'parent_uuid': parent_uuid})
        self.assertEqual(resp.status_code, 200)
        items = json.loads(resp.data)['items']
        names = [i['name'] for i in items]
        self.assertIn('test-list-child-1', names)
        self.assertIn('test-list-child-2', names)
        self.assertNotIn('test-list-parent', names)

    def test_list_filter_root_only(self):
        rp = self._post('/api/items', {'name': 'test-root-only-parent'})
        parent_uuid = json.loads(rp.data)['uuid']
        self._post('/api/items', {'name': 'test-root-only-child', 'parent_uuid': parent_uuid})

        resp = self._get('/api/items', {'parent_uuid': 'none'})
        self.assertEqual(resp.status_code, 200)
        items = json.loads(resp.data)['items']
        names = [i['name'] for i in items]
        self.assertIn('test-root-only-parent', names)
        self.assertNotIn('test-root-only-child', names)

    def test_get_item_includes_path(self):
        rp = self._post('/api/items', {'name': 'test-path-root'})
        root_uuid = json.loads(rp.data)['uuid']
        rm = self._post('/api/items', {'name': 'test-path-mid', 'parent_uuid': root_uuid})
        mid_uuid = json.loads(rm.data)['uuid']
        rl = self._post('/api/items', {'name': 'test-path-leaf', 'parent_uuid': mid_uuid})
        leaf_uuid = json.loads(rl.data)['uuid']

        resp = self._get(f'/api/items/{leaf_uuid}')
        data = json.loads(resp.data)
        path_names = [p['name'] for p in data['path']]
        self.assertEqual(path_names, ['test-path-root', 'test-path-mid'])

    def test_child_count_in_response(self):
        rp = self._post('/api/items', {'name': 'test-childcount-parent'})
        parent_uuid = json.loads(rp.data)['uuid']
        self._post('/api/items', {'name': 'test-childcount-c1', 'parent_uuid': parent_uuid})
        self._post('/api/items', {'name': 'test-childcount-c2', 'parent_uuid': parent_uuid})

        resp = self._get(f'/api/items/{parent_uuid}')
        data = json.loads(resp.data)
        self.assertEqual(data['child_count'], 2)



# =============================================================================
# Move artefact tests
# =============================================================================

class TestMoveArtefact(unittest.TestCase):
    """Test moving artefacts between items via REST API."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.client = cls.app.test_client()
        _worker_key = os.environ['WORKER_API_KEY']
        cls.headers = {'X-API-Key': _worker_key, 'Content-Type': 'application/json'}

    def setUp(self):
        from myapp.database import Item, Artefact
        with self.app.app_context():
            self.db.session.query(Artefact).delete()
            self.db.session.query(Item).filter(Item.name.like('test-mv-%')).delete()
            self.db.session.commit()

    def _post(self, url, data):
        return self.client.post(url, data=json.dumps(data), headers=self.headers)

    def _get(self, url, params=None):
        return self.client.get(url, query_string=params, headers=self.headers)

    def _create_item(self, name, parent_uuid=None):
        data = {'name': name}
        if parent_uuid:
            data['parent_uuid'] = parent_uuid
        resp = self._post('/api/items', data)
        self.assertEqual(resp.status_code, 201)
        return json.loads(resp.data)['uuid']

    def _create_artefact(self, item_uuid, label='Test Artefact'):
        """Create a minimal artefact via direct DB insertion."""
        from myapp.database import Item, Artefact, ArtefactType
        from myapp.utils.slugs import generate_slug, ensure_unique_slug
        with self.app.app_context():
            item = Item.query.filter_by(uuid=item_uuid).first()
            slug = ensure_unique_slug(generate_slug(label), Artefact, scope_filter={'item_id': item.id})
            artefact = Artefact(
                item_id=item.id,
                label=label,
                slug=slug,
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='test.img',
                storage_path='test-fake-path.img',
            )
            self.db.session.add(artefact)
            self.db.session.commit()
            return artefact.uuid

    def _create_derived_artefact(self, parent_uuid, label='Derived'):
        """Create a derived artefact under a parent artefact."""
        from myapp.database import Artefact, ArtefactType
        from myapp.utils.slugs import generate_slug, ensure_unique_slug
        with self.app.app_context():
            parent = Artefact.query.filter_by(uuid=parent_uuid).first()
            slug = ensure_unique_slug(generate_slug(label), Artefact, scope_filter={'item_id': parent.item_id})
            derived = Artefact(
                item_id=parent.item_id,
                parent_artefact_id=parent.id,
                label=label,
                slug=slug,
                artefact_type=ArtefactType.RAW_SECTOR,
                original_filename='derived.img',
                storage_path='derived-fake-path.img',
            )
            self.db.session.add(derived)
            self.db.session.commit()
            return derived.uuid

    def test_move_root_artefact(self):
        """Move a root artefact from Item A to Item B."""
        item_a = self._create_item('test-mv-source')
        item_b = self._create_item('test-mv-target')
        art_uuid = self._create_artefact(item_a, 'movable')

        resp = self._post(f'/api/artefacts/{art_uuid}/move', {'target_item_uuid': item_b})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data['item_uuid'], item_b)

    def test_move_with_derived_artefacts(self):
        """Moving a root artefact also moves all derived artefacts."""
        item_a = self._create_item('test-mv-src-derived')
        item_b = self._create_item('test-mv-tgt-derived')
        root_uuid = self._create_artefact(item_a, 'root-art')
        derived_uuid = self._create_derived_artefact(root_uuid, 'derived-art')

        resp = self._post(f'/api/artefacts/{root_uuid}/move', {'target_item_uuid': item_b})
        self.assertEqual(resp.status_code, 200)

        # Check derived artefact also moved
        resp2 = self._get(f'/api/artefacts/{derived_uuid}')
        self.assertEqual(resp2.status_code, 200)
        derived_data = json.loads(resp2.data)
        self.assertEqual(derived_data['item_uuid'], item_b)

    def test_move_derived_artefact_rejected(self):
        """Moving a derived (non-root) artefact should fail."""
        item_a = self._create_item('test-mv-reject-src')
        item_b = self._create_item('test-mv-reject-tgt')
        root_uuid = self._create_artefact(item_a, 'root')
        derived_uuid = self._create_derived_artefact(root_uuid, 'derived')

        resp = self._post(f'/api/artefacts/{derived_uuid}/move', {'target_item_uuid': item_b})
        self.assertEqual(resp.status_code, 400)

    def test_move_to_nonexistent_item(self):
        """Moving to a non-existent item returns 404."""
        item_a = self._create_item('test-mv-noexist-src')
        art_uuid = self._create_artefact(item_a)

        resp = self._post(f'/api/artefacts/{art_uuid}/move',
                          {'target_item_uuid': 'deadbeefdeadbeefdeadbeefdeadbeef'})
        self.assertEqual(resp.status_code, 404)

    def test_move_to_same_item_rejected(self):
        """Moving to the same item returns 400."""
        item_a = self._create_item('test-mv-same')
        art_uuid = self._create_artefact(item_a)

        resp = self._post(f'/api/artefacts/{art_uuid}/move', {'target_item_uuid': item_a})
        self.assertEqual(resp.status_code, 400)

    def test_slug_collision_on_move(self):
        """If target item already has an artefact with the same slug, it should be renamed."""
        item_a = self._create_item('test-mv-slug-src')
        item_b = self._create_item('test-mv-slug-tgt')
        # Create artefacts with same label (=> same slug) in both items
        art_a = self._create_artefact(item_a, 'duplicate-label')
        _art_b = self._create_artefact(item_b, 'duplicate-label')

        resp = self._post(f'/api/artefacts/{art_a}/move', {'target_item_uuid': item_b})
        self.assertEqual(resp.status_code, 200)
        # The moved artefact should have a different slug now (e.g. duplicate-label-2)
        with self.app.app_context():
            from myapp.database import Artefact
            moved = Artefact.query.filter_by(uuid=art_a).first()
            self.assertNotEqual(moved.slug, 'duplicate-label')
            self.assertTrue(moved.slug.startswith('duplicate-label'))


if __name__ == '__main__':
    unittest.main()
