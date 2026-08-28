"""Reproduce real-snapshot acceptance without touching the source database."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle.store import Store


def digest(path):
    c=sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)
    h=hashlib.sha256()
    for row in c.execute('SELECT * FROM points ORDER BY device_id,t,protocol'):
        h.update(json.dumps(row,separators=(',',':')).encode())
    c.close()
    return h.hexdigest()


def validate(source, filtered, device='6094510', start=1787728135, end=1787740798):
    store=Store(filtered)
    began=time.monotonic();q=store.query(device,start,end)
    out=dict(query_seconds=time.monotonic()-began,raw_sha256=digest(source),replay_raw_sha256=digest(filtered),
             summary=q['summary'],quality=q['quality'],
             filtered_ranges={k:dict(min=min((r[2] for r in rows if r[2] is not None),default=None),
                                     max=max((r[3] for r in rows if r[3] is not None),default=None)) for k,rows in q['series'].items()},
             track_points=len(q['track']),track_static=sum(bool(p['stationary_context']) for p in q['track']))
    assert out['raw_sha256']==out['replay_raw_sha256'], 'Original measurements were changed'
    assert q['summary']['distance_km']==0 and q['summary']['moving_s']==0, 'Known-static data became motion'
    assert out['filtered_ranges']['heading']['max'] is None, 'Unavailable heading was exposed'
    assert out['quality']['pending_samples']==0, 'Backfill incomplete'
    assert q['summary']['max_kmh']<=1.08, 'Stationary velocity outlier leaked'
    return out


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('filtered');p.add_argument('--output',required=True)
    args=p.parse_args();out=validate(args.source,args.filtered)
    Path(args.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(raw_unchanged=True,total=out['quality']['total'],anomaly_samples=out['quality']['anomaly_samples'],
                          distance_km=out['summary']['distance_km'],query_seconds=out['query_seconds'])))
