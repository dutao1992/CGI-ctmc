"""Read-only audit for vehicle-speed integrity and stationary lifecycle replay."""
import argparse
import collections
import json
import math
from pathlib import Path
import sqlite3

from vehicle import quality


def quantiles(values):
    return {
        key: quality.percentile(values, fraction)
        for key, fraction in [('p50', .5), ('p90', .9), ('p95', .95), ('p99', .99), ('max', 1)]
    } if values else {}


def active_context(index, device, evidence):
    context = {
        'id': f'audit-{index}',
        'device_id': device,
        'start': evidence['candidate_start'],
        'end': None,
        'kind': 'confirmed_stationary_active',
        'active': True,
        'profile': {
            'anchor': {key:evidence['anchor'][key] for key in ('lat', 'lon')},
            'position_limit_m': 15.0,
        },
    }
    return context


def audit(db, device, confirmed_static_start=None, replay_start=None):
    connection = sqlite3.connect('file:'+str(Path(db).resolve())+'?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    columns = ('device_id,t,protocol,status_text,valid_pos,nav_mode,fix_mode,lat,lon,alt,'
               'speed,ve,vn,vu,gx,gy,gz,ax,ay,az,lat_std,lon_std')
    sql = f'SELECT {columns} FROM points WHERE device_id=? ORDER BY t,protocol'
    velocity = dict(total=0,first_t=None,last_t=None,physical_limit_kmh=quality.MAX_VALID_VEHICLE_SPEED_KMH,
                    scalar_max_kmh=0,horizontal_max_kmh=0,vertical_max_kmh=0,
                    effective_scalar_max_kmh=0,scalar_gt_limit=0,horizontal_gt_limit=0,
                    vertical_gt_limit=0,quarantined_navigation_points=0,intervals=[])
    current_interval = None
    entry_detector = quality.AutomaticStationaryEntryDetector()
    exit_detector = quality.ActiveStationaryExitDetector()
    inferred, active = [], None
    anchor_training = []
    for row in connection.execute(sql, (device,)):
        p = dict(row);velocity['total'] += 1
        velocity['first_t'] = p['t'] if velocity['first_t'] is None else velocity['first_t']
        velocity['last_t'] = p['t']
        scalar = p['speed'] * 3.6
        horizontal = math.hypot(p['ve'], p['vn']) * 3.6
        vertical = abs(p['vu']) * 3.6
        velocity['scalar_max_kmh'] = max(velocity['scalar_max_kmh'], scalar)
        velocity['horizontal_max_kmh'] = max(velocity['horizontal_max_kmh'], horizontal)
        velocity['vertical_max_kmh'] = max(velocity['vertical_max_kmh'], vertical)
        flags = (scalar > quality.MAX_VALID_VEHICLE_SPEED_KMH,
                 horizontal > quality.MAX_VALID_VEHICLE_SPEED_KMH,
                 vertical > quality.MAX_VALID_VEHICLE_SPEED_KMH)
        velocity['scalar_gt_limit'] += flags[0]
        velocity['horizontal_gt_limit'] += flags[1]
        velocity['vertical_gt_limit'] += flags[2]
        if any(flags):
            velocity['quarantined_navigation_points'] += 1
            if current_interval is None or p['t'] - current_interval['end'] > 1.0:
                current_interval = dict(start=p['t'],end=p['t'],samples=0,max_scalar_kmh=0,
                                        max_horizontal_kmh=0,max_vertical_kmh=0,status_counts=collections.Counter())
                velocity['intervals'].append(current_interval)
            current_interval['end'] = p['t'];current_interval['samples'] += 1
            current_interval['max_scalar_kmh'] = max(current_interval['max_scalar_kmh'], scalar)
            current_interval['max_horizontal_kmh'] = max(current_interval['max_horizontal_kmh'], horizontal)
            current_interval['max_vertical_kmh'] = max(current_interval['max_vertical_kmh'], vertical)
            current_interval['status_counts'][p['status_text']] += 1
        else:
            velocity['effective_scalar_max_kmh'] = max(velocity['effective_scalar_max_kmh'], scalar)

        if confirmed_static_start is not None and p['t'] >= confirmed_static_start:
            if p['t'] <= confirmed_static_start + 600 and p['valid_pos'] and p['nav_mode']:
                anchor_training.append((p['lat'], p['lon']))

        if replay_start is not None and p['t'] >= replay_start:
            if active:
                evidence = exit_detector.observe(p, active)
                if evidence:
                    inferred[-1]['end'] = evidence['candidate_start'] - .001
                    inferred[-1]['exit'] = evidence
                    active = None
                    entry_detector.forget(device)
            else:
                evidence = entry_detector.observe(p)
                if evidence:
                    active = active_context(len(inferred)+1, device, evidence)
                    inferred.append(dict(start=evidence['candidate_start'],detected_at=evidence['detected_at'],
                                         end=None,entry=evidence))

    for interval in velocity['intervals']:
        interval['status_counts'] = dict(interval['status_counts'])
    output = dict(quality_version=quality.VERSION,device_id=device,velocity=velocity,
                  automatic_stationary_replay=dict(start=replay_start,candidates=inferred,
                                                   policy=quality.automatic_entry_policy()))
    if anchor_training:
        anchor = {'lat':quality.median([x[0] for x in anchor_training]),
                  'lon':quality.median([x[1] for x in anchor_training])}
        radii, speeds, gyros, accel, stds = [], [], [], [], []
        samples = 0;stationary_end = confirmed_static_start
        for row in connection.execute('''SELECT t,valid_pos,nav_mode,lat,lon,speed,gx,gy,gz,ax,ay,az,lat_std,lon_std
                                         FROM points WHERE device_id=? AND t>=? ORDER BY t,protocol''',
                                      (device,confirmed_static_start)):
            p = dict(row);samples += 1;stationary_end = p['t']
            speeds.append(p['speed']*3.6)
            gyros.append(math.sqrt(p['gx']**2+p['gy']**2+p['gz']**2))
            accel.append(math.sqrt(p['ax']**2+p['ay']**2+p['az']**2))
            stds.append(max(p['lat_std'],p['lon_std']))
            if p['valid_pos'] and p['nav_mode']:
                radii.append(quality.distance(p, anchor))
        output['confirmed_stationary'] = dict(
            start=confirmed_static_start,end=stationary_end,samples=samples,anchor=anchor,
            speed_kmh=quantiles(speeds),gyro_norm_dps=quantiles(gyros),accel_norm_g=quantiles(accel),
            radius_m=quantiles(radii),position_std_m=quantiles(stds),
            radius_gt_15m=sum(value>15 for value in radii),
            radius_gt_30m=sum(value>30 for value in radii),
            position_std_gt_5m=sum(value>5 for value in stds),
        )
    connection.close()
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--device', default='6094510')
    parser.add_argument('--confirmed-static-start', type=float)
    parser.add_argument('--replay-start', type=float)
    parser.add_argument('--output')
    args = parser.parse_args()
    result = audit(args.db,args.device,args.confirmed_static_start,args.replay_start)
    data = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(data+'\n')
    print(data)
