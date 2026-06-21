"""
Tests for the read-only similarity diagnostics
(myapp/services/similarity_stats.py + the `flask similarity-stats` CLI).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_similarity_stats -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-similarity-stats-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _h(seed: str) -> str:
    return (seed * 64)[:64]


def _add_artefact(db, item, label, files):
    from arcology_shared.enums import ArtefactType
    from myapp.database import Artefact, ExtractedFile, FilesystemType, Partition
    art = Artefact(item_id=item.id, label=label, artefact_type=ArtefactType.RAW_SECTOR,
                   original_filename=f'{label}.img', storage_path=f'uploads/{label}.img')
    db.session.add(art)
    db.session.flush()
    part = Partition(artefact_id=art.id, partition_index=0, filesystem=FilesystemType.ADFS)
    db.session.add(part)
    db.session.flush()
    for path, seed, size in files:
        db.session.add(ExtractedFile(
            partition_id=part.id, path=path, filename=path.split('/')[-1],
            file_size=size, is_directory=False, sha256=_h(seed)))
    db.session.commit()
    return art


class TestSimilarityStats(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db
        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.db = _db
        with cls.app.app_context():
            _db.create_all()

    def setUp(self):
        from myapp.database import (
            Artefact,
            ArtefactComponent,
            ArtefactSimilarity,
            ComponentSimilarity,
            ExtractedFile,
            Item,
            Partition,
            Platform,
        )
        self.ctx = self.app.app_context()
        self.ctx.push()
        for model in (ComponentSimilarity, ArtefactSimilarity, ArtefactComponent,
                      ExtractedFile, Partition, Artefact, Item, Platform):
            model.query.delete()
        self.db.session.commit()
        self.item = Item(name='Games')
        self.db.session.add(self.item)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def _build(self):
        from myapp.services.similarity import rebuild_all
        # Two near-identical discs (share a big common file) + one unrelated.
        common = [('SHARED', 'c', 100000)]
        _add_artefact(self.db, self.item, 'A', common + [('UA', 'ua', 1000)])
        _add_artefact(self.db, self.item, 'B', common + [('UB', 'ub', 1000)])
        _add_artefact(self.db, self.item, 'C', [('LONE', 'z', 5000)])
        rebuild_all()

    def test_scale_and_structure(self):
        from myapp.services.similarity_stats import collect_similarity_stats
        self._build()
        stats = collect_similarity_stats()
        sc = stats['scale']
        self.assertEqual(sc['artefacts_with_hashable_files'], 3)
        self.assertEqual(sc['hashable_file_rows'], 5)
        self.assertEqual(sc['partitions'], 3)
        # A and B share a distinctive file -> exactly one candidate + stored pair.
        self.assertEqual(sc['candidate_pairs_generated'], 1)
        self.assertEqual(sc['artefact_pairs_stored'], 1)
        # Required top-level sections present.
        for key in ('distributions', 'pre_gate_ratios', 'noise', 'components', 'top_matches'):
            self.assertIn(key, stats)

    def test_score_and_df_histograms(self):
        from myapp.services.similarity_stats import collect_similarity_stats
        self._build()
        stats = collect_similarity_stats()
        # One stored pair (A~B), high score.
        self.assertEqual(sum(stats['noise']['score_histogram'].values()), 1)
        # SHARED is on 2 of 3 discs; the unique files are on 1 each.
        df = stats['noise']['df_histogram']
        self.assertEqual(df.get('2', 0), 1)
        self.assertEqual(df.get('1 (unique)', 0), 3)
        self.assertEqual(stats['noise']['unique_hashes'], 3)

    def test_top_matches_listed(self):
        from myapp.services.similarity_stats import collect_similarity_stats
        self._build()
        stats = collect_similarity_stats(top_n=5)
        self.assertEqual(len(stats['top_matches']), 1)
        labels = {stats['top_matches'][0]['a_label'], stats['top_matches'][0]['b_label']}
        self.assertEqual(labels, {'A', 'B'})

    def test_exclusion_graceful_when_absent(self):
        # On a build without Phase 1 the exclusion section reports not-configured
        # rather than erroring.
        from myapp.services.similarity_stats import collect_similarity_stats
        self._build()
        exc = collect_similarity_stats()['noise']['exclusion']
        self.assertIn('configured', exc)

    def test_empty_collection(self):
        from myapp.services.similarity_stats import collect_similarity_stats
        stats = collect_similarity_stats()
        self.assertEqual(stats['scale']['artefacts_with_hashable_files'], 0)
        self.assertEqual(stats['scale']['candidate_pairs_generated'], 0)

    def test_cli_runs(self):
        self._build()
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=['similarity-stats', '--top', '3'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('A. Scale', result.output)
        self.assertIn('Top 3 matches', result.output)

    def test_cli_json(self):
        import json
        self._build()
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=['similarity-stats', '--json'])
        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload['scale']['artefact_pairs_stored'], 1)


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
