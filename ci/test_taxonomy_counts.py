"""
Tests the taxonomy count helpers: GROUP BY counts, visibility-filtered.

Platform/category tree pages and the tags page previously counted items with
``node.items|length`` / ``tag.items|length``, which loaded every tagged entity
per node AND counted private rows on these public-readable pages.  The counts
now come from visibility-filtered GROUP BY helpers; this proves an anonymous
viewer's counts exclude private items/artefacts while an admin's include them.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_taxonomy_counts -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-taxonomy-counts-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestTaxonomyCounts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Artefact,
            Category,
            Item,
            Platform,
            Tag,
            User,
            UserPermission,
        )
        from myapp.extensions import db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = db
        with cls.app.app_context():
            db.create_all()
            admin = User(username='tax-admin', password_hash='x', is_admin=True,
                         permission=UserPermission.READ_WRITE)
            owner = User(username='tax-owner', password_hash='x',
                         permission=UserPermission.READ_WRITE)
            plat = Platform(name='Acorn')
            cat = Category(name='Games')
            tag = Tag(name='demo')
            db.session.add_all([admin, owner, plat, cat, tag])
            db.session.flush()
            cls.admin_id, cls.plat_id, cls.cat_id, cls.tag_id = admin.id, plat.id, cat.id, tag.id

            # One public and one private item, both on the platform + category + tag.
            pub = Item(name='public-item', platform_id=plat.id, category_id=cat.id)
            # private_effective is denormalised (maintained by the app on toggle),
            # so set it alongside is_private for the fixture.
            priv = Item(name='private-item', platform_id=plat.id, category_id=cat.id,
                        is_private=True, private_effective=True, owner_id=owner.id)
            pub.tags.append(tag)
            priv.tags.append(tag)
            db.session.add_all([pub, priv])
            db.session.flush()

            # A public and a private artefact under the public item, both tagged.
            pub_art = Artefact(item_id=pub.id, label='pub-art', artefact_type=ArtefactType.HFE,
                               original_filename='a.hfe', storage_path='a.hfe')
            priv_art = Artefact(item_id=pub.id, label='priv-art', artefact_type=ArtefactType.HFE,
                                original_filename='b.hfe', storage_path='b.hfe',
                                is_private=True, owner_id=owner.id)
            pub_art.tags.append(tag)
            priv_art.tags.append(tag)
            db.session.add_all([pub_art, priv_art])
            db.session.commit()

    def _as(self, user_id):
        """Return (item_by_platform, item_by_category, tag_items, tag_artefacts) counts
        as seen by *user_id* (None = anonymous)."""
        from flask_login import login_user
        from myapp.blueprints.taxonomy import (
            _visible_item_counts_by,
            _visible_tag_artefact_counts,
            _visible_tag_item_counts,
        )
        from myapp.database import Item, User
        with self.app.test_request_context():
            if user_id is not None:
                login_user(self.db.session.get(User, user_id))
            return (
                _visible_item_counts_by(Item.platform_id).get(self.plat_id, 0),
                _visible_item_counts_by(Item.category_id).get(self.cat_id, 0),
                _visible_tag_item_counts().get(self.tag_id, 0),
                _visible_tag_artefact_counts().get(self.tag_id, 0),
            )

    def test_anonymous_counts_exclude_private(self):
        plat, cat, t_items, t_arts = self._as(None)
        self.assertEqual(plat, 1)      # public item only
        self.assertEqual(cat, 1)
        self.assertEqual(t_items, 1)   # private item excluded
        self.assertEqual(t_arts, 1)    # private artefact excluded

    def test_admin_counts_include_private(self):
        plat, cat, t_items, t_arts = self._as(self.admin_id)
        self.assertEqual(plat, 2)
        self.assertEqual(cat, 2)
        self.assertEqual(t_items, 2)
        self.assertEqual(t_arts, 2)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
