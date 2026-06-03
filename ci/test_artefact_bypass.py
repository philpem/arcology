"""
Per-artefact restriction bypass tests (UserArtefactBypass / DB-5).

Covers the access-control logic in User.can_bypass_all_restrictions() and the
cascade behaviour of the user_artefact_bypasses table:

  - A per-artefact grant unlocks the granted artefact but not another.
  - Partial coverage (global bypass for one type + per-artefact grant for the
    remaining type) succeeds.
  - A missing grant leaves the user blocked.
  - artefact_id=None falls back to global bypasses only.
  - Admins implicitly bypass everything.
  - Deleting the artefact or the user cascades away the bypass row.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_artefact_bypass -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-bypass-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _enable_sqlite_fks(app, _db):
    """Enable SQLite foreign key enforcement so cascades match PostgreSQL."""
    from sqlalchemy import event

    @event.listens_for(_db.engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _create_app_and_db():
    from myapp.app import create_app
    from myapp.extensions import db as _db

    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _enable_sqlite_fks(app, _db)
        _db.create_all()
    return app, _db


def _make_artefact(db, label='Art', filename='art.img', parent=None):
    """Create an Artefact (with a Platform + Item) and return it.

    When ``parent`` is given the new artefact is created as a derived child of
    it (sharing the parent's item) instead of a fresh root.
    """
    from myapp.database import Artefact, Item, Platform
    from shared.enums import ArtefactType

    if parent is not None:
        item_id = parent.item_id
    else:
        platform = Platform(name=f'{label} Platform')
        db.session.add(platform)
        db.session.flush()
        item = Item(name=f'{label} Item', platform_id=platform.id)
        db.session.add(item)
        db.session.flush()
        item_id = item.id
    artefact = Artefact(
        item_id=item_id,
        label=label,
        artefact_type=ArtefactType.RAW_SECTOR,
        original_filename=filename,
        storage_path=f'uploads/{filename}',
        parent_artefact_id=parent.id if parent is not None else None,
    )
    db.session.add(artefact)
    db.session.flush()
    return artefact


def _restrict(db, artefact, rtype):
    from myapp.database import ArtefactRestriction
    r = ArtefactRestriction(artefact_id=artefact.id, restriction_type=rtype, reason='test')
    db.session.add(r)
    db.session.flush()
    return r


def _make_user(db, username, is_admin=False):
    import bcrypt
    from myapp.database import User
    pw = bcrypt.hashpw(b'testpassword1234', bcrypt.gensalt()).decode('utf-8')
    user = User(username=username, password_hash=pw, is_admin=is_admin)
    db.session.add(user)
    db.session.flush()
    return user


class TestBypassAuthz(unittest.TestCase):
    """can_bypass_all_restrictions() logic for per-artefact grants."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_per_artefact_grant_unlocks_only_that_artefact(self):
        with self.app.app_context():
            from myapp.database import RestrictionType, UserArtefactBypass

            granted = _make_artefact(self.db, 'Granted', 'granted.img')
            other = _make_artefact(self.db, 'Other', 'other.img')
            _restrict(self.db, granted, RestrictionType.COPYRIGHT)
            _restrict(self.db, other, RestrictionType.COPYRIGHT)
            user = _make_user(self.db, 'patron')
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=granted.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.commit()

            self.assertTrue(
                user.can_bypass_all_restrictions(granted.restrictions, artefact_id=granted.id))
            self.assertFalse(
                user.can_bypass_all_restrictions(other.restrictions, artefact_id=other.id))

    def test_partial_global_plus_per_artefact(self):
        """Global bypass covers one type, per-artefact grant covers the rest."""
        with self.app.app_context():
            from myapp.database import (
                RestrictionType,
                UserArtefactBypass,
                UserRestrictionBypass,
            )

            artefact = _make_artefact(self.db, 'Partial', 'partial.img')
            _restrict(self.db, artefact, RestrictionType.COPYRIGHT)
            _restrict(self.db, artefact, RestrictionType.EXPLICIT)
            user = _make_user(self.db, 'partialuser')
            # Global bypass for COPYRIGHT only.
            self.db.session.add(UserRestrictionBypass(
                user_id=user.id, restriction_type=RestrictionType.COPYRIGHT))
            self.db.session.commit()

            # Missing EXPLICIT -> still blocked.
            self.assertFalse(
                user.can_bypass_all_restrictions(artefact.restrictions, artefact_id=artefact.id))

            # Add per-artefact grant for EXPLICIT -> now fully covered.
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=artefact.id,
                restriction_type=RestrictionType.EXPLICIT,
            ))
            self.db.session.commit()
            self.assertTrue(
                user.can_bypass_all_restrictions(artefact.restrictions, artefact_id=artefact.id))

    def test_no_grant_is_blocked(self):
        with self.app.app_context():
            from myapp.database import RestrictionType

            artefact = _make_artefact(self.db, 'Blocked', 'blocked.img')
            _restrict(self.db, artefact, RestrictionType.MALWARE)
            user = _make_user(self.db, 'blockeduser')
            self.db.session.commit()

            self.assertFalse(
                user.can_bypass_all_restrictions(artefact.restrictions, artefact_id=artefact.id))

    def test_artefact_id_none_uses_global_only(self):
        """Without artefact_id, a per-artefact grant must not be consulted."""
        with self.app.app_context():
            from myapp.database import RestrictionType, UserArtefactBypass

            artefact = _make_artefact(self.db, 'NoId', 'noid.img')
            _restrict(self.db, artefact, RestrictionType.PII)
            user = _make_user(self.db, 'noiduser')
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=artefact.id,
                restriction_type=RestrictionType.PII,
            ))
            self.db.session.commit()

            self.assertFalse(user.can_bypass_all_restrictions(artefact.restrictions))
            self.assertTrue(
                user.can_bypass_all_restrictions(artefact.restrictions, artefact_id=artefact.id))

    def test_admin_bypasses_everything(self):
        with self.app.app_context():
            from myapp.database import RestrictionType

            artefact = _make_artefact(self.db, 'AdminArt', 'admin.img')
            _restrict(self.db, artefact, RestrictionType.LEGAL_HOLD)
            admin = _make_user(self.db, 'superadmin', is_admin=True)
            self.db.session.commit()

            self.assertTrue(
                admin.can_bypass_all_restrictions(artefact.restrictions, artefact_id=artefact.id))


