"""Regression: a stationary interval must render at one actual position."""
import unittest
import math
import json

import test_vehicle as fixtures
from vehicle.protocol import parse


class StationaryPositionTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    def ingest(self, *frames):
        # Anchor unit tests take independently confirmed motion labels. The
        # estimator's end-to-end static/moving tests live in test_ground_speed.
        fixtures.StoreTests.ingest(self, *frames)
        with self.store.connect() as c:
            for frame in frames:
                p = parse(frame)
                stationary = p['ax'] == 1
                value = 0 if stationary else p['speed']
                estimate = dict(value=value, state='stationary' if stationary else 'moving',
                                reason='test_confirmed_label', sigma_ms=.02, source='test', window_s=2)
                c.execute('UPDATE point_ground_speed SET estimate=? WHERE device_id=? AND t=? AND protocol=?',
                          (json.dumps(estimate), p['device_id'], p['t'], p['protocol']))
        self.store.rebuild_rollups()

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

    def test_stationary_anchor_is_shared_across_navigation_jump_breaks(self):
        point = parse(fixtures.LIVE[0])
        anchor_lat, anchor_lon = point['lat'] + .00015, point['lon']
        tow = point['tow']
        frames = [
            fixtures.altered(tow=tow, status='42', ax=1.02, speed=0,
                             lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow+1, status='42', ax=1, speed=0,
                             lat=anchor_lat, lon=anchor_lon),
            # This isolated GNSS jump is quarantined and breaks the rendered
            # route, but it must not create a second static position anchor.
            fixtures.altered(tow=tow+2, status='91', ax=1, speed=0,
                             lat=anchor_lat+.01, lon=anchor_lon),
            fixtures.altered(tow=tow+3, status='91', ax=1, speed=0,
                             lat=anchor_lat, lon=anchor_lon),
            fixtures.altered(tow=tow+4, status='91', ax=1, speed=0,
                             lat=anchor_lat+.00020, lon=anchor_lon),
            fixtures.altered(tow=tow+5, status='42', ax=1.02, speed=0,
                             lat=anchor_lat, lon=anchor_lon),
        ]
        self.ingest(*frames)
        timestamps = [parse(frame)['t'] for frame in frames]
        result = self.store.query(point['device_id'], timestamps[0] - .1,
                                  timestamps[-1] + .1, bins=20)
        static_track = [item for item in result['track'] if item['motion_state'] == 'stationary']
        print('\nstatic anchors across jump:',
              [(item['t'], item['lat'], item.get('position_source')) for item in static_track], flush=True)
        self.assertGreaterEqual(len(static_track), 2)
        self.assertTrue(all(abs(item['lat'] - anchor_lat) < 1e-10 for item in static_track))
        self.assertTrue(all(item.get('position_source') == 'stationary_anchor'
                            for item in static_track))

    def test_stationary_anchor_does_not_follow_drift_between_jump_split_parts(self):
        """Navigation jump breaks must not turn one stop into two positions."""
        from vehicle.aggregate import QueryCombiner

        anchor = dict(lat=31.24572566, lon=121.61562931)
        drift = dict(lat=31.24592566, lon=121.61562931)
        track = [
            dict(t=100.0, **anchor, motion_state='stationary'),
            dict(t=101.0, **drift, motion_state='stationary'),
        ]
        parts = [
            dict(start=100.0, end=100.0, state='stationary',
                 position_candidates=[dict(**anchor, t=100.0, confidence=100)]),
            dict(start=101.0, end=101.0, state='stationary',
                 position_candidates=[dict(**drift, t=101.0, confidence=100)]),
        ]
        rendered = QueryCombiner._anchor_stationary_track(track, parts)
        print('jump-split stationary anchors:',
              [(item['t'], item['lat'], item.get('position_source')) for item in rendered], flush=True)
        self.assertTrue(all(abs(item['lat'] - anchor['lat']) < 1e-10 for item in rendered))


if __name__ == '__main__':
    unittest.main()
