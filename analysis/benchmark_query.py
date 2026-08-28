"""Repeatable query benchmark; read results only, never edits raw points."""
import argparse
import gzip
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle.store import Store


def run(db, device, hours, bins):
    store = Store(db)
    with store.connect() as c:
        row = c.execute('SELECT last_t FROM devices WHERE id=?',(device,)).fetchone()
    if row is None:
        raise ValueError('设备不存在')
    end = row[0]
    results = []
    for hour in hours:
        start = end-hour*3600
        for attempt in range(2):
            began = time.perf_counter()
            data = store.query(device,start,end,bins)
            encoded = json.dumps(data,separators=(',',':'),ensure_ascii=False).encode()
            compressed = gzip.compress(encoded,compresslevel=4)
            results.append(dict(hours=hour,attempt=attempt+1,total=data['total'],buckets=data['aggregation']['buckets'],
                                source=data['aggregation']['source'],source_resolution_s=data['aggregation']['source_resolution_s'],
                                cache_hit=data['aggregation']['cache_hit'],server_query_ms=data['aggregation']['query_ms'],
                                wall_ms=round((time.perf_counter()-began)*1000,1),json_bytes=len(encoded),gzip_bytes=len(compressed),
                                gzip_ratio=round(len(compressed)/len(encoded),3)))
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--device',required=True)
    parser.add_argument('--hours',nargs='+',type=float,default=[1,24])
    parser.add_argument('--bins',type=int,default=480)
    args = parser.parse_args()
    print(json.dumps(run(args.db,args.device,args.hours,args.bins),ensure_ascii=False,indent=2))