class TestBypassCascade(unittest.TestCase):
    """Deleting the artefact or user must remove the bypass row."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_delete_artefact_cascades_to_bypass(self):
        with self.app.app_context():
            from myapp.database import (
                Artefact,
                RestrictionType,
                UserArtefactBypass,
            )

            artefact = _make_artefact(self.db, 'DelArt', 'delart.img')
            _restrict(self.db, artefact, RestrictionType.COPYRIGHT)
            user = _make_user(self.db, 'cascadeuser1')
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=artefact.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.commit()
            artefact_id = artefact.id

            self.db.session.delete(Artefact.query.get(artefact_id))
            self.db.session.commit()

            self.assertEqual(
                UserArtefactBypass.query.filter_by(artefact_id=artefact_id).count(), 0)

    def test_delete_user_cascades_to_bypass(self):
        with self.app.app_context():
            from myapp.database import (
                RestrictionType,
                User,
                UserArtefactBypass,
            )

            artefact = _make_artefact(self.db, 'UserDelArt', 'userdel.img')
            _restrict(self.db, artefact, RestrictionType.COPYRIGHT)
            user = _make_user(self.db, 'cascadeuser2')
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=artefact.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.commit()
            user_id = user.id

            self.db.session.delete(User.query.get(user_id))
            self.db.session.commit()

            self.assertEqual(
                UserArtefactBypass.query.filter_by(user_id=user_id).count(), 0)


def _restrict_file(db, artefact, rtype, path='secret.bin'):
    """Create a Partition + ExtractedFile on the artefact and restrict the file."""
    from myapp.database import (
        ExtractedFile,
        ExtractedFileRestriction,
        FilesystemType,
        Partition,
    )
    partition = Partition.query.filter_by(artefact_id=artefact.id).first()
    if partition is None:
        partition = Partition(
            artefact_id=artefact.id,
            filesystem=FilesystemType.UNKNOWN,
            partition_index=0,
        )
        db.session.add(partition)
        db.session.flush()
    ef = ExtractedFile(
        partition_id=partition.id,
        path=path,
        filename=path.rsplit('/', 1)[-1],
    )
    db.session.add(ef)
    db.session.flush()
    r = ExtractedFileRestriction(
        extracted_file_id=ef.id, restriction_type=rtype, reason='test')
    db.session.add(r)
    db.session.flush()
    return ef, r


class TestGrantableBypassTypes(unittest.TestCase):
    """_grantable_bypass_rtypes() covers artefact-level and file-level types."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_artefact_level_only(self):
        with self.app.app_context():
            from myapp.blueprints.artefacts import _grantable_bypass_rtypes
            from myapp.database import RestrictionType

            artefact = _make_artefact(self.db, 'ArtOnly', 'artonly.img')
            _restrict(self.db, artefact, RestrictionType.COPYRIGHT)
            self.db.session.commit()

            self.assertEqual(
                _grantable_bypass_rtypes(artefact), {RestrictionType.COPYRIGHT})

    def test_file_level_restriction_is_grantable(self):
        """A type that only appears on an extracted file is still grantable."""
        with self.app.app_context():
            from myapp.blueprints.artefacts import _grantable_bypass_rtypes
            from myapp.database import RestrictionType

            artefact = _make_artefact(self.db, 'FileOnly', 'fileonly.img')
            _restrict_file(self.db, artefact, RestrictionType.EXPLICIT)
            self.db.session.commit()

            # Artefact itself carries no restriction, but the file does.
            self.assertEqual(artefact.restrictions, [])
            self.assertEqual(
                _grantable_bypass_rtypes(artefact), {RestrictionType.EXPLICIT})

    def test_union_of_artefact_and_file_types(self):
        with self.app.app_context():
            from myapp.blueprints.artefacts import _grantable_bypass_rtypes
            from myapp.database import RestrictionType

            artefact = _make_artefact(self.db, 'Both', 'both.img')
            _restrict(self.db, artefact, RestrictionType.COPYRIGHT)
            _restrict_file(self.db, artefact, RestrictionType.EXPLICIT)
            self.db.session.commit()

            self.assertEqual(
                _grantable_bypass_rtypes(artefact),
                {RestrictionType.COPYRIGHT, RestrictionType.EXPLICIT})

    def test_other_artefacts_file_restriction_not_included(self):
        """File restrictions on a different artefact must not leak in."""
        with self.app.app_context():
            from myapp.blueprints.artefacts import _grantable_bypass_rtypes
            from myapp.database import RestrictionType

            target = _make_artefact(self.db, 'Target', 'target.img')
            other = _make_artefact(self.db, 'OtherArt', 'otherart.img')
            _restrict_file(self.db, other, RestrictionType.EXPLICIT)
            self.db.session.commit()

            self.assertEqual(_grantable_bypass_rtypes(target), set())

    def test_derived_artefact_file_restriction_is_grantable(self):
        """A file restriction on a derived child is grantable from the parent."""
        with self.app.app_context():
            from myapp.blueprints.artefacts import _grantable_bypass_rtypes
            from myapp.database import RestrictionType

            parent = _make_artefact(self.db, 'FluxParent', 'flux.scp')
            child = _make_artefact(self.db, 'DecodedChild', 'decoded.img', parent=parent)
            _restrict_file(self.db, child, RestrictionType.EXPLICIT)
            self.db.session.commit()

            # The parent carries no restriction of its own, but its derived
            # child has a restricted file — the parent page must offer it.
            self.assertEqual(parent.restrictions, [])
            self.assertEqual(
                _grantable_bypass_rtypes(parent), {RestrictionType.EXPLICIT})

    def test_derived_artefact_level_restriction_is_grantable(self):
        """A whole-artefact restriction on a derived child is grantable from the parent."""
        with self.app.app_context():
            from myapp.blueprints.artefacts import _grantable_bypass_rtypes
            from myapp.database import RestrictionType

            parent = _make_artefact(self.db, 'AL-Parent', 'al-flux.scp')
            child = _make_artefact(self.db, 'AL-Child', 'al-decoded.img', parent=parent)
            _restrict(self.db, child, RestrictionType.COPYRIGHT)
            self.db.session.commit()

            self.assertEqual(parent.restrictions, [])
            self.assertEqual(
                _grantable_bypass_rtypes(parent), {RestrictionType.COPYRIGHT})


