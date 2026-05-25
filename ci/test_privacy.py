"""
Tests for private items and artefacts.

Covers:
  - Privacy inheritance (strict descend) via recompute_item_privacy /
    Item.private_effective and Artefact.effective_private.
  - Per-object visibility helpers (can_view_item / can_view_artefact) for
    owner, admin, another user, and anonymous viewers.
  - SQLAlchemy visibility filter clauses used by list/search queries.
  - End-to-end REST API behaviour: ownership capture, list filtering, and
    404 hiding for non-owners (with the worker key seeing everything).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_privacy -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-privacy-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


def _make_user(db, username, *, is_admin=False):
    from myapp.database import ApiKey, ApiKeyPermission, User, UserPermission
    user = User(username=username, password_hash='x', is_admin=is_admin,
                permission=UserPermission.READ_WRITE, can_use_api=True)
    db.session.add(user)
    db.session.flush()
    key_obj, raw = ApiKey.create(user_id=user.id, name=f'{username}-key',
                                 permission=ApiKeyPermission.READ_WRITE)
    db.session.add(key_obj)
    db.session.commit()
    return user, raw


class TestPrivacyInheritance(unittest.TestCase):
    """Strict-descend inheritance of privacy down the item hierarchy."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def _new_item(self, name, parent=None, is_private=False, owner=None):
        from myapp.database import Item
        item = Item(name=name, parent_id=parent.id if parent else None,
                    is_private=is_private, owner_id=owner.id if owner else None)
        self.db.session.add(item)
        self.db.session.flush()
        return item

    def test_parent_private_descends_to_children(self):
        from myapp.utils.privacy import recompute_item_privacy
        with self.app.app_context():
            root = self._new_item('root', is_private=True)
            child = self._new_item('child', parent=root)
            grandchild = self._new_item('grandchild', parent=child)
            self.db.session.flush()
            recompute_item_privacy(root)
            self.db.session.commit()
            self.assertTrue(root.private_effective)
            self.assertTrue(child.private_effective)
            self.assertTrue(grandchild.private_effective)
            # The explicit flag is unchanged on descendants.
            self.assertFalse(child.is_private)

    def test_toggling_parent_off_clears_descendants(self):
        from myapp.utils.privacy import recompute_item_privacy
        with self.app.app_context():
            root = self._new_item('r2', is_private=True)
            child = self._new_item('c2', parent=root)
            self.db.session.flush()
            recompute_item_privacy(root)
            self.db.session.commit()
            self.assertTrue(child.private_effective)
            # Now make the root public again.
            root.is_private = False
            recompute_item_privacy(root)
            self.db.session.commit()
            self.assertFalse(root.private_effective)
            self.assertFalse(child.private_effective)

    def test_child_private_does_not_affect_parent(self):
        from myapp.utils.privacy import recompute_item_privacy
        with self.app.app_context():
            root = self._new_item('r3')
            child = self._new_item('c3', parent=root, is_private=True)
            self.db.session.flush()
            recompute_item_privacy(root)
            self.db.session.commit()
            self.assertFalse(root.private_effective)
            self.assertTrue(child.private_effective)

    def test_artefact_effective_private_via_item(self):
        from myapp.database import Artefact, ArtefactType
        from myapp.utils.privacy import recompute_item_privacy
        with self.app.app_context():
            root = self._new_item('r4', is_private=True)
            recompute_item_privacy(root)
            self.db.session.flush()
            art = Artefact(item_id=root.id, label='a', artefact_type=ArtefactType.UNKNOWN,
                           original_filename='a.bin', storage_path='a.bin')
            self.db.session.add(art)
            self.db.session.commit()
            self.assertFalse(art.is_private)
            self.assertTrue(art.effective_private)

    def test_artefact_private_in_public_item(self):
        from myapp.database import Artefact, ArtefactType
        with self.app.app_context():
            pub = self._new_item('r5')
            self.db.session.flush()
            art = Artefact(item_id=pub.id, label='b', artefact_type=ArtefactType.UNKNOWN,
                           original_filename='b.bin', storage_path='b.bin', is_private=True)
            self.db.session.add(art)
            self.db.session.commit()
            self.assertFalse(pub.private_effective)
            self.assertTrue(art.effective_private)


