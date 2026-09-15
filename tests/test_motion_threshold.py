"""Minimal regression for the 12.38 km/h static false-positive."""
import unittest

import test_vehicle as fixtures
from vehicle.protocol import parse


class MotionThresholdTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest

    def test_static_noise_interval_zeroes_gpchcx_speed_and_anchors_drift(self):
        point = parse(fixtures.LIVE[0])
        tow = point['tow']
        anchor_lat, anchor_lon = point['lat'] + .00015, point['lon']
        # The three middle samples are stationary: their resultant specific
        # force is 1.007 g, while the reported GNSS speed contains a 12.38
        # km/h residual and the coordinates wander around the true anchor.
        frames = [
            fixtures.altered(tow=tow, status='42', ax=1.02, ay=0, az=0,
                             speed=0, ve=0, vn=0, vu=0,
                             lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow + 1, status='42', ax=1.007, ay=0, az=0,
                             speed=12.38 / 3.6, ve=0, vn=0, vu=0,
                             lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow + 2, status='91', ax=1.007, ay=0, az=0,
                             speed=12.38 / 3.6, ve=0, vn=0, vu=0,
                             lat=anchor_lat + .00020, lon=anchor_lon),
            fixtures.altered(tow=tow + 3, status='91', ax=1.007, ay=0, az=0,
                             speed=12.38 / 3.6, ve=0, vn=0, vu=0,
                             lat=anchor_lat + .00010, lon=anchor_lon),
            fixtures.altered(tow=tow + 4, status='42', ax=1.02, ay=0, az=0,
                             speed=0, ve=0, vn=0, vu=0,
                             lat=anchor_lat, lon=anchor_lon),
        ]
        self.ingest(*frames)
        timestamps = [parse(frame)['t'] for frame in frames]
        result = self.store.query(point['device_id'], timestamps[0] - .1,
                                  timestamps[-1] + .1, bins=20)
        static_times = set(timestamps[1:4])
        static_track = [item for item in result['track'] if item['t'] in static_times]
        print(
            '\nthreshold diagnostic:',
            {
                'deviation_g': .007,
                'summary_max_kmh': result['summary']['max_kmh'],
                'motion_states': [(item['t'], item['motion_state']) for item in result['track']],
                'static_track': [(item['t'], item['lat'], item['speed'], item.get('position_source'))
                                 for item in static_track],
                'segments': [(item['state'], item['max_kmh']) for item in result['segments']],
            },
            flush=True,
        )
        self.assertEqual(result['summary']['max_kmh'], 0.0)
        self.assertEqual([item['motion_state'] for item in static_track],
                         ['stationary', 'stationary', 'stationary'])
        self.assertTrue(all(item['speed'] == 0 for item in static_track))
        self.assertTrue(all(abs(item['lat'] - anchor_lat) < 1e-10 for item in static_track))
        self.assertTrue(all(item.get('position_source') == 'stationary_anchor'
                            for item in static_track))


if __name__ == '__main__':
    unittest.main()
