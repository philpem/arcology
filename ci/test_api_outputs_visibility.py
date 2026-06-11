"""
Tests for visibility enforcement on GET /api/outputs/<path:filename>.

The API output-file endpoint serves analysis outputs (visualisations, extracted
text, file listings) from the shared outputs directory.  Output paths follow the
scheme ``{item_part}/{artefact_uuid}_{slug}/{file...}``.

Regression guard for an IDOR: a low-privilege user API key must NOT be able to
fetch the outputs of a private artefact it cannot view, even when it knows (or
guesses) the on-disk output path.  The owner and the worker key may fetch it.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_api_outputs_visibility -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-api-outputs-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


def _make_user(db, username, permission):
    from myapp.database import ApiKey, ApiKeyPermission, User
    user = User(username=username, password_hash='x', is_admin=False,
                permission=permission, can_use_api=True)
    db.session.add(user)
    db.session.flush()
    # Cap the API-key permission at READ_ONLY so the key matches the user tier.
    key_perm = ApiKeyPermission.READ_ONLY
    key_obj, raw = ApiKey.create(user_id=user.id, name=f'{username}-key',
                                 permission=key_perm)
    db.session.add(key_obj)
    db.session.commit()
    return user, raw


class TestApiOutputVisibility(unittest.TestCase):
    """A non-owner READ_ONLY key must not read a private artefact's outputs."""

    @classmethod
    def setUpClass(cls):
        from arcology_shared.storage import LocalStorage
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            ArtefactType,
            Item,
            UserPermission,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db

        # Point storage at a throwaway outputs directory we control.
        cls._tmp = tempfile.TemporaryDirectory()
        outputs = Path(cls._tmp.name) / 'outputs'
        uploads = Path(cls._tmp.name) / 'uploads'
        cls.app.storage = LocalStorage(uploads_dir=uploads, outputs_dir=outputs)

        with cls.app.app_context():
            db.create_all()
            owner, cls.key_owner = _make_user(db, 'out-owner', UserPermission.READ_WRITE)
            _other, cls.key_other = _make_user(db, 'out-other', UserPermission.READ_ONLY)

            # Private item + artefact owned by `owner`.
            item = Item(name='private-item', is_private=True, owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            from myapp.utils.privacy import recompute_item_privacy
            art = Artefact(item_id=item.id, label='secret', artefact_type=ArtefactType.UNKNOWN,
                           original_filename='secret.bin', storage_path='secret.bin',
                           owner_id=owner.id)
            db.session.add(art)
            db.session.flush()
            recompute_item_privacy(item)
            db.session.commit()
            cls.artefact_uuid = art.uuid

            # Write a fake output file at the documented output-path scheme:
            #   {item_part}/{artefact_uuid}_{slug}/{file}
            out_subdir = outputs / 'private-item' / f'{art.uuid}_secret'
            out_subdir.mkdir(parents=True, exist_ok=True)
            (out_subdir / 'visualisation.txt').write_text('SENSITIVE PRIVATE CONTENT')
            cls.output_path = f'private-item/{art.uuid}_secret/visualisation.txt'

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _get(self, key):
        return self.client.get(f'/api/outputs/{self.output_path}',
                               headers={'X-API-Key': key})

    def test_owner_can_read_own_output(self):
        r = self._get(self.key_owner)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn(b'SENSITIVE PRIVATE CONTENT', r.data)

    def test_worker_can_read_any_output(self):
        r = self._get(_WORKER_KEY)
        self.assertEqual(r.status_code, 200, r.data)

    def test_other_user_cannot_read_private_output(self):
        # The IDOR: a non-owner READ_ONLY key knows the path but must be denied.
        r = self._get(self.key_other)
        self.assertEqual(r.status_code, 404, r.data)
        self.assertNotIn(b'SENSITIVE PRIVATE CONTENT', r.data)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
