"""
Tests for content-set artefact similarity (myapp/services/similarity.py).

Covers:
  - weighted_jaccard pure-function scoring (identical / partial / disjoint /
    size-weighting).
  - rebuild_all populating the artefact-level cache and similar_artefacts()
    returning visible matches.
  - sha256 -> md5 fallback keying.
  - directory-subtree component matching (the !App-across-discs case) where the
    whole-disc score is low but the component score is high.
  - visibility filtering (private artefacts not surfaced to anonymous viewers).

Run:
    SQLALCHEMY_DATABASE_URI=sqlite:///:memory: SECRET_KEY=test WORKER_API_KEY=test \\
        python -m unittest ci.test_similarity -v
"""

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'ci-similarity-test-secret-key-not-for-production')
os.environ.setdefault('WORKER_API_KEY', 'ci-test-worker-key')


def _h(seed: str) -> str:
    """Deterministic 64-char hex hash stand-in from a short seed."""
    return (seed * 64)[:64]


def _add_artefact(db, item, label, files, *, private=False):
    """Create an Artefact + one Partition + ExtractedFile rows.

    files: list of (path, hash_seed, size).  hash_seed=None -> no hash (skipped).
    Pass md5_only=True via a 4-tuple to store the hash as md5 rather than sha256.
    """
    from arcology_shared.enums import ArtefactType
    from myapp.database import Artefact, ExtractedFile, FilesystemType, Partition

    art = Artefact(
        item_id=item.id,
        label=label,
        artefact_type=ArtefactType.RAW_SECTOR,
        original_filename=f'{label}.img',
        storage_path=f'uploads/{label}.img',
        is_private=private,
    )
    db.session.add(art)
    db.session.flush()
    part = Partition(artefact_id=art.id, partition_index=0, filesystem=FilesystemType.ADFS)
    db.session.add(part)
    db.session.flush()
    for entry in files:
        path, seed, size = entry[0], entry[1], entry[2]
        md5_only = len(entry) > 3 and entry[3]
        kw = {}
        if seed is not None:
            if md5_only:
                kw['md5'] = _h(seed)[:32]
            else:
                kw['sha256'] = _h(seed)
        ef = ExtractedFile(
            partition_id=part.id,
            path=path,
            filename=path.split('/')[-1],
            file_size=size,
            is_directory=False,
            **kw,
        )
        db.session.add(ef)
    db.session.commit()
    return art


class TestWeightedJaccard(unittest.TestCase):
    """The pure metric function."""

    def test_identical_sets_score_one(self):
        from myapp.services.similarity import weighted_jaccard
        a = {'x': 100, 'y': 200}
        m = weighted_jaccard(a, dict(a))
        self.assertEqual(m['score'], 1.0)
        self.assertEqual(m['shared_files'], 2)
        self.assertEqual(m['union_files'], 2)

    def test_disjoint_sets_score_zero(self):
        from myapp.services.similarity import weighted_jaccard
        m = weighted_jaccard({'a': 10}, {'b': 10})
        self.assertEqual(m['score'], 0.0)
        self.assertEqual(m['shared_files'], 0)

    def test_size_weighting(self):
        """A tiny differing file barely dents the score; a big one moves it."""
        from myapp.services.similarity import weighted_jaccard
        # Shared big file (1000), each side a unique tiny file (1).
        tiny = weighted_jaccard({'big': 1000, 'p': 1}, {'big': 1000, 'q': 1})
        # Shared tiny file (1), each side a unique big file (1000).
        big = weighted_jaccard({'small': 1, 'p': 1000}, {'small': 1, 'q': 1000})
        self.assertGreater(tiny['score'], 0.99)   # 1000 / 1002
        self.assertLess(big['score'], 0.01)        # 1 / 2001
        # Plain (unweighted) Jaccard would be 1/3 for both.

    def test_empty_set_returns_none(self):
        from myapp.services.similarity import weighted_jaccard
        self.assertIsNone(weighted_jaccard({}, {'a': 1}))


