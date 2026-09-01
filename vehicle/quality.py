"""Versioned, reversible measurement quarantine. Never mutates original points.

Stationarity is supplied ground truth for a bounded device/time interval, not
inferred from quiet IMU data (constant-velocity motion can also have quiet IMU).
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import median
import time

from .protocol import NUMERIC
from .rules import distance

VERSION = 3
# Frozen stationary manifests from the previous release remain valid facts.
# Their profile thresholds are still usable by the v3 assessor; only the
# decision version changes when the field-quarantine logic changes.
COMPATIBLE_PROFILE_VERSIONS = (1, 2, VERSION)
# Stored as a numeric sentinel to keep the existing SQLite schema/index.  API
# callers see ``end: null`` and ``active: true`` instead of a year-9999 date.
OPEN_END = 253402300799.0
AUTO_EXIT_POLICY = {
    # Automatic closure is deliberately stricter than ordinary motion/event
    # detection.  It changes a user-confirmed fact, so speed alone or one GNSS
    # jump must never be enough.
    # A commissioning trolley may only reach 0.5 m/s.  Low speed is accepted
    # only together with a long RTK displacement and a coherent path.
    'min_speed_ms': 0.15,
    'min_duration_s': 15.0,
    'min_samples': 10,
    'max_gap_s': 1.5,
    'max_position_std_m': 1.0,
    'min_anchor_distance_m': 30.0,
    'min_displacement_m': 8.0,
    'min_path_efficiency': 0.5,
    'nav_modes': [2],
    # Heading readiness is not required to prove translation.  RTK fixed/float
    # positions are accepted both with and without dual-antenna direction.
    'fix_modes': [4, 5, 8, 9],
    # A separate, substantially stronger route recovers ordinary vehicle runs
    # when the receiver remains in satellite-navigation mode. It deliberately
    # does not weaken the 0.5 m/s commissioning-trolley route above.
    'vehicle_motion': {
        'min_speed_ms': 1.0,
        'min_duration_s': 10.0,
        'min_samples': 20,
        'max_gap_s': 0.3,
        'max_position_std_m': 5.0,
        'min_anchor_distance_m': 30.0,
        'min_displacement_m': 25.0,
        'min_path_efficiency': 0.65,
        'min_distance_ratio': 0.65,
        'max_distance_ratio': 1.35,
        'max_velocity_error_ms': 0.75,
        'nav_modes': [1, 2],
        'fix_modes': [4, 5, 8, 9],
    },
}
BITS = {key: 1 << i for i, key in enumerate(NUMERIC)}
ALL_FIELDS = sum(BITS.values())
REASONS = {
    # These are retained as status evidence, not field-quarantine reasons.
    # Initialization, heading readiness and low-speed course must remain
    # visible in the measurement view even when they are not business-ready.
    'invalid_navigation': ('导航初始化 / 定位无效（保留显示）', 'status'),
    'heading_unavailable': ('定向未就绪（保留显示）', 'status'),
    'course_unavailable': ('静止或低速航迹角（保留显示）', 'status'),
    # v3 has one and only one quarantine rule: a confirmed stationary point
    # whose position has drifted beyond the fitted stationary reference.
    'stationary_position': ('静止定位偏差', 'anomaly'),
    # Kept as stable reason names so older clients can still parse an audit
    # record; v3 no longer emits these quarantine reasons.
    'position_uncertainty': ('历史规则：水平定位不确定度超限', 'legacy'),
    'stationary_altitude': ('历史规则：静止高程离群', 'legacy'),
    'altitude_uncertainty': ('历史规则：高程不确定度超限', 'legacy'),
    'stationary_velocity': ('历史规则：静止水平速度异常', 'legacy'),
    'stationary_vertical': ('历史规则：静止垂向速度异常', 'legacy'),
    'stationary_gyro': ('历史规则：静止角速度离群', 'legacy'),
    'stationary_accel': ('历史规则：静止比力离群', 'legacy'),
    'stationary_attitude': ('历史规则：静止姿态离群', 'legacy'),
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
 PRIMARY KEY(device_id,t,protocol)) WITHOUT ROWID;
'''
JOIN = '''SELECT p.*,q.version AS q_version,q.context_id AS q_context,
 q.mask AS q_mask,q.reasons AS q_reasons FROM points p LEFT JOIN point_quality q
 ON q.device_id=p.device_id AND q.t=p.t AND q.protocol=p.protocol'''


def contexts(c, sn=None):
    rows = c.execute('SELECT * FROM quality_contexts' + (' WHERE device_id=?' if sn else '') + ' ORDER BY start', (sn,) if sn else ())
    closures = {}
    for row in c.execute("SELECT target,actor,action,detail FROM audit WHERE action IN ('quality.stationary_context.close','quality.stationary_context.auto_close') ORDER BY id"):
        try:
            detail = json.loads(row['detail'])
        except (TypeError, ValueError):
            detail = {'provenance': str(row['detail'] or '')}
        closures[row['target']] = dict(actor=row['actor'], action=row['action'], **detail)
    result = []
    for row in rows:
        item = dict(row, profile=json.loads(row['profile']))
        item['active'] = item['kind'] == 'confirmed_stationary_active' and item['end'] == OPEN_END
        if item['active']:
            item['end'] = None
            item['auto_exit_policy'] = automatic_exit_policy(item)
        elif row['id'] in closures:
            item['closure'] = closures[row['id']]
        result.append(item)
    return result


def context_end(scope):
    return OPEN_END if scope.get('active') or scope.get('end') is None else scope['end']


def context_for(p, scopes):
    return next((s for s in scopes if s['device_id'] == p['device_id'] and s['start'] <= p['t'] <= context_end(s)), None)


def automatic_exit_policy(context):
    """Return the effective conservative policy for an active context."""
    policy = dict(AUTO_EXIT_POLICY)
    policy['vehicle_motion'] = dict(AUTO_EXIT_POLICY['vehicle_motion'])
    overrides = context.get('profile', {}).get('auto_exit', {})
    policy.update({key:value for key,value in overrides.items() if key != 'vehicle_motion'})
    policy['vehicle_motion'].update(overrides.get('vehicle_motion', {}))
    policy['min_anchor_distance_m'] = max(
        float(policy['min_anchor_distance_m']),
        2 * float(context['profile']['position_limit_m']),
    )
    policy['vehicle_motion']['min_anchor_distance_m'] = max(
        float(policy['vehicle_motion']['min_anchor_distance_m']),
        2 * float(context['profile']['position_limit_m']),
    )
    return policy


class ActiveStationaryExitDetector:
    """Confirm unmistakable sustained motion without trusting a lone field."""
    def __init__(self):
        self.candidates = {}

    def forget(self, context_id):
        for key in list(self.candidates):
            if key[0] == context_id:
                self.candidates.pop(key, None)

    def observe(self, p, context):
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


def assess(p, context=None, explain=False):
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

    if context and p.get('valid_pos') and p.get('nav_mode') != 0:
        # The sole v3 quarantine rule.  A confirmed stationary interval may
        # retain every inertial, speed and quality channel; only coordinates
        # that drift away from the fitted anchor are removed from the running
        # projection so they cannot form a false route.
        profile = context['profile']
        anchor = profile['anchor']
        radius = distance(p, anchor)
        horizontal_std = max(p.get('lat_std') or 0, p.get('lon_std') or 0)
        horizontal_std_limit = profile.get('position_std_limit_m')
        position_bad = radius > profile['position_limit_m']
        if horizontal_std_limit is not None and horizontal_std > horizontal_std_limit:
            position_bad = True
        if position_bad:
            reject('stationary_position', ['lat','lon'],
                   dict(anchor, distance_m=radius, horizontal_std_m=horizontal_std),
                   profile['position_limit_m'])
    return mask, reasons, details


def write_assessment(c, p, scopes):
    context = context_for(p, scopes)
    mask, reasons, _ = assess(p, context)
    c.execute('INSERT OR REPLACE INTO point_quality VALUES (?,?,?,?,?,?,?)',
              (p['device_id'],p['t'],p['protocol'],VERSION,context['id'] if context else None,mask,reasons))
    return mask, reasons, context['id'] if context else None


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
    if info:
        p['quality'] = dict(version=VERSION, pending=pending, excluded_fields=fields,
                            reasons=[dict(code=k,label=label,category=kind) for k,(label,kind) in REASONS.items() if reasons & REASON_BITS[k]],
                            context_id=p['stationary_context'])
    for key in ('q_version','q_context','q_mask','q_reasons'):
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
                   method='median + 6 × 1.4826 × MAD with engineering noise floors',version=VERSION)
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


def install_context(c, scope):
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
              (time.time(),'user-confirmed/deployment','quality.stationary_context',scope['device_id'],json.dumps(scope,ensure_ascii=False)))
    # Old decisions for this exact scope must not remain available while backfill runs.
    c.execute('DELETE FROM point_quality WHERE device_id=? AND t BETWEEN ? AND ?', (scope['device_id'],scope['start'],scope['end']))
    return True


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


def backfill(store):
    """Small atomic batches; restartable, bounded, and shared with ingestion lock."""
    written = 0
    with store.ingestion_lock():
        while True:
            with store.connect() as c:
                scopes = contexts(c)
                rows = c.execute(JOIN+' WHERE q.version IS NULL OR q.version!=? LIMIT 2000', (VERSION,)).fetchall()
                if not rows:
                    break
                for row in rows:
                    write_assessment(c, dict(row), scopes)
                written += len(rows)
    if written:
        # Rollups are derived from the filtered projection.  Any reassessment
        # invalidates them, so rebuild from authoritative raw points once after
        # the restartable backfill completes.
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
