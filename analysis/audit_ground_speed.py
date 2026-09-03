"""Read-only raw-data replay; no device configuration or database writes."""
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle.ground_speed import Estimator

TZ = timezone(timedelta(hours=8))
REFERENCES = {
    'trolley_20260903': (1788409335.28, 1788411300.),
    'vehicle_20260901': (1788230309.6, 1788230319.6),
    'stop_20260902': (1788323760., 1788323940.),
}


def bucket():
    return dict(rows=0, first_t=None, last_t=None, raw_max_kmh=None,
                accepted_max_kmh=None, max_point=None, states=Counter(), reasons=Counter(), modes=Counter())


def add(b, p, estimate):
    b['rows'] += 1
    b['first_t'] = b['first_t'] or p['t']
    b['last_t'] = p['t']
    raw = p.get('speed')
    if raw is not None:
        b['raw_max_kmh'] = max(b['raw_max_kmh'] or 0, raw*3.6)
    value = estimate['value']
    if value is not None and (b['accepted_max_kmh'] is None or value*3.6 > b['accepted_max_kmh']):
        b['accepted_max_kmh'] = value*3.6
        b['max_point'] = {k:p.get(k) for k in ('t','speed','ve','vn','vu','ve_std','vn_std','vu_std','nav_mode','fix_mode')}
    b['states'][estimate['state']] += 1
    b['reasons'][estimate['reason']] += 1
    b['modes'][str(p['nav_mode'])] += 1


def audit(db, device):
    c = sqlite3.connect('file:'+str(Path(db).resolve())+'?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    c.execute('BEGIN')  # One stable read snapshot while reception continues.
    days, refs = {}, {k:bucket() for k in REFERENCES}
    estimator = Estimator()
    for row in c.execute('SELECT * FROM points WHERE device_id=? ORDER BY t,protocol', (device,)):
        p = dict(row)
        estimate = estimator.observe(p)
        day = datetime.fromtimestamp(p['t'], TZ).strftime('%Y-%m-%d')
        add(days.setdefault(day, bucket()), p, estimate)
        for name, (start, end) in REFERENCES.items():
            if start <= p['t'] <= end:
                add(refs[name], p, estimate)
    c.close()
    for item in [*days.values(), *refs.values()]:
        item['accepted_pct'] = (100*(item['rows']-item['states']['unknown'])/item['rows']) if item['rows'] else None
    return dict(device=device, source='read-only points snapshot', timezone='Asia/Shanghai', days=days, references=refs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--device', default='6094510')
    args = parser.parse_args()
    print(json.dumps(audit(args.db, args.device), ensure_ascii=False, indent=2))
