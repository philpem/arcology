"""
Tests that POST /api/partitions/<uuid>/recognise-step is worker-only.

This endpoint runs server-side product recognition for a partition and
*overwrites* its RecognisedProduct rows (delete + insert).  It is gated at
@require_auth('read_write') and additionally checks ``_is_worker_request()``
— without that worker-only gate any read_write user could wipe or falsify
recognition results on artefacts they do not own (and can merely view).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_recognised_products_auth -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-recprod-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')

_WORKER_KEY = os.environ['WORKER_API_KEY']


class TestRecognisedProductsAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            ApiKey,
            ApiKeyPermission,
            Artefact,
            FilesystemType,
            HashDatabase,
            Item,
            KnownProduct,
            Partition,
            RecognisedProduct,
            StorageDirectory,
            User,
            UserPermission,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.client = cls.app.test_client()
        cls.db = db
        cls._RecognisedProduct = RecognisedProduct
        with cls.app.app_context():
            db.create_all()

            # A different user owns the (public) artefact; the attacker is a
            # separate read_write user who can view it but does not own it.
            owner = User(username='rp-owner', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            attacker = User(username='rp-attacker', password_hash='x',
                            permission=UserPermission.READ_WRITE, can_use_api=True)
            db.session.add_all([owner, attacker])
            db.session.flush()
            key, cls.attacker_key = ApiKey.create(
                user_id=attacker.id, name='k', permission=ApiKeyPermission.READ_WRITE)
            db.session.add(key)

            hdb = HashDatabase(name='HDB')
            db.session.add(hdb)
            db.session.flush()
            prod = KnownProduct(database_id=hdb.id, title='Prod')
            db.session.add(prod)
            db.session.flush()
            cls.product_id = prod.id

            item = Item(name='public-item', owner_id=owner.id)
            db.session.add(item)
            db.session.flush()
            art = Artefact(item_id=item.id, label='Disc', artefact_type=ArtefactType.HFE,
                           original_filename='d.ssd', storage_path='d.ssd',
                           storage_directory=StorageDirectory.UPLOADS, owner_id=owner.id)
            db.session.add(art)
            db.session.flush()
            part = Partition(artefact_id=art.id, partition_index=0, label='Main',
                             filesystem=FilesystemType.DFS)
            db.session.add(part)
            db.session.flush()
            cls.part_uuid = part.uuid
            cls.part_id = part.id

            db.session.add(RecognisedProduct(
                partition_id=part.id, product_id=prod.id, folder_path='/',
                required_matched=1, required_total=1,
                optional_matched=0, optional_total=0))
            db.session.commit()

    def _count(self):
        with self.app.app_context():
            return self._RecognisedProduct.query.filter_by(partition_id=self.part_id).count()

    def test_read_write_user_cannot_run_recognition(self):
        # A non-worker read_write user must not be able to drive the step
        # (which would delete/rewrite this partition's recognition rows).
        r = self.client.post(f'/api/partitions/{self.part_uuid}/recognise-step',
                             headers={'X-API-Key': self.attacker_key},
                             json={'last_product_id': 0, 'limit': 25})
        self.assertEqual(r.status_code, 403, r.data)
        # The pre-existing recognition row must survive.
        self.assertEqual(self._count(), 1)

    def test_worker_can_run_recognition(self):
        r = self.client.post(f'/api/partitions/{self.part_uuid}/recognise-step',
                             headers={'X-API-Key': _WORKER_KEY},
                             json={'last_product_id': 0, 'limit': 25})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.get_json()['done'])


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
