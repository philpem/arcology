"""PostgreSQL regression tests for overlapping destination slug allocation.

Run with SQLALCHEMY_DATABASE_URI pointing at a test PostgreSQL database.
Each run creates and drops its own schema; SQLite skips these row-lock tests.
"""

import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import event, select, text
from arcology_shared.enums import ArtefactType
from myapp.database import Artefact, Item
from myapp.extensions import db
from myapp.services.artefact_lifecycle import move_artefact_to_item, move_artefacts_to_item


class TestMoveConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myapp.app import create_app

        cls.app = create_app()
        with cls.app.app_context():
            if db.engine.dialect.name != 'postgresql':
                raise unittest.SkipTest('PostgreSQL required for row-lock concurrency tests')
            cls.schema = 'move_test_' + uuid.uuid4().hex
            with db.engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA {cls.schema}'))
            db.engine.update_execution_options(schema_translate_map={None: cls.schema})
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            with db.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA {cls.schema} CASCADE'))
            db.engine.dispose()

    def _overlapping_moves(self, *, second_is_batch):
        with self.app.app_context():
            sources = [Item(name='Source one'), Item(name='Source two')]
            target = Item(name='Target')
            db.session.add_all(sources + [target])
            db.session.flush()
            roots = [Artefact(item_id=s.id, label='Disc', slug='disc',
                              artefact_type=ArtefactType.RAW_SECTOR,
                              original_filename='disc.img', storage_path='disc.img')
                     for s in sources]
            db.session.add_all(roots)
            db.session.commit()
            inputs = [(s.id, a.id, a.uuid, target.id) for s, a in zip(sources, roots, strict=True)]
            engine = db.engine

        first_snapshot = threading.Event()
        release_first = threading.Event()
        second_snapshot = threading.Event()
        second_ready = threading.Event()
        second_finished = threading.Event()
        second_pid = []

        def pause_snapshot(conn, cursor, statement, parameters, context, executemany):
            namespace_query = (
                statement.startswith('SELECT ' + self.schema + '.artefacts.slug')
                or ('WHERE ' + self.schema + '.artefacts.slug =') in statement
            )
            if namespace_query:
                # Pause after the namespace query executes, before its result
                # is consumed. Two unlocked allocators would see the same empty
                # namespace, even if one commits before the other resumes.
                if conn.info.get('move_test_first'):
                    first_snapshot.set()
                    if not release_first.wait(10):
                        raise TimeoutError('first move was not released')
                elif conn.info.get('move_test_second'):
                    second_snapshot.set()

        def move(values, first):
            source_id, artefact_id, artefact_uuid, target_id = values
            with self.app.app_context():
                conn = db.session.connection()
                marker = 'move_test_first' if first else 'move_test_second'
                connection_info = conn.info
                connection_info[marker] = True
                try:
                    if not first:
                        second_pid.append(conn.scalar(text('SELECT pg_backend_pid()')))
                        second_ready.set()
                    source = db.session.get(Item, source_id)
                    destination = db.session.get(Item, target_id)
                    if first or second_is_batch:
                        move_artefacts_to_item(source, destination, [artefact_uuid], None, sees_all=True)
                    else:
                        move_artefact_to_item(db.session.get(Artefact, artefact_id), destination)
                finally:
                    connection_info.pop(marker, None)
                    if not first:
                        second_finished.set()

        event.listen(engine, 'after_cursor_execute', pause_snapshot)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(move, inputs[0], True)
                try:
                    self.assertTrue(first_snapshot.wait(10), 'first move never read its namespace')
                    second = pool.submit(move, inputs[1], False)
                    self.assertTrue(second_ready.wait(10), 'second connection never started')
                    # Release the first move once the second either reaches its
                    # namespace query (broken behavior) or waits on a PG lock.
                    # Observing the database avoids guessing thread timing.
                    deadline = time.monotonic() + 10
                    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as observer:
                        while not second_snapshot.is_set() and not second_finished.is_set():
                            wait = observer.scalar(text(
                                'SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid'
                            ), {'pid': second_pid[0]})
                            if wait == 'Lock':
                                break
                            if time.monotonic() >= deadline:
                                self.fail('second move neither blocked nor reached slug allocation')
                            time.sleep(0.01)
                finally:
                    release_first.set()
                first.result(timeout=10)
                second.result(timeout=10)
        finally:
            release_first.set()
            event.remove(engine, 'after_cursor_execute', pause_snapshot)

        with self.app.app_context():
            slugs = list(db.session.scalars(select(Artefact.slug).where(
                Artefact.item_id == inputs[0][3])))
            self.assertEqual(set(slugs), {'disc', 'disc-2'})
            self.assertEqual(len(slugs), 2)

    def test_overlapping_batches_allocate_distinct_slugs(self):
        self._overlapping_moves(second_is_batch=True)

    def test_single_move_and_batch_allocate_distinct_slugs(self):
        self._overlapping_moves(second_is_batch=False)


if __name__ == '__main__':
    unittest.main()
