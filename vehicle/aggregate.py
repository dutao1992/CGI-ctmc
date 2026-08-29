"""Minute rollups for long-window visualization.

Raw points remain authoritative.  Rollups are deterministic, rebuildable
projections that preserve filtered mean/min/max envelopes and operational
summaries while avoiding a Python pass over hundreds of thousands of rows on
every chart request.
"""
import collections
import json
import math

from . import quality
from .rules import distance


METRICS = ['speed','heading','pitch','roll','gx','gy','gz','ax','ay','az','ve','vn','vu','alt',
           'lat_std','lon_std','alt_std','heading_std','roll_std','pitch_std','age','sat1','sat2',
           've_std','vn_std','vu_std','course','course_std']
ROLLUP_SECONDS = 60
ROLLUP_LEVELS = (60,600)
# Bump when the payload shape changes; the deployment preflight rebuilds both
# levels before serving queries so every bucket carries vibration statistics.
ROLLUP_VERSION = quality.VERSION * 100 + 3
ENDPOINT_FIELDS = list(dict.fromkeys(
    ['t','lat','lon','speed','heading','fix_mode','nav_mode','valid_pos','stationary_context','lat_std','lon_std'] + METRICS
))


def bucket_start(t, seconds=ROLLUP_SECONDS):
    return int(math.floor(t / seconds) * seconds)


def _endpoint(p):
    return {key:p.get(key) for key in ENDPOINT_FIELDS}


def _transition(prev, current):
    delta = current['t'] - prev['t'] if prev else 0
    contiguous = bool(prev and 0 < delta <= 3)
    jump = bool(contiguous and not prev.get('stationary_context') and not current.get('stationary_context') and
                prev.get('valid_pos') and current.get('valid_pos') and
                distance(prev,current) > (max(prev.get('speed') or 0,current.get('speed') or 0)+15)*delta +
                max(20,5*(current.get('lat_std') or 0),5*(current.get('lon_std') or 0)))
    covered = delta if contiguous and prev.get('valid_pos') and current.get('valid_pos') and not jump else 0
    mileage = moving = 0
    speed_ok = current.get('speed') is not None
    if covered and not current.get('stationary_context') and not prev.get('stationary_context') and speed_ok and prev.get('speed') is not None and current['speed'] >= 1 and prev['speed'] >= 1:
        mileage = (current['speed']+prev['speed'])/2*delta
        moving = delta
    return dict(delta=delta,contiguous=contiguous,jump=jump,covered=covered,mileage=mileage,moving=moving,
                gap=bool(prev and delta > 3))


def _motion_state(p):
    if p.get('stationary_context'):
        return 'confirmed_stationary'
    speed_ok = p.get('speed') is not None
    if p.get('valid_pos') and speed_ok:
        return 'moving' if p['speed'] >= 1 else 'stopped'
    return 'unknown'


def _specific_force_magnitude(p):
    """Return the gravity-included three-axis specific-force magnitude."""
    values = [p.get(key) for key in ('ax', 'ay', 'az')]
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        return None
    return math.sqrt(sum(value * value for value in values))


def _vibration_stats(points):
    """Compact min/max/sum/sum-of-squares/count stats for one time bucket."""
    values = [value for _, value in points if isinstance(value, (int, float)) and math.isfinite(value)]
    if not values:
        return [None, None, 0.0, 0.0, 0]
    return [min(values), max(values), sum(values), sum(value * value for value in values), len(values)]


