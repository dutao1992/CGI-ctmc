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
from vehicle.aggregate import QueryCombiner
measure={}
def wrap_method(cls,name):
    old=getattr(cls,name)
    def timed(self,*a,**kw):
        t=time.perf_counter()
        try:return old(self,*a,**kw)
        finally:
            measure[name]=measure.get(name,0)+(time.perf_counter()-t)*1000
    setattr(cls,name,timed)
for name in ('add','finish','_merge_segments','_merge_metrics','_append_track'):
    wrap_method(QueryCombiner,name)
oldanchor=QueryCombiner._anchor_stationary_track
@classmethod
def anchor(cls,track,segments):
    measure['anchor_track_count']=len(track);measure['anchor_segment_count']=len(segments)
    measure['stationary_segment_count']=sum(x.get('state')=='stationary' for x in segments)
    t=time.perf_counter()
    try:return oldanchor(track,segments)
    finally:measure['anchor_ms']=(time.perf_counter()-t)*1000
QueryCombiner._anchor_stationary_track=anchor
s=ReadOnlyStore()
with s.connect() as c:d=c.execute('select id,last_t from devices order by point_count desc limit 1').fetchone()
t=time.perf_counter();r=s.query(d['id'],math.ceil(d['last_t'])-345600,math.ceil(d['last_t']),288)
print(json.dumps({'query_ms':(time.perf_counter()-t)*1000,'timings':s.timings,'combiner':measure}),flush=True)
