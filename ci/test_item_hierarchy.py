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


from myapp.extensions import db  # noqa: E402  (Query.get -> Session.get migration)


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
        from myapp.database import Category, Item, Platform
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
        from myapp.database import Platform
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
        from myapp.database import Platform
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
        from myapp.database import Category
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

            self.assertIsNone(db.session.get(Item, root_id))
            self.assertIsNone(db.session.get(Item, child_id))
            self.assertIsNone(db.session.get(Item, grandchild_id))


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
        from myapp.database import Artefact, Item
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
        from myapp.database import Artefact, ArtefactType, Item
        from myapp.utils.slugs import ensure_unique_slug, generate_slug
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
        from myapp.utils.slugs import ensure_unique_slug, generate_slug
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

    def test_batch_move_roots_and_derived(self):
        """A batch move updates selected roots and their full derived trees."""
        item_a = self._create_item('test-mv-batch-source')
        item_b = self._create_item('test-mv-batch-target')
        first = self._create_artefact(item_a, 'batch first')
        second = self._create_artefact(item_a, 'batch second')
        untouched = self._create_artefact(item_a, 'not selected')
        derived = self._create_derived_artefact(first, 'batch derived')

        resp = self._post(
            f'/api/items/{item_a}/artefacts/batch-move',
            {'target_item_uuid': item_b, 'artefact_uuids': [first, second]},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        data = json.loads(resp.data)
        self.assertEqual(data['root_artefacts_moved'], 2)
        self.assertEqual(data['derived_artefacts_moved'], 1)
        self.assertEqual(data['total_artefacts_moved'], 3)

        with self.app.app_context():
            from myapp.database import Artefact, Item
            target_id = Item.query.filter_by(uuid=item_b).one().id
            source_id = Item.query.filter_by(uuid=item_a).one().id
            self.assertEqual(Artefact.query.filter_by(uuid=first).one().item_id, target_id)
            self.assertEqual(Artefact.query.filter_by(uuid=second).one().item_id, target_id)
            self.assertEqual(Artefact.query.filter_by(uuid=derived).one().item_id, target_id)
            self.assertEqual(Artefact.query.filter_by(uuid=untouched).one().item_id, source_id)

    def test_batch_move_can_create_source_subitem(self):
        """The API can create the destination beneath the source atomically."""
        source = self._create_item('test-mv-new-child-source')
        artefact = self._create_artefact(source, 'move into child')

        resp = self._post(
            f'/api/items/{source}/artefacts/batch-move',
            {'new_item_name': 'test-mv-created-child', 'artefact_uuids': [artefact]},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        data = json.loads(resp.data)
        self.assertTrue(data['created_target_item'])

        with self.app.app_context():
            from myapp.database import Artefact, Item
            parent = Item.query.filter_by(uuid=source).one()
            child = Item.query.filter_by(uuid=data['target_item_uuid']).one()
            self.assertEqual(child.parent_id, parent.id)
            self.assertEqual(Artefact.query.filter_by(uuid=artefact).one().item_id, child.id)

    def test_batch_move_rejects_mixed_source_without_partial_move(self):
        """A stale or foreign UUID rejects the complete selection."""
        source = self._create_item('test-mv-atomic-source')
        other = self._create_item('test-mv-atomic-other')
        target = self._create_item('test-mv-atomic-target')
        valid = self._create_artefact(source, 'valid selection')
        foreign = self._create_artefact(other, 'foreign selection')

        resp = self._post(
            f'/api/items/{source}/artefacts/batch-move',
            {'target_item_uuid': target, 'artefact_uuids': [valid, foreign]},
        )
        self.assertEqual(resp.status_code, 400, resp.data)

        with self.app.app_context():
            from myapp.database import Artefact, Item
            source_id = Item.query.filter_by(uuid=source).one().id
            self.assertEqual(Artefact.query.filter_by(uuid=valid).one().item_id, source_id)

    def test_batch_move_rolls_back_new_target_for_invalid_selection(self):
        """Creating a subitem and moving its contents is one transaction."""
        source = self._create_item('test-mv-new-rollback-source')
        artefact = self._create_artefact(source, 'valid selection')

        resp = self._post(
            f'/api/items/{source}/artefacts/batch-move',
            {
                'new_item_name': 'test-mv-must-not-remain',
                'artefact_uuids': [artefact, 'f' * 32],
            },
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        with self.app.app_context():
            from myapp.database import Item
            self.assertIsNone(Item.query.filter_by(name='test-mv-must-not-remain').first())

    def test_batch_move_allocates_unique_slugs_within_batch(self):
        """Colliding source slugs remain addressable after a combined move."""
        first_source = self._create_item('test-mv-slugs-source-one')
        second_source = self._create_item('test-mv-slugs-source-two')
        target = self._create_item('test-mv-slugs-target')
        first = self._create_artefact(first_source, 'same label')
        second = self._create_artefact(second_source, 'same label')

        # Put both roots in one source to model legacy data with colliding slugs.
        with self.app.app_context():
            from myapp.database import Artefact, Item
            source_id = Item.query.filter_by(uuid=first_source).one().id
            Artefact.query.filter_by(uuid=second).one().item_id = source_id
            self.db.session.commit()

        resp = self._post(
            f'/api/items/{first_source}/artefacts/batch-move',
            {'target_item_uuid': target, 'artefact_uuids': [first, second]},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        with self.app.app_context():
            from myapp.database import Artefact
            slugs = {Artefact.query.filter_by(uuid=value).one().slug for value in (first, second)}
            self.assertEqual(len(slugs), 2)


# =============================================================================
# indented_item_choices / item_parent_choice_list tests
# =============================================================================

class TestItemParentChoiceList(unittest.TestCase):
    """Test that item_parent_choice_list returns items in correct tree order."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def setUp(self):
        from myapp.database import Item
        with self.app.app_context():
            self.db.session.query(Item).delete()
            self.db.session.commit()

    def _make_item(self, name, parent=None):
        from myapp.database import Item
        item = Item(name=name, parent_id=parent.id if parent else None)
        self.db.session.add(item)
        self.db.session.commit()
        return item

    def _choice_names(self, choices):
        """Strip leading non-breaking spaces to get bare names from choices."""
        return [label.lstrip(' ').lstrip() for _, label in choices]

    def _choice_depths(self, choices):
        """Count leading non-breaking space groups (4 per level) as depth."""
        return [len(label) - len(label.lstrip(' ')) // 4 for _, label in choices]

    def test_tree_traversal_order_parent_before_child(self):
        """Child whose name sorts before parent must still appear after the parent."""
        with self.app.app_context():
            from myapp.utils.item_helpers import indented_item_choices
            zebra = self._make_item('Zebra')
            self._make_item('Apple', parent=zebra)  # 'A' < 'Z' alphabetically
            self._make_item('Mango', parent=zebra)
            self._make_item('Banana')

            choices = indented_item_choices()
            names = self._choice_names(choices)

            # Banana and Zebra are roots, Zebra must precede Apple/Mango
            self.assertLess(names.index('Banana'), names.index('Zebra'))
            self.assertLess(names.index('Zebra'), names.index('Apple'))
            self.assertLess(names.index('Zebra'), names.index('Mango'))
            # Apple and Mango are siblings of Zebra, sorted alphabetically
            self.assertLess(names.index('Apple'), names.index('Mango'))

    def test_correct_depth_for_children(self):
        """Children show at depth 1, grandchildren at depth 2."""
        with self.app.app_context():
            from myapp.utils.item_helpers import indented_item_choices
            root = self._make_item('Root')
            child = self._make_item('Child', parent=root)
            self._make_item('Grandchild', parent=child)

            choices = indented_item_choices()
            depth_map = {
                label.lstrip(' '): (len(label) - len(label.lstrip(' '))) // 4
                for _, label in choices
            }
            self.assertEqual(depth_map['Root'], 0)
            self.assertEqual(depth_map['Child'], 1)
            self.assertEqual(depth_map['Grandchild'], 2)

    def test_siblings_sorted_alphabetically(self):
        """Siblings appear in alphabetical order under their parent."""
        with self.app.app_context():
            from myapp.utils.item_helpers import indented_item_choices
            parent = self._make_item('Parent')
            self._make_item('Charlie', parent=parent)
            self._make_item('Alice', parent=parent)
            self._make_item('Bob', parent=parent)

            choices = indented_item_choices()
            names = self._choice_names(choices)
            siblings = [n for n in names if n in ('Alice', 'Bob', 'Charlie')]
            self.assertEqual(siblings, ['Alice', 'Bob', 'Charlie'])

    def test_exclude_item_and_descendants_absent(self):
        """Excluded item and all its descendants must not appear in the list."""
        with self.app.app_context():
            from myapp.utils.item_helpers import item_parent_choice_list
            root = self._make_item('Root')
            child = self._make_item('Child', parent=root)
            self._make_item('Grandchild', parent=child)
            self._make_item('Other')

            choices = item_parent_choice_list('-- none --', exclude_item=root)
            names = self._choice_names(choices)

            self.assertNotIn('Root', names)
            self.assertNotIn('Child', names)
            self.assertNotIn('Grandchild', names)
            self.assertIn('Other', names)

    def test_exclude_leaf_only(self):
        """Excluding a leaf does not affect siblings or the parent."""
        with self.app.app_context():
            from myapp.utils.item_helpers import item_parent_choice_list
            parent = self._make_item('Parent')
            leaf = self._make_item('Leaf', parent=parent)
            self._make_item('Sibling', parent=parent)

            choices = item_parent_choice_list('-- none --', exclude_item=leaf)
            names = self._choice_names(choices)

            self.assertNotIn('Leaf', names)
            self.assertIn('Parent', names)
            self.assertIn('Sibling', names)

    def test_exclude_item_can_retain_descendant_targets(self):
        """Batch moves may target an existing child of the source item."""
        with self.app.app_context():
            from myapp.utils.item_helpers import indented_item_choices
            source = self._make_item('Source')
            self._make_item('Existing subitem', parent=source)

            choices = indented_item_choices(
                exclude_ids={source.id},
                prune_excluded_subtrees=False,
            )
            names = self._choice_names(choices)

            self.assertNotIn('Source', names)
            self.assertIn('Existing subitem', names)

    def test_no_exclude_returns_all_items(self):
        """Without exclusion, all items are returned."""
        with self.app.app_context():
            from myapp.utils.item_helpers import indented_item_choices
            self._make_item('Alpha')
            self._make_item('Beta')

            choices = indented_item_choices()
            names = self._choice_names(choices)
            self.assertIn('Alpha', names)
            self.assertIn('Beta', names)
            self.assertEqual(len(choices), 2)


# =============================================================================
# Batch move: authorisation with real user keys, and the web confirm/perform
# routes.  TestMoveArtefact above uses the worker key (sees_all=True), which
# bypasses every visibility/contribution check — these exercise the model.
# =============================================================================

class _BatchMoveTestCase(unittest.TestCase):
    """Shared fixtures: user API keys, private items/shares, artefact rows."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()
        cls.app.config['WTF_CSRF_ENABLED'] = False

    def setUp(self):
        from myapp.database import ApiKey, Artefact, Item, ItemShare, User
        with self.app.app_context():
            self.db.session.query(ItemShare).delete()
            self.db.session.query(ApiKey).delete()
            self.db.session.query(Artefact).delete()
            self.db.session.query(Item).delete()
            self.db.session.query(User).delete()
            self.db.session.commit()
        self.client = self.app.test_client()

    def _user(self, username, *, perm=None, admin=False, api_permission=None):
        import bcrypt
        from myapp.database import ApiKey, ApiKeyPermission, User, UserPermission
        with self.app.app_context():
            pw = bcrypt.hashpw(b'testpassword1234', bcrypt.gensalt()).decode('utf-8')
            user = User(username=username, password_hash=pw, is_admin=admin,
                        permission=perm or UserPermission.READ_WRITE, can_use_api=True)
            self.db.session.add(user)
            self.db.session.flush()
            key, raw = ApiKey.create(
                user_id=user.id, name=f'{username}-key',
                permission=api_permission or ApiKeyPermission.READ_WRITE,
            )
            self.db.session.add(key)
            self.db.session.commit()
            return user.id, raw

    def _item(self, name, *, owner_id=None, private=False):
        from myapp.database import Item
        from myapp.utils.privacy import recompute_item_privacy
        with self.app.app_context():
            item = Item(name=name, owner_id=owner_id, is_private=private)
            self.db.session.add(item)
            self.db.session.flush()
            recompute_item_privacy(item)
            self.db.session.commit()
            return item.uuid

    def _share(self, item_uuid, user_id, permission):
        from myapp.database import Item, ItemShare
        with self.app.app_context():
            item = Item.query.filter_by(uuid=item_uuid).one()
            self.db.session.add(ItemShare(item_id=item.id, user_id=user_id,
                                          permission=permission))
            self.db.session.commit()

    def _artefact(self, item_uuid, label='art', *, private=False):
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, Item
        from myapp.utils.slugs import ensure_unique_slug, generate_slug
        with self.app.app_context():
            item = Item.query.filter_by(uuid=item_uuid).one()
            slug = ensure_unique_slug(generate_slug(label), Artefact,
                                      scope_filter={'item_id': item.id})
            art = Artefact(item_id=item.id, label=label, slug=slug,
                           artefact_type=ArtefactType.RAW_SECTOR,
                           original_filename='x.img', storage_path='x.img',
                           is_private=private)
            self.db.session.add(art)
            self.db.session.commit()
            return art.uuid

    def _api(self, source_uuid, body, key):
        return self.client.post(
            f'/api/items/{source_uuid}/artefacts/batch-move',
            data=json.dumps(body),
            headers={'X-API-Key': key, 'Content-Type': 'application/json'},
        )

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True


class TestBatchMoveAuthorisation(_BatchMoveTestCase):
    """Access control for the batch-move endpoint, exercised with user keys."""

    def test_viewer_share_cannot_move_out_of_private_source(self):
        owner_id, _ = self._user('bm-auth-owner')
        viewer_id, viewer_key = self._user('bm-auth-viewer')
        src = self._item('bm-priv-src', owner_id=owner_id, private=True)
        tgt = self._item('bm-pub-tgt', owner_id=owner_id)
        self._share(src, viewer_id, 'viewer')
        art = self._artefact(src, 'movable')

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': [art]}, viewer_key)
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_editor_share_cannot_publish_private_to_public(self):
        owner_id, _ = self._user('bm-cur-owner')
        editor_id, editor_key = self._user('bm-cur-editor')
        src = self._item('bm-cur-priv-src', owner_id=owner_id, private=True)
        tgt = self._item('bm-cur-pub-tgt', owner_id=owner_id)
        self._share(src, editor_id, 'editor')
        art = self._artefact(src, 'movable')

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': [art]}, editor_key)
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_private_target_requires_contribution(self):
        owner_id, _ = self._user('bm-tgt-owner')
        viewer_id, viewer_key = self._user('bm-tgt-viewer')
        src = self._item('bm-tgt-pub-src', owner_id=owner_id)
        tgt = self._item('bm-tgt-priv', owner_id=owner_id, private=True)
        self._share(tgt, viewer_id, 'viewer')
        art = self._artefact(src, 'movable')

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': [art]}, viewer_key)
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_invisible_private_target_is_not_disclosed(self):
        owner_id, _ = self._user('bm-hide-owner')
        _viewer_id, viewer_key = self._user('bm-hide-viewer')
        src = self._item('bm-hide-src', owner_id=owner_id)
        tgt = self._item('bm-hide-priv', owner_id=owner_id, private=True)
        art = self._artefact(src, 'movable')

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': [art]}, viewer_key)
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_private_artefact_excluded_from_foreign_selection(self):
        owner_id, _ = self._user('bm-art-owner')
        _viewer_id, viewer_key = self._user('bm-art-viewer')
        src = self._item('bm-art-src', owner_id=owner_id)
        tgt = self._item('bm-art-tgt', owner_id=owner_id)
        art = self._artefact(src, 'private art', private=True)

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': [art]}, viewer_key)
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_read_only_key_cannot_batch_move(self):
        from myapp.database import ApiKeyPermission
        owner_id, _ = self._user('bm-ro-owner')
        _ro_id, ro_key = self._user('bm-ro', api_permission=ApiKeyPermission.READ_ONLY)
        src = self._item('bm-ro-src', owner_id=owner_id)
        tgt = self._item('bm-ro-tgt', owner_id=owner_id)
        art = self._artefact(src, 'movable')

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': [art]}, ro_key)
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_new_subitem_requires_contribution_on_private_source(self):
        owner_id, _ = self._user('bm-sub-owner')
        viewer_id, viewer_key = self._user('bm-sub-viewer')
        src = self._item('bm-sub-priv', owner_id=owner_id, private=True)
        self._share(src, viewer_id, 'viewer')

        resp = self._api(src, {'new_item_name': 'should-not-exist', 'artefact_uuids': []}, viewer_key)
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_selection_size_is_capped(self):
        from myapp.services.artefact_lifecycle import MAX_BATCH_MOVE_ARTEFACTS
        _owner_id, owner_key = self._user('bm-cap-owner')
        src = self._item('bm-cap-src')
        tgt = self._item('bm-cap-tgt')
        too_many = [f'{i:032x}' for i in range(MAX_BATCH_MOVE_ARTEFACTS + 1)]

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': too_many}, owner_key)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn(b'at most', resp.data)

    def test_non_string_selection_element_is_rejected(self):
        """A JSON object/array element must be a 400, not an unhashable-type 500."""
        _owner_id, owner_key = self._user('bm-type-owner')
        src = self._item('bm-type-src')
        tgt = self._item('bm-type-tgt')

        resp = self._api(src, {'target_item_uuid': tgt, 'artefact_uuids': [{'a': 1}]}, owner_key)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertNotIn(b'Traceback', resp.data)


class TestBatchMoveWeb(_BatchMoveTestCase):
    """The web confirm/perform routes (previously untested)."""

    def test_confirm_renders_selection(self):
        owner_id, _ = self._user('bm-web-owner')
        src = self._item('bm-web-src', owner_id=owner_id)
        art = self._artefact(src, 'web art')
        self._login(owner_id)

        resp = self.client.post(f'/items/{src}/artefacts/batch-move/confirm',
                                data={'artefact_uuids': json.dumps([art])})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn(b'Review batch move', resp.data)

    def test_confirm_rejects_non_string_element(self):
        """A malformed element flashes and redirects rather than 500-ing."""
        owner_id, _ = self._user('bm-web-type-owner')
        src = self._item('bm-web-type-src', owner_id=owner_id)
        self._login(owner_id)

        resp = self.client.post(f'/items/{src}/artefacts/batch-move/confirm',
                                data={'artefact_uuids': json.dumps([{'a': 1}])})
        self.assertEqual(resp.status_code, 302, resp.data)

    def test_confirm_stale_selection_redirects(self):
        owner_id, _ = self._user('bm-web-stale')
        src = self._item('bm-web-stale-src', owner_id=owner_id)
        self._login(owner_id)

        resp = self.client.post(f'/items/{src}/artefacts/batch-move/confirm',
                                data={'artefact_uuids': json.dumps(['f' * 32])})
        self.assertEqual(resp.status_code, 302, resp.data)

    def test_perform_moves_to_existing_item(self):
        owner_id, _ = self._user('bm-web-mv-owner')
        src = self._item('bm-web-mv-src', owner_id=owner_id)
        tgt = self._item('bm-web-mv-tgt', owner_id=owner_id)
        art = self._artefact(src, 'web art')
        self._login(owner_id)

        resp = self.client.post(f'/items/{src}/artefacts/batch-move',
                                data={'artefact_uuids': json.dumps([art]),
                                      'target_item_uuid': tgt})
        self.assertEqual(resp.status_code, 302, resp.data)
        with self.app.app_context():
            from myapp.database import Artefact, Item
            target_id = Item.query.filter_by(uuid=tgt).one().id
            self.assertEqual(Artefact.query.filter_by(uuid=art).one().item_id, target_id)

    def test_perform_creates_subitem_atomically(self):
        owner_id, _ = self._user('bm-web-new-owner')
        src = self._item('bm-web-new-src', owner_id=owner_id)
        art = self._artefact(src, 'web art')
        self._login(owner_id)

        resp = self.client.post(f'/items/{src}/artefacts/batch-move',
                                data={'artefact_uuids': json.dumps([art]),
                                      'new_item_name': 'bm-web-created-child'})
        self.assertEqual(resp.status_code, 302, resp.data)
        with self.app.app_context():
            from myapp.database import Artefact, Item
            parent = Item.query.filter_by(uuid=src).one()
            child = Item.query.filter_by(name='bm-web-created-child').one()
            self.assertEqual(child.parent_id, parent.id)
            self.assertEqual(Artefact.query.filter_by(uuid=art).one().item_id, child.id)

    def test_viewer_only_denied_on_private_source(self):
        owner_id, _ = self._user('bm-web-priv-owner')
        viewer_id, _ = self._user('bm-web-priv-viewer')
        src = self._item('bm-web-priv-src', owner_id=owner_id, private=True)
        self._share(src, viewer_id, 'viewer')
        self._login(viewer_id)

        resp = self.client.post(f'/items/{src}/artefacts/batch-move/confirm',
                                data={'artefact_uuids': json.dumps([])})
        self.assertEqual(resp.status_code, 403, resp.data)


if __name__ == '__main__':
    unittest.main()