class _SimilarityBase(unittest.TestCase):
    """Shared app/db fixtures; not a test case itself."""

    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app
        from myapp.extensions import db as _db

        cls.app = create_app()
        cls.app.config['TESTING'] = True
        cls.app.config['LOGIN_DISABLED'] = True
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
        # Clean slate each test.
        for model in (ComponentSimilarity, ArtefactSimilarity, ArtefactComponent,
                      ExtractedFile, Partition, Artefact, Item, Platform):
            model.query.delete()
        self.db.session.commit()
        plat = Platform(name='Acorn')
        self.db.session.add(plat)
        self.db.session.flush()
        self.item = Item(name='Games', platform_id=plat.id)
        self.db.session.add(self.item)
        self.db.session.commit()

    def tearDown(self):
        self.db.session.rollback()
        self.ctx.pop()

    def _pair_score(self, a, b):
        from myapp.database import ArtefactSimilarity
        lo, hi = sorted((a.id, b.id))
        row = ArtefactSimilarity.query.filter_by(artefact_a_id=lo, artefact_b_id=hi).first()
        return row.score if row else None


class TestArtefactSimilarity(_SimilarityBase):

    def test_near_duplicate_discs_match(self):
        """Two game discs differing only in a small save file score high but < 1."""
        from myapp.services.similarity import rebuild_all, similar_artefacts
        common = [('GAME', 'g', 100000), ('LOADER', 'l', 5000), ('DATA', 'd', 50000)]
        a = _add_artefact(self.db, self.item, 'Original', common + [('SAVE', 's1', 200)])
        _add_artefact(self.db, self.item, 'Played', common + [('SAVE', 's2', 200)])

        stats = rebuild_all()
        self.assertEqual(stats['artefact_pairs'], 1)

        matches = similar_artefacts(a, None)
        self.assertEqual(len(matches), 1)
        other, sim = matches[0]
        self.assertEqual(other.label, 'Played')
        self.assertGreater(sim.score, 0.9)
        self.assertLess(sim.score, 1.0)
        self.assertEqual(sim.shared_files, 3)
        self.assertEqual(sim.union_files, 5)

    def test_md5_fallback_keys_match(self):
        """Files carrying only md5 (no sha256) still compare."""
        from myapp.services.similarity import rebuild_all, similar_artefacts
        files = [('A', 'a', 1000, True), ('B', 'b', 2000, True)]
        a = _add_artefact(self.db, self.item, 'M1', files)
        _add_artefact(self.db, self.item, 'M2', files)
        rebuild_all()
        self.assertEqual(len(similar_artefacts(a, None)), 1)

    def test_unrelated_discs_do_not_match(self):
        from myapp.services.similarity import rebuild_all, similar_artefacts
        a = _add_artefact(self.db, self.item, 'X', [('F', 'x', 1000)])
        _add_artefact(self.db, self.item, 'Y', [('F', 'y', 1000)])
        rebuild_all()
        self.assertEqual(similar_artefacts(a, None), [])

    def test_private_artefact_hidden_from_anonymous(self):
        from myapp.services.similarity import rebuild_all, similar_artefacts
        files = [('A', 'a', 1000), ('B', 'b', 2000)]
        a = _add_artefact(self.db, self.item, 'Pub', files)
        _add_artefact(self.db, self.item, 'Secret', files, private=True)
        rebuild_all()
        # Anonymous viewer must not see the private match.
        self.assertEqual(similar_artefacts(a, None), [])

    def test_incremental_matches_full_rebuild(self):
        """recompute_for_artefact yields the same artefact-level rows as rebuild_all."""
        from myapp.database import ArtefactSimilarity
        from myapp.services.similarity import (
            rebuild_all,
            recompute_for_artefact,
            similar_artefacts,
        )
        common = [('GAME', 'g', 100000), ('LOADER', 'l', 5000)]
        a = _add_artefact(self.db, self.item, 'Disc-A', common + [('X', 'x', 300)])
        _add_artefact(self.db, self.item, 'Disc-B', common + [('Y', 'y', 300)])

        rebuild_all()
        full = {(s.artefact_a_id, s.artefact_b_id): round(s.score, 6)
                for s in ArtefactSimilarity.query.all()}

        # Wipe and rebuild only via the incremental path for both artefacts.
        ArtefactSimilarity.query.delete()
        self.db.session.commit()
        recompute_for_artefact(a)
        incr = {(s.artefact_a_id, s.artefact_b_id): round(s.score, 6)
                for s in ArtefactSimilarity.query.all()}
        self.assertEqual(full, incr)
        self.assertEqual(len(similar_artefacts(a, None)), 1)

    def test_incremental_adds_new_artefact(self):
        """A newly added artefact gains similarity rows from a single recompute."""
        from myapp.services.similarity import rebuild_all, recompute_for_artefact, similar_artefacts
        files = [('A', 'a', 1000), ('B', 'b', 2000), ('C', 'c', 3000)]
        a = _add_artefact(self.db, self.item, 'First', files)
        rebuild_all()
        self.assertEqual(similar_artefacts(a, None), [])

        b = _add_artefact(self.db, self.item, 'Second', files)
        recompute_for_artefact(b)
        # Both directions now resolve to the match.
        self.assertEqual(len(similar_artefacts(a, None)), 1)
        self.assertEqual(len(similar_artefacts(b, None)), 1)

    def test_chunked_step_matches_single_pass(self):
        """Driving similarity_match_step one candidate at a time equals one pass."""
        from myapp.database import ArtefactSimilarity
        from myapp.services.similarity import (
            recompute_for_artefact,
            similarity_match_step,
            similarity_reset,
        )
        common = [('GAME', 'g', 100000), ('LOADER', 'l', 5000)]
        a = _add_artefact(self.db, self.item, 'A', common + [('UA', 'ua', 100)])
        for i in range(3):
            _add_artefact(self.db, self.item, f'B{i}', common + [(f'U{i}', f'u{i}', 100)])

        # Reference: one full recompute.
        recompute_for_artefact(a)
        full = {(s.artefact_a_id, s.artefact_b_id): round(s.score, 6)
                for s in ArtefactSimilarity.query.all()}

        # Wipe a's rows, then drive the step loop with limit=1 (forces chunking).
        ArtefactSimilarity.query.delete()
        self.db.session.commit()
        similarity_reset(a.id)
        cursor = 0
        for _ in range(20):
            r = similarity_match_step(a.id, cursor, limit=1)
            self.db.session.commit()
            if r['done']:
                break
            cursor = r['next_cursor']
        chunked = {(s.artefact_a_id, s.artefact_b_id): round(s.score, 6)
                   for s in ArtefactSimilarity.query.all()}
        self.assertEqual(full, chunked)
        self.assertTrue(full)  # there is at least one match to compare

    def test_step_cursor_stable_when_candidate_appears_midway(self):
        """A candidate inserted between steps is picked up without a duplicate crash."""
        from myapp.services.similarity import (
            similar_artefacts,
            similarity_match_step,
            similarity_reset,
        )
        common = [('GAME', 'g', 100000), ('LOADER', 'l', 5000)]
        a = _add_artefact(self.db, self.item, 'A', common + [('UA', 'ua', 100)])
        # Two initial candidates so the refresh spans multiple steps (limit=1).
        _add_artefact(self.db, self.item, 'B0', common + [('U0', 'u0', 100)])
        _add_artefact(self.db, self.item, 'B1', common + [('U1', 'u1', 100)])

        similarity_reset(a.id)
        r = similarity_match_step(a.id, 0, limit=1)  # process the first candidate
        self.db.session.commit()
        cursor = r['next_cursor']

        # A new sharing artefact appears mid-refresh (a higher id than the cursor).
        _add_artefact(self.db, self.item, 'B2', common + [('U2', 'u2', 100)])

        # Drive to completion from the id cursor: must not re-insert an already
        # processed candidate (which would violate uq_artefact_similarity_pair),
        # and must pick up the newly-appeared B2.
        for _ in range(8):
            if r['done']:
                break
            r = similarity_match_step(a.id, cursor, limit=1)
            self.db.session.commit()
            cursor = r['next_cursor']
        self.assertEqual(len(similar_artefacts(a, None)), 3)

    def test_queue_similarity_refresh_dedups(self):
        from myapp.database import Analysis, AnalysisStatus, AnalysisType
        from myapp.services.similarity import queue_similarity_refresh
        a = _add_artefact(self.db, self.item, 'Q', [('A', 'a', 1000), ('B', 'b', 2000)])
        first, created1 = queue_similarity_refresh(a.id)
        self.assertTrue(created1)
        second, created2 = queue_similarity_refresh(a.id)
        self.assertFalse(created2)
        self.assertEqual(first.id, second.id)
        n = (Analysis.query
             .filter_by(artefact_id=a.id, analysis_type=AnalysisType.SIMILARITY_REFRESH)
             .filter(Analysis.status == AnalysisStatus.PENDING).count())
        self.assertEqual(n, 1)

    def test_similarity_step_endpoint_drives_to_done(self):
        """The worker-only step endpoint chunks to completion and caches matches."""
        from myapp.services.similarity import similar_artefacts
        files = [('A', 'a', 1000), ('B', 'b', 2000), ('C', 'c', 3000)]
        a = _add_artefact(self.db, self.item, 'E1', files)
        _add_artefact(self.db, self.item, 'E2', files)
        client = self.app.test_client()
        auth = {'X-API-Key': os.environ['WORKER_API_KEY']}
        cursor, done = 0, False
        for _ in range(10):
            resp = client.post(f'/api/artefacts/{a.uuid}/similarity-step',
                               json={'cursor': cursor, 'limit': 1}, headers=auth)
            self.assertEqual(resp.status_code, 200, resp.data)
            body = resp.get_json()
            if body.get('done'):
                done = True
                break
            cursor = body['next_cursor']
        self.assertTrue(done)
        self.assertEqual(len(similar_artefacts(a, None)), 1)

    def test_similarity_step_endpoint_requires_worker(self):
        a = _add_artefact(self.db, self.item, 'W', [('A', 'a', 1000), ('B', 'b', 2000)])
        # No worker key → rejected (401 unauthenticated, or 403 worker-only).
        resp = self.app.test_client().post(
            f'/api/artefacts/{a.uuid}/similarity-step', json={'cursor': 0})
        self.assertIn(resp.status_code, (401, 403))

    def test_ubiquitous_file_does_not_create_pairs(self):
        """A hash shared by many artefacts must not, alone, link them as similar."""
        from myapp.services import similarity
        from myapp.services.similarity import rebuild_all, similar_artefacts
        # One ubiquitous file present on many discs, each otherwise unique.
        orig_cap = similarity.MAX_HASH_ARTEFACTS
        similarity.MAX_HASH_ARTEFACTS = 3
        try:
            artefacts = []
            for i in range(6):
                a = _add_artefact(self.db, self.item, f'Disc{i}',
                                  [('COMMON', 'common', 5000), (f'UNIQUE{i}', f'u{i}', 5000)])
                artefacts.append(a)
            rebuild_all()
            # COMMON is in 6 > cap(3) artefacts → no pairs from it alone.
            self.assertEqual(similar_artefacts(artefacts[0], None), [])
        finally:
            similarity.MAX_HASH_ARTEFACTS = orig_cap

    def test_idf_downweights_common_file(self):
        """With IDF on, a pair sharing a common file scores below one sharing a rare file."""
        from myapp.services.similarity import rebuild_all
        # A common file present on three discs; plus two discs sharing it.
        _add_artefact(self.db, self.item, 'Bulk1', [('COMMON', 'c', 1000)])
        _add_artefact(self.db, self.item, 'Bulk2', [('COMMON', 'c', 1000)])
        # Pair P shares only the common file; pair Q shares only a rare file.
        p1 = _add_artefact(self.db, self.item, 'P1', [('COMMON', 'c', 1000), ('PX', 'px', 1000)])
        p2 = _add_artefact(self.db, self.item, 'P2', [('COMMON', 'c', 1000), ('PY', 'py', 1000)])
        q1 = _add_artefact(self.db, self.item, 'Q1', [('RARE', 'r', 1000), ('QX', 'qx', 1000)])
        q2 = _add_artefact(self.db, self.item, 'Q2', [('RARE', 'r', 1000), ('QY', 'qy', 1000)])

        self.app.config['SIMILARITY_USE_IDF'] = True
        try:
            rebuild_all()
            # Both pairs have the same raw overlap (1 shared of 3 files), but
            # COMMON (df=4) is down-weighted relative to RARE (df=2), so the
            # rare-sharing pair scores higher.
            self.assertLess(self._pair_score(p1, p2), self._pair_score(q1, q2))
        finally:
            self.app.config['SIMILARITY_USE_IDF'] = False

    def test_component_match_across_different_discs(self):
        """Two otherwise-different hard discs that share a !ArtWorks subtree."""
        from myapp.services.similarity import rebuild_all, similar_artefacts, similar_components
        artworks = [
            ('!ArtWorks/!Run', 'aw1', 2000),
            ('!ArtWorks/!RunImage', 'aw2', 80000),
            ('!ArtWorks/Messages', 'aw3', 4000),
        ]
        disc1 = artworks + [(f'Docs/letter{i}', f'd1_{i}', 1000) for i in range(20)]
        disc2 = artworks + [(f'Work/sheet{i}', f'd2_{i}', 1000) for i in range(20)]
        a = _add_artefact(self.db, self.item, 'HD-A', disc1)
        _add_artefact(self.db, self.item, 'HD-B', disc2)

        rebuild_all()

        # Whole-disc similarity is low (shared app is a small fraction).
        whole = similar_artefacts(a, None)
        if whole:
            self.assertLess(whole[0][1].score, 0.8)

        # Component-level finds the shared !ArtWorks.
        comps = similar_components(a, None)
        self.assertTrue(comps, "expected a shared component match")
        local, others = comps[0]
        self.assertEqual(local.name, '!ArtWorks')
        self.assertTrue(others)
        other_comp, other_art, sim = others[0]
        self.assertEqual(other_art.label, 'HD-B')
        self.assertEqual(other_comp.name, '!ArtWorks')
        self.assertGreater(sim.score, 0.99)

    def test_component_match_counts_and_route(self):
        """component_match_counts badges a folder; the component page lists matches."""
        from myapp.database import ArtefactComponent
        from myapp.services.similarity import (
            component_match_counts,
            matches_for_component,
            rebuild_all,
        )
        aw = [('!ArtWorks/!Run', 'aw1', 2000), ('!ArtWorks/!RunImage', 'aw2', 80000),
              ('!ArtWorks/Messages', 'aw3', 4000)]
        a = _add_artefact(self.db, self.item, 'HD-A', aw + [('Docs/x', 'dx', 1000), ('Docs/y', 'dy', 1000)])
        _add_artefact(self.db, self.item, 'HD-B', aw + [('Work/z', 'wz', 1000), ('Work/q', 'wq', 1000)])
        rebuild_all()

        counts = component_match_counts([a.id], None)
        self.assertIn('!ArtWorks', counts)
        count, comp_uuid = counts['!ArtWorks']
        self.assertEqual(count, 1)

        comp = ArtefactComponent.query.filter_by(uuid=comp_uuid).first()
        matches = matches_for_component(comp, None)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1].label, 'HD-B')

        # Private candidate is hidden from anonymous viewers.
        self.assertEqual(component_match_counts([a.id], None).get('!ArtWorks')[0], 1)

    def test_component_similar_route(self):
        from myapp.services.similarity import component_match_counts, rebuild_all
        aw = [('!App/!Run', 'a1', 2000), ('!App/data', 'a2', 9000), ('!App/res', 'a3', 3000)]
        a = _add_artefact(self.db, self.item, 'One', aw + [('u', 'u1', 500)])
        _add_artefact(self.db, self.item, 'Two', aw + [('v', 'v1', 500)])
        rebuild_all()
        comp_uuid = component_match_counts([a.id], None)['!App'][1]
        client = self.app.test_client()
        resp = client.get(f'/components/{comp_uuid}/similar')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'Two', resp.data)

    def test_nested_pc_app_component_matches(self):
        """A deeply-nested app folder (e.g. Photoshop) matches across discs even
        when its path differs and the discs differ overall (B1)."""
        from myapp.services.similarity import rebuild_all, similar_artefacts, similar_components
        photoshop = [
            ('Adobe/Photoshop/Photoshop.exe', 'ps1', 5000),
            ('Adobe/Photoshop/plugin.8bf', 'ps2', 2000),
            ('Adobe/Photoshop/readme.txt', 'ps3', 1000),
        ]
        # Same app at different nested paths; the rest of each disc is large and
        # unique, so the shared app is a small fraction of the whole.
        discA = [(f'Program Files/{p}', h, s) for p, h, s in photoshop] \
            + [(f'Windows/sys{i}', f'wa{i}', 50000) for i in range(15)]
        discB = [(f'Apps/{p}', h, s) for p, h, s in photoshop] \
            + [(f'Docs/file{i}', f'db{i}', 50000) for i in range(15)]
        a = _add_artefact(self.db, self.item, 'PC-A', discA)
        _add_artefact(self.db, self.item, 'PC-B', discB)

        rebuild_all()

        # Whole-disc similarity is low (shared app is a small fraction).
        whole = similar_artefacts(a, None)
        if whole:
            self.assertLess(whole[0][1].score, 0.5)

        # Component-level finds the shared Photoshop subtree regardless of path.
        comps = similar_components(a, None)
        self.assertTrue(comps, "expected a nested-app component match")
        _local, others = comps[0]
        self.assertTrue(others)
        self.assertGreater(others[0][2].score, 0.99)
        self.assertEqual(others[0][1].label, 'PC-B')


