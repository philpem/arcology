"""
Tests for per-user and per-group item sharing (ACL).

Covers:
  - Direct user share makes a private item visible to that user.
  - Group share: adding a user to a group grants access to items shared with
    that group.
  - Share on an ancestor item descends to the child (via recursive CTE in
    item_visibility_clause).
  - Revoking a share removes access.
  - Non-owner cannot add/remove shares (API returns 403).
  - Worker and admin are unaffected by shares (they see everything).
  - item_visibility_clause includes shared private items in query results.
  - arcology-prefix groups cannot be used for sharing (API returns 400).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_sharing -v
"""

import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-sharing-test-secret-key-not-for-production')
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


def _make_private_item(db, name, owner, parent=None):
    from myapp.database import Item
    from myapp.utils.privacy import recompute_item_privacy
    item = Item(name=name, is_private=True, owner_id=owner.id,
                parent_id=parent.id if parent else None)
    db.session.add(item)
    db.session.flush()
    recompute_item_privacy(item)
    db.session.commit()
    return item


class TestSharingModels(unittest.TestCase):
    """Unit-level model tests for Group and ItemShare."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def test_group_creation(self):
        from myapp.database import Group
        with self.app.app_context():
            g = Group(name='test-group', source='local')
            self.db.session.add(g)
            self.db.session.commit()
            fetched = Group.query.filter_by(name='test-group').first()
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.source, 'local')

    def test_user_group_membership(self):
        from myapp.database import Group
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'share-model-owner')
            other, _ = _make_user(self.db, 'share-model-other')
            g = Group(name='model-test-group', source='local')
            self.db.session.add(g)
            self.db.session.flush()
            g.members.append(other)
            self.db.session.commit()
            self.assertIn(other, g.members)
            self.assertIn(g, other.groups)

    def test_item_share_user(self):
        from myapp.database import ItemShare
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'share-item-owner')
            other, _ = _make_user(self.db, 'share-item-other')
            item = _make_private_item(self.db, 'Shared Item', owner)
            share = ItemShare(item_id=item.id, user_id=other.id)
            self.db.session.add(share)
            self.db.session.commit()
            self.assertEqual(len(item.shares), 1)
            self.assertEqual(item.shares[0].user_id, other.id)

    def test_item_share_group(self):
        from myapp.database import Group, ItemShare
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'share-grp-owner')
            g = Group(name='item-share-grp', source='local')
            self.db.session.add(g)
            self.db.session.flush()
            item = _make_private_item(self.db, 'Group Shared Item', owner)
            share = ItemShare(item_id=item.id, group_id=g.id)
            self.db.session.add(share)
            self.db.session.commit()
            self.assertEqual(len(item.shares), 1)
            self.assertEqual(item.shares[0].group_id, g.id)

    def test_item_share_cascade_delete_on_item(self):
        from myapp.database import ItemShare
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'share-del-owner')
            other, _ = _make_user(self.db, 'share-del-other')
            item = _make_private_item(self.db, 'Delete Item', owner)
            item_id = item.id
            share = ItemShare(item_id=item.id, user_id=other.id)
            self.db.session.add(share)
            self.db.session.commit()
            # Delete the item; shares should cascade
            self.db.session.delete(item)
            self.db.session.commit()
            remaining = ItemShare.query.filter_by(item_id=item_id).all()
            self.assertEqual(remaining, [])


class TestSharingVisibility(unittest.TestCase):
    """Tests for can_view_item / item_visibility_clause with shares."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def test_direct_user_share_grants_access(self):
        from myapp.database import ItemShare
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'vis-owner1')
            viewer, _ = _make_user(self.db, 'vis-viewer1')
            item = _make_private_item(self.db, 'VIS Direct Share', owner)
            # Before share: not visible
            self.assertFalse(can_view_item(item, viewer))
            # Add share
            share = ItemShare(item_id=item.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()
            self.assertTrue(can_view_item(item, viewer))

    def test_group_share_grants_access_to_member(self):
        from myapp.database import Group, ItemShare
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'vis-owner2')
            member, _ = _make_user(self.db, 'vis-member2')
            nonmember, _ = _make_user(self.db, 'vis-nonmember2')
            item = _make_private_item(self.db, 'VIS Group Share', owner)
            g = Group(name='vis-group2', source='local')
            self.db.session.add(g)
            self.db.session.flush()
            g.members.append(member)
            share = ItemShare(item_id=item.id, group_id=g.id)
            self.db.session.add(share)
            self.db.session.commit()
            # Member can see it, non-member cannot
            self.assertTrue(can_view_item(item, member))
            self.assertFalse(can_view_item(item, nonmember))

    def test_ancestor_share_descends_to_child(self):
        from myapp.database import Item, ItemShare
        from myapp.utils.privacy import recompute_item_privacy
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'vis-owner3')
            viewer, _ = _make_user(self.db, 'vis-viewer3')
            parent = _make_private_item(self.db, 'VIS Parent', owner)
            child = Item(name='VIS Child', is_private=False, owner_id=owner.id,
                         parent_id=parent.id)
            self.db.session.add(child)
            self.db.session.flush()
            recompute_item_privacy(child)
            self.db.session.commit()
            # Child inherits private_effective from parent
            self.assertTrue(child.private_effective)
            # Share on parent
            share = ItemShare(item_id=parent.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()
            # Both parent and child should be visible
            self.assertTrue(can_view_item(parent, viewer))
            self.assertTrue(can_view_item(child, viewer))

    def test_revoking_share_removes_access(self):
        from myapp.database import ItemShare
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'vis-owner4')
            viewer, _ = _make_user(self.db, 'vis-viewer4')
            item = _make_private_item(self.db, 'VIS Revoke', owner)
            share = ItemShare(item_id=item.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()
            self.assertTrue(can_view_item(item, viewer))
            self.db.session.delete(share)
            self.db.session.commit()
            self.assertFalse(can_view_item(item, viewer))

    def test_admin_sees_all(self):
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'vis-owner5')
            admin, _ = _make_user(self.db, 'vis-admin5', is_admin=True)
            item = _make_private_item(self.db, 'VIS Admin', owner)
            # Admin sees private items even without a share
            self.assertTrue(can_view_item(item, admin))

    def test_item_visibility_clause_includes_shared(self):
        from myapp.database import Item, ItemShare
        from myapp.visibility import item_visibility_clause
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'vis-owner6')
            viewer, _ = _make_user(self.db, 'vis-viewer6')
            item = _make_private_item(self.db, 'VIS Clause', owner)
            # Viewer should NOT see it in list query before share
            results = Item.query.filter(item_visibility_clause(viewer)).all()
            item_ids = [i.id for i in results]
            self.assertNotIn(item.id, item_ids)
            # Add share
            share = ItemShare(item_id=item.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()
            # Now should appear
            results = Item.query.filter(item_visibility_clause(viewer)).all()
            item_ids = [i.id for i in results]
            self.assertIn(item.id, item_ids)

    def test_anonymous_cannot_see_shared_private_items(self):
        from myapp.database import ItemShare
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'vis-owner7')
            viewer, _ = _make_user(self.db, 'vis-viewer7')
            item = _make_private_item(self.db, 'VIS Anon', owner)
            share = ItemShare(item_id=item.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()
            # Anonymous user (None) should still not see it
            self.assertFalse(can_view_item(item, None))


class TestSharingApi(unittest.TestCase):
    """Integration tests for share management via the REST API."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            owner, owner_key = _make_user(db, 'api-share-owner')
            other, other_key = _make_user(db, 'api-share-other')
            admin, admin_key = _make_user(db, 'api-share-admin', is_admin=True)
            # Store IDs and keys (not ORM objects, to avoid DetachedInstanceError)
            cls.owner_id = owner.id
            cls.owner_key = owner_key
            cls.other_id = other.id
            cls.other_key = other_key
            cls.admin_id = admin.id
            cls.admin_key = admin_key

    def setUp(self):
        self.client = self.app.test_client()

    def _auth(self, key):
        return {'X-API-Key': key}

    def _make_item(self):
        from myapp.database import User
        with self.app.app_context():
            owner = self.db.session.get(User, self.owner_id)
            item = _make_private_item(self.db, 'API Share Item', owner)
            return item.uuid

    def test_owner_can_list_shares(self):
        uuid = self._make_item()
        r = self.client.get(f'/api/items/{uuid}/shares', headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.data)
        self.assertIsInstance(data, list)

    def test_owner_can_add_user_share(self):
        uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.data)
        self.assertIsNotNone(data.get('id'))

    def test_non_owner_cannot_add_share(self):
        uuid = self._make_item()
        payload = json.dumps({'user_id': self.owner_id})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.other_key))
        # other cannot view the private item at all → 404
        self.assertIn(r.status_code, (403, 404))

    def test_worker_cannot_add_share(self):
        uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(_WORKER_KEY))
        self.assertEqual(r.status_code, 403)

    def test_owner_can_remove_share(self):
        uuid = self._make_item()
        # Add first
        payload = json.dumps({'user_id': self.other_id})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)
        share_id = json.loads(r.data)['id']
        # Now remove
        r = self.client.delete(f'/api/items/{uuid}/shares/{share_id}',
                               headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 200)

    def test_arcology_prefix_group_rejected(self):
        from myapp.database import Group
        uuid = self._make_item()
        with self.app.app_context():
            g = Group(name='arcology-test-reserved', source='oidc')
            self.db.session.add(g)
            self.db.session.commit()
            gid = g.id
        payload = json.dumps({'group_id': gid})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 400)

    def test_admin_sees_shared_item_via_admin_flag(self):
        """Admin always sees private items regardless of shares."""
        uuid = self._make_item()
        r = self.client.get(f'/api/items/{uuid}', headers=self._auth(self.admin_key))
        self.assertEqual(r.status_code, 200)

    def test_worker_sees_all_items(self):
        """Worker key grants sees_all=True so private items are always visible."""
        uuid = self._make_item()
        r = self.client.get(f'/api/items/{uuid}', headers=self._auth(_WORKER_KEY))
        self.assertEqual(r.status_code, 200)

    def test_add_share_by_username(self):
        from myapp.database import User
        uuid = self._make_item()
        with self.app.app_context():
            other = self.db.session.get(User, self.other_id)
            username = other.username
        payload = json.dumps({'username': username})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)

    def test_add_share_by_group_name(self):
        from myapp.database import Group
        uuid = self._make_item()
        with self.app.app_context():
            g = Group(name='api-named-group', source='local')
            self.db.session.add(g)
            self.db.session.commit()
            gname = g.name
        payload = json.dumps({'group_name': gname})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)

    def test_duplicate_share_returns_409(self):
        uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id})
        r1 = self.client.post(f'/api/items/{uuid}/shares',
                              data=payload, content_type='application/json',
                              headers=self._auth(self.owner_key))
        self.assertEqual(r1.status_code, 201)
        r2 = self.client.post(f'/api/items/{uuid}/shares',
                              data=payload, content_type='application/json',
                              headers=self._auth(self.owner_key))
        self.assertEqual(r2.status_code, 409)

    def test_shared_user_cannot_list_shares(self):
        """A user who only has share-viewer access cannot enumerate other shares."""
        uuid = self._make_item()
        # Grant other user view access
        payload = json.dumps({'user_id': self.other_id})
        self.client.post(f'/api/items/{uuid}/shares',
                         data=payload, content_type='application/json',
                         headers=self._auth(self.owner_key))
        # other can view the item but must not see the share list
        r = self.client.get(f'/api/items/{uuid}/shares', headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 403)

    def test_worker_cannot_list_shares(self):
        """Worker key is blocked from listing shares."""
        uuid = self._make_item()
        r = self.client.get(f'/api/items/{uuid}/shares', headers=self._auth(_WORKER_KEY))
        self.assertEqual(r.status_code, 403)

    def test_arcology_prefix_case_insensitive_rejected(self):
        """Mixed-case arcology- prefix is also blocked."""
        from myapp.database import Group
        uuid = self._make_item()
        with self.app.app_context():
            g = Group(name='Arcology-Mixed', source='local')
            self.db.session.add(g)
            self.db.session.commit()
            gid = g.id
        payload = json.dumps({'group_id': gid})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 400)

    def test_nonexistent_user_returns_404(self):
        """Sharing with a non-existent user_id returns 404, not 500 or misleading error."""
        uuid = self._make_item()
        payload = json.dumps({'user_id': 999999})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 404)

    def test_nonexistent_group_returns_404(self):
        uuid = self._make_item()
        payload = json.dumps({'group_id': 999999})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 404)

    def test_public_item_cannot_be_shared(self):
        """Shares are only valid on private/effectively-private items."""
        from myapp.database import Item, User
        with self.app.app_context():
            owner = self.db.session.get(User, self.owner_id)
            item = Item(name='Public Share Reject', is_private=False, owner_id=owner.id)
            self.db.session.add(item)
            self.db.session.commit()
            uuid = item.uuid
        payload = json.dumps({'user_id': self.other_id})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 400)

    def test_shared_user_cannot_upload_to_private_item(self):
        """Share recipients can view private items but cannot add artefacts."""
        uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)
        r = self.client.post(f'/api/items/{uuid}/artefacts/upload',
                             data={'label': 'blocked'},
                             headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 403)

    def test_shared_user_cannot_init_chunked_upload_to_private_item(self):
        uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id})
        r = self.client.post(f'/api/items/{uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)
        r = self.client.post('/api/uploads/chunked/init',
                             data=json.dumps({
                                 'filename': 'blocked.scp',
                                 'total_chunks': 1,
                                 'item_uuid': uuid,
                                 'label': 'blocked',
                             }),
                             content_type='application/json',
                             headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 403)

    def test_shared_user_cannot_create_child_under_private_parent(self):
        """A share recipient must not write into a private item hierarchy."""
        parent_uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id})
        r = self.client.post(f'/api/items/{parent_uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)
        r = self.client.post('/api/items',
                             data=json.dumps({'name': 'Forbidden Child', 'parent_uuid': parent_uuid}),
                             content_type='application/json',
                             headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 403)

    def test_editor_share_can_create_child_under_private_parent(self):
        """Editor shares permit content contribution within a private item hierarchy."""
        parent_uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id, 'permission': 'editor'})
        r = self.client.post(f'/api/items/{parent_uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.data)
        self.assertEqual(data['permission'], 'editor')
        r = self.client.post('/api/items',
                             data=json.dumps({'name': 'Editor Child', 'parent_uuid': parent_uuid}),
                             content_type='application/json',
                             headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 201)

    def test_curator_share_can_create_child_under_private_parent(self):
        """Curator (top-tier) shares also permit content contribution."""
        parent_uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id, 'permission': 'curator'})
        r = self.client.post(f'/api/items/{parent_uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 201)
        data = json.loads(r.data)
        self.assertEqual(data['permission'], 'curator')
        r = self.client.post('/api/items',
                             data=json.dumps({'name': 'Curator Child', 'parent_uuid': parent_uuid}),
                             content_type='application/json',
                             headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 201)

    def test_curator_share_can_toggle_privacy(self):
        """Curator shares allow toggling is_private on the item."""
        parent_uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id, 'permission': 'curator'})
        self.client.post(f'/api/items/{parent_uuid}/shares',
                         data=payload, content_type='application/json',
                         headers=self._auth(self.owner_key))
        # Curator can set is_private=False (publish the item)
        r = self.client.put(f'/api/items/{parent_uuid}',
                            data=json.dumps({'is_private': False}),
                            content_type='application/json',
                            headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 200)

    def test_editor_share_cannot_toggle_privacy(self):
        """Editor shares do NOT allow toggling is_private."""
        parent_uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id, 'permission': 'editor'})
        self.client.post(f'/api/items/{parent_uuid}/shares',
                         data=payload, content_type='application/json',
                         headers=self._auth(self.owner_key))
        r = self.client.put(f'/api/items/{parent_uuid}',
                            data=json.dumps({'is_private': False}),
                            content_type='application/json',
                            headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 403)

    def test_curator_share_can_manage_shares(self):
        """Curator shares allow adding further shares to the item."""
        parent_uuid = self._make_item()
        # Grant curator access to other
        payload = json.dumps({'user_id': self.other_id, 'permission': 'curator'})
        self.client.post(f'/api/items/{parent_uuid}/shares',
                         data=payload, content_type='application/json',
                         headers=self._auth(self.owner_key))
        # Create a third user to share with
        with self.app.app_context():
            third, third_key = _make_user(self.db, 'api-curator-share-third')
            third_id = third.id
        # Curator adds a viewer share for the third user
        r = self.client.post(f'/api/items/{parent_uuid}/shares',
                             data=json.dumps({'user_id': third_id, 'permission': 'viewer'}),
                             content_type='application/json',
                             headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 201)

    def test_editor_share_cannot_manage_shares(self):
        """Editor shares do NOT allow managing shares."""
        parent_uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id, 'permission': 'editor'})
        self.client.post(f'/api/items/{parent_uuid}/shares',
                         data=payload, content_type='application/json',
                         headers=self._auth(self.owner_key))
        with self.app.app_context():
            fourth, _ = _make_user(self.db, 'api-editor-share-fourth')
            fourth_id = fourth.id
        r = self.client.post(f'/api/items/{parent_uuid}/shares',
                             data=json.dumps({'user_id': fourth_id, 'permission': 'viewer'}),
                             content_type='application/json',
                             headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 403)

    def test_invalid_share_permission_rejected(self):
        parent_uuid = self._make_item()
        payload = json.dumps({'user_id': self.other_id, 'permission': 'owner'})
        r = self.client.post(f'/api/items/{parent_uuid}/shares',
                             data=payload, content_type='application/json',
                             headers=self._auth(self.owner_key))
        self.assertEqual(r.status_code, 400)


class TestOidcGroupSync(unittest.TestCase):
    """OIDC group sync behaviour."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['OIDC_GROUP_SYNC_ENABLED'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def test_oidc_sync_links_local_group_on_name_collision(self):
        """A local group with the same name as an OIDC claim is linked to the IdP.

        This mirrors how local user accounts are linked on first SSO login.
        Existing ItemShare rows on the group are preserved; future membership
        is controlled by the IdP.
        """
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import Group, User, UserPermission
        with self.app.app_context():
            local_g = Group(name='link-collision-group', source='local')
            self.db.session.add(local_g)
            user = User(username='sync-link-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add(user)
            self.db.session.commit()
            local_id = local_g.id

            _sync_groups(user, {'groups': ['link-collision-group']})
            self.db.session.commit()

            # Group must now be IdP-managed (same row, updated in place)
            g = self.db.session.get(Group, local_id)
            self.assertEqual(g.source, 'oidc')
            self.assertEqual(g.oidc_claim_name, 'link-collision-group')
            # User is a member because IdP says so
            self.assertIn(g, user.groups)

    def test_oidc_sync_linked_group_removed_when_no_longer_in_claim(self):
        """Once linked, a group is removed from the user when the IdP drops the claim."""
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import Group, User, UserPermission
        with self.app.app_context():
            g = Group(name='transient-oidc-group', source='oidc',
                      oidc_claim_name='transient-oidc-group')
            user = User(username='sync-transient-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add_all([g, user])
            self.db.session.flush()
            user.groups = [g]
            self.db.session.commit()

            # IdP no longer includes the group
            _sync_groups(user, {'groups': []})
            self.db.session.commit()

            self.assertNotIn(g, user.groups)

    def test_oidc_sync_reserved_prefix_skipped(self):
        """arcology- prefix groups (any case) are not synced."""
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import User, UserPermission
        with self.app.app_context():
            user = User(username='sync-reserved-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add(user)
            self.db.session.commit()
            _sync_groups(user, {'groups': ['Arcology-Admin', 'arcology-read-write', 'normal-team']})
            self.db.session.commit()
            group_names = {g.name for g in user.groups}
            self.assertNotIn('Arcology-Admin', group_names)
            self.assertNotIn('arcology-read-write', group_names)
            self.assertIn('normal-team', group_names)

    def test_oidc_sync_strips_before_reserved_prefix_check(self):
        """Whitespace cannot bypass the reserved arcology- group prefix."""
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import User, UserPermission
        with self.app.app_context():
            user = User(username='sync-reserved-space-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add(user)
            self.db.session.commit()
            _sync_groups(user, {'groups': [' arcology-admin ', ' normal-spaced ']})
            self.db.session.commit()
            group_names = {g.name for g in user.groups}
            self.assertNotIn('arcology-admin', group_names)
            self.assertIn('normal-spaced', group_names)

    def test_oidc_sync_preserves_local_memberships(self):
        """Sync only removes OIDC-sourced groups, not local ones."""
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import Group, User, UserPermission
        with self.app.app_context():
            local_g = Group(name='local-only-group-preserve', source='local')
            oidc_g = Group(name='oidc-current-group', source='oidc',
                           oidc_claim_name='oidc-current-group')
            user = User(username='sync-preserve-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add_all([local_g, oidc_g, user])
            self.db.session.flush()
            user.groups = [local_g, oidc_g]
            self.db.session.commit()

            # Sync with empty OIDC group list — removes oidc_g but keeps local_g
            _sync_groups(user, {'groups': []})
            self.db.session.commit()

            group_names = {g.name for g in user.groups}
            self.assertIn('local-only-group-preserve', group_names)
            self.assertNotIn('oidc-current-group', group_names)

    def test_oidc_sync_missing_claim_is_noop(self):
        """Absent group claim (IdP not configured) does not clear memberships."""
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import Group, User, UserPermission
        with self.app.app_context():
            oidc_g = Group(name='noop-oidc-group', source='oidc',
                           oidc_claim_name='noop-oidc-group')
            user = User(username='sync-noop-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add_all([oidc_g, user])
            self.db.session.flush()
            user.groups = [oidc_g]
            self.db.session.commit()

            # Userinfo has no 'groups' key at all → should be a no-op
            _sync_groups(user, {})
            self.db.session.commit()

            # Membership must be untouched
            self.assertIn(oidc_g, user.groups)

    def test_oidc_sync_idp_case_shift_does_not_lock_out_user(self):
        """If IdP changes claim case (Engineering→engineering), OIDC_GROUP_LINK_LOCAL=False
        must NOT remove the user from their OIDC-managed group."""
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import Group, User, UserPermission
        with self.app.app_context():
            self.app.config['OIDC_GROUP_LINK_LOCAL'] = False
            # Group already OIDC-managed with uppercase claim name
            oidc_g = Group(name='case-shift-group', source='oidc',
                           oidc_claim_name='Case-Shift-Group')
            user = User(username='sync-caseshift-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add_all([oidc_g, user])
            self.db.session.flush()
            user.groups = [oidc_g]
            self.db.session.commit()
            oidc_id = oidc_g.id

            # IdP sends the claim in lowercase now
            _sync_groups(user, {'groups': ['case-shift-group']})
            self.db.session.commit()

            g = self.db.session.get(Group, oidc_id)
            # oidc_claim_name updated to lowercase
            self.assertEqual(g.oidc_claim_name, 'case-shift-group')
            # User must STILL be a member (no lockout)
            self.assertIn(g, user.groups)

            self.app.config['OIDC_GROUP_LINK_LOCAL'] = True  # restore

    def test_oidc_sync_link_local_disabled_skips_collision(self):
        """OIDC_GROUP_LINK_LOCAL=False refuses to link a same-named local group."""
        from myapp.blueprints.oidc_auth import _sync_groups
        from myapp.database import Group, User, UserPermission
        with self.app.app_context():
            self.app.config['OIDC_GROUP_LINK_LOCAL'] = False
            local_g = Group(name='no-link-local-group', source='local')
            user = User(username='sync-nolink-user', password_hash='x',
                        is_admin=False, permission=UserPermission.READ_WRITE,
                        can_use_api=False, oidc_managed=True)
            self.db.session.add_all([local_g, user])
            self.db.session.commit()
            local_id = local_g.id

            _sync_groups(user, {'groups': ['no-link-local-group']})
            self.db.session.commit()

            # Group must still be local (not linked)
            g = self.db.session.get(Group, local_id)
            self.assertEqual(g.source, 'local')
            # User must NOT be a member (collision skipped)
            self.assertNotIn(g, user.groups)

            self.app.config['OIDC_GROUP_LINK_LOCAL'] = True  # restore


class TestSharingAdditional(unittest.TestCase):
    """Additional sharing tests: artefact visibility, deep recursion, item move."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def test_artefact_visible_via_item_share(self):
        """An artefact on a shared private item is visible to the share recipient."""
        from myapp.database import Artefact, ArtefactType, ItemShare
        from myapp.visibility import can_view_artefact
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'artefact-vis-owner')
            viewer, _ = _make_user(self.db, 'artefact-vis-viewer')
            item = _make_private_item(self.db, 'ArtVis Item', owner)
            artefact = Artefact(
                item_id=item.id,
                label='test.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='test.scp',
                storage_path='dummy/test.scp',
            )
            self.db.session.add(artefact)
            self.db.session.commit()

            # Before share: artefact not visible
            self.assertFalse(can_view_artefact(artefact, viewer))

            # Add item share
            share = ItemShare(item_id=item.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()

            # After share: artefact visible
            self.assertTrue(can_view_artefact(artefact, viewer))

    def test_public_item_share_does_not_expose_private_artefact(self):
        """A stale/public-item share must not grant access to artefact-private data."""
        from myapp.database import Artefact, ArtefactType, Item, ItemShare
        from myapp.visibility import can_view_artefact
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'public-art-owner')
            viewer, _ = _make_user(self.db, 'public-art-viewer')
            item = Item(name='Public With Private Artefact', is_private=False,
                        owner_id=owner.id)
            self.db.session.add(item)
            self.db.session.flush()
            artefact = Artefact(
                item_id=item.id,
                label='private.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='private.scp',
                storage_path='dummy/private.scp',
                is_private=True,
                owner_id=owner.id,
            )
            share = ItemShare(item_id=item.id, user_id=viewer.id)
            self.db.session.add_all([artefact, share])
            self.db.session.commit()

            self.assertFalse(can_view_artefact(artefact, viewer))

    def test_private_artefact_on_shared_private_item_is_visible(self):
        """Item shares expose artefact-private contents inside shared private items."""
        from myapp.database import Artefact, ArtefactType, ItemShare
        from myapp.visibility import can_view_artefact
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'private-art-owner')
            viewer, _ = _make_user(self.db, 'private-art-viewer')
            item = _make_private_item(self.db, 'Private Item With Private Artefact', owner)
            artefact = Artefact(
                item_id=item.id,
                label='private-under-private.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='private-under-private.scp',
                storage_path='dummy/private-under-private.scp',
                is_private=True,
                owner_id=owner.id,
            )
            share = ItemShare(item_id=item.id, user_id=viewer.id)
            self.db.session.add_all([artefact, share])
            self.db.session.commit()

            self.assertTrue(can_view_artefact(artefact, viewer))

    def test_share_descends_to_grandchild(self):
        """Share on grandparent grants access to grandchild (CTE depth ≥ 2)."""
        from myapp.database import Item, ItemShare
        from myapp.utils.privacy import recompute_item_privacy
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'deep-cte-owner')
            viewer, _ = _make_user(self.db, 'deep-cte-viewer')

            grandparent = _make_private_item(self.db, 'GP', owner)

            child = Item(name='Child', is_private=False, owner_id=owner.id,
                         parent_id=grandparent.id)
            self.db.session.add(child)
            self.db.session.flush()
            recompute_item_privacy(child)
            self.db.session.commit()

            grandchild = Item(name='GC', is_private=False, owner_id=owner.id,
                              parent_id=child.id)
            self.db.session.add(grandchild)
            self.db.session.flush()
            recompute_item_privacy(grandchild)
            self.db.session.commit()

            self.assertTrue(grandchild.private_effective)

            # Share only the grandparent
            share = ItemShare(item_id=grandparent.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()

            # All three must be visible
            self.assertTrue(can_view_item(grandparent, viewer))
            self.assertTrue(can_view_item(child, viewer))
            self.assertTrue(can_view_item(grandchild, viewer))

    def test_share_survives_item_reparent(self):
        """An ItemShare row survives when an item's parent changes."""
        from myapp.database import Item, ItemShare
        from myapp.utils.privacy import recompute_item_privacy
        from myapp.visibility import can_view_item
        with self.app.app_context():
            owner, _ = _make_user(self.db, 'move-owner')
            viewer, _ = _make_user(self.db, 'move-viewer')

            parent_a = _make_private_item(self.db, 'Parent A', owner)
            parent_b = _make_private_item(self.db, 'Parent B', owner)

            child = Item(name='Movable Child', is_private=False, owner_id=owner.id,
                         parent_id=parent_a.id)
            self.db.session.add(child)
            self.db.session.flush()
            recompute_item_privacy(child)
            self.db.session.commit()

            # Share the child directly
            share = ItemShare(item_id=child.id, user_id=viewer.id)
            self.db.session.add(share)
            self.db.session.commit()
            self.assertTrue(can_view_item(child, viewer))

            # Reparent child to parent_b
            child.parent_id = parent_b.id
            recompute_item_privacy(child)
            self.db.session.commit()

            # Share must still grant access
            self.assertTrue(can_view_item(child, viewer))
            remaining = ItemShare.query.filter_by(item_id=child.id).all()
            self.assertEqual(len(remaining), 1)


class TestSharingWebAndApiRegressions(unittest.TestCase):
    """Regression coverage for ACL bypasses spanning web and API routes."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            owner, owner_key = _make_user(db, 'acl-reg-owner')
            other, other_key = _make_user(db, 'acl-reg-other')
            cls.owner_id = owner.id
            cls.owner_key = owner_key
            cls.other_id = other.id
            cls.other_key = other_key

    def setUp(self):
        self.client = self.app.test_client()

    def _auth(self, key):
        return {'X-API-Key': key}

    def _login_other(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.other_id)
            sess['_fresh'] = True

    def _make_shared_private_item_with_artefacts(self):
        from myapp.database import Artefact, ArtefactType, ItemShare, User
        with self.app.app_context():
            owner = self.db.session.get(User, self.owner_id)
            item = _make_private_item(self.db, 'ACL Shared Private Item', owner)
            public_art = Artefact(
                item_id=item.id,
                label='visible.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='visible.scp',
                storage_path='dummy/visible.scp',
                owner_id=owner.id,
            )
            private_art = Artefact(
                item_id=item.id,
                label='hidden.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='hidden.scp',
                storage_path='dummy/hidden.scp',
                is_private=True,
                owner_id=owner.id,
            )
            share = ItemShare(item_id=item.id, user_id=self.other_id)
            self.db.session.add_all([public_art, private_art, share])
            self.db.session.commit()
            return item.uuid, item.id, public_art.uuid, public_art.url_slug, private_art.uuid

    def _upgrade_other_to_editor(self, item_id):
        from myapp.database import ItemShare
        with self.app.app_context():
            share = ItemShare.query.filter_by(item_id=item_id, user_id=self.other_id).one()
            share.permission = 'editor'
            self.db.session.commit()

    def test_api_item_serialization_includes_private_artefacts_for_share_recipient(self):
        item_uuid, _item_id, _public_uuid, _public_slug, _private_uuid = (
            self._make_shared_private_item_with_artefacts()
        )
        r = self.client.get(f'/api/items/{item_uuid}', headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 200)
        labels = {a['label'] for a in json.loads(r.data)['artefacts']}
        self.assertIn('visible.scp', labels)
        self.assertIn('hidden.scp', labels)

    def test_shared_user_cannot_create_child_under_private_parent_in_web_ui(self):
        item_uuid, item_id, _public_uuid, _public_slug, _private_uuid = (
            self._make_shared_private_item_with_artefacts()
        )
        self._login_other()
        r = self.client.post('/items/new', data={
            'name': 'Forbidden Web Child',
            'description': '',
            'parent_id': item_id,
            'platform_id': 0,
            'category_id': 0,
            'tags': '',
        })
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            from myapp.database import Item
            child = Item.query.filter_by(name='Forbidden Web Child', parent_id=item_id).first()
            self.assertIsNone(child)
            self.assertIsNotNone(Item.query.filter_by(uuid=item_uuid).first())

    def test_editor_can_create_child_under_private_parent_in_web_ui(self):
        item_uuid, item_id, _public_uuid, _public_slug, _private_uuid = (
            self._make_shared_private_item_with_artefacts()
        )
        self._upgrade_other_to_editor(item_id)
        self._login_other()
        r = self.client.post('/items/new', data={
            'name': 'Allowed Web Child',
            'description': '',
            'parent_id': item_id,
            'platform_id': 0,
            'category_id': 0,
            'tags': '',
        })
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            from myapp.database import Item
            child = Item.query.filter_by(name='Allowed Web Child', parent_id=item_id).first()
            self.assertIsNotNone(child)
            self.assertIsNotNone(Item.query.filter_by(uuid=item_uuid).first())

    def test_shared_user_cannot_mutate_private_artefact_restrictions_in_web_ui(self):
        item_uuid, _item_id, _public_uuid, public_slug, _private_uuid = (
            self._make_shared_private_item_with_artefacts()
        )
        self._login_other()
        r = self.client.post(
            f'/items/{item_uuid}/artefacts/{public_slug}/restrictions',
            data={'action': 'add', 'category': 'sensitive', 'reason': 'blocked'},
        )
        self.assertEqual(r.status_code, 403)

    def test_shared_user_cannot_compute_private_item_hashes_in_web_ui(self):
        item_uuid, _item_id, _public_uuid, public_slug, _private_uuid = (
            self._make_shared_private_item_with_artefacts()
        )
        self._login_other()
        r = self.client.post(f'/items/{item_uuid}/artefacts/{public_slug}/compute-hashes')
        self.assertEqual(r.status_code, 403)

    def test_curator_cannot_exfiltrate_artefact_to_public_item_via_api(self):
        """Curator moving an artefact from a private item to a public item must be blocked."""
        from myapp.database import Artefact, ArtefactType, Item, ItemShare, User
        with self.app.app_context():
            owner = self.db.session.get(User, self.owner_id)
            other = self.db.session.get(User, self.other_id)
            private_item = _make_private_item(self.db, 'Curator Exfil Private', owner)
            public_item = Item(name='Curator Exfil Public', is_private=False, owner_id=owner.id)
            self.db.session.add(public_item)
            artefact = Artefact(
                item_id=private_item.id,
                label='secret.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='secret.scp',
                storage_path='dummy/secret.scp',
                owner_id=owner.id,
            )
            share = ItemShare(item_id=private_item.id, user_id=other.id, permission='editor')
            self.db.session.add_all([artefact, share])
            self.db.session.commit()
            artefact_uuid = artefact.uuid
            public_item_uuid = public_item.uuid

        import json
        r = self.client.post(
            f'/api/artefacts/{artefact_uuid}/move',
            data=json.dumps({'target_item_uuid': public_item_uuid}),
            content_type='application/json',
            headers=self._auth(self.other_key),
        )
        self.assertEqual(r.status_code, 403, "Curator must not move artefacts from private to public item")

    def test_curator_can_move_artefact_between_two_private_items_via_api(self):
        """Curator with write access to both items may move artefacts between them."""
        from myapp.database import Artefact, ArtefactType, ItemShare, User
        with self.app.app_context():
            owner = self.db.session.get(User, self.owner_id)
            other = self.db.session.get(User, self.other_id)
            src_item = _make_private_item(self.db, 'Curator Move Src', owner)
            dst_item = _make_private_item(self.db, 'Curator Move Dst', owner)
            artefact = Artefact(
                item_id=src_item.id,
                label='movable.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='movable.scp',
                storage_path='dummy/movable.scp',
                owner_id=owner.id,
            )
            share_src = ItemShare(item_id=src_item.id, user_id=other.id, permission='editor')
            share_dst = ItemShare(item_id=dst_item.id, user_id=other.id, permission='editor')
            self.db.session.add_all([artefact, share_src, share_dst])
            self.db.session.commit()
            artefact_uuid = artefact.uuid
            dst_item_uuid = dst_item.uuid

        import json
        r = self.client.post(
            f'/api/artefacts/{artefact_uuid}/move',
            data=json.dumps({'target_item_uuid': dst_item_uuid}),
            content_type='application/json',
            headers=self._auth(self.other_key),
        )
        self.assertEqual(r.status_code, 200, "Curator with access to both private items may move artefacts")


class TestSharingApiObjectVisibility(unittest.TestCase):
    """Regression tests for direct API object lookups under private items."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            owner, owner_key = _make_user(db, 'api-object-owner')
            other, other_key = _make_user(db, 'api-object-other')
            cls.owner_id = owner.id
            cls.owner_key = owner_key
            cls.other_id = other.id
            cls.other_key = other_key

    def setUp(self):
        self.client = self.app.test_client()

    def _auth(self, key):
        return {'X-API-Key': key}

    def _make_private_analysis_tree(self):
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            Artefact,
            ArtefactType,
            ExtractedFile,
            FilesystemType,
            Partition,
            User,
        )
        with self.app.app_context():
            owner = self.db.session.get(User, self.owner_id)
            item = _make_private_item(self.db, 'Private Object Tree', owner)
            artefact = Artefact(
                item_id=item.id,
                label='object.scp',
                artefact_type=ArtefactType.SCP,
                original_filename='object.scp',
                storage_path='dummy/object.scp',
                owner_id=owner.id,
            )
            self.db.session.add(artefact)
            self.db.session.flush()
            analysis = Analysis(
                artefact_id=artefact.id,
                analysis_type=AnalysisType.METADATA_EXTRACT,
                status=AnalysisStatus.FAILED,
                error_message='private failure detail',
            )
            partition = Partition(
                artefact_id=artefact.id,
                partition_index=0,
                filesystem=FilesystemType.UNKNOWN,
            )
            self.db.session.add_all([analysis, partition])
            self.db.session.flush()
            extracted = ExtractedFile(
                partition_id=partition.id,
                path='$.Secret',
                filename='Secret',
                md5='0' * 32,
            )
            self.db.session.add(extracted)
            self.db.session.commit()
            return analysis.uuid, partition.uuid

    def test_direct_analysis_lookup_honours_visibility(self):
        analysis_uuid, _ = self._make_private_analysis_tree()
        r = self.client.get(f'/api/analysis/{analysis_uuid}',
                            headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 404)

    def test_direct_partition_lookup_honours_visibility(self):
        _, partition_uuid = self._make_private_analysis_tree()
        r = self.client.get(f'/api/partitions/{partition_uuid}',
                            headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 404)

    def test_partition_files_lookup_honours_visibility(self):
        _, partition_uuid = self._make_private_analysis_tree()
        r = self.client.get(f'/api/partitions/{partition_uuid}/files',
                            headers=self._auth(self.other_key))
        self.assertEqual(r.status_code, 404)


# vim: ts=4 sw=4 et