class TestVisibilityHelpers(unittest.TestCase):
    """can_view_* helpers and SQLAlchemy filter clauses."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            cls.owner, _ = _make_user(db, 'owner-u')
            cls.other, _ = _make_user(db, 'other-u')
            cls.admin, _ = _make_user(db, 'admin-u', is_admin=True)
            cls.owner_id = cls.owner.id
            cls.other_id = cls.other.id
            cls.admin_id = cls.admin.id

    def _ctx(self):
        return self.app.app_context()

    def test_can_view_item(self):
        from myapp.database import Item, User
        from myapp.utils.privacy import recompute_item_privacy
        from myapp.visibility import can_view_item
        with self._ctx():
            owner = self.db.session.get(User, self.owner_id)
            other = self.db.session.get(User, self.other_id)
            admin = self.db.session.get(User, self.admin_id)
            item = Item(name='priv', is_private=True, owner_id=owner.id)
            self.db.session.add(item)
            self.db.session.flush()
            recompute_item_privacy(item)
            self.db.session.commit()
            self.assertTrue(can_view_item(item, owner))
            self.assertTrue(can_view_item(item, admin))
            self.assertFalse(can_view_item(item, other))
            self.assertFalse(can_view_item(item, None))           # anonymous
            self.assertTrue(can_view_item(item, None, sees_all=True))  # worker

    def test_public_item_visible_to_all(self):
        from myapp.database import Item
        from myapp.visibility import can_view_item
        with self._ctx():
            item = Item(name='pub', is_private=False)
            self.db.session.add(item)
            self.db.session.commit()
            self.assertTrue(can_view_item(item, None))

    def test_item_visibility_clause_filters(self):
        from myapp.database import Item, User
        from myapp.utils.privacy import recompute_item_privacy
        from myapp.visibility import item_visibility_clause
        with self._ctx():
            owner = self.db.session.get(User, self.owner_id)
            other = self.db.session.get(User, self.other_id)
            admin = self.db.session.get(User, self.admin_id)
            secret = Item(name='clause-secret', is_private=True, owner_id=owner.id)
            public = Item(name='clause-public')
            self.db.session.add_all([secret, public])
            self.db.session.flush()
            recompute_item_privacy(secret)
            recompute_item_privacy(public)
            self.db.session.commit()

            def names(user, **kw):
                rows = Item.query.filter(item_visibility_clause(user, **kw)).all()
                return {i.name for i in rows}

            owner_names = names(owner)
            self.assertIn('clause-secret', owner_names)
            self.assertIn('clause-public', owner_names)

            other_names = names(other)
            self.assertNotIn('clause-secret', other_names)
            self.assertIn('clause-public', other_names)

            self.assertIn('clause-secret', names(admin))
            self.assertIn('clause-secret', names(None, sees_all=True))
            self.assertNotIn('clause-secret', names(None))

    def test_artefact_visibility_clause_filters(self):
        from myapp.database import Artefact, ArtefactType, Item, User
        from myapp.utils.privacy import recompute_item_privacy
        from myapp.visibility import artefact_visibility_clause
        with self._ctx():
            owner = self.db.session.get(User, self.owner_id)
            other = self.db.session.get(User, self.other_id)
            pub_item = Item(name='av-pub')
            self.db.session.add(pub_item)
            self.db.session.flush()
            recompute_item_privacy(pub_item)
            a_pub = Artefact(item_id=pub_item.id, label='av-a-pub', artefact_type=ArtefactType.UNKNOWN,
                             original_filename='x', storage_path='x1', owner_id=owner.id)
            a_priv = Artefact(item_id=pub_item.id, label='av-a-priv', artefact_type=ArtefactType.UNKNOWN,
                              original_filename='y', storage_path='y1', owner_id=owner.id, is_private=True)
            self.db.session.add_all([a_pub, a_priv])
            self.db.session.commit()

            def labels(user):
                rows = (Artefact.query
                        .join(Item, Artefact.item_id == Item.id)
                        .filter(Artefact.item_id == pub_item.id)
                        .filter(artefact_visibility_clause(user))
                        .all())
                return {a.label for a in rows}

            owner_labels = labels(owner)
            self.assertIn('av-a-pub', owner_labels)
            self.assertIn('av-a-priv', owner_labels)

            other_labels = labels(other)
            self.assertIn('av-a-pub', other_labels)
            self.assertNotIn('av-a-priv', other_labels)


class TestPrivacyApi(unittest.TestCase):
    """End-to-end REST API ownership capture and visibility filtering."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            _, cls.key_a = _make_user(db, 'api-a')
            _, cls.key_b = _make_user(db, 'api-b')

    def _post_item(self, key, name, is_private=False):
        return self.client.post('/api/items',
                                headers={'X-API-Key': key},
                                json={'name': name, 'is_private': is_private})

    def test_owner_recorded_and_private_flag(self):
        resp = self._post_item(self.key_a, 'apriv-1', is_private=True)
        self.assertEqual(resp.status_code, 201, resp.data)
        data = resp.get_json()
        self.assertEqual(data['owner'], 'api-a')
        self.assertTrue(data['is_private'])
        self.assertTrue(data['private_effective'])

    def test_list_hides_other_users_private_item(self):
        r = self._post_item(self.key_a, 'apriv-2', is_private=True)
        uuid = r.get_json()['uuid']

        # Owner sees it
        names_a = {i['name'] for i in self.client.get(
            '/api/items?per_page=100', headers={'X-API-Key': self.key_a}).get_json()['items']}
        self.assertIn('apriv-2', names_a)

        # Other user does not
        names_b = {i['name'] for i in self.client.get(
            '/api/items?per_page=100', headers={'X-API-Key': self.key_b}).get_json()['items']}
        self.assertNotIn('apriv-2', names_b)

        # Worker sees everything
        names_w = {i['name'] for i in self.client.get(
            '/api/items?per_page=100', headers={'X-API-Key': _WORKER_KEY}).get_json()['items']}
        self.assertIn('apriv-2', names_w)

        # Direct GET: owner 200, other 404, worker 200
        self.assertEqual(self.client.get(f'/api/items/{uuid}', headers={'X-API-Key': self.key_a}).status_code, 200)
        self.assertEqual(self.client.get(f'/api/items/{uuid}', headers={'X-API-Key': self.key_b}).status_code, 404)
        self.assertEqual(self.client.get(f'/api/items/{uuid}', headers={'X-API-Key': _WORKER_KEY}).status_code, 200)

    def test_public_item_visible_to_other_user(self):
        r = self._post_item(self.key_a, 'apub-1', is_private=False)
        uuid = r.get_json()['uuid']
        self.assertEqual(self.client.get(f'/api/items/{uuid}', headers={'X-API-Key': self.key_b}).status_code, 200)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
