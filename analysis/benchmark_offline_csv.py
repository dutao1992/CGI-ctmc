"""Generate a bounded valid export in memory and benchmark offline parsing.

Synthetic rows are never sent to the receiver or written to the production DB.
"""
import csv
import argparse
import io
import json
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle import quality
from vehicle.offline import EXPORT_FIELDS, MAX_BYTES, analyze_csv, analyze_stream
from vehicle.protocol import NUMERIC, parse


LIVE = (Path(__file__).parent.parent/'tests/fixtures/gpchcx-live.txt').read_bytes().splitlines()[0]


def make_csv(rows=100_000):
    point = parse(LIVE)
    duration=max(0,rows-1)*.1;gps_absolute=point['week']*604800+point['tow']-duration
    point=dict(point,t=point['t']-duration,week=int(gps_absolute//604800),tow=gps_absolute%604800)
    mask,reasons,_ = quality.assess(point,None)
    excluded = [key for key,bit in quality.BITS.items() if mask&bit]
    reason_codes = [key for key,bit in quality.REASON_BITS.items() if reasons&bit]
    out=io.StringIO();writer=csv.writer(out);writer.writerow(EXPORT_FIELDS)
    for index in range(rows):
        absolute_tow=point['tow']+index*.1
        p=dict(point,t=point['t']+index*.1,week=point['week']+int(absolute_tow//604800),tow=absolute_tow%604800)
        p.update(source='benchmark/not-persisted.log',source_offset=index,data_view='filtered',
                 filter_version=quality.VERSION,excluded_fields='|'.join(excluded),
                 filter_reasons='|'.join(reason_codes),stationary_context='')
        for key in excluded:p[key]=None
        writer.writerow([p.get(key) for key in EXPORT_FIELDS])
    return ('\ufeff'+out.getvalue()).encode()


class SyntheticCsvStream:
    """Generate a production-schema stream without allocating the whole file."""
    def __init__(self, rows, hz=10, block=5000):
        self.rows,self.hz,self.block = rows,hz,block
        self.index=0;self.buffer=bytearray();self.bytes_emitted=0
        self.point=parse(LIVE);duration=max(0,rows-1)/hz
        gps_absolute=self.point['week']*604800+self.point['tow']-duration
        self.point=dict(self.point,t=self.point['t']-duration,week=int(gps_absolute//604800),tow=gps_absolute%604800)
        mask,reasons,_=quality.assess(self.point,None)
        self.excluded=[key for key,bit in quality.BITS.items() if mask&bit]
        self.reason_codes=[key for key,bit in quality.REASON_BITS.items() if reasons&bit]

    def _fill(self):
        if self.index>=self.rows:return
        out=io.StringIO();writer=csv.writer(out)
        if self.index==0:writer.writerow(EXPORT_FIELDS)
        stop=min(self.rows,self.index+self.block)
        for index in range(self.index,stop):
            offset=index/self.hz;absolute_tow=self.point['tow']+offset
            p=dict(self.point,t=self.point['t']+offset,week=self.point['week']+int(absolute_tow//604800),tow=absolute_tow%604800)
            p.update(source='benchmark/not-persisted.log',source_offset=index,data_view='filtered',
                     filter_version=quality.VERSION,excluded_fields='|'.join(self.excluded),
                     filter_reasons='|'.join(self.reason_codes),stationary_context='')
            for key in self.excluded:p[key]=None
            writer.writerow([p.get(key) for key in EXPORT_FIELDS])
        chunk=(('\ufeff' if self.index==0 else '')+out.getvalue()).encode()
        self.index=stop;self.buffer.extend(chunk)

    def read(self,size=-1):
        while (size<0 or len(self.buffer)<size) and self.index<self.rows:self._fill()
        if size<0:size=len(self.buffer)
        chunk=bytes(self.buffer[:size]);del self.buffer[:size];self.bytes_emitted+=len(chunk);return chunk


def run(rows=100_000):
    payload=make_csv(rows);began=time.perf_counter();result=analyze_csv(payload,mount_mode='unconfirmed')
    elapsed=time.perf_counter()-began
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes=rss if sys.platform=='darwin' else rss*1024
    return dict(rows=rows,csv_bytes=len(payload),elapsed_s=round(elapsed,3),rows_per_s=round(rows/elapsed),
                output_buckets=result['aggregation']['buckets'],track_points=len(result['track']),
                events=result['events']['total'],peak_ax=max(row[3] for row in result['series']['ax']),
                max_rss_bytes=rss_bytes,persisted=result['offline']['persisted'])


def run_stream(rows,hz=10):
    stream=SyntheticCsvStream(rows,hz);began=time.perf_counter()
    result=analyze_stream(stream,MAX_BYTES,mount_mode='unconfirmed',require_complete=False)
    elapsed=time.perf_counter()-began
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes=rss if sys.platform=='darwin' else rss*1024
    return dict(rows=rows,days=round(rows/hz/86400,2),hz=hz,csv_bytes=stream.bytes_emitted,
                elapsed_s=round(elapsed,3),rows_per_s=round(rows/elapsed),
                output_buckets=result['aggregation']['buckets'],source_resolution_s=result['aggregation']['source_resolution_s'],
                source_rollups=round(rows/hz/result['aggregation']['source_resolution_s']),
                track_points=len(result['track']),events=result['events']['total'],
                peak_ax=max(row[3] for row in result['series']['ax']),max_rss_bytes=rss_bytes,
                persisted=result['offline']['persisted'])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('rows',nargs='?',type=int)
    parser.add_argument('--days',type=float);parser.add_argument('--hz',type=int,default=10)
    args=parser.parse_args();count=args.rows or round((args.days or 0)*86400*args.hz)
    result=run_stream(count,args.hz) if args.days else run(count or 100_000)
    print(json.dumps(result,ensure_ascii=False,indent=2))
