"""Versioned, reversible measurement quarantine. Never mutates original points.

v8 stores a causal, precision-gated ground-speed estimate separately from
immutable navigation/IMU evidence. Low specific-force deviation alone never
proves rest; invalid/unresolved navigation never becomes a fabricated zero.
"""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
from statistics import median
import time

from .protocol import NUMERIC
from .rules import distance
from . import ground_speed

VERSION = 9
# The 0.01 g signal remains available for IMU diagnostics. Operational motion
# state now comes from the versioned ground_speed estimate, not this signal.
MOTION_IMPACT_THRESHOLD_G = 0.01
MOTION_STRATEGY = 'quality_gated_ground_speed'
# Frozen manifests from earlier releases are retained for audit only.  They
# must never become the active filter after the v8 migration.
COMPATIBLE_PROFILE_VERSIONS = (1, 2, 3, 4, 8, VERSION)
MAX_VALID_VEHICLE_SPEED_KMH = 130.0
MAX_VALID_VEHICLE_SPEED_MS = MAX_VALID_VEHICLE_SPEED_KMH / 3.6
# A ground vehicle can have a small vertical velocity, but a satellite-only
# solution reporting >18 km/h vertically is a navigation failure, not trolley
# motion.  The velocity sigma gate catches the same failure before it reaches
# the maximum-speed aggregation.
MAX_VALID_VERTICAL_SPEED_KMH = 18.0
MAX_VALID_VERTICAL_SPEED_MS = MAX_VALID_VERTICAL_SPEED_KMH / 3.6
MAX_VALID_VELOCITY_STD_MS = 5.0
BACKFILL_BATCH_SIZE = 20_000
AUTO_ENTRY_POLICY = {
    'strategy': MOTION_STRATEGY,
    'threshold_g': MOTION_IMPACT_THRESHOLD_G,
    'stationary': '2 s qualified low velocity + quiet IMU',
    'moving': '0.5 s significant qualified horizontal velocity',
}
# Stored as a numeric sentinel to keep the existing SQLite schema/index.  API
# callers see ``end: null`` and ``active: true`` instead of a year-9999 date.
OPEN_END = 253402300799.0
AUTO_EXIT_POLICY = dict(AUTO_ENTRY_POLICY)
BITS = {key: 1 << i for i, key in enumerate(NUMERIC)}
ALL_FIELDS = sum(BITS.values())


def policy_description():
    """Return the human-readable policy shared by API and rollup responses."""
    return (f'v{VERSION}：可信地速取 √(Ve²+Vn²)，V_2D 仅做同源一致性检查；'
            '卫导/组合导航须通过速度标准差与连续性门控。持续 2 秒速度≤0.15 m/s、'
            '水平速度标准差≤0.25 m/s、比力偏差≤0.01 g 且陀螺安静才判静止并归零；'
            '初始化、纯惯导或异常解算留空；质量合格的低信噪比连续窗可显示独立参考地速，'
            '参考值不判运动、不计最高速度/里程/告警，误差包络不是经标定的置信区间。'
            '不从坐标漂移计算速度；原始字段和 IMU 留档。')


