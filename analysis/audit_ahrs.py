"""Read-only AHRS compatibility audit; never infer configured mode from nav state.

Run against the existing platform database, optionally checking its raw logs.
The configured carrier mode is supplied by the operator, not present in GPCHCX.
"""
import argparse
from collections import Counter, OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle import quality
from vehicle.protocol import FRAME, checksum, parse
from vehicle.store import Store

TZ = timezone(timedelta(hours=8))


def local(t):
    return datetime.fromtimestamp(t, TZ).isoformat(timespec='milliseconds')


class ReadOnlyStore(Store):
    def __init__(self, path):
        # Do not run Store's schema creation or migration on an audit source.
        self.path = str(Path(path).resolve())
        self._query_cache = OrderedDict()
        self._cache_lock = threading.Lock()

    @contextmanager
    def connect(self):
        c = sqlite3.connect(Path(self.path).as_uri()+'?mode=ro', uri=True)
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        try:
            yield c
        finally:
            c.close()


def summarize(rows):
    ranges = {}
    for key in ('pitch', 'roll', 'heading', 'pitch_std', 'roll_std', 'heading_std',
                'ax', 'ay', 'az', 'gx', 'gy', 'gz', 'sat1', 'sat2'):
        values = [p[key] for p in rows if p[key] is not None]
        ranges[key] = [min(values), max(values)] if values else None
    estimates = [json.loads(p['q_ground']) for p in rows if p['q_ground']]
    departures = [dict(t=local(p['t']), deviation_g=abs(math.sqrt(sum(p[k]**2 for k in ('ax','ay','az')))-1))
                  for p in rows if all(p[k] is not None for k in ('ax','ay','az'))]
    over = [p for p in departures if p['deviation_g'] > .01]
    return dict(rows=len(rows), first=local(rows[0]['t']) if rows else None,
                last=local(rows[-1]['t']) if rows else None,
                statuses=dict(Counter(p['status_text'] for p in rows)),
                valid_position=sum(bool(p['valid_pos']) for p in rows),
                states=dict(Counter(p['state'] for p in estimates)),
                reasons=dict(Counter(p['reason'] for p in estimates)),
                ranges=ranges, imu_over_001g=len(over),
                imu_over_first=over[0] if over else None,
                imu_over_last=over[-1] if over else None,
                largest_specific_force_departure=max(departures, key=lambda p:p['deviation_g'], default=None))


def audit(db, device, day, raw_root=None):
    start = datetime.fromisoformat(day+'T14:00:00+08:00').timestamp()
    end = start + 10*3600
    store = ReadOnlyStore(db)
    with store.connect() as c:
        rows = [dict(r) for r in c.execute(quality.JOIN+
                ' WHERE p.device_id=? AND p.t>=? AND p.t<? ORDER BY p.t,p.protocol', (device,start,end))]
        files = sorted({p['source'] for p in rows})
        cursors = [dict(r) for path in files for r in c.execute('SELECT * FROM cursors WHERE path=?',(path,))]
        latest_valid = c.execute('SELECT t,status_text FROM points WHERE device_id=? AND valid_pos=1 AND t<? ORDER BY t DESC LIMIT 1', (device,end)).fetchone()
    windows = {}
    for label, first, last in [('14:00-15:00',start,start+3600),('15:00-end',start+3600,end),
                               ('15:15-15:25',start+4500,start+5100)]:
        windows[label] = summarize([p for p in rows if first <= p['t'] < last])
    minutes = {}
    for p in rows:
        key = local(p['t'])[:16]
        minutes.setdefault(key, []).append(p)
    minute_stats = {key:summarize(values) for key,values in minutes.items()}
    query = store.query(device,start+3600,end,bins=200)
    projections = []
    for row in rows:
        p = quality.project(row)
        projections.append(all(p[k] == row[k] for k in ('pitch','roll','ax','ay','az','gx','gy','gz')))
    raw = []
    if raw_root:
        for relative in files:
            path = Path(raw_root)/relative
            payload = path.read_bytes()
            counts, errors, states, weeks = Counter(), Counter(), Counter(), Counter()
            aux_after, latest = {}, {}
            current_t = None
            for match in FRAME.finditer(payload):
                frame = match[1]
                typ = frame[1:].split(b',')[0].decode('ascii')
                counts[typ] += 1
                if not checksum(frame):
                    errors['checksum'] += 1
                    continue
                latest[typ] = frame.decode('ascii')
                if typ == 'GPCHCX':
                    try:
                        p = parse(frame)
                        current_t = p['t']
                        states[p['status_text']] += 1
                        weeks[str(p['week'])] += 1
                    except ValueError as exc:
                        errors[str(exc)] += 1
                elif current_t is not None and start+3600 <= current_t < end:
                    # Associate auxiliary frames with the preceding timestamped
                    # GPCHCX on this stream; retain the raw last line for review.
                    fields = frame.decode('ascii').split('*')[0].split(',')
                    state = (','.join(fields[6:8]) if typ == 'GPGGA' else
                             ','.join(frame.decode('ascii').split(';')[-1].split(',')[:2]) if typ == 'INSPVAXA' else 'auxiliary')
                    aux_after.setdefault(typ, Counter())[state] += 1
            raw.append(dict(file=relative,bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest(),
                            protocols=dict(counts),errors=dict(errors),statuses=dict(states),gps_weeks=dict(weeks),
                            auxiliary_after_1500=aux_after,last_frames=latest))
    return dict(observed_at=local(datetime.now(TZ).timestamp()),device=device,configured_mode='AHRS (operator reported)',
                latest_valid=dict(latest_valid) if latest_valid else None, windows=windows,minutes=minute_stats,
                cursors=cursors, raw=raw,
                production_query=dict(total=query['total'],summary=query['summary'],track_points=len(query['track']),
                                      quality=query['quality'],
                                      vibration={k:v for k,v in query['vibration'].items() if k not in ('time','spectrum','range')},
                                      vibration_range=query['vibration']['range']['metrics'],
                                      imu_attitude_unchanged_rows=sum(projections),checked_rows=len(projections)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--device', default='6094510')
    parser.add_argument('--day', default='2026-09-03')
    parser.add_argument('--raw-root')
    args = parser.parse_args()
    print(json.dumps(audit(args.db,args.device,args.day,args.raw_root),ensure_ascii=False,indent=2))
