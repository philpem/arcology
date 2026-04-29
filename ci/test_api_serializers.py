"""
Tests for myapp.utils.api_serializers.

Verifies that the artefact JSON shape includes the slug fields needed by API
clients to build canonical URLs without having to recompute slugs locally.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_api_serializers -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-api-serializer-test-secret-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


class TestArtefactToDictSlugs(unittest.TestCase):
    """artefact_to_dict() must expose artefact.slug and artefact.item.slug."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.database import Artefact, Item
        from myapp.extensions import db
        from shared.enums import ArtefactType

        cls.app = create_app()
        cls.app.config['TESTING'] = True

        with cls.app.app_context():
            db.create_all()

            item = Item(name='Serializer Test Item')
            item.slug = 'serializer-test-item'
            db.session.add(item)
            db.session.commit()

            artefact = Artefact(
                item_id=item.id,
                label='Disc 1',
                artefact_type=ArtefactType.UNKNOWN,
                original_filename='disc1.img',
                storage_path='disc1.img',
            )
            artefact.slug = 'disc-1'
            db.session.add(artefact)
            db.session.commit()

            cls.artefact_id = artefact.id
            cls.expected_artefact_slug = 'disc-1'
            cls.expected_item_slug = 'serializer-test-item'

    def test_artefact_to_dict_includes_slug(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import artefact_to_dict

        with self.app.app_context():
            artefact = Artefact.query.get(self.artefact_id)
            d = artefact_to_dict(artefact)

        self.assertIn('slug', d)
        self.assertEqual(d['slug'], self.expected_artefact_slug)

    def test_artefact_to_dict_includes_item_slug(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import artefact_to_dict

        with self.app.app_context():
            artefact = Artefact.query.get(self.artefact_id)
            d = artefact_to_dict(artefact)

        self.assertIn('item_slug', d)
        self.assertEqual(d['item_slug'], self.expected_item_slug)

    def test_analysis_tree_node_includes_slug(self):
        from myapp.database import Artefact
        from myapp.utils.api_serializers import analysis_tree_node

        with self.app.app_context():
            artefact = Artefact.query.get(self.artefact_id)
            node = analysis_tree_node(artefact)

        self.assertIn('slug', node)
        self.assertEqual(node['slug'], self.expected_artefact_slug)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