REASONS = {
    # These are retained as status evidence, not field-quarantine reasons.
    # Initialization, heading readiness and low-speed course must remain
    # visible in the measurement view even when they are not business-ready.
    'invalid_navigation': ('导航初始化 / 定位无效（保留显示）', 'status'),
    'heading_unavailable': ('定向未就绪（保留显示）', 'status'),
    'course_unavailable': ('静止或低速航迹角（保留显示）', 'status'),
    # v8 retains stable reason names for the two field-level navigation
    # quarantines; raw measurements remain untouched.
    'navigation_position_drift': ('显著位置漂移', 'anomaly'),
    'navigation_velocity_outlier': ('导航速度解算失真（速度 / 垂向速度 / 不确定度）', 'anomaly'),
    # Kept as stable reason names so older clients can still parse an audit
    # record; v8 no longer emits these legacy stationary-fit reasons.
    'stationary_position': ('历史规则：静止定位偏差', 'legacy'),
    'position_uncertainty': ('历史规则：水平定位不确定度超限', 'legacy'),
    'stationary_altitude': ('历史规则：静止高程离群', 'legacy'),
    'altitude_uncertainty': ('历史规则：高程不确定度超限', 'legacy'),
    'stationary_velocity': ('历史规则：静止水平速度异常', 'legacy'),
    'stationary_vertical': ('历史规则：静止垂向速度异常', 'legacy'),
    'stationary_gyro': ('历史规则：静止角速度离群', 'legacy'),
    'stationary_accel': ('历史规则：静止比力离群', 'legacy'),
    'stationary_attitude': ('历史规则：静止姿态离群', 'legacy'),
    'speed_unavailable': ('可信地速证据不足 / 预热 / 解算不可靠', 'unavailable'),
}
REASON_BITS = {key: 1 << i for i, key in enumerate(REASONS)}
ANOMALY_BITS = sum(REASON_BITS[k] for k, (_, kind) in REASONS.items() if kind == 'anomaly')
UNAVAILABLE_BITS = sum(REASON_BITS[k] for k, (_, kind) in REASONS.items() if kind == 'unavailable')
STATUS_BITS = sum(REASON_BITS[k] for k, (_, kind) in REASONS.items() if kind == 'status')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS quality_contexts (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
 kind TEXT NOT NULL, profile TEXT NOT NULL, provenance TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS quality_context_range ON quality_contexts(device_id,start,end);
CREATE TABLE IF NOT EXISTS point_quality (
 device_id TEXT NOT NULL, t REAL NOT NULL, protocol TEXT NOT NULL,
 version INTEGER NOT NULL, context_id TEXT, mask INTEGER NOT NULL, reasons INTEGER NOT NULL,
 motion_state TEXT NOT NULL DEFAULT 'unknown',
 PRIMARY KEY(device_id,t,protocol)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS point_ground_speed (
 device_id TEXT NOT NULL, t REAL NOT NULL, protocol TEXT NOT NULL,
 version INTEGER NOT NULL, estimate TEXT NOT NULL,
 PRIMARY KEY(device_id,t,protocol)) WITHOUT ROWID;
'''
JOIN = '''SELECT p.*,q.version AS q_version,q.context_id AS q_context,
 q.mask AS q_mask,q.reasons AS q_reasons,q.motion_state AS q_motion_state,
 g.estimate AS q_ground FROM points p LEFT JOIN point_quality q
 ON q.device_id=p.device_id AND q.t=p.t AND q.protocol=p.protocol
 LEFT JOIN point_ground_speed g ON g.device_id=p.device_id AND g.t=p.t AND g.protocol=p.protocol AND g.version=q.version'''


def ensure_schema(c):
    """Apply additive quality migrations to databases created by v1-v4."""
    columns = {row['name'] for row in c.execute('PRAGMA table_info(point_quality)')}
    if columns and 'motion_state' not in columns:
        c.execute("ALTER TABLE point_quality ADD COLUMN motion_state TEXT NOT NULL DEFAULT 'unknown'")


def contexts(c, sn=None, include_legacy=False):
    rows = c.execute('SELECT * FROM quality_contexts' + (' WHERE device_id=?' if sn else '') + ' ORDER BY start', (sn,) if sn else ())
    closures = {}
    for row in c.execute("SELECT target,actor,action,detail FROM audit WHERE action IN ('quality.stationary_context.close','quality.stationary_context.auto_close') ORDER BY id"):
        try:
            detail = json.loads(row['detail'])
        except (TypeError, ValueError):
            detail = {'provenance': str(row['detail'] or '')}
        closures[row['target']] = dict(actor=row['actor'], action=row['action'], **detail)
    origins = {}
    for row in c.execute("""SELECT actor,action,detail FROM audit WHERE action IN (
            'quality.stationary_context','quality.stationary_context.historical_replay',
            'quality.stationary_context.auto_open') ORDER BY id"""):
        try:
            detail = json.loads(row['detail'])
        except (TypeError, ValueError):
            continue
        if detail.get('id'):
            origins[detail['id']] = dict(actor=row['actor'], action=row['action'])
    result = []
    for row in rows:
        item = dict(row, profile=json.loads(row['profile']))
        # v1-v4 stationary facts remain queryable by an explicit audit, but
        # cannot affect the v8 operational projection.  This prevents the old
        # open context from turning all later running coordinates into static
        # residuals after the threshold migration.
        current = (item['profile'].get('strategy') == MOTION_STRATEGY and
                   item['profile'].get('version') == VERSION)
        if not include_legacy and not current:
            continue
        item['active'] = item['kind'] == 'confirmed_stationary_active' and item['end'] == OPEN_END
        if item['active']:
            item['end'] = None
            item['auto_exit_policy'] = automatic_exit_policy(item)
        elif row['id'] in closures:
            item['closure'] = closures[row['id']]
        if row['id'] in origins:
            item['origin'] = origins[row['id']]
        result.append(item)
    return result


def context_end(scope):
    return OPEN_END if scope.get('active') or scope.get('end') is None else scope['end']


def context_for(p, scopes):
    return next((s for s in scopes
                 if s.get('profile', {}).get('strategy') == MOTION_STRATEGY and
                 s.get('profile', {}).get('version') == VERSION and
                 s['device_id'] == p['device_id'] and s['start'] <= p['t'] <= context_end(s)), None)


def automatic_exit_policy(context):
    """Return the v8 threshold policy for API compatibility."""
    return dict(AUTO_EXIT_POLICY)


def automatic_entry_policy():
    return dict(AUTO_ENTRY_POLICY)


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(position)
    weight = position - lower
    return values[lower] * (1 - weight) + values[min(lower + 1, len(values) - 1)] * weight


def tri_axis_resultant(p):
    """Return the gravity-included resultant of the three accelerometers."""
    values = [p.get(key) for key in ('ax', 'ay', 'az')]
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        return None
    return math.sqrt(sum(value * value for value in values))


def tri_axis_peak_deviation(p):
    """Return the absolute resultant deviation from the 1 g gravity baseline."""
    resultant = tri_axis_resultant(p)
    return abs(resultant - 1.0) if resultant is not None else None


def motion_state(p):
    """Classify one sample using only the tri-axis peak-deviation threshold."""
    deviation = tri_axis_peak_deviation(p)
    if deviation is None:
        return 'unknown'
    return 'moving' if deviation > MOTION_IMPACT_THRESHOLD_G else 'stationary'


def navigation_position_drift(previous, current):
    """Return conservative evidence for a single severe coordinate jump.

    The filter is intentionally independent of the motion state: ordinary
    running coordinates stay drawable, while a jump that cannot be covered by
    the reported speed plus a generous 15 m/s allowance is isolated.
    """
    if not previous or not current:
        return None
    required = ('lat', 'lon')
    if any(not isinstance(point.get(key), (int, float)) or not math.isfinite(point[key])
           for point in (previous, current) for key in required):
        return None
    delta = current.get('t') - previous.get('t')
    if not isinstance(delta, (int, float)) or not 0 < delta <= 3:
        return None
    jump = distance(current, previous)
    speed = max(previous.get('speed') or 0, current.get('speed') or 0)
    tolerance = max(20.0, 5 * (current.get('lat_std') or 0), 5 * (current.get('lon_std') or 0))
    limit = (speed + 15.0) * delta + tolerance
    if jump <= limit:
        return None
    return dict(distance_m=jump, delta_s=delta, limit_m=limit,
                previous_speed_ms=previous.get('speed'), current_speed_ms=current.get('speed'),
                tolerance_m=tolerance)


class AutomaticStationaryEntryDetector:
    """Deprecated v4 lifecycle hook; v8 never opens inferred contexts."""
    def __init__(self):
        self.windows = {}

    def forget(self, device_id):
        self.windows.pop(device_id, None)

    def observe(self, p, context=None):
        # Kept as a no-op compatibility shim for old audit callers.  All live
        # and historical decisions use motion_state() directly.
        return None
        # Legacy implementation retained below only for forensic source
        # comparison; it is unreachable in v8.
        device_id = p['device_id']
        if context or p['t'] > time.time():
            self.forget(device_id)
            return None
        required = ('speed','ve','vn','gx','gy','gz','ax','ay','az')
        if any(not isinstance(p.get(key), (int, float)) or not math.isfinite(p[key]) for key in required):
            self.forget(device_id)
            return None
        window = self.windows.setdefault(device_id, collections.deque())
        policy = automatic_entry_policy()
        if window and p['t'] <= window[-1]['t']:
            self.forget(device_id)
            return None
        if window and p['t'] - window[-1]['t'] > policy['max_gap_s']:
            window.clear()
        if window and p['t'] - window[-1]['t'] < policy['sample_interval_s'] - 1e-6:
            return None
        sample = {key:p.get(key) for key in (
            'device_id','t','lat','lon','speed','ve','vn','gx','gy','gz','ax','ay','az',
            'lat_std','lon_std','valid_pos','nav_mode','fix_mode',
        )}
        window.append(sample)
        while window and p['t'] - window[0]['t'] > policy['min_duration_s'] + policy['sample_interval_s']:
            window.popleft()
        duration = window[-1]['t'] - window[0]['t']
        if duration < policy['min_duration_s'] or len(window) < policy['min_samples']:
            return None
        valid = [x for x in window if x.get('valid_pos') and x.get('nav_mode') != 0 and
                 isinstance(x.get('lat'), (int, float)) and isinstance(x.get('lon'), (int, float)) and
                 x['lat'] and x['lon']]
        if len(valid) < policy['min_valid_position_samples']:
            return None
        speeds = [x['speed'] for x in window]
        vector_speeds = [math.hypot(x['ve'], x['vn']) for x in window]
        gyro_norms = [math.sqrt(x['gx']**2 + x['gy']**2 + x['gz']**2) for x in window]
        accel_norms = [math.sqrt(x['ax']**2 + x['ay']**2 + x['az']**2) for x in window]
        accel_center = median(accel_norms)
        position_stds = [max(x.get('lat_std') or 0, x.get('lon_std') or 0) for x in valid]
        anchor = {'lat':median([x['lat'] for x in valid]), 'lon':median([x['lon'] for x in valid])}
        radii = [distance(x, anchor) for x in valid]
        path_distance = sum(distance(left, right) for left, right in zip(valid, valid[1:]))
        displacement = distance(valid[0], valid[-1])
        path_efficiency = displacement / path_distance if path_distance else 0.0
        evidence = dict(
            candidate_start=window[0]['t'], detected_at=window[-1]['t'],
            duration_s=round(duration, 3), samples=len(window), valid_position_samples=len(valid),
            median_speed_ms=round(median(speeds), 4),
            median_vector_speed_ms=round(median(vector_speeds), 4),
            p95_gyro_dps=round(percentile(gyro_norms, .95), 4),
            p95_accel_deviation_g=round(percentile([abs(v-accel_center) for v in accel_norms], .95), 6),
            median_position_std_m=round(median(position_stds), 3),
            displacement_m=round(displacement, 3), path_distance_m=round(path_distance, 3),
            path_efficiency=round(path_efficiency, 4), p90_radius_m=round(percentile(radii, .9), 3),
            anchor=anchor, policy=policy,
        )
        qualified = (
            evidence['median_speed_ms'] <= policy['max_median_speed_ms'] and
            evidence['median_vector_speed_ms'] <= policy['max_median_speed_ms'] and
            evidence['p95_gyro_dps'] <= policy['max_p95_gyro_dps'] and
            evidence['p95_accel_deviation_g'] <= policy['max_p95_accel_deviation_g'] and
            evidence['median_position_std_m'] <= policy['max_median_position_std_m'] and
            evidence['displacement_m'] <= policy['max_displacement_m'] and
            evidence['p90_radius_m'] <= policy['max_p90_radius_m'] and
            evidence['path_efficiency'] <= policy['max_path_efficiency']
        )
        if not qualified:
            return None
        self.forget(device_id)
        return evidence


class ActiveStationaryExitDetector:
    """Deprecated v4 lifecycle hook; v8 has no inferred exit lifecycle."""
    def __init__(self):
        self.candidates = {}

    def forget(self, context_id):
        for key in list(self.candidates):
            if key[0] == context_id:
                self.candidates.pop(key, None)

    def observe(self, p, context):
        return None
        # Legacy implementation retained below only for forensic source
        # comparison; it is unreachable in v8.
        if not context or context.get('kind') != 'confirmed_stationary_active' or not context.get('active'):
            return None
        policy = automatic_exit_policy(context)
        routes = [
            ('combined_low_speed', {key:value for key,value in policy.items() if key != 'vehicle_motion'}),
            ('satellite_vehicle_motion', policy['vehicle_motion']),
        ]
        for route, route_policy in routes:
            evidence = self._observe_route(p, context, route, route_policy)
            if evidence:
                self.forget(context['id'])
                return evidence
        return None

    def _observe_route(self, p, context, route, policy):
        std = max(p.get('lat_std') if p.get('lat_std') is not None else math.inf,
                  p.get('lon_std') if p.get('lon_std') is not None else math.inf)
        vector_speed = None
        if p.get('ve') is not None and p.get('vn') is not None:
            vector_speed = math.hypot(p['ve'], p['vn'])
        velocity_error = abs(vector_speed - p['speed']) if vector_speed is not None and p.get('speed') is not None else math.inf
        velocity_consistent = (
            'max_velocity_error_ms' not in policy or
            velocity_error <= max(policy['max_velocity_error_ms'], .25 * p['speed'])
        )
        qualified = (
            p.get('valid_pos') and p.get('nav_mode') in policy['nav_modes'] and
            p.get('fix_mode') in policy['fix_modes'] and p.get('speed') is not None and
            p['speed'] >= policy['min_speed_ms'] and std <= policy['max_position_std_m'] and
            velocity_consistent and
            p['t'] <= time.time() and
            distance(p, context['profile']['anchor']) >= policy['min_anchor_distance_m']
        )
        key = (context['id'], route)
        if not qualified:
            self.candidates.pop(key, None)
            return None
        candidate = self.candidates.get(key)
        if (candidate is None or p['t'] <= candidate['last_t'] or
                p['t'] - candidate['last_t'] > policy['max_gap_s']):
            point = {'lat':p['lat'],'lon':p['lon']}
            candidate = dict(start_t=p['t'], last_t=p['t'], start_point=point, last_point=point,
                             path_distance=0.0, speed_distance=0.0, count=1,
                             min_speed=p['speed'], last_speed=p['speed'], max_speed=p['speed'],
                             max_position_std=std)
            self.candidates[key] = candidate
            return None
        point = {'lat':p['lat'],'lon':p['lon']}
        delta_t = p['t'] - candidate['last_t']
        candidate['path_distance'] += distance(candidate['last_point'], point)
        candidate['speed_distance'] += (candidate['last_speed'] + p['speed']) / 2 * delta_t
        candidate['last_point'] = point
        candidate['last_t'] = p['t']
        candidate['last_speed'] = p['speed']
        candidate['count'] += 1
        candidate['min_speed'] = min(candidate['min_speed'], p['speed'])
        candidate['max_speed'] = max(candidate['max_speed'], p['speed'])
        candidate['max_position_std'] = max(candidate['max_position_std'], std)
        duration = p['t'] - candidate['start_t']
        displacement = distance(candidate['start_point'], p)
        path_efficiency = displacement / candidate['path_distance'] if candidate['path_distance'] else 0
        distance_ratio = displacement / candidate['speed_distance'] if candidate['speed_distance'] else 0
        if (duration < policy['min_duration_s'] or candidate['count'] < policy['min_samples'] or
                displacement < policy['min_displacement_m'] or
                path_efficiency < policy['min_path_efficiency'] or
                distance_ratio < policy.get('min_distance_ratio', 0) or
                distance_ratio > policy.get('max_distance_ratio', math.inf)):
            return None
        anchor_distance = distance(p, context['profile']['anchor'])
        evidence = dict(
            route=route, detected_at=p['t'], candidate_start=candidate['start_t'], duration_s=round(duration,3),
            samples=candidate['count'], displacement_m=round(displacement,3),
            path_distance_m=round(candidate['path_distance'],3),
            path_efficiency=round(path_efficiency,3),
            speed_distance_m=round(candidate['speed_distance'],3),
            distance_ratio=round(distance_ratio,3),
            anchor_distance_m=round(anchor_distance,3), min_speed_ms=round(candidate['min_speed'],3),
            max_speed_ms=round(candidate['max_speed'],3), position_std_m=round(std,3),
            max_position_std_m=round(candidate['max_position_std'],3),
            velocity_error_ms=round(velocity_error,3) if math.isfinite(velocity_error) else None,
            nav_mode=p['nav_mode'], fix_mode=p['fix_mode'], policy=policy,
        )
        return evidence


def angle_delta(value, reference):
    return (value-reference+180) % 360-180


def assess(p, context=None, explain=False, previous=None):
    mask = reasons = 0
    details = []
    def reject(code, fields, reference=None, limit=None):
        nonlocal mask, reasons
        fields = [k for k in fields if p.get(k) is not None]
        if not fields:
            return
        mask |= sum(BITS[k] for k in fields)
        reasons |= REASON_BITS[code]
        if explain:
            details.append(dict(code=code, label=REASONS[code][0], category=REASONS[code][1],
                                fields=fields, values={k:p[k] for k in fields}, reference=reference, limit=limit))
    def note(code, fields):
        """Record a readiness status without masking any measurement field."""
        nonlocal reasons
        fields = [k for k in fields if p.get(k) is not None]
        if not fields:
            return
        reasons |= REASON_BITS[code]
        if explain:
            details.append(dict(code=code, label=REASONS[code][0], category=REASONS[code][1],
                                fields=fields, values={k:p[k] for k in fields}, reference=None, limit=None))

    # Readiness is an explicit status, not a data-removal rule.  Keep every
    # supplied value so initialization and undirected/low-speed samples remain
    # available for diagnostics and later curve recomputation.
    if not p.get('valid_pos') or p.get('nav_mode') == 0:
        note('invalid_navigation', ['lat','lon','alt','speed','ve','vn','vu','course','course_std',
                                     'lat_std','lon_std','alt_std','ve_std','vn_std','vu_std'])
    if p.get('nav_mode') != 2 or p.get('fix_mode') not in (1,2,3,4,5):
        note('heading_unavailable', ['heading','heading_std'])
    if context or p.get('speed') is None or p['speed'] < 1 or not p.get('valid_pos'):
        note('course_unavailable', ['course','course_std'])

    horizontal_speed = None
    if p.get('ve') is not None and p.get('vn') is not None:
        horizontal_speed = math.hypot(p['ve'], p['vn'])
    velocity_stds = [abs(p[key]) for key in ('ve_std', 'vn_std', 'vu_std')
                     if isinstance(p.get(key), (int, float)) and math.isfinite(p[key])]
    velocity_std = max(velocity_stds) if velocity_stds else None
    vertical_outlier = p.get('vu') is not None and abs(p['vu']) > MAX_VALID_VERTICAL_SPEED_MS
    uncertainty_outlier = velocity_std is not None and velocity_std > MAX_VALID_VELOCITY_STD_MS
    drift = navigation_position_drift(previous, p)
    velocity_outlier = (
        (p.get('speed') is not None and p['speed'] > MAX_VALID_VEHICLE_SPEED_MS) or
        (horizontal_speed is not None and horizontal_speed > MAX_VALID_VEHICLE_SPEED_MS) or
        (p.get('vu') is not None and abs(p['vu']) > MAX_VALID_VEHICLE_SPEED_MS) or
        vertical_outlier or uncertainty_outlier
    )
    if velocity_outlier:
        # A navigation solution can be badly wrong long before it reaches the
        # broad 130 km/h vehicle cap.  Quarantine scalar/vector velocity and
        # its coordinate for the effective view, but retain IMU, uncertainty
        # evidence and the immutable raw point.
        reject('navigation_velocity_outlier',
               ['lat','lon','alt','speed','ve','vn','vu','course','course_std'],
               dict(speed_ms=p.get('speed'), horizontal_speed_ms=horizontal_speed,
                    vertical_speed_ms=p.get('vu'), velocity_std_ms=velocity_std),
               dict(max_speed_ms=MAX_VALID_VEHICLE_SPEED_MS,
                    max_vertical_speed_ms=MAX_VALID_VERTICAL_SPEED_MS,
                    max_velocity_std_ms=MAX_VALID_VELOCITY_STD_MS))

    # Do not replace or hide coordinates merely because a sample is static,
    # initialized, undirected, or low speed.  Only an impossible single-step
    # jump or an internally inconsistent navigation solution is quarantined;
    # this keeps normal running and accurate static GNSS positions available
    # to the map and every parameter curve.
    if drift:
        reject('navigation_position_drift', ['lat', 'lon'], drift, drift['limit_m'])
    return mask, reasons, details


def seed_estimator(c, p):
    estimator = ground_speed.Estimator()
    for row in c.execute('SELECT * FROM points WHERE device_id=? AND t>=? AND t<? ORDER BY t,protocol',
                         (p['device_id'], p['t']-ground_speed.WINDOW_S, p['t'])):
        estimator.observe(dict(row))
    return estimator


def ground_speed_details(row):
    p = dict(row)
    encoded = p.get('q_ground')
    if not encoded:
        return []
    estimate = json.loads(encoded) if isinstance(encoded, str) else encoded
    if estimate.get('value') is not None:
        return []
    labels = {
        'missing_velocity_precision':'缺少完整速度标准差',
        'navigation_unavailable':'导航未就绪或纯惯导不可确认',
        'velocity_uncertain':'水平速度标准差超过 0.5 m/s',
        'velocity_fields_disagree':'V_2D 与东/北向速度不一致',
        'velocity_solution_outlier':'水平或垂向速度解算异常',
        'motion_unresolved':'速度不足以区分低速运动与噪声，且未满足持续静止条件',
        'warming_up':'尚未满足连续 0.5 秒可信运动证据',
        'velocity_jump':'短窗水平速度向量跳变',
    }
    return [dict(code='speed_unavailable', label=labels.get(estimate['reason'], '地速证据不足'),
                 category='unavailable', fields=['speed'], values={'speed':p.get('speed')},
                 reference=estimate, limit=None)]


def write_assessment(c, p, scopes, previous=None, estimator=None):
    context = context_for(p, scopes)
    mask, reasons, _ = assess(p, context, previous=previous)
    estimate = (estimator or seed_estimator(c, p)).observe(p)
    # Field quarantine and the derived projection must never disagree: a
    # stricter navigation-level gate cannot be resurrected by ground speed.
    if mask & BITS['speed']:
        estimate.update(value=None, state='unknown', reason='velocity_solution_outlier')
        estimate.pop('reference', None)
    if estimate['value'] is None:
        mask |= BITS['speed']
        reasons |= REASON_BITS['speed_unavailable']
    state = estimate['state']
    c.execute('INSERT OR REPLACE INTO point_quality VALUES (?,?,?,?,?,?,?,?)',
              (p['device_id'],p['t'],p['protocol'],VERSION,context['id'] if context else None,mask,reasons,state))
    encoded = json.dumps(estimate, separators=(',', ':'), allow_nan=False)
    c.execute('INSERT OR REPLACE INTO point_ground_speed VALUES (?,?,?,?,?)',
              (p['device_id'],p['t'],p['protocol'],VERSION,encoded))
    return mask, reasons, context['id'] if context else None, encoded


def project(row, info=False):
    """The only operational projection. Missing assessments fail closed."""
    p = dict(row)
    pending = p.get('q_version') != VERSION
    mask = ALL_FIELDS if pending else p['q_mask']
    reasons = p.get('q_reasons') or 0
    fields = [k for k, bit in BITS.items() if mask & bit]
    for key in fields:
        p[key] = None
    p['valid_pos'] = int(bool(p['valid_pos'] and p['lat'] is not None and p['lon'] is not None))
    p['stationary_context'] = p.get('q_context') if not pending else None
    estimate = p.get('q_ground')
    estimate = json.loads(estimate) if isinstance(estimate, str) else estimate
    if pending or not estimate:
        estimate = dict(value=None, state='unknown', reason='assessment_pending', sigma_ms=None,
                        source='unavailable', window_s=0)
    p['ground_speed'] = estimate
    p['motion_state'] = estimate['state']
    # Vehicle speed follows the motion decision, not GNSS velocity residuals
    # or displacement of retained static positions.  Normalize before any
    # consumer (curves, rollups, replay, exports, or event rules) sees it.
    # The input row and raw evidence remain untouched; unknown/pending motion
    # must never be turned into a fabricated zero.
    p['speed'] = estimate['value']
    reference = (estimate.get('reference') or {}) if ground_speed.valid_reference(estimate) else {}
    p['speed_reference'] = reference.get('value')
    p['speed_reference_low'] = reference.get('lower_ms')
    p['speed_reference_high'] = reference.get('upper_ms')
    if info:
        p['quality'] = dict(version=VERSION, pending=pending, excluded_fields=fields,
                            reasons=[dict(code=k,label=label,category=kind) for k,(label,kind) in REASONS.items() if reasons & REASON_BITS[k]],
                            context_id=p['stationary_context'], motion_state=p['motion_state'],
                            motion_threshold_g=MOTION_IMPACT_THRESHOLD_G)
    for key in ('q_version','q_context','q_mask','q_reasons','q_motion_state','q_ground'):
        p.pop(key, None)
    return p


def build_context(c, sn, start, end, provenance):
    """Fit only accepted-status observations; cap reference sample memory."""
    count = c.execute('SELECT COUNT(*) FROM points WHERE device_id=? AND t BETWEEN ? AND ?', (sn,start,end)).fetchone()[0]
    if count < 100:
        raise ValueError('静止参考至少需要 100 条采样')
    fields = ['lat','lon','alt','speed','ve','vn','vu','gx','gy','gz','ax','ay','az','pitch','roll']
    values = {key:[] for key in fields}
    stride = max(1, math.ceil(count/50000))
    population = 0
    for i, r in enumerate(c.execute('SELECT '+','.join(fields)+' FROM points WHERE device_id=? AND t BETWEEN ? AND ? AND valid_pos=1 AND nav_mode!=0 ORDER BY t,protocol', (sn,start,end))):
        population += 1
        if i % stride == 0:
            for key in fields:
                values[key].append(r[key])
    if population < 100:
        raise ValueError('静止参考有效导航采样不足')
    centers = {key:median(v) for key,v in values.items()}
    # Circular attitude residuals avoid false outliers at +/-180 degrees.
    for key in ('pitch','roll'):
        reference = values[key][0]
        centers[key] = (reference+median(angle_delta(v,reference) for v in values[key])+180)%360-180
    mad = {key:median(abs(angle_delta(v,centers[key]) if key in ('pitch','roll') else v-centers[key]) for v in values[key]) for key in fields}
    floors = {'alt':10,'speed':.3,'ve':.3,'vn':.3,'vu':.5,'gx':.15,'gy':.15,'gz':.15,
              'ax':.02,'ay':.02,'az':.02,'pitch':1,'roll':1}
    limits = {key:max(floor,6*1.4826*mad[key]) for key,floor in floors.items()}
    anchor = {key:centers[key] for key in ('lat','lon')}
    radii = [distance(dict(lat=a,lon=b),anchor) for a,b in zip(values['lat'],values['lon'])]
    radial_median = median(radii)
    radial_mad = median(abs(v-radial_median) for v in radii)
    profile = dict(anchor=anchor,centers=centers,mad=mad,limits=limits,
                   position_limit_m=max(10,radial_median+6*1.4826*radial_mad),
                   horizontal_limit_ms=max(limits[k] for k in ('speed','ve','vn')),
                   radial_median_m=radial_median,radial_mad_m=radial_mad,
                   population=count,valid_population=population,training_samples=len(radii),stride=stride,
                   method='v8 tri-axis peak-deviation state plus navigation consistency quarantine; legacy fitted profile retained for audit only',
                   strategy=MOTION_STRATEGY, motion_threshold_g=MOTION_IMPACT_THRESHOLD_G, version=VERSION)
    scope = dict(device_id=sn,start=start,end=end,kind='confirmed_stationary',profile=profile,provenance=provenance)
    scope['id'] = hashlib.sha256(json.dumps(scope,sort_keys=True).encode()).hexdigest()[:20]
    return scope


def make_active(scope, training_start=None, training_end=None):
    """Turn a fitted bounded profile into an explicit until-revoked fact."""
    active = json.loads(json.dumps(scope))
    active['kind'] = 'confirmed_stationary_active'
    active['active'] = True
    active['start'] = training_start if training_start is not None else active['start']
    active['end'] = None
    profile = active['profile']
    profile['training_start'] = training_start if training_start is not None else scope['start']
    profile['training_end'] = training_end if training_end is not None else scope['end']
    profile['version'] = VERSION
    # Confirmed-static transport display thresholds.  Robust fitting still
    # supplies the center; caps prevent a long GNSS drift from training itself
    # into an acceptable vehicle trajectory.
    profile['position_limit_m'] = min(profile['position_limit_m'], 15.0)
    profile['limits']['alt'] = min(profile['limits']['alt'], 15.0)
    profile['position_std_limit_m'] = 5.0
    profile['altitude_std_limit_m'] = 8.0
    profile['auto_exit'] = automatic_exit_policy({'profile':profile})
    profile['method'] = 'median + 6 × 1.4826 × MAD, engineering floors and confirmed-static quality caps'
    active.pop('id', None)
    active['id'] = hashlib.sha256(json.dumps({k:v for k,v in active.items() if k != 'active'},sort_keys=True).encode()).hexdigest()[:20]
    return active


def install_context(c, scope, actor='user-confirmed/deployment', action='quality.stationary_context'):
    active = scope['kind'] == 'confirmed_stationary_active' and scope.get('end') is None
    bounded = scope['kind'] == 'confirmed_stationary' and scope.get('end') is not None
    profile_version = scope.get('profile', {}).get('version')
    training_end = scope.get('profile', {}).get('training_end', scope.get('end'))
    if (not (bounded or active) or profile_version not in COMPATIBLE_PROFILE_VERSIONS or
            not scope['start'] < context_end(scope) or training_end is None or training_end > time.time()):
        raise ValueError('静止范围或算法版本无效')
    existing = c.execute('SELECT * FROM quality_contexts WHERE id=?', (scope['id'],)).fetchone()
    if existing:
        # A once-active fact may have been explicitly closed.  Subsequent code
        # deployments must not silently reopen it from the packaged manifest.
        if (scope['kind'] == 'confirmed_stationary_active' and
                existing['kind'] == 'confirmed_stationary' and existing['end'] < OPEN_END and
                json.loads(existing['profile']) == scope['profile'] and
                all(existing[k] == scope[k] for k in ('device_id','start','provenance'))):
            return False
        stored_end = context_end(scope)
        if json.loads(existing['profile']) != scope['profile'] or any(existing[k] != scope[k] for k in ('device_id','start','kind','provenance')) or existing['end'] != stored_end:
            raise ValueError('不可覆盖已冻结的过滤上下文')
        return False
    stored_end = context_end(scope)
    if c.execute('SELECT 1 FROM quality_contexts WHERE device_id=? AND start<=? AND end>=?',(scope['device_id'],stored_end,scope['start'])).fetchone():
        raise ValueError('已确认区间不可重叠')
    c.execute('INSERT INTO quality_contexts VALUES (?,?,?,?,?,?,?,?)',
              (scope['id'],scope['device_id'],scope['start'],stored_end,scope['kind'],json.dumps(scope['profile']),scope['provenance'],time.time()))
    c.execute('INSERT INTO audit(t,actor,action,target,detail) VALUES (?,?,?,?,?)',
              (time.time(),actor,action,scope['device_id'],json.dumps(scope,ensure_ascii=False)))
    # Old decisions for this exact scope must not remain available while backfill runs.
    c.execute('DELETE FROM point_quality WHERE device_id=? AND t BETWEEN ? AND ?',
              (scope['device_id'],scope['start'],stored_end))
    return True


def auto_open_stationary_context(c, p, evidence):
    """Create an auditable active context after multi-signal confirmation."""
    start, detected_at = evidence['candidate_start'], evidence['detected_at']
    fitted = build_context(
        c, p['device_id'], start, detected_at,
        '系统自动识别：持续低速、稳定陀螺/比力与非连贯位置位移共同证明设备静止',
    )
    scope = make_active(fitted, start, detected_at)
    scope['profile']['automatic_entry'] = evidence
    scope.pop('id', None)
    scope['id'] = hashlib.sha256(json.dumps({k:v for k,v in scope.items() if k != 'active'},
                                            sort_keys=True).encode()).hexdigest()[:20]
    install_context(c, scope, 'system/automatic', 'quality.stationary_context.auto_open')
    return scope


def _close_active_context(c, context_id, end, provenance, actor, action, evidence=None):
    row = c.execute('SELECT * FROM quality_contexts WHERE id=?', (context_id,)).fetchone()
    if row is None or row['kind'] != 'confirmed_stationary_active' or row['end'] != OPEN_END:
        raise ValueError('持续静止状态不存在或已经关闭')
    if end is None or not row['start'] < end <= time.time():
        raise ValueError('关闭时间必须晚于开始且不能在未来')
    note = str(provenance).strip()
    if not note:
        raise ValueError('关闭持续静止状态需要说明原因')
    c.execute("UPDATE quality_contexts SET end=?,kind='confirmed_stationary' WHERE id=?", (end,context_id))
    # Re-evaluate already-arrived samples after the closure as ordinary data.
    c.execute('DELETE FROM point_quality WHERE device_id=? AND t>?', (row['device_id'],end))
    c.execute('INSERT INTO audit(t,actor,action,target,detail) VALUES (?,?,?,?,?)',
              (time.time(),actor,action,context_id,
               json.dumps(dict({'end':end,'provenance':note}, **(evidence or {})),ensure_ascii=False)))
    return True


def close_active_context(c, context_id, end, provenance):
    """Close an explicit live state once; retain its historical decisions."""
    return _close_active_context(c, context_id, end, provenance,
                                 'operator/explicit', 'quality.stationary_context.close')


def auto_close_active_context(c, context_id, end, evidence):
    """Close after conservative raw-sample motion confirmation and audit why."""
    return _close_active_context(
        c, context_id, end,
        '系统确认高质量持续位移离开静止锚点，自动恢复普通运动规则',
        'system/automatic', 'quality.stationary_context.auto_close', evidence,
    )


def previous_point(c, p):
    """Seek the composite primary key; an OR predicate scans the device tail."""
    row = c.execute(
        'SELECT * FROM points WHERE device_id=? AND (t,protocol)<(?,?) '
        'ORDER BY t DESC,protocol DESC LIMIT 1',
        (p['device_id'], p['t'], p['protocol'])).fetchone()
    return dict(row) if row else None


def backfill(store, progress=None):
    """Reassess rows in ordered, restartable batches.

    The previous raw sample is carried into each assessment so the only
    coordinate quarantine rule (a severe one-step jump) is reproduced exactly
    during the historical replay as it is during live ingestion.
    """
    written = 0
    cursor = None
    estimators = {}
    with store.ingestion_lock():
        while True:
            with store.connect() as c:
                scopes = contexts(c)
                after = ' AND (p.device_id,p.t,p.protocol)>(?,?,?)' if cursor else ''
                rows = c.execute(JOIN+' WHERE (q.version IS NULL OR q.version!=? OR g.estimate IS NULL)'
                                  +after+' ORDER BY p.device_id,p.t,p.protocol LIMIT ?',
                                  (VERSION, *(cursor or ()), BACKFILL_BATCH_SIZE)).fetchall()
                if not rows:
                    break
                for row in rows:
                    item = dict(row)
                    # Missing quality rows may be sparse after a repair. Seed
                    # from the actual raw predecessor, not the last repaired row.
                    previous = previous_point(c, item)
                    estimator = estimators.get(item['device_id'])
                    if estimator is None or (previous and estimator.window and previous['t'] != estimator.window[-1]['t']):
                        estimator = seed_estimator(c, item)
                        estimators[item['device_id']] = estimator
                    write_assessment(c, item, scopes, previous=previous, estimator=estimator)
            # Only advance/report after the batch transaction has committed.
            cursor = tuple(rows[-1][key] for key in ('device_id','t','protocol'))
            written += len(rows)
            if progress:
                progress(dict(stage='quality', assessed=written, cursor=cursor))
    if written:
        # Rollups are derived from the filtered projection.  Any reassessment
        # invalidates them, so rebuild from authoritative raw points once after
        # the restartable backfill completes.
        if progress:
            progress(dict(stage='rollups', assessed=written))
        store.rebuild_rollups()
    return written


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--fit-device')
    parser.add_argument('--active', action='store_true')
    parser.add_argument('--start', type=float)
    parser.add_argument('--end', type=float)
    parser.add_argument('--output')
    parser.add_argument('--context')
    parser.add_argument('--close-context')
    parser.add_argument('--close-end', type=float)
    parser.add_argument('--close-reason')
    parser.add_argument('--backfill', action='store_true')
    args = parser.parse_args()
    from .store import Store
    store = Store(args.db)
    if args.fit_device:
        with store.connect() as c:
            scope = build_context(c,args.fit_device,args.start,args.end,'用户确认：截至 2026-08-26 本次历史采集期间设备一直原地未挪动；仅覆盖所列时间，不推断未来状态')
            if args.active:
                scope = make_active(scope,args.start,args.end)
        Path(args.output).write_text(json.dumps(scope,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(scope,ensure_ascii=False))
    if args.context:
        with store.ingestion_lock(), store.connect() as c:
            print(json.dumps({'installed':install_context(c,json.loads(Path(args.context).read_text()))}))
    if args.close_context:
        with store.ingestion_lock(), store.connect() as c:
            closed = close_active_context(c,args.close_context,args.close_end,args.close_reason)
        print(json.dumps({'closed':closed,'context_id':args.close_context,'end':args.close_end}))
    if args.backfill:
        print(json.dumps({'assessed':backfill(store),'version':VERSION}))


if __name__ == '__main__':
    main()
