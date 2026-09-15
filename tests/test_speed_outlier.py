"""Minimal regression for the 52.344 km/h trolley speed spike."""
import math
import unittest

import test_vehicle as fixtures
from vehicle.protocol import parse


class SpeedOutlierTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest

    def test_navigation_spike_cannot_become_effective_trolley_speed(self):
        point = parse(fixtures.LIVE[0])
        tow = point['tow']
        common = dict(ax=1.02, ay=0, az=0,
                      lat=point['lat'], lon=point['lon'])
        frames = [
            fixtures.altered(tow=tow, speed=.5, ve=.5, vn=0, vu=0, status='71', **common),
            # Captured production row at 2026-09-03 12:27:50.800:
            # speed=14.54 m/s -> 52.344 km/h, vu=31.82 m/s.
            fixtures.altered(tow=tow + .1, speed=14.54, ve=8.03, vn=-12.12,
                             vu=31.82, status='71', **common),
            # A smaller RTK-float/no-heading spike is also a false speed when
            # its 5.8 m position jump cannot explain a 4.57 m/s vector.
            fixtures.altered(tow=tow + .2, speed=4.57, ve=-2.64, vn=-3.73,
                             vu=1.15, status='91', lat=point['lat'] + .0000526,
                             **{k: v for k, v in common.items() if k != 'lat'}),
            fixtures.altered(tow=tow + .3, speed=.5, ve=.5, vn=0, vu=0, status='71', **common),
        ]
        self.ingest(*frames)
        timestamps = [parse(frame)['t'] for frame in frames]
        result = self.store.query(point['device_id'], timestamps[0] - .1,
                                  timestamps[-1] + .1, bins=20)
        raw_spike = self.store.point(point['device_id'], timestamps[1], raw=True)
        effective_spike = self.store.point(point['device_id'], timestamps[1])
        effective_position_spike = self.store.point(point['device_id'], timestamps[2])
        print(
            '\nspeed diagnostic:',
            {
                'raw_speed_ms': raw_spike['speed'],
                'raw_speed_kmh': raw_spike['speed'] * 3.6,
                'raw_velocity_ms': [raw_spike['ve'], raw_spike['vn'], raw_spike['vu']],
                'horizontal_norm_ms': math.hypot(raw_spike['ve'], raw_spike['vn']),
                'effective_speed': effective_spike['speed'],
                'position_spike_effective_speed': effective_position_spike['speed'],
                'effective_quality': effective_spike.get('quality'),
                'summary_max_kmh': result['summary']['max_kmh'],
            },
            flush=True,
        )
        self.assertAlmostEqual(raw_spike['speed'], math.hypot(raw_spike['ve'], raw_spike['vn']), delta=.01)
        # Valid neighboring points keep their original GPCHCX.speed; both
        # captured drift spikes are absent rather than clamped or zeroed.
        self.assertAlmostEqual(result['summary']['max_kmh'], 1.8)
        self.assertIsNone(effective_spike['speed'])
        self.assertIsNone(effective_position_spike['speed'])


if __name__ == '__main__':
    unittest.main()
