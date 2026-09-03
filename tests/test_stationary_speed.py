"""Regression: stationary GNSS drift must never become vehicle speed."""
import unittest

import test_vehicle as fixtures
from vehicle.protocol import parse


class StationarySpeedTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest

    def test_stationary_drift_is_zero_in_raw_and_both_rollup_queries(self):
        point = parse(fixtures.LIVE[0])
        start = int(point['t'] // 600) * 600
        tow = point['tow'] + start + 100 - point['t']
        frames = [fixtures.navigation_frame(
            tow=tow+i, status='42', ax=1, ay=0, az=0,
            speed=speed/3.6, ve=speed/3.6, vn=0, vu=0,
            lat=point['lat']+i*.000001, lon=point['lon'],
        ) for i, speed in enumerate((.04, .06, .08))]
        # Establish the static fact using the preceding raw window. A 10.73
        # km/h residual alone is no longer labelled static from quiet IMU.
        self.ingest(*[fixtures.navigation_frame(tow=tow-2+i/10,status='42',
                                                ax=1,speed=.01) for i in range(20)], *frames)

        for span, source, resolution in ((300, 'raw', 0),
                                          (21600, 'rollup', 60),
                                          (172800, 'rollup', 600)):
            with self.subTest(source=source, resolution_s=resolution):
                result = self.store.query(point['device_id'], start+100, start+100+span, bins=50)
                self.assertEqual(result['total'], 3)
                self.assertEqual(result['aggregation']['source'], source)
                self.assertEqual(result['aggregation']['source_resolution_s'], resolution)
                self.assertTrue(result['motion_states'])
                self.assertTrue(all(row[1] == 'stationary' for row in result['motion_states']))
                values = [row[1:] for row in result['series']['speed']]
                print(f'\n{source}/{resolution}s: stationary speed [mean,min,max] m/s = {values}', flush=True)
                self.assertEqual(values, [[0, 0, 0] for _ in values],
                                 'Stationary samples must contribute zero, not GNSS velocity residuals')
                self.assertEqual(result['summary']['distance_km'], 0)
                self.assertTrue(all(p['speed'] == 0 for p in result['track']))

        for frame in frames:
            sample = parse(frame)
            with self.subTest(point_t=sample['t']):
                raw = self.store.point(point['device_id'], sample['t'], raw=True)
                effective = self.store.point(point['device_id'], sample['t'])
                self.assertEqual(raw['speed'], sample['speed'], 'Raw evidence must stay unchanged')
                self.assertEqual(raw['lat'], sample['lat'])
                self.assertEqual(effective['motion_state'], 'stationary')
                self.assertEqual(effective['speed'], 0)


if __name__ == '__main__':
    unittest.main()
