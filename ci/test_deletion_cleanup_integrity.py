"""
Deletion / cleanup integrity regressions.

* Deleting a user who has added a restriction no longer FK-violates: the
  restriction's added_by_id now uses ON DELETE SET NULL (like
  UserArtefactBypass.granted_by_id), so the row survives with the attribution
  cleared. Exercised with SQLite FK enforcement ON, matching PostgreSQL.

* A re-analysis reset clears the root artefact's SearchDocument rows. The
  search_documents ON DELETE CASCADE fires only when the artefact row itself is
  deleted; a reset keeps the root, so its stale document-search rows would
  otherwise keep matching `content:` searches and point at removed extracted
  files.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_deletion_cleanup_integrity -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-deletion-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _enable_sqlite_fks(_db):
    from sqlalchemy import event

    @event.listens_for(_db.engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


class TestDeleteUserWithRestriction(unittest.TestCase):
    """Deleting a user who added a restriction nulls the FK instead of erroring."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            _enable_sqlite_fks(db)
            db.create_all()

    def test_delete_curator_leaves_restriction_with_null_added_by(self):
        with self.app.app_context():
            from arcology_shared.enums import ArtefactType
            from myapp.database import (
                Artefact,
                ArtefactRestriction,
                Item,
                RestrictionType,
                User,
                UserPermission,
            )
            curator = User(username='curator', password_hash='x',
                           permission=UserPermission.STAFF)
            owner = User(username='owner2', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            self.db.session.add_all([curator, owner])
            self.db.session.flush()
            item = Item(name='pub', owner_id=owner.id)
            self.db.session.add(item)
            self.db.session.flush()
            art = Artefact(item_id=item.id, label='a',
                           artefact_type=ArtefactType.RAW_SECTOR,
                           original_filename='a.img', storage_path='uploads/a.img',
                           owner_id=owner.id)
            self.db.session.add(art)
            self.db.session.flush()
            r = ArtefactRestriction(artefact_id=art.id,
                                    restriction_type=RestrictionType.MALWARE,
                                    added_by_id=curator.id)
            self.db.session.add(r)
            self.db.session.commit()
            rid, cid = r.id, curator.id

            # Curator owns nothing, so a real delete_user guard would let this
            # through; the FK must not blow up.
            self.db.session.delete(self.db.session.get(User, cid))
            self.db.session.commit()

            surviving = self.db.session.get(ArtefactRestriction, rid)
            self.assertIsNotNone(surviving, 'restriction row was destroyed')
            self.assertIsNone(surviving.added_by_id, 'added_by_id should be nulled')


class TestReanalysisResetClearsSearchDocuments(unittest.TestCase):
    """reset_artefact_for_reanalysis removes the root's SearchDocument rows."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()

    def test_reset_deletes_search_documents_for_root(self):
        with self.app.app_context():
            from arcology_shared.enums import ArtefactType
            from myapp.database import Artefact, Item, SearchDocument
            from myapp.services.artefact_lifecycle import reset_artefact_for_reanalysis

            item = Item(name='doc-item')
            self.db.session.add(item)
            self.db.session.flush()
            art = Artefact(item_id=item.id, label='doc',
                           artefact_type=ArtefactType.RAW_SECTOR,
                           original_filename='d.txt', storage_path='uploads/d.txt')
            self.db.session.add(art)
            self.db.session.flush()
            self.db.session.add(SearchDocument(artefact_id=art.id,
                                               file_path='readme.txt',
                                               content='stale indexed text'))
            self.db.session.commit()
            aid = art.id
            self.assertEqual(
                SearchDocument.query.filter_by(artefact_id=aid).count(), 1)

            reset_artefact_for_reanalysis(self.db.session.get(Artefact, aid))

            self.assertEqual(
                SearchDocument.query.filter_by(artefact_id=aid).count(), 0,
                'stale SearchDocument rows survived the re-analysis reset')


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
