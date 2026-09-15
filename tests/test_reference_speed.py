"""Regression: the removed v9 reference-speed channel must stay absent."""
import csv
import io
import json
import unittest

import test_ground_speed as ground_fixtures
import test_vehicle as fixtures
from vehicle import quality
from vehicle.ground_speed import Estimator, METHOD, valid_reference
from vehicle.protocol import parse


class SingleGroundSpeedChannelTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest
    frames = ground_fixtures.GroundSpeedTests.frames
    last = ground_fixtures.GroundSpeedTests.last

    def test_projection_has_no_reference_speed_fields(self):
        frames = self.frames(n=3)
        self.ingest(*frames)
        point = self.last(frames)
        self.assertFalse({'speed_reference', 'speed_reference_low', 'speed_reference_high'} & set(point))
        self.assertNotIn('reference', point['ground_speed'])

    def test_query_has_only_one_speed_series(self):
        frames = self.frames(n=31)
        self.ingest(*frames)
        start, end = parse(frames[0])['t'], parse(frames[-1])['t']
        result = self.store.query(parse(frames[0])['device_id'], start, end, bins=20)
        self.assertIn('speed', result['series'])
        self.assertFalse({'speed_reference', 'speed_reference_low', 'speed_reference_high'} & set(result['series']))

    def test_summary_has_no_reference_coverage(self):
        frames = self.frames(n=31)
        self.ingest(*frames)
        result = self.store.query(parse(frames[0])['device_id'],
                                  parse(frames[0])['t'], parse(frames[-1])['t'], bins=20)
        self.assertIn('speed_coverage_pct', result['summary'])
        self.assertNotIn('reference_speed_samples', result['summary'])
        self.assertNotIn('reference_speed_coverage_pct', result['summary'])
        self.assertNotIn('display_speed_coverage_pct', result['summary'])

    def test_vector_direction_and_magnitude_never_change_gpchcx_speed(self):
        estimator = Estimator()
        base = parse(self.frames(n=1, speed=.7)[0])
        for ve, vn in ((0, 0), (3, 4), (-20, 15)):
            estimate = estimator.observe(dict(base, ve=ve, vn=vn, ax=1.02))
            self.assertEqual(estimate['value'], .7)

    def test_low_speed_motion_is_not_promoted_to_a_second_channel(self):
        estimate = Estimator().observe(parse(self.frames(n=1, speed=.03, ax=1.02)[0]))
        self.assertEqual(estimate['value'], .03)
        self.assertEqual(estimate['state'], 'moving')
        self.assertNotIn('reference', estimate)

    def test_reference_metadata_is_rejected(self):
        estimate = Estimator().observe(parse(self.frames(n=1)[0]))
        self.assertTrue(valid_reference(estimate))
        self.assertFalse(valid_reference(dict(estimate, reference={'value': .5})))

    def test_raw_sixty_and_six_hundred_second_rollups_keep_same_speed(self):
        frames = self.frames(n=1301, speed=.5)
        self.ingest(*frames)
        point = parse(frames[0])
        start = point['t']
        cases = ((frames[-1] and parse(frames[-1])['t'], 'raw', 0),
                 (start + 21600, 'rollup', 60),
                 (start + 172800, 'rollup', 600))
        for end, source, resolution in cases:
            with self.subTest(source=source):
                result = self.store.query(point['device_id'], start, end, bins=50)
                values = [row[1] for row in result['series']['speed'] if row[1] is not None]
                self.assertTrue(values)
                self.assertTrue(all(abs(value - .5) < 1e-12 for value in values))
                self.assertEqual(result['aggregation']['source'], source)
                self.assertEqual(result['aggregation']['source_resolution_s'], resolution)

    def test_versioned_record_names_exact_source_and_method(self):
        frame = self.frames(n=1, speed=.8)[0]
        self.ingest(frame)
        estimate = self.last([frame])['ground_speed']
        self.assertEqual(estimate['source'], 'GPCHCX.speed')
        self.assertEqual(estimate['method'], METHOD)
        self.assertEqual(estimate['raw_speed'], .8)


class SingleGroundSpeedExportTests(unittest.TestCase):
    setUp = fixtures.ApiTests.setUp
    tearDown = fixtures.ApiTests.tearDown
    ingest = fixtures.ApiTests.ingest
    request = fixtures.ApiTests.request
    frames = ground_fixtures.GroundSpeedTests.frames

    def test_export_roundtrip_and_reference_metadata_rejected(self):
        frames = self.frames(n=31, speed=.5)
        self.ingest(*frames)
        point = parse(frames[-1])
        status, payload = self.request(
            f'/api/export?device={point["device_id"]}&start={point["t"]-1}&end={point["t"]}', 'reader')
        self.assertEqual(status, 200)
        status, body = self.request('/api/offline/analyze', 'reader', payload,
                                    headers={'Content-Type': 'text/csv'})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertGreater(result['summary']['speed_samples'], 0)
        self.assertNotIn('speed_reference', result['series'])

        rows = list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
        estimate = json.loads(rows[0]['ground_speed_json'])
        estimate['reference'] = {'value': .5}
        rows[0]['ground_speed_json'] = json.dumps(estimate)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
        status, body = self.request('/api/offline/analyze', 'reader', output.getvalue().encode(),
                                    headers={'Content-Type': 'text/csv'})
        self.assertEqual(status, 400)
        self.assertIn('元数据无效', json.loads(body)['error'])


if __name__ == '__main__':
    unittest.main()
