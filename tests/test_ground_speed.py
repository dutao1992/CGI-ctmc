"""Regression coverage for the GPCHCX.speed + vibration-zero policy."""
import unittest

import test_vehicle as fixtures
from vehicle import quality
from vehicle.ground_speed import Estimator
from vehicle.protocol import parse
from vehicle.store import Ingestor


class GroundSpeedTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest

    def frames(self, n=41, **changes):
        base = parse(fixtures.LIVE[0])
        common = dict(status='91', speed=.5, ve=20, vn=-15, vu=0,
                      ve_std=.4, vn_std=.4, vu_std=.4,
                      ax=1.02, ay=0, az=0, gx=0, gy=0, gz=0)
        common.update(changes)
        return [fixtures.altered(tow=base['tow']+i/10,
                                 lat=base['lat'], lon=base['lon']+(i//2)*.000001,
                                 **common) for i in range(n)]

    def last(self, frames, raw=False):
        point = parse(frames[-1])
        return self.store.point(point['device_id'], point['t'], raw=raw)

    def test_moving_speed_is_exact_raw_gpchcx_speed_not_vector_norm(self):
        frames = self.frames(speed=.5, ve=3, vn=4)
        self.ingest(*frames)
        point = self.last(frames)
        self.assertEqual(point['speed'], .5)
        self.assertEqual(point['ground_speed']['raw_speed'], .5)
        self.assertEqual(point['ground_speed']['reason'], 'gpchcx_speed')
        self.assertEqual(point['motion_state'], 'moving')

    def test_velocity_standard_deviation_does_not_estimate_or_zero_speed(self):
        frames = self.frames(speed=.7, ve=.1, vn=.1, ve_std=4, vn_std=4, vu_std=4)
        self.ingest(*frames)
        self.assertEqual(self.last(frames)['speed'], .7)

    def test_vibration_stationary_immediately_sets_only_derived_speed_to_zero(self):
        frame = self.frames(n=1, speed=10.73, ax=1, ay=0, az=0)[0]
        self.ingest(frame)
        raw, effective = self.last([frame], True), self.last([frame])
        self.assertEqual(raw['speed'], 10.73)
        self.assertEqual(effective['speed'], 0)
        self.assertEqual(effective['motion_state'], 'stationary')
        self.assertEqual(effective['ground_speed']['reason'], 'vibration_stationary')

    def test_missing_vibration_is_unknown_not_fabricated_zero(self):
        estimate = Estimator().observe(dict(parse(fixtures.LIVE[0]), ax=None))
        self.assertEqual((estimate['state'], estimate['value'], estimate['reason']),
                         ('unknown', None, 'motion_unknown'))

    def test_non_gpchcx_never_enters_ground_speed_profile(self):
        estimate = Estimator().observe(dict(parse(fixtures.LIVE[0]), protocol='GPCHC', ax=1.02))
        self.assertEqual((estimate['state'], estimate['value'], estimate['reason']),
                         ('unknown', None, 'non_gpchcx'))

    def test_negative_or_nonfinite_gpchcx_speed_is_missing_not_zero(self):
        base = parse(fixtures.LIVE[0])
        for bad_speed in (-1, float('nan'), float('inf')):
            with self.subTest(speed=bad_speed):
                estimate = Estimator().observe(dict(base, speed=bad_speed, ax=1.02))
                self.assertEqual((estimate['state'], estimate['value'], estimate['reason']),
                                 ('unknown', None, 'missing_speed'))

    def test_obvious_velocity_solution_outlier_is_missing_not_zero(self):
        frames = self.frames(speed=14.54, ve=8.03, vn=-12.12, vu=31.82,
                             ve_std=10.74, vn_std=6.62, vu_std=9.78)
        self.ingest(*frames)
        point = self.last(frames)
        self.assertIsNone(point['speed'])
        self.assertEqual(point['motion_state'], 'unknown')
        self.assertIn('speed', point['quality']['excluded_fields'])

    def test_obvious_position_jump_also_removes_its_speed(self):
        base = parse(fixtures.LIVE[0])
        frames = [fixtures.navigation_frame(tow=base['tow'], speed=.5, ax=1.02,
                                             lat=base['lat'], lon=base['lon']),
                  fixtures.navigation_frame(tow=base['tow']+.1, speed=4.57, ax=1.02,
                                             lat=base['lat']+.0005, lon=base['lon'])]
        self.ingest(*frames)
        point = self.last(frames)
        self.assertIsNone(point['speed'])
        self.assertEqual(point['ground_speed']['reason'], 'navigation_drift')
        self.assertIn('navigation_position_drift', [item['code'] for item in point['quality']['reasons']])

    def test_restart_sparse_backfill_is_idempotent_and_preserves_raw(self):
        frames = self.frames(n=51)
        self.ingest(*frames[:25])
        self.ing = Ingestor(self.store, self.raw)
        self.ingest(*frames[25:])
        with self.store.connect() as connection:
            raw = [tuple(row) for row in connection.execute('SELECT * FROM points ORDER BY t')]
            estimates = [tuple(row) for row in connection.execute('SELECT * FROM point_ground_speed ORDER BY t')]
            for frame in frames[7::9]:
                connection.execute('DELETE FROM point_ground_speed WHERE t=?', (parse(frame)['t'],))
        self.assertGreater(quality.backfill(self.store), 0)
        self.assertEqual(quality.backfill(self.store), 0)
        with self.store.connect() as connection:
            self.assertEqual(raw, [tuple(row) for row in connection.execute('SELECT * FROM points ORDER BY t')])
            self.assertEqual(estimates, [tuple(row) for row in connection.execute('SELECT * FROM point_ground_speed ORDER BY t')])

    def test_late_point_does_not_change_any_other_pointwise_speed(self):
        frames = self.frames(n=31)
        self.ingest(*frames[:12], *frames[13:])
        with self.store.connect() as connection:
            before = {row['t']: row['estimate'] for row in connection.execute('SELECT * FROM point_ground_speed')}
        self.ingest(frames[12])
        with self.store.connect() as connection:
            after = {row['t']: row['estimate'] for row in connection.execute('SELECT * FROM point_ground_speed')}
        self.assertEqual(before, {t: value for t, value in after.items() if t in before})

    def test_valid_speed_does_not_require_navigation_mode_or_heading(self):
        frames = self.frames(status='00', speed=.6, ve=0, vn=0)
        self.ingest(*frames)
        point = self.last(frames)
        self.assertEqual(point['speed'], .6)
        self.assertEqual(point['motion_state'], 'moving')


if __name__ == '__main__':
    unittest.main()