class RollupBuilder:
    """Build one independent time bucket from ordered joined point rows."""
    def __init__(self, device_id, start, seconds=ROLLUP_SECONDS):
        self.device_id = device_id
        self.start = int(start)
        self.seconds = int(seconds)
        self.count = self.fixed = self.valid = 0
        self.first = self.last = self.prev = None
        self.max_speed = None
        self.fix_counts = collections.Counter()
        self.metrics = {key:[None,None,0.0,0,None] for key in METRICS}
        self.quality_groups = collections.Counter()
        self.covered = self.mileage = self.moving = 0.0
        self.gaps = []
        self.segments = []
        self.current = None
        self.track = []
        self.route_break = True
        # Keep one preferred protocol per timestamp so GPCHC + GPCHCX pairs
        # cannot double-count the same vibration sample.  The map is bounded
        # by the current source bucket (60 s online; offline can promote to
        # the bounded 10/60/600/1800 s source levels).
        self.vibration_points = {}

    def add(self, row):
        raw = dict(row)
        q_version = raw.get('q_version')
        q_mask = raw.get('q_mask') or 0
        q_reasons = raw.get('q_reasons') or 0
        self.quality_groups[(q_version,q_mask,q_reasons)] += 1
        p = quality.project(raw)
        self.count += 1
        self.first = self.first or _endpoint(p)
        self.last = _endpoint(p)
        self.fixed += int(p['fix_mode'] in (4,8))
        self.valid += int(bool(p['valid_pos']))
        self.fix_counts[str(p['fix_mode'])] += 1
        if p['speed'] is not None:
            self.max_speed = max(self.max_speed or 0,p['speed']*3.6)
        for metric in METRICS:
            value = p[metric]
            if value is None:
                continue
            a = self.metrics[metric]
            a[0] = value if a[0] is None else min(a[0],value)
            a[1] = value if a[1] is None else max(a[1],value)
            a[2] += value
            a[3] += 1
            a[4] = value

        magnitude = _specific_force_magnitude(p)
        if magnitude is not None:
            previous = self.vibration_points.get(p['t'])
            if previous is None or p.get('protocol') == 'GPCHCX':
                self.vibration_points[p['t']] = (p.get('protocol'), magnitude)

        transition = _transition(self.prev,p)
        self.covered += transition['covered']
        self.mileage += transition['mileage']
        self.moving += transition['moving']
        if transition['gap']:
            self.gaps.append([self.prev['t'],p['t']])
        state = _motion_state(p)
        if not self.current or state != self.current['state'] or not transition['contiguous'] or transition['jump']:
            if self.current:
                self.segments.append(self.current)
            self.current = {'start':p['t'],'end':p['t'],'state':state,'distance_m':0,
                            'max_kmh':p['speed']*3.6 if p['speed'] is not None else None,
                            'break_before':not transition['contiguous'] or transition['jump']}
        else:
            self.current['end'] = p['t']
            if p['speed'] is not None:
                self.current['max_kmh'] = max(self.current['max_kmh'] or 0,p['speed']*3.6)
        self.current['distance_m'] += transition['mileage']

        if p['valid_pos']:
            pt = {key:p.get(key) for key in ['t','lat','lon','speed','heading','fix_mode','nav_mode','stationary_context']}
            pt['break_before'] = self.route_break
            if self.route_break or not self.track:
                self.track.append(pt)
            elif len(self.track)>1 and self.track[-1].get('tail'):
                self.track[-1] = dict(pt,tail=True)
            else:
                self.track.append(dict(pt,tail=True))
            self.route_break = transition['jump']
        elif self.prev and self.prev.get('valid_pos'):
            self.route_break = True
        self.prev = p

    def snapshot(self):
        if self.current:
            self.segments.append(self.current)
            self.current = None
        return dict(version=ROLLUP_VERSION,device_id=self.device_id,bucket_start=self.start,bucket_s=self.seconds,
                    count=self.count,first=self.first,last=self.last,fixed=self.fixed,valid=self.valid,
                    max_speed=self.max_speed,fix_counts=dict(self.fix_counts),metrics=self.metrics,
                    quality_groups=[[v if v is not None else None,m,r,n] for (v,m,r),n in self.quality_groups.items()],
                    covered_s=self.covered,mileage_m=self.mileage,moving_s=self.moving,
                    vibration=_vibration_stats(self.vibration_points.values()),
                    gaps=self.gaps,segments=self.segments,track=self.track)