class TestAncestorBypass(unittest.TestCase):
    """A grant on an ancestor artefact covers a derived artefact's files."""

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _create_app_and_db()

    def test_grant_on_parent_covers_child_file_restriction(self):
        with self.app.app_context():
            from myapp.database import RestrictionType, UserArtefactBypass

            parent = _make_artefact(self.db, 'AncParent', 'ancflux.scp')
            child = _make_artefact(self.db, 'AncChild', 'ancdecoded.img', parent=parent)
            ef, restr = _restrict_file(self.db, child, RestrictionType.EXPLICIT)
            user = _make_user(self.db, 'ancpatron')
            # Grant is created against the PARENT artefact (what the admin views).
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=parent.id,
                restriction_type=RestrictionType.EXPLICIT,
            ))
            self.db.session.commit()

            ancestor_ids = child.ancestor_ids
            self.assertIn(parent.id, ancestor_ids)
            self.assertIn(child.id, ancestor_ids)
            # Grant on the parent unblocks the restricted file on the child.
            self.assertTrue(
                user.can_bypass_all_restrictions([restr], artefact_id=ancestor_ids))
            # But a grant on the child alone would not reach a sibling tree:
            self.assertFalse(
                user.can_bypass_all_restrictions([restr], artefact_id=child.id))

    def test_grant_on_parent_covers_child_artefact_restriction(self):
        """A whole-artefact restriction on a derived child is bypassed by a parent grant."""
        with self.app.app_context():
            from myapp.database import RestrictionType, UserArtefactBypass

            parent = _make_artefact(self.db, 'ALAncParent', 'alancflux.scp')
            child = _make_artefact(self.db, 'ALAncChild', 'alancdecoded.img', parent=parent)
            restr = _restrict(self.db, child, RestrictionType.COPYRIGHT)
            user = _make_user(self.db, 'alancpatron')
            self.db.session.add(UserArtefactBypass(
                user_id=user.id, artefact_id=parent.id,
                restriction_type=RestrictionType.COPYRIGHT,
            ))
            self.db.session.commit()

            # Enforcement passes the child's ancestor chain, which includes the
            # parent the grant was made on.
            self.assertTrue(
                user.can_bypass_all_restrictions([restr], artefact_id=child.ancestor_ids))

    def test_ancestor_ids_property(self):
        with self.app.app_context():
            parent = _make_artefact(self.db, 'PropParent', 'prop.scp')
            child = _make_artefact(self.db, 'PropChild', 'prop.img', parent=parent)
            grandchild = _make_artefact(self.db, 'PropGC', 'prop.bin', parent=child)
            self.db.session.commit()

            self.assertEqual(parent.ancestor_ids, {parent.id})
            self.assertEqual(grandchild.ancestor_ids,
                             {grandchild.id, child.id, parent.id})


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
