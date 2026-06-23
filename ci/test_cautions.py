"""
Tests for the disc-protection "Analysis cautions" aggregation + standalone page.

Large hard-disc images can produce tens of thousands of protection indicators
(e.g. a bad-CRC sector per block).  The inline "Analysis cautions" rollup on the
artefact page used to emit one table row per indicator, bloating the page DOM.
It now collapses a huge indicator type to a summary that links to a dedicated,
paginated cautions page, and indicators are aggregated across the artefact and
all its derived children.

Covers:
  * collect_protection_cautions() merges indicators across parent + child,
    tags each row with its source, counts per type, and dedupes re-runs.
  * GET /artefacts/<uuid>/cautions returns 200, defaults to the most populous
    type, honours ?type=, and paginates.
  * The inline rollup collapses a type above the threshold to a "View the full
    list" link instead of thousands of rows.

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_cautions -v
"""

import json
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-cautions-test-key')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _protection_details(indicators):
    return json.dumps({'indicators': indicators})


class TestCautions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from arcology_shared.enums import ArtefactType
        from myapp.app import create_app
        from myapp.database import (
            Analysis,
            AnalysisStatus,
            AnalysisType,
            Artefact,
            Item,
        )
        from myapp.extensions import db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['LOGIN_DISABLED'] = True
        cls.app.config['PUBLIC_MODE'] = True
        cls.client = cls.app.test_client()
        cls.db = db

        with cls.app.app_context():
            db.create_all()

            item = Item(name='hd-item')
            db.session.add(item)
            db.session.flush()

            # Parent (hard-disc image) with a huge bad_crc set + a few ddam.
            parent = Artefact(item_id=item.id, label='HD LBA',
                              artefact_type=ArtefactType.SCP,
                              original_filename='hd.scp', storage_path='uploads/hd.scp')
            db.session.add(parent)
            db.session.flush()
            cls.parent_uuid = parent.uuid

            # Derived child (a partition) with its own protection indicators.
            child = Artefact(item_id=item.id, label='partition 0',
                             artefact_type=ArtefactType.RAW_SECTOR,
                             original_filename='p0.img', storage_path='uploads/p0.img',
                             parent_artefact_id=parent.id)
            db.session.add(child)
            db.session.flush()
            cls.child_uuid = child.uuid

            # An older protection re-run on the parent (inserted first → lower id)
            # must NOT be double-counted; only the newest analysis per artefact wins.
            db.session.add(Analysis(
                artefact_id=parent.id, analysis_type=AnalysisType.DISC_PROTECTION_DETECT,
                status=AnalysisStatus.COMPLETED,
                details=_protection_details([{'type': 'bad_crc', 'track': 0, 'side': 0, 'sect': 1}])))
            db.session.flush()

            big_bad_crc = [{'type': 'bad_crc', 'track': t, 'side': 0, 'sect': 1}
                           for t in range(300)]
            ddam = [{'type': 'ddam', 'track': 5, 'side': 1, 'sect': 9}]
            db.session.add(Analysis(
                artefact_id=parent.id, analysis_type=AnalysisType.DISC_PROTECTION_DETECT,
                status=AnalysisStatus.COMPLETED, details=_protection_details(big_bad_crc + ddam)))

            weak = [{'type': 'weak_bits', 'track': 2, 'side': 0, 'count': 7},
                    {'type': 'weak_bits', 'track': 3, 'side': 0, 'count': 4}]
            db.session.add(Analysis(
                artefact_id=child.id, analysis_type=AnalysisType.DISC_PROTECTION_DETECT,
                status=AnalysisStatus.COMPLETED, details=_protection_details(weak)))

            db.session.commit()
            cls.parent_id = parent.id
            cls.child_id = child.id

    def test_aggregates_across_parent_and_child(self):
        from myapp.blueprints.artefacts import collect_protection_cautions
        with self.app.app_context():
            data = collect_protection_cautions([self.parent_id, self.child_id])
        # 300 bad_crc + 1 ddam (newest parent analysis) + 2 weak_bits (child).
        self.assertEqual(data['by_type']['bad_crc'], 300)
        self.assertEqual(data['by_type']['ddam'], 1)
        self.assertEqual(data['by_type']['weak_bits'], 2)
        self.assertEqual(len(data['indicators']), 303)
        # Two distinct source artefacts contribute.
        self.assertEqual(data['source_count'], 2)
        # Every row is tagged with its source + analysis.
        self.assertTrue(all('_source_label' in i and '_analysis_uuid' in i
                            for i in data['indicators']))

    def test_newest_protection_analysis_wins(self):
        """The older parent re-run (1 bad_crc) must not inflate the count."""
        from myapp.blueprints.artefacts import collect_protection_cautions
        with self.app.app_context():
            data = collect_protection_cautions([self.parent_id])
        self.assertEqual(data['by_type']['bad_crc'], 300)  # not 301

    def test_cautions_page_defaults_to_most_populous_type(self):
        resp = self.client.get(f'/artefacts/{self.parent_uuid}/cautions')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # bad_crc is the most populous → selected by default, type tabs present.
        self.assertIn('Bad CRC', body)
        self.assertIn('Weak Bits', body)

    def test_cautions_page_type_filter_and_pagination(self):
        # First page of bad_crc at 100/page.
        resp = self.client.get(
            f'/artefacts/{self.parent_uuid}/cautions?type=bad_crc&per_page=100&page=1')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('of 300', resp.get_data(as_text=True))  # pagination total

        # weak_bits lives on the derived child but is reachable from the parent.
        resp = self.client.get(f'/artefacts/{self.parent_uuid}/cautions?type=weak_bits')
        self.assertEqual(resp.status_code, 200)

    def test_inline_rollup_collapses_huge_type(self):
        resp = self.client.get(f'/artefacts/{self.parent_uuid}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # The huge bad_crc type collapses to a link, not 300 table rows.
        self.assertIn(f'/artefacts/{self.parent_uuid}/cautions', body)
        self.assertIn('View the full list', body)


if __name__ == '__main__':
    unittest.main()
