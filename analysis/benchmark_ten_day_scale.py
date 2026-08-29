"""CPU/response benchmark for a 10 Hz, 10-day rollup result.

This does not fabricate raw receiver evidence or write a database.  It feeds
1,440 mergeable ten-minute summaries, representing 8.64 million raw samples,
through the same QueryCombiner used by the production query path, including
the compact vibration statistics used by the overview trend.
"""
import gzip
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle import quality
from vehicle.aggregate import METRICS, ROLLUP_VERSION, QueryCombiner


def snapshot(device, start, index, count=6000):
    peak = 5.0 if index == 719 else 1.0
    first = {key:1.0 for key in METRICS}
    first.update(t=start+.05,lat=31.245,lon=121.616,speed=.2,heading=0,
                 fix_mode=4,nav_mode=2,valid_pos=1,stationary_context=None,
                 lat_std=.02,lon_std=.02)
    last = dict(first,t=start+599.95)
    metrics = {key:[1.0,peak if key=='ax' else 1.0,
                    count+4 if key=='ax' and peak>1 else float(count),count,1.0]
               for key in METRICS}
    track = [dict(t=first['t'],lat=first['lat'],lon=first['lon'],speed=.2,heading=0,
                  fix_mode=4,nav_mode=2,stationary_context=None,break_before=True),
             dict(t=last['t'],lat=last['lat'],lon=last['lon'],speed=.2,heading=0,
                  fix_mode=4,nav_mode=2,stationary_context=None,break_before=False)]
    return dict(version=ROLLUP_VERSION,device_id=device,bucket_start=start,bucket_s=600,
                count=count,first=first,last=last,fixed=count,valid=count,max_speed=.72,
                fix_counts={'4':count},metrics=metrics,
                vibration=[1.0,1.0,float(count),float(count),count],
                quality_groups=[[quality.VERSION,0,0,count]],covered_s=599.9,
                mileage_m=0,moving_s=0,gaps=[],segments=[dict(start=first['t'],end=last['t'],
                state='stopped',distance_m=0,max_kmh=.72,break_before=True)],track=track)


def run(days=10, hz=10, bins=360):
    start = 1_800_000_000
    bucket_count = days*24*6
    combiner = QueryCombiner('SCALE-10D',start,start+days*86400,bins)
    began = time.perf_counter()
    for index in range(bucket_count):
        combiner.add(snapshot('SCALE-10D',start+index*600,index,600*hz))
    result = combiner.finish([], 'rollup', 600, (time.perf_counter()-began)*1000)
    encoded = json.dumps(result,separators=(',',':'),ensure_ascii=False).encode()
    compressed = gzip.compress(encoded,compresslevel=4)
    elapsed_ms = (time.perf_counter()-began)*1000
    peak = max(row[3] for row in result['series']['ax'])
    assert result['total'] == days*86400*hz
    assert result['aggregation']['buckets'] <= bins
    assert peak == 5.0
    assert result['vibration_range']['samples'] == days*86400*hz
    assert result['vibration_range']['buckets'] <= bins
    return dict(days=days,hz=hz,equivalent_raw_points=result['total'],rollup_rows=bucket_count,
                output_buckets=result['aggregation']['buckets'],peak_ax=peak,
                combine_and_encode_ms=round(elapsed_ms,1),json_bytes=len(encoded),
                gzip_bytes=len(compressed),gzip_ratio=round(len(compressed)/len(encoded),3))


if __name__ == '__main__':
    print(json.dumps(run(),ensure_ascii=False,indent=2))
