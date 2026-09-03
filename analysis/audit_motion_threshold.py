"""Read-only v6 replay for motion-state and position quality evidence.

The script never opens a write transaction.  It is intended for a production
SQLite copy or a read-only URI and reports the threshold result, physical
velocity outliers, and severe coordinate jumps without changing the service.
"""
import argparse
import collections
import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle import quality


REFERENCE_RANGES = {
    '12:28-12:36 运行参考': ('2026-09-02T12:28:00+08:00', '2026-09-02T12:36:00+08:00'),
    '12:36-12:39 静止参考': ('2026-09-02T12:36:00+08:00', '2026-09-02T12:39:00+08:00'),
    '12:39-12:45 运行参考': ('2026-09-02T12:39:00+08:00', '2026-09-02T12:45:00+08:00'),
    '12:48-12:50 移动参考': ('2026-09-02T12:48:00+08:00', '2026-09-02T12:50:00+08:00'),
}


def epoch(value):
    return dt.datetime.fromisoformat(value).timestamp()


def quantiles(values):
    return {key: quality.percentile(values, fraction)
            for key, fraction in (('p50', .5), ('p90', .9), ('p95', .95), ('p99', .99), ('max', 1))} if values else {}


def audit(db, device='6094510', references=None):
    connection = sqlite3.connect('file:' + str(Path(db).resolve()) + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    columns = ('device_id,t,protocol,valid_pos,nav_mode,fix_mode,lat,lon,speed,ve,vn,vu,'
               'ax,ay,az,lat_std,lon_std')
    sql = f'SELECT {columns} FROM points WHERE device_id=? ORDER BY t,protocol'
    velocity = dict(total=0, first_t=None, last_t=None,
                    physical_limit_kmh=quality.MAX_VALID_VEHICLE_SPEED_KMH,
                    scalar_max_kmh=0, horizontal_max_kmh=0, vertical_max_kmh=0,
                    scalar_gt_limit=0, horizontal_gt_limit=0, vertical_gt_limit=0)
    deviations, states = [], collections.Counter()
    ranges = references or {name: (epoch(start), epoch(end)) for name, (start, end) in REFERENCE_RANGES.items()}
    reference_stats = {name: dict(start=begin, end=end, samples=0, states=collections.Counter(),
                                  deviations=[], severe_position_jumps=0)
                       for name, (begin, end) in ranges.items()}
    jumps = []
    previous = None
    for row in connection.execute(sql, (device,)):
        point = dict(row)
        velocity['total'] += 1
        velocity['first_t'] = point['t'] if velocity['first_t'] is None else velocity['first_t']
        velocity['last_t'] = point['t']
        scalar = (point.get('speed') or 0) * 3.6
        horizontal = math.hypot(point.get('ve') or 0, point.get('vn') or 0) * 3.6
        vertical = abs(point.get('vu') or 0) * 3.6
        velocity['scalar_max_kmh'] = max(velocity['scalar_max_kmh'], scalar)
        velocity['horizontal_max_kmh'] = max(velocity['horizontal_max_kmh'], horizontal)
        velocity['vertical_max_kmh'] = max(velocity['vertical_max_kmh'], vertical)
        velocity['scalar_gt_limit'] += scalar > quality.MAX_VALID_VEHICLE_SPEED_KMH
        velocity['horizontal_gt_limit'] += horizontal > quality.MAX_VALID_VEHICLE_SPEED_KMH
        velocity['vertical_gt_limit'] += vertical > quality.MAX_VALID_VEHICLE_SPEED_KMH
        deviation = quality.tri_axis_peak_deviation(point)
        state = quality.motion_state(point)
        states[state] += 1
        if deviation is not None:
            deviations.append(deviation)
        for name, item in reference_stats.items():
            if item['start'] <= point['t'] <= item['end']:
                item['samples'] += 1
                item['states'][state] += 1
                if deviation is not None:
                    item['deviations'].append(deviation)
                if quality.navigation_position_drift(previous, point):
                    item['severe_position_jumps'] += 1
        drift = quality.navigation_position_drift(previous, point)
        if drift:
            jumps.append(dict(t=point['t'], protocol=point['protocol'], **drift))
        previous = point
    connection.close()
    for item in reference_stats.values():
        item['states'] = dict(item['states'])
        item['deviation'] = quantiles(item.pop('deviations'))
        item['dominant_state'] = max(item['states'], key=item['states'].get) if item['states'] else 'unknown'
        item['moving_fraction'] = round(item['states'].get('moving', 0) / item['samples'], 6) if item['samples'] else 0
    return dict(quality_version=quality.VERSION, strategy=quality.MOTION_STRATEGY,
                motion_threshold_g=quality.MOTION_IMPACT_THRESHOLD_G, device_id=device,
                velocity=velocity, motion=dict(states=dict(states), deviation=quantiles(deviations)),
                severe_position_jumps=dict(total=len(jumps), first=jumps[:20]), references=reference_stats)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--device', default='6094510')
    parser.add_argument('--output')
    args = parser.parse_args()
    result = audit(args.db, args.device)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + '\n')
    print(text)
