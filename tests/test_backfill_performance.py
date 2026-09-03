"""Bound SQL work, not machine-dependent wall time; exercise the real backfill."""
import sqlite3
import unittest
from unittest.mock import patch

import test_vehicle as fixtures
from vehicle import quality
from vehicle.protocol import parse
from vehicle.store import Store


class MeteredStore(Store):
    sql_steps = 0
    sql_budget = None

    def connect(self):
        connection = super().connect()

        def progress():
            self.sql_steps += 100
            return int(self.sql_budget is not None and self.sql_steps > self.sql_budget)

        connection.set_progress_handler(progress, 100)
        return connection


class BackfillPerformanceTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown

    def test_batched_backfill_has_bounded_sql_work_and_preserves_raw(self):
        store = MeteredStore(self.store.path)
        base = parse(fixtures.navigation_frame(status='91', speed=.5, ax=1))
        count = 2000
        with store.connect() as c:
            keys = list(base)
            c.executemany(
                f'INSERT INTO points ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})',
                [list(dict(base, t=base['t']+i/10).values()) for i in range(count)])
            before = [tuple(row) for row in c.execute('SELECT * FROM points ORDER BY device_id,t,protocol')]
        store.sql_steps = 0
        store.sql_budget = 1_500_000
        try:
            # Small real batches expose repeated prefix scans across commits.
            with patch.object(quality, 'BACKFILL_BATCH_SIZE', 32):
                written = quality.backfill(store)
        except sqlite3.OperationalError as error:
            self.fail(f'backfill exceeded {store.sql_budget:,} SQL VM steps: {error}')
        finally:
            print(f'\nbackfill {count} rows: {store.sql_steps:,} SQL VM steps (budget 1,500,000)', flush=True)
            store.sql_budget = None
        self.assertEqual(written, count)
        with store.connect() as c:
            self.assertEqual(before, [tuple(row) for row in c.execute('SELECT * FROM points ORDER BY device_id,t,protocol')])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM point_ground_speed WHERE version=?', (quality.VERSION,)).fetchone()[0], count)
        self.assertEqual(quality.backfill(store), 0)

    def test_committed_progress_can_resume_and_seek_same_timestamp_protocol(self):
        base = parse(fixtures.navigation_frame(status='91', speed=.5, ax=1))
        with self.store.connect() as c:
            points = [dict(base, t=base['t']+i/10) for i in range(9)]
            points.append(dict(base, protocol='GPCHC'))
            keys = list(base)
            c.executemany(f'INSERT INTO points ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})',
                          [list(p.values()) for p in points])
            self.assertEqual(quality.previous_point(c, base)['protocol'], 'GPCHC')
            self.assertIsNone(quality.previous_point(c, dict(base, protocol='GPCHC')))
            self.assertIsNone(quality.previous_point(c, dict(base, device_id='different-device')))

        def stop_after_commit(event):
            with self.store.connect() as reader:
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM point_ground_speed').fetchone()[0], event['assessed'])
            raise RuntimeError('simulated interruption after commit')

        with patch.object(quality, 'BACKFILL_BATCH_SIZE', 3):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                quality.backfill(self.store, progress=stop_after_commit)
            events = []
            self.assertEqual(quality.backfill(self.store, progress=events.append), 7)
        self.assertEqual([e['assessed'] for e in events if e['stage']=='quality'], [3, 6, 7])
        with self.store.connect() as c:
            repaired = [tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY device_id,t,protocol')]
            c.execute('DELETE FROM point_quality')
        self.assertEqual(quality.backfill(self.store), 10)
        with self.store.connect() as c:
            self.assertEqual(repaired, [tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY device_id,t,protocol')])


if __name__ == '__main__':
    unittest.main()
