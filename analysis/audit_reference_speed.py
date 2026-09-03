"""Read-only full-dataset shadow comparison against stored trusted estimates.

Run with the candidate release's Python path. Does not initialize Store or write
SQLite. Existing navigation-level outliers stay excluded, as in write_assessment.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vehicle.ground_speed import Estimator, valid_reference


def audit(db):
    started = time.monotonic()
    counts, days, windows = Counter(), defaultdict(Counter), defaultdict(Counter)
    scopes = {'trolley': (1788409335.28, 1788411300),
              'vehicle': (1788230309.6, 1788230319.6),
              'stationary': (1788323760, 1788323940)}
    estimator, device = None, None
    c = sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        sql = '''SELECT p.*, g.payload AS stored_estimate FROM points p
                 JOIN point_ground_speed g USING(device_id,t,protocol)
                 ORDER BY p.device_id,p.t,p.protocol'''
        # The schema uses estimate_json; resolve explicitly rather than silently
        # accepting a missing old estimate in an inner join.
        columns = [r[1] for r in c.execute('PRAGMA table_info(point_ground_speed)')]
        sql = sql.replace('g.payload', 'g.'+columns[-1])
        expected = c.execute('SELECT count(*) FROM points').fetchone()[0]
        for row in c.execute(sql):
            p = dict(row)
            old = json.loads(p.pop('stored_estimate'))
            if p['device_id'] != device:
                device, estimator = p['device_id'], Estimator()
            new = estimator.observe(p)
            if old['reason'] == 'velocity_solution_outlier':
                new.update(value=None, state='unknown', reason='velocity_solution_outlier')
                new.pop('reference', None)
            mismatch = any(new.get(k) != old.get(k) for k in ('value', 'state', 'reason'))
            if mismatch and counts['mismatches'] < 5:
                print(json.dumps(dict(mismatch_t=p['t'], old=old, new=new)), flush=True)
            counts['mismatches'] += mismatch
            assert valid_reference(new), new
            tags = ['total', 'trusted' if new['value'] is not None else 'untrusted']
            if new.get('reference') is not None:
                tags += ['reference']
            if new['state'] == 'stationary': tags += ['stationary']
            if new['state'] == 'moving': tags += ['moving']
            day = datetime.fromtimestamp(p['t'], timezone(timedelta(hours=8))).date().isoformat()
            for counter in [counts, days[day]] + [windows[k] for k,(a,b) in scopes.items() if a <= p['t'] <= b]:
                counter.update(tags)
            if counts['total'] % 200000 == 0:
                print(json.dumps(dict(progress=counts['total'], elapsed_s=round(time.monotonic()-started,1))), flush=True)
        result = dict(counts=counts, days=days, windows=windows, elapsed_s=round(time.monotonic()-started,1))
        print(json.dumps(result), flush=True)
        assert counts['total'] == expected, (counts['total'], expected)
        assert counts['mismatches'] == 0, counts
        return result
    finally:
        c.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    audit(parser.parse_args().db)