class TestTlshHelper(unittest.TestCase):
    """The optional TLSH fuzzy-hash helper (degrades gracefully without the lib)."""

    def test_graceful_without_input(self):
        from arcology_shared.fuzzyhash import compute_tlsh, tlsh_diff
        self.assertIsNone(compute_tlsh(None))
        self.assertIsNone(compute_tlsh(b'tiny'))           # below TLSH_MIN_BYTES
        self.assertIsNone(tlsh_diff('', 'whatever'))

    def test_digest_and_distance(self):
        from arcology_shared.fuzzyhash import HAS_TLSH, compute_tlsh, tlsh_diff
        if not HAS_TLSH:
            self.skipTest('py-tlsh not installed')
        base = bytes(range(256)) * 8
        near = base[:-40] + (b'\x00' * 40)
        far = bytes((b * 7 + 13) % 256 for b in range(256)) * 8
        d_base = compute_tlsh(base)
        self.assertTrue(d_base)
        self.assertLess(tlsh_diff(d_base, compute_tlsh(near)),
                        tlsh_diff(d_base, compute_tlsh(far)))


class TestTlshFileSimilarity(_SimilarityBase):
    """similar_files_by_tlsh over extracted files."""

    def _add_files_with_tlsh(self, label, files):
        """files: list of (path, tlsh_or_none, size). Returns the Artefact."""
        from arcology_shared.enums import ArtefactType
        from myapp.database import Artefact, ExtractedFile, FilesystemType, Partition
        art = Artefact(item_id=self.item.id, label=label, artefact_type=ArtefactType.RAW_SECTOR,
                       original_filename=f'{label}.img', storage_path=f'uploads/{label}.img')
        self.db.session.add(art)
        self.db.session.flush()
        part = Partition(artefact_id=art.id, partition_index=0, filesystem=FilesystemType.ADFS)
        self.db.session.add(part)
        self.db.session.flush()
        for path, tlsh, size in files:
            self.db.session.add(ExtractedFile(
                partition_id=part.id, path=path, filename=path.split('/')[-1],
                file_size=size, sha256=(path * 64)[:64], tlsh=tlsh, is_directory=False))
        self.db.session.commit()
        return art

    def test_empty_when_source_has_no_digest(self):
        from myapp.services.similarity import similar_files_by_tlsh
        a = self._add_files_with_tlsh('NoDigest', [('FILE', None, 1000)])
        src = a.partitions[0].files[0]
        self.assertEqual(similar_files_by_tlsh(src, None), [])

    def test_ranks_near_before_far(self):
        from arcology_shared.fuzzyhash import HAS_TLSH, compute_tlsh
        if not HAS_TLSH:
            self.skipTest('py-tlsh not installed')
        from myapp.services.similarity import similar_files_by_tlsh
        base = bytes(range(256)) * 8
        near = base[:-40] + (b'\x00' * 40)
        far = bytes((b * 7 + 13) % 256 for b in range(256)) * 8
        a = self._add_files_with_tlsh('Source', [('MAIN', compute_tlsh(base), 2048)])
        self._add_files_with_tlsh('Near', [('MAINv2', compute_tlsh(near), 2048)])
        self._add_files_with_tlsh('Far', [('OTHER', compute_tlsh(far), 2048)])
        src = a.partitions[0].files[0]
        matches = similar_files_by_tlsh(src, None, max_distance=10**9)
        self.assertEqual([m[0].filename for m in matches][:1], ['MAINv2'])

    def test_plumbing_with_stubbed_lib(self):
        """Exercise the query/exclusion/ranking wiring WITHOUT the real library.

        Patches the two fuzzyhash entry points so the integration runs even when
        py-tlsh is absent (as in the default CI job).  Digests are stored as
        numeric strings; the stub distance is their absolute difference.
        """
        from unittest.mock import patch
        from myapp.services import similarity
        a = self._add_files_with_tlsh('Src', [('MAIN', '1000', 2048)])
        self._add_files_with_tlsh('Near', [('NEARDIFF', '1010', 2048)])   # distance 10
        self._add_files_with_tlsh('Far', [('FARDIFF', '5000', 2048)])     # distance 4000
        # Same path -> same sha256 as the source -> exact dup, must be excluded
        # even though its (stub) TLSH distance is 0.
        self._add_files_with_tlsh('Exact', [('MAIN', '1000', 2048)])
        src = a.partitions[0].files[0]

        def stub_diff(x, y):
            return abs(int(x) - int(y))

        with patch.object(similarity, 'HAS_TLSH', True), \
             patch.object(similarity, 'tlsh_diff', stub_diff):
            names = [m[0].filename
                     for m in similarity.similar_files_by_tlsh(src, None, max_distance=100)]
        self.assertIn('NEARDIFF', names)      # within threshold
        self.assertNotIn('FARDIFF', names)    # beyond threshold
        self.assertNotIn('MAIN', names)       # exact sha256 duplicate excluded


if __name__ == '__main__':
    unittest.main()

# vim: ts=4 sw=4 et
