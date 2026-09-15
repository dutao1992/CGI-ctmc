import collections,threading,sqlite3,time,json,gzip,math
from vehicle.store import Store,Connection
class ReadOnlyStore(Store):
    def __init__(self):
        self.path='/srv/ctmc-vehicle/data/vehicle.sqlite'
        self._query_cache=collections.OrderedDict();self._cache_lock=threading.Lock();self.timings={}
    def connect(self):
        c=sqlite3.connect('file:'+self.path+'?mode=ro',uri=True,timeout=5,factory=Connection)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
        return c
    def _vibration(self,*args):
        t=time.perf_counter();r=super()._vibration(*args);self.timings['vibration_ms']=round((time.perf_counter()-t)*1000,1);return r
    def events(self,*args):
        t=time.perf_counter();r=super().events(*args);self.timings['events_ms']=round((time.perf_counter()-t)*1000,1);return r
s=ReadOnlyStore()
with s.connect() as c:
    d=c.execute('select id,last_t,point_count from devices order by point_count desc limit 1').fetchone()
end=math.ceil(d['last_t']);sn=d['id']
print(json.dumps({'device':sn,'end':end,'point_count':d['point_count'],'mode':'read-only separate process; cold application cache, OS cache uncontrolled'}),flush=True)
for span,bins in [(900,360),(3600,360),(86400,360),(345600,288),(864000,240),(2592000,180)]:
    s.timings={};t=time.perf_counter();r=s.query(sn,end-span,end,bins);cold=(time.perf_counter()-t)*1000
    t=time.perf_counter();raw=json.dumps(r,ensure_ascii=False,allow_nan=False).encode();ser=(time.perf_counter()-t)*1000
    t=time.perf_counter();z=gzip.compress(raw,compresslevel=4);gz=(time.perf_counter()-t)*1000
    t=time.perf_counter();cached=s.query(sn,end-span,end,bins);warm=(time.perf_counter()-t)*1000
    print(json.dumps({'span_s':span,'bins':bins,'samples':r['total'],'cold_ms':round(cold,1),'warm_ms':round(warm,1),'stages':s.timings,'serialize_ms':round(ser,1),'gzip_ms':round(gz,1),'json_bytes':len(raw),'gzip_bytes':len(z),'aggregation':r['aggregation'],'sections_bytes':{k:len(json.dumps(v,ensure_ascii=False).encode()) for k,v in r.items()},'event_count':r['events']['total'],'vibration_available':r['vibration'].get('available')},ensure_ascii=False),flush=True)