class QueryCombiner:
    """Merge ordered raw/rollup snapshots into the public query projection."""
    def __init__(self, device_id, start, end, bins):
        self.device_id,self.start,self.end,self.bins = device_id,start,end,bins
        self.groups = {}
        self.count = self.fixed = self.valid = 0
        self.first = self.last = self.prev = None
        self.first_point = None
        self.max_speed = None
        self.fix_counts = collections.Counter()
        self.quality_groups = collections.Counter()
        self.covered = self.mileage = self.moving = 0.0
        self.gaps = []
        self.segments = []
        self.track = []
        self.track_truncated = False

    @staticmethod
    def _empty_vibration_stats():
        return [None, None, 0.0, 0.0, 0]

    def _key(self, t):
        return min(self.bins-1,max(0,int((t-self.start)/(self.end-self.start)*self.bins)))

    def _merge_metrics(self, snap):
        key = self._key((snap['first']['t']+snap['last']['t'])/2)
        group = self.groups.setdefault(key,{'t':snap['first']['t'],
                                            'values':{k:[None,None,0.0,0,None] for k in METRICS},
                                            'vibration':self._empty_vibration_stats()})
        group['t'] = min(group['t'],snap['first']['t'])
        for metric, source in snap['metrics'].items():
            target = group['values'][metric]
            lo,hi,total,n,last = source
            if n:
                target[0] = lo if target[0] is None else min(target[0],lo)
                target[1] = hi if target[1] is None else max(target[1],hi)
                target[2] += total
                target[3] += n
                target[4] = last
        source_vibration = snap.get('vibration') or self._empty_vibration_stats()
        target_vibration = group['vibration']
        lo, hi, total, squares, count = source_vibration
        if count:
            target_vibration[0] = lo if target_vibration[0] is None else min(target_vibration[0],lo)
            target_vibration[1] = hi if target_vibration[1] is None else max(target_vibration[1],hi)
            target_vibration[2] += total
            target_vibration[3] += squares
            target_vibration[4] += count

    def _merge_segments(self, snap, transition):
        parts = [dict(part) for part in snap['segments']]
        if not parts:
            return
        parts[0]['break_before'] = not transition['contiguous'] or transition['jump']
        parts[0]['distance_m'] += transition['mileage']
        for part in parts:
            if self.segments and not part.get('break_before') and self.segments[-1]['state'] == part['state']:
                previous = self.segments[-1]
                previous['end'] = part['end']
                previous['distance_m'] += part['distance_m']
                if part.get('max_kmh') is not None:
                    previous['max_kmh'] = max(previous.get('max_kmh') or 0,part['max_kmh'])
            else:
                self.segments.append(part)

    def _append_track(self, snap, transition):
        points = [dict(p) for p in snap['track']]
        if not points:
            return
        connectable = bool(snap['first'].get('valid_pos') and points[0]['t'] == snap['first']['t'])
        points[0]['break_before'] = not (connectable and transition['contiguous'] and not transition['jump'] and self.prev and self.prev.get('valid_pos'))
        for p in points:
            key = self._key(p['t'])
            if p.get('break_before') or not self.track or key != self.track[-1].get('_bucket'):
                self.track.append(dict(p,_bucket=key))
            elif len(self.track)>1 and self.track[-1].get('tail'):
                self.track[-1] = dict(p,_bucket=key,tail=True)
            else:
                self.track.append(dict(p,_bucket=key,tail=True))
        if len(self.track) > 12000:
            self.track_truncated = True

    def add(self, snap):
        if not snap or not snap.get('count'):
            return
        transition = _transition(self.prev,snap['first'])
        self.first = self.first or snap['first']['t']
        self.first_point = self.first_point or snap['first']
        self.last = snap['last']['t']
        self.count += snap['count']; self.fixed += snap['fixed']; self.valid += snap['valid']
        self.max_speed = max(self.max_speed or 0,snap['max_speed'] or 0) if snap['max_speed'] is not None else self.max_speed
        self.fix_counts.update(snap['fix_counts'])
        for version,mask,reasons,n in snap['quality_groups']:
            self.quality_groups[(version,mask,reasons)] += n
        self.covered += snap['covered_s'] + transition['covered']
        self.mileage += snap['mileage_m'] + transition['mileage']
        self.moving += snap['moving_s'] + transition['moving']
        if transition['gap']:
            self.gaps.append([self.prev['t'],snap['first']['t']])
        self.gaps.extend(snap['gaps'])
        self._merge_metrics(snap)
        self._merge_segments(snap,transition)
        self._append_track(snap,transition)
        self.prev = snap['last']

    def snapshot(self, start, seconds):
        """Return a mergeable derived bucket, used to build coarser levels."""
        values = self.groups.get(0,{'values':{k:[None,None,0.0,0,None] for k in METRICS}})['values']
        vibration = self.groups.get(0,{}).get('vibration',self._empty_vibration_stats())
        track = []
        for point in self.track:
            item = dict(point);item.pop('_bucket',None);item.pop('tail',None);track.append(item)
        return dict(version=ROLLUP_VERSION,device_id=self.device_id,bucket_start=int(start),bucket_s=int(seconds),
                    count=self.count,first=self.first_point,last=self.prev,fixed=self.fixed,valid=self.valid,
                    max_speed=self.max_speed,fix_counts=dict(self.fix_counts),metrics=values,
                    quality_groups=[[version,mask,reasons,n] for (version,mask,reasons),n in self.quality_groups.items()],
                    covered_s=self.covered,mileage_m=self.mileage,moving_s=self.moving,
                    vibration=vibration,
                    gaps=self.gaps,segments=self.segments,track=track)

    def quality_summary(self, contexts):
        reason_counts, field_counts = collections.Counter(),collections.Counter()
        excluded = anomalies = unavailable = pending = 0
        for (version,mask,reasons),n in self.quality_groups.items():
            if version != quality.VERSION:
                pending += n
                continue
            excluded += n if mask else 0
            anomalies += n if reasons & quality.ANOMALY_BITS else 0
            unavailable += n if reasons & quality.UNAVAILABLE_BITS else 0
            for key,bit in quality.REASON_BITS.items():
                if reasons & bit: reason_counts[key] += n
            for key,bit in quality.BITS.items():
                if mask & bit: field_counts[key] += n
        scopes = [s for s in contexts if s['start']<=self.end and quality.context_end(s)>=self.start]
        return dict(version=quality.VERSION,total=self.count,excluded_samples=excluded,anomaly_samples=anomalies,
                    unavailable_samples=unavailable,pending_samples=pending,excluded_fields=dict(field_counts),
                    reasons=[dict(code=k,label=label,category=kind,count=reason_counts[k]) for k,(label,kind) in quality.REASONS.items()],
                    contexts=scopes,policy='按字段隔离；原始值保留，空缺不补零、不插值；数值离群与状态不可用可在同一采样重叠')

    def finish(self, contexts, source, source_resolution_s, elapsed_ms):
        series = {key:[] for key in METRICS}
        vibration_series = []
        vibration_samples = 0
        vibration_sum = 0.0
        vibration_dynamic_squares = 0.0
        vibration_peak = None
        vibration_peak_to_peak = None
        for key in sorted(self.groups):
            group = self.groups[key]
            for metric,(lo,hi,total,n,last) in group['values'].items():
                mean = last if metric in ('heading','course') else total/n if n else None
                series[metric].append([group['t']*1000,mean,lo,hi])
            lo,hi,total,squares,count = group.get('vibration',self._empty_vibration_stats())
            if count:
                mean = total / count
                variance = max(0.0, squares / count - mean * mean)
                rms = math.sqrt(variance)
                peak = max(abs(lo - mean), abs(hi - mean))
                vibration_series.append([
                    round(group['t'] * 1000), round(rms, 7), round(peak, 7),
                    round(mean, 7), round(lo, 7), round(hi, 7), count,
                ])
                vibration_samples += count
                vibration_sum += total
                vibration_dynamic_squares += max(0.0, squares - total * total / count)
                vibration_peak = peak if vibration_peak is None else max(vibration_peak, peak)
                span = hi - lo
                vibration_peak_to_peak = span if vibration_peak_to_peak is None else max(vibration_peak_to_peak, span)
        track = []
        for p in self.track[:10000]:
            p = dict(p); p.pop('_bucket',None); p.pop('tail',None); track.append(p)
        segments = []
        for segment in self.segments:
            item = dict(segment); item.pop('break_before',None)
            if item['end']-item['start'] >= 30:
                segments.append(item)
        span = self.last-self.first if self.first is not None else 0
        range_metrics = {
            'rms_g': round(math.sqrt(vibration_dynamic_squares / vibration_samples), 7) if vibration_samples else None,
            'peak_g': round(vibration_peak, 7) if vibration_peak is not None else None,
            'peak_to_peak_g': round(vibration_peak_to_peak, 7) if vibration_peak_to_peak is not None else None,
            'mean_g': round(vibration_sum / vibration_samples, 7) if vibration_samples else None,
        }
        vibration_range = dict(
            available=bool(vibration_series), start=self.start, end=self.end,
            reason=None if vibration_series else ('所选时段无采样' if not self.count else '筛选时段没有包含三轴比力的有效值'),
            samples=vibration_samples, buckets=len(vibration_series),
            series_fields=['timestamp_ms','rms_g','peak_g','mean_g','min_g','max_g','count'],
            series=vibration_series,
            metrics=range_metrics,
            method='筛选时间内按等时桶统计三轴比力合成模长；RMS 为桶内去均值动态幅值，峰值为桶内极值偏差',
            source=source, source_resolution_s=source_resolution_s,
            capability='10 Hz 仅用于 0-4 Hz 低频载体振动观察；聚合曲线用于趋势和取值，不用于轴承、齿轮等高频故障诊断',
        )
        return dict(device_id=self.device_id,start=self.start,end=self.end,total=self.count,track=track,
                    gaps=self.gaps[:2000],track_truncated=self.track_truncated or len(self.track)>10000,
                    series=series,quality=self.quality_summary(contexts),segments=segments[:1000],
                    vibration_range=vibration_range,
                    summary=dict(first_t=self.first,last_t=self.last,distance_km=self.mileage/1000,moving_s=self.moving,
                                 covered_s=self.covered,max_kmh=self.max_speed,fixed_pct=100*self.fixed/self.count if self.count else 0,
                                 valid_pct=100*self.valid/self.count if self.count else 0,gap_count=len(self.gaps),
                                 fix_counts=dict(self.fix_counts),sample_hz=(self.count-1)/span if span else 0),
                    aggregation=dict(buckets=len(self.groups),bucket_s=(self.end-self.start)/self.bins,
                                     source=source,source_resolution_s=source_resolution_s,query_ms=round(elapsed_ms,1),cache_hit=False,
                                     method='先按字段过滤再计算等时桶均值/最小/最大；不插值；长窗口分层读取可重建的 60 秒或 10 分钟聚合，首尾读取原始采样；已确认静止区段不画漂移轨迹；剔除原值见数据质量',
                                     timezone='Asia/Shanghai',coordinates='WGS84 原始坐标；前端高德底图单独转换为 GCJ-02 展示',
                                     mileage='有效定位且连续速度≥3.6 km/h 时的速度梯形积分；缺测不外推，非 CAN 里程'))


def encode(snapshot):
    return json.dumps(snapshot,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def decode(payload):
    return json.loads(payload)
