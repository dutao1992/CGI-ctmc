"""Acceptance written before the v8 implementation; all inputs are synthetic.

The failure magnitudes/uncertainties are copied from the 2026-09-03 raw evidence.
"""
import unittest
import json
import test_vehicle as fixtures
from vehicle.protocol import parse
from vehicle import quality
from vehicle.store import Ingestor


class GroundSpeedTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest

    def frames(self, n=41, **changes):
        base = parse(fixtures.LIVE[0])
        common = dict(status='91', speed=.5, ve=.5, vn=0, vu=0,
                      ve_std=.02, vn_std=.02, vu_std=.03,
                      ax=1, ay=0, az=0, gx=0, gy=0, gz=0)
        common.update(changes)
        return [fixtures.altered(tow=base['tow']+i/10,
                                 # A 5 Hz navigation solution held in 10 Hz frames.
                                 lat=base['lat'], lon=base['lon']+(i//2)*.000001,
                                 **common) for i in range(n)]

    def last(self, frames, raw=False):
        p = parse(frames[-1])
        return self.store.point(p['device_id'], p['t'], raw=raw)

    def test_quiet_constant_trolley_motion_is_not_zeroed(self):
        frames = self.frames()
        self.ingest(*frames)
        p = self.last(frames)
        print('\nconstant trolley: expected 1.8 km/h, got', p['speed'], 'm/s', flush=True)
        self.assertAlmostEqual(p['speed'], .5)
        self.assertEqual(p['motion_state'], 'moving')

    def test_held_navigation_is_not_a_position_speed_contradiction(self):
        frames = self.frames(speed=8, ve=8, ax=1.02)
        self.ingest(*frames)
        self.assertAlmostEqual(self.last(frames[:-1])['speed'], 8)
        self.assertAlmostEqual(self.last(frames)['speed'], 8)

    def test_invalid_navigation_is_unknown_not_stationary_zero(self):
        frames = self.frames(status='00', speed=0, ve=0)
        self.ingest(*frames)
        p = self.last(frames)
        self.assertIsNone(p['speed'])
        self.assertEqual(p['motion_state'], 'unknown')

    def test_uncertain_trolley_spike_is_missing_not_clamped_or_zero(self):
        frames = self.frames(speed=14.54, ve=8.03, vn=-12.12, vu=31.82,
                             ve_std=10.74, vn_std=6.62, vu_std=9.78)
        self.ingest(*frames)
        p = self.last(frames)
        print('\nraw spike:', self.last(frames, raw=True)['speed']*3.6,
              'km/h; derived:', p['speed'], flush=True)
        self.assertIsNone(p['speed'])
        self.assertEqual(p['motion_state'], 'unknown')

    def test_confirmed_quiet_stop_is_zero_with_a_fixed_track(self):
        frames = self.frames(speed=.04, ve=.04)
        self.ingest(*frames)
        p = self.last(frames)
        self.assertEqual(p['speed'], 0)
        self.assertEqual(p['motion_state'], 'stationary')
        result = self.store.query(p['device_id'], p['t']-1, p['t'], bins=20)
        self.assertEqual(result['summary']['max_kmh'], 0)
        self.assertEqual(len({(x['lat'], x['lon']) for x in result['track']}), 1)

    def test_velocity_uncertainty_cannot_be_reported_as_exact_slow_speed(self):
        frames = self.frames(speed=.5, ve=.5, ve_std=.4, vn_std=.4, ax=1.02)
        self.ingest(*frames)
        self.assertIsNone(self.last(frames)['speed'])

    def test_restart_sparse_backfill_and_raw_immutability(self):
        frames = self.frames(n=51)
        self.ingest(*frames[:25])
        self.ing = Ingestor(self.store, self.raw)
        self.ingest(*frames[25:])
        with self.store.connect() as c:
            raw = [tuple(r) for r in c.execute('SELECT * FROM points ORDER BY t')]
            estimates = [tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY t')]
            for frame in frames[7::9]:
                p = parse(frame)
                c.execute('DELETE FROM point_ground_speed WHERE t=?', (p['t'],))
        self.assertGreater(quality.backfill(self.store), 0)
        self.assertEqual(quality.backfill(self.store), 0)
        with self.store.connect() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM points ORDER BY t')], raw)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY t')], estimates)

    def test_late_frame_repairs_following_window(self):
        frames = self.frames(n=31)
        self.ingest(*frames[:12], *frames[13:])
        self.ingest(frames[12])
        with self.store.connect() as c:
            late = [tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY t')]
            c.execute('DELETE FROM point_quality')
        quality.backfill(self.store)
        with self.store.connect() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM point_ground_speed ORDER BY t')], late)

    def test_isolated_low_sigma_velocity_spike_is_not_smoothed_into_speed(self):
        frames = self.frames(n=31)
        p = parse(frames[-1])
        spike = fixtures.navigation_frame(tow=p['tow']+.1,status='91',speed=10,ax=1.02)
        self.ingest(*frames, spike)
        value = self.last([spike])
        self.assertIsNone(value['speed'])
        self.assertEqual(value['ground_speed']['reason'], 'velocity_jump')

    def test_current_bad_solution_never_holds_last_good_speed(self):
        frames = self.frames(n=31)
        p = parse(frames[-1])
        bad = fixtures.navigation_frame(tow=p['tow']+.1,status='00',speed=0,ax=1)
        self.ingest(*frames, bad)
        self.assertIsNone(self.last([bad])['speed'])

    def test_navigation_field_quarantine_cannot_be_resurrected_by_estimator(self):
        frames = self.frames(speed=20, ve=20, vu=6, ax=1.02)
        self.ingest(*frames)
        point = self.last(frames)
        self.assertIn('speed', point['quality']['excluded_fields'])
        self.assertIsNone(point['speed'])


if __name__ == '__main__':
    unittest.main()
