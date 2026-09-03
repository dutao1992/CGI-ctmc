"""Reference-only low-speed coverage; trusted statistics must stay unchanged."""
import json
import unittest

import test_vehicle as fixtures
import test_ground_speed as ground_fixtures
from vehicle import quality
from vehicle.protocol import parse


class ReferenceSpeedTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest
    frames = ground_fixtures.GroundSpeedTests.frames
    last = ground_fixtures.GroundSpeedTests.last

    def test_minimal_low_speed_reference_does_not_claim_trusted_motion(self):
        frames = self.frames(n=31, ve_std=.2, vn_std=.2)
        self.ingest(*frames)
        p = self.last(frames)
        reference = p.get('speed_reference')
        print('\nlow-speed regression: raw=0.5 m/s, trusted=', p['speed'],
              'reference=', reference, 'state=', p['motion_state'], flush=True)
        self.assertAlmostEqual(reference, .5)
        self.assertIsNone(p['speed'])
        self.assertEqual(p['motion_state'], 'unknown')
        self.assertGreaterEqual(p['ground_speed']['reference']['sigma_ms'], .2828)
        result = self.store.query(p['device_id'], parse(frames[0])['t'], p['t'], bins=20)
        self.assertIsNone(result['summary']['max_kmh'])
        self.assertEqual(result['summary']['moving_s'], 0)
        self.assertEqual(result['summary']['distance_km'], 0)
        self.assertEqual(result['summary']['speed_samples'], 0)
        self.assertGreater(result['summary']['reference_speed_samples'], 0)
        self.assertTrue(any(row[1] is not None for row in result['series']['speed_reference']))

    def test_reference_never_revives_invalid_navigation_or_bad_precision(self):
        for changes in [dict(status='00'), dict(ve_std=.4, vn_std=.4),
                        dict(speed=14.54, ve=8.03, vn=-12.12, vu=31.82,
                             ve_std=10.74, vn_std=6.62, vu_std=9.78)]:
            from vehicle.ground_speed import Estimator
            estimator = Estimator()
            for frame in self.frames(**changes):
                estimate = estimator.observe(parse(frame))
                self.assertIsNone(estimate.get('reference'))

    def test_reference_current_bad_frame_and_recovery_window(self):
        from vehicle.ground_speed import Estimator
        estimator = Estimator()
        points = [parse(f) for f in self.frames(n=61, ve_std=.2, vn_std=.2)]
        for p in points[:25]:
            estimate = estimator.observe(p)
        self.assertIsNotNone(estimate.get('reference'))
        bad = dict(points[25], nav_mode=0)
        self.assertIsNone(estimator.observe(bad).get('reference'))
        for p in points[26:36]:
            self.assertIsNone(estimator.observe(p).get('reference'))
        self.assertIsNotNone(estimator.observe(points[36]).get('reference'))

    def test_confirmed_stop_and_trusted_motion_never_also_get_reference(self):
        from vehicle.ground_speed import Estimator
        for speed in (.04, 8.):
            estimator = Estimator()
            for frame in self.frames(speed=speed, ve=speed):
                estimate = estimator.observe(parse(frame))
                if estimate['value'] is not None:
                    self.assertIsNone(estimate.get('reference'))
            self.assertEqual(estimate['value'], 0 if speed == .04 else 8.)

    def test_reference_restart_backfill_does_not_change_raw(self):
        frames = self.frames(n=41, ve_std=.2, vn_std=.2)
        self.ingest(*frames)
        with self.store.connect() as c:
            raw = [tuple(r) for r in c.execute('SELECT * FROM points ORDER BY t')]
            before = [tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY t')]
            for frame in frames[12::7]:
                c.execute('DELETE FROM point_ground_speed WHERE t=?', (parse(frame)['t'],))
        quality.backfill(self.store)
        with self.store.connect() as c:
            self.assertEqual(raw, [tuple(r) for r in c.execute('SELECT * FROM points ORDER BY t')])
            self.assertEqual(before, [tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY t')])
        self.assertIsNotNone(json.loads(before[-1][-1]).get('reference'))

    def test_vector_median_rejects_noisy_direction_without_positive_speed_bias(self):
        from vehicle.ground_speed import Estimator
        points = [parse(f) for f in self.frames(ve_std=.2, vn_std=.2)]
        estimator = Estimator()
        for i, p in enumerate(points):
            p.update(ve=.5 if i % 2 else -.5)
            result = estimator.observe(p)
        self.assertIsNone(result.get('reference'))

    def test_reference_breaks_on_mode_change_gap_and_single_bad_current_frame(self):
        from vehicle.ground_speed import Estimator
        for changes in [dict(nav_mode=2), dict(t=10000), dict(ve=2., speed=2.)]:
            estimator = Estimator()
            points = [parse(f) for f in self.frames(n=31, ve_std=.2, vn_std=.2)]
            for p in points[:-1]: estimator.observe(p)
            self.assertIsNone(estimator.observe(dict(points[-1], **changes)).get('reference'))

    def test_reference_aggregation_preserves_counts_extrema_and_trusted_summary(self):
        from vehicle.aggregate import RollupBuilder, QueryCombiner
        frames = self.frames(n=1301, ve_std=.2, vn_std=.2)
        self.ingest(*frames)
        with self.store.connect() as c:
            rows = c.execute(quality.JOIN+' ORDER BY p.device_id,p.t,p.protocol').fetchall()
        start, end = rows[0]['t'], rows[-1]['t']
        results = []
        for seconds in (1, 60, 600):
            combiner = QueryCombiner(rows[0]['device_id'], start, end, 20)
            current = builder = None
            for row in rows:
                key = int(row['t']//seconds)*seconds
                if key != current:
                    if builder: combiner.add(builder.snapshot())
                    current, builder = key, RollupBuilder(row['device_id'], key, seconds)
                builder.add(row)
            combiner.add(builder.snapshot())
            results.append(combiner.finish([], 'test', seconds, 0))
        for result in results:
            s = result['summary']
            self.assertIsNone(s['max_kmh'])
            self.assertEqual((s['speed_samples'], s['moving_s'], s['distance_km']), (0,0,0))
            self.assertEqual(s['reference_speed_samples'], 1291)
            self.assertAlmostEqual(s['display_speed_coverage_pct'], 100*1291/1301)
            self.assertTrue(all(r[2:] == [.5,.5] for r in result['series']['speed_reference'] if r[1] is not None))


class ReferenceExportTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    tearDown = fixtures.ApiTests.tearDown
    ingest = fixtures.ApiTests.ingest
    request = fixtures.ApiTests.request
    frames = ground_fixtures.GroundSpeedTests.frames

    def test_reference_export_offline_roundtrip_and_invalid_metadata_rejected(self):
        import csv
        import io
        frames = self.frames(n=31, ve_std=.2, vn_std=.2)
        # The API fixture already contains the first timestamp; replace is not
        # allowed during ingest, so export only the later reference-only window.
        self.ingest(*frames)
        p = parse(frames[-1])
        status, payload = self.request(f'/api/export?device={p["device_id"]}&start={p["t"]-1}&end={p["t"]}', 'reader')
        self.assertEqual(status, 200)
        status, body = self.request('/api/offline/analyze', 'reader', payload, headers={'Content-Type':'text/csv'})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['summary']['reference_speed_samples'], 11)
        self.assertEqual(result['summary']['speed_samples'], 0)
        self.assertIsNone(result['summary']['max_kmh'])
        rows = list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
        estimate = json.loads(rows[0]['ground_speed_json'])
        estimate['reference']['value'] = float('nan')
        rows[0]['ground_speed_json'] = json.dumps(estimate)
        output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
        status, body = self.request('/api/offline/analyze', 'reader', output.getvalue().encode(), headers={'Content-Type':'text/csv'})
        self.assertEqual(status, 400)
        self.assertIn('元数据无效', json.loads(body)['error'])


if __name__ == '__main__':
    unittest.main()
