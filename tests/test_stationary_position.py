"""Regression: a stationary interval must render at one actual position."""
import unittest
import math

import test_vehicle as fixtures
from vehicle.protocol import parse


class StationaryPositionTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest

    def test_stationary_drift_is_replaced_by_high_confidence_position_in_track(self):
        point = parse(fixtures.LIVE[0])
        anchor_lat, anchor_lon = point['lat'] + .00015, point['lon']
        tow = point['tow']
        frames = [
            fixtures.altered(tow=tow, status='42', ax=1.02, speed=2,
                             lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow+1, status='42', ax=1.02, speed=2,
                             lat=anchor_lat+.00001, lon=anchor_lon),
            # Static GNSS drift wanders either side of the true position.  The
            # middle sample is the high-confidence combined-navigation fix.
            fixtures.altered(tow=tow+2, status='91', ax=1, speed=0,
                             lat=point['lat']+.00010, lon=anchor_lon),
            fixtures.altered(tow=tow+3, status='42', ax=1, speed=0,
                             lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow+4, status='91', ax=1, speed=0,
                             lat=point['lat']+.00020, lon=anchor_lon),
            fixtures.altered(tow=tow+5, status='91', ax=1, speed=0,
                             lat=point['lat']+.00020, lon=anchor_lon),
            fixtures.altered(tow=tow+6, status='42', ax=1.02, speed=2,
                             lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow+7, status='42', ax=1.02, speed=2,
                             lat=anchor_lat+.00001, lon=anchor_lon),
        ]
        self.ingest(*frames)
        start = point['t'] - 1
        result = self.store.query(point['device_id'], start, point['t'] + 8, bins=9)
        static_track = [p for p in result['track'] if p['motion_state'] == 'stationary']
        print('\nstationary track:',
              [(p['t'], p['lat'], p['lon'], p['nav_mode']) for p in static_track], flush=True)
        self.assertGreaterEqual(len(static_track), 3)
        self.assertTrue(all(abs(p['lat'] - anchor_lat) < 1e-10 for p in static_track),
                        'Static drift must not be rendered as movement')
        self.assertTrue(all(abs(p['lon'] - anchor_lon) < 1e-10 for p in static_track))
        self.assertTrue(all(p.get('position_source') == 'stationary_anchor' for p in static_track))
        self.assertEqual(result['track'][0]['lat'], anchor_lat)
        self.assertEqual(result['track'][-1]['lat'], anchor_lat+.00001)

        for frame in frames[2:6]:
            sample = parse(frame)
            raw = self.store.point(point['device_id'], sample['t'], raw=True)
            self.assertEqual(raw['lat'], sample['lat'], 'Raw coordinates must remain evidence')

    def test_stationary_anchor_survives_sixty_second_rollup_boundaries(self):
        point = parse(fixtures.LIVE[0])
        anchor_lat, anchor_lon = point['lat'] + .00015, point['lon']
        tow = point['tow']
        frames = [
            fixtures.altered(tow=tow, status='42', ax=1.02, speed=2, lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow+1, status='42', ax=1.02, speed=2, lat=anchor_lat, lon=anchor_lon),
        ]
        for index in range(2, 123):
            drift = .0001 * math.sin(index / 9)
            frames.append(fixtures.altered(
                tow=tow+index, status='42' if index == 62 else '91', ax=1, speed=0,
                lat=anchor_lat if index == 62 else anchor_lat+drift, lon=anchor_lon))
        frames.extend([
            fixtures.altered(tow=tow+123, status='42', ax=1.02, speed=2, lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow+124, status='42', ax=1.02, speed=2, lat=anchor_lat+.00001, lon=anchor_lon),
        ])
        self.ingest(*frames)
        bucket = int(point['t'] // 60) * 60
        result = self.store.query(point['device_id'], bucket, bucket+21600, bins=700)
        self.assertEqual(result['aggregation']['source'], 'rollup')
        static_track = [p for p in result['track'] if p['motion_state'] == 'stationary']
        print('rollup stationary track:',
              [(p['t'], p['lat'], p['nav_mode'], p.get('position_source')) for p in static_track], flush=True)
        self.assertGreaterEqual(len(static_track), 3)
        self.assertTrue(all(abs(p['lat'] - anchor_lat) < 1e-10 for p in static_track))


if __name__ == '__main__':
    unittest.main()
