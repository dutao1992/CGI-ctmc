"""Bounded, non-persistent analysis of filtered telemetry CSV exports."""
import collections
import csv
import gzip
import io
import math
import re
import time

from .aggregate import QueryCombiner, RollupBuilder
from .protocol import NUMERIC, gps_to_unix
from .rules import DEFAULTS, LABELS, conditions
from . import quality


EXPORT_FIELDS = (['device_id','t','protocol','week','tow'] + NUMERIC +
                 ['status_text','nav_mode','fix_mode','warning','source','source_offset','data_view',
                  'filter_version','excluded_fields','filter_reasons','stationary_context'])
MAX_BYTES = 12 * 1024 * 1024 * 1024
MAX_GZIP_BYTES = 1024 * 1024 * 1024
MAX_ROWS = 30_000_000
MAX_SPAN = 31 * 86400
SOURCE_LEVELS = (1,10,60,600,1800)
MAX_SOURCE_BUCKETS = 1600
csv.field_size_limit(1024 * 1024)


class _LimitedRaw(io.RawIOBase):
    """Expose exactly one HTTP request body without owning its socket."""
    def __init__(self, source, remaining):
        self.source,self.remaining = source,remaining

    def readable(self): return True

    def readinto(self, buffer):
        if self.remaining <= 0: return 0
        size = min(len(buffer),self.remaining)
        chunk = self.source.read(size)
        if not chunk: return 0
        buffer[:len(chunk)] = chunk
        self.remaining -= len(chunk)
        return len(chunk)


def _reader(source, compressed=False):
    binary = gzip.GzipFile(fileobj=source,mode='rb') if compressed else source
    text = io.TextIOWrapper(binary,encoding='utf-8-sig',errors='strict',newline='')
    reader = csv.DictReader(text)
    if reader.fieldnames != EXPORT_FIELDS:
        raise ValueError('离线表格列名或顺序与“导出有效数据”不一致，请使用原始 CSV 文件')
    return text,reader


def _finite(value, field, required=False):
    value = (value or '').strip()
    if not value:
        if required:
            raise ValueError(f'{field} 不能为空')
        return None
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f'{field} 不是有效数字') from None
    if not math.isfinite(number):
        raise ValueError(f'{field} 不能是 NaN 或无穷值')
    return number


def _integer(value, field, lo=None, hi=None):
    number = _finite(value,field,True)
    if number != int(number):
        raise ValueError(f'{field} 必须是整数')
    number = int(number)
    if lo is not None and not lo <= number <= hi:
        raise ValueError(f'{field} 超出允许范围')
    return number


def _point(row, line):
    protocol = (row['protocol'] or '').strip()
    if protocol not in ('GPCHC','GPCHCX'):
        raise ValueError(f'第 {line} 行协议类型无效')
    if (row['data_view'] or '').strip() != 'filtered':
        raise ValueError(f'第 {line} 行不是有效数据 filtered 视图')
    version = _integer(row['filter_version'],f'第 {line} 行 filter_version')
    if version != quality.VERSION:
        raise ValueError(f'第 {line} 行过滤版本 v{version} 与当前 v{quality.VERSION} 不一致，请重新导出')
    excluded = {value for value in (row['excluded_fields'] or '').split('|') if value}
    unknown = excluded-set(NUMERIC)
    if unknown:
        raise ValueError(f'第 {line} 行包含未知剔除字段：{sorted(unknown)[0]}')
    reason_codes = {value for value in (row['filter_reasons'] or '').split('|') if value}
    unknown = reason_codes-set(quality.REASON_BITS)
    if unknown:
        raise ValueError(f'第 {line} 行包含未知过滤原因：{sorted(unknown)[0]}')
    p = dict(device_id=(row['device_id'] or '').strip(),t=_finite(row['t'],'t',True),protocol=protocol,
             week=_integer(row['week'],'week',0,8191),tow=_finite(row['tow'],'tow',True),
             status_text=(row['status_text'] or '').strip(),nav_mode=_integer(row['nav_mode'],'nav_mode',0,3),
             fix_mode=_integer(row['fix_mode'],'fix_mode',0,9),warning=_integer(row['warning'],'warning',0,65535))
    for key in NUMERIC:
        p[key] = _finite(row[key],f'第 {line} 行 {key}')
        if key in excluded and p[key] is not None:
            raise ValueError(f'第 {line} 行 {key} 已标记剔除但仍有数值，不是有效数据导出格式')
    if p['tow'] is None or not 0 <= p['tow'] < 604800:
        raise ValueError(f'第 {line} 行 GPS 周秒越界')
    if abs(gps_to_unix(p['week'],p['tow'])-p['t']) > .002:
        raise ValueError(f'第 {line} 行 GPS 周/周秒与 UTC 时间不一致')
    if p['lat'] is not None and not -90 <= p['lat'] <= 90:
        raise ValueError(f'第 {line} 行纬度越界')
    if p['lon'] is not None and not -180 <= p['lon'] <= 180:
        raise ValueError(f'第 {line} 行经度越界')
    if p['speed'] is not None and not 0 <= p['speed'] <= 300:
        raise ValueError(f'第 {line} 行速度越界')
    if p['age'] is not None and p['age'] < 0:
        raise ValueError(f'第 {line} 行差分延迟不能为负数')
    p.update(valid_pos=int(p['fix_mode'] != 0 and p['lat'] is not None and p['lon'] is not None and
                           (p['lat'] != 0 or p['lon'] != 0)),
             q_version=version,q_context=(row['stationary_context'] or '').strip() or None,
             q_mask=sum(quality.BITS[key] for key in excluded),
             q_reasons=sum(quality.REASON_BITS[key] for key in reason_codes))
    return p


class EventDetector:
    def __init__(self, rules, mount_confirmed):
        self.rules,self.mount_confirmed = rules,mount_confirmed
        self.previous = None
        self.history = collections.deque()
        self.active = {}
        self.items = collections.deque(maxlen=2000)
        self.total = self.next_id = 0

    def add(self, raw):
        p = quality.project(raw)
        while self.history and self.history[0]['t'] < p['t']-1.5:
            self.history.popleft()
        baseline = next((item for item in self.history if p['t']-item['t']>=.8),None)
        signals = conditions(p,self.previous,baseline,self.rules,self.mount_confirmed)
        for kind in list(self.active):
            if kind not in signals: del self.active[kind]
        for kind,(value,threshold,severity,dwell) in signals.items():
            entry = self.active.get(kind)
            if not entry or p['t']-entry['last'] > self.rules['gap_s']:
                entry = dict(start=p['t'],last=p['t'],peak=value,samples=0,point_t=p['t'],event=None)
                self.active[kind] = entry
            entry['last']=p['t'];entry['samples']+=1
            if value > entry['peak']:
                entry['peak']=value;entry['point_t']=p['t']
            if p['t']-entry['start']+1e-6 < dwell:
                continue
            if entry['event'] is None:
                self.next_id += 1;self.total += 1
                entry['event'] = dict(id=self.next_id,device_id=p['device_id'],kind=kind,label=LABELS.get(kind,kind),
                    severity=severity,start=entry['start'],end=p['t'],peak=entry['peak'],threshold=threshold,
                    samples=entry['samples'],rule_version=self.rules['version'],point_t=entry['point_t'],
                    status='offline',note='',actor='',updated=p['t'])
                self.items.append(entry['event'])
            else:
                entry['event'].update(end=p['t'],peak=entry['peak'],samples=entry['samples'],point_t=entry['point_t'])
        self.previous = p
        self.history.append(p)

    def finish(self):
        return dict(total=self.total,items=list(reversed(self.items)),truncated=self.total>len(self.items),
                    interpretation='由离线有效数据按当前规则重新计算的候选事件，不含在线处置状态')


def _promote(snapshots, device, seconds):
    """Merge completed source buckets to a coarser bounded representation."""
    output=[];combiner=None;current=None
    for snapshot in snapshots:
        key=int(snapshot['bucket_start']//seconds*seconds)
        if key != current:
            if combiner: output.append(combiner.snapshot(current,seconds))
            current=key;combiner=QueryCombiner(device,key,key+seconds,1)
        combiner.add(snapshot)
    if combiner:output.append(combiner.snapshot(current,seconds))
    return output


def analyze_stream(source, byte_size, rule_resolver=None, mount_mode='auto', compressed=False,
                   require_complete=True):
    """Parse once from a binary stream and retain only 10-minute rollups.

    A 10 Hz, 31-day file contains about 26.8 million rows. Keeping raw rows or
    reading the request body into bytes would exhaust the service; fixed source
    rollups make memory proportional to time span instead of sample count.
    """
    limit = MAX_GZIP_BYTES if compressed else MAX_BYTES
    if not 0 < byte_size <= limit:
        kind = 'CSV.GZ' if compressed else 'CSV'
        raise ValueError(f'离线 {kind} 文件为空或超过允许大小')
    if mount_mode not in ('auto','confirmed','unconfirmed'):
        raise ValueError('安装确认选项无效')
    began = time.perf_counter();text = None
    limited = _LimitedRaw(source,byte_size)
    source = io.BufferedReader(limited,buffer_size=1024*1024)
    count = 0;device = None;first = last = previous = None
    rules = dict(DEFAULTS);mount_confirmed = False;rule_source = '默认工程规则'
    events = None;builder = None;current = None;snapshots = [];source_level=0
    try:
        text,reader = _reader(source,compressed)
        for line,row in enumerate(reader,2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'第 {line} 行字段数量与表头不一致')
            count += 1
            if count > MAX_ROWS:
                raise ValueError(f'离线表格超过 {MAX_ROWS:,} 行，请按设备或月份分文件分析')
            sn = (row['device_id'] or '').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',sn):
                raise ValueError(f'第 {line} 行设备 SN 无效')
            if device is None:
                device = sn
                if rule_resolver:
                    resolved = rule_resolver(device)
                    if resolved: rules,mount_confirmed,rule_source = resolved
                if mount_mode == 'confirmed': mount_confirmed = True
                elif mount_mode == 'unconfirmed': mount_confirmed = False
                events = EventDetector(rules,mount_confirmed)
            elif sn != device:
                raise ValueError('一个离线文件只能包含一台设备，请按 SN 分文件分析')
            p = _point(row,line);t = p['t']
            if t <= 0 or t >= time.time()+86400:
                raise ValueError(f'第 {line} 行设备时间超出允许范围')
            if previous is not None and t < previous:
                raise ValueError(f'第 {line} 行时间倒序；请使用平台原始导出顺序')
            first = t if first is None else first;last = previous = t
            if last-first > MAX_SPAN:
                raise ValueError('离线表格时间跨度超过 31 天，请按月份分段分析')
            source_seconds=SOURCE_LEVELS[source_level]
            key = int(t//source_seconds*source_seconds)
            if key != current:
                if builder:
                    snapshots.append(builder.snapshot())
                    if len(snapshots)>MAX_SOURCE_BUCKETS and source_level<len(SOURCE_LEVELS)-1:
                        source_level+=1;source_seconds=SOURCE_LEVELS[source_level]
                        snapshots=_promote(snapshots,device,source_seconds)
                    key=int(t//source_seconds*source_seconds)
                current = key;builder = RollupBuilder(device,key,source_seconds)
            builder.add(p);events.add(p)
        if not count: raise ValueError('离线表格没有数据行')
        if builder: snapshots.append(builder.snapshot())
        if require_complete and limited.remaining:
            raise ValueError('离线文件上传不完整，请重新选择原文件上传')
    except (UnicodeDecodeError,gzip.BadGzipFile,EOFError):
        raise ValueError('离线表格不是有效的 UTF-8 CSV 或 CSV.GZ') from None
    except csv.Error as e:
        raise ValueError('CSV 结构无效：'+str(e)[:100]) from None
    finally:
        if text is not None: text.close()
    span = max(.001,last-first)
    bins = 320 if span>7*86400 else 420 if span>86400 else 520 if span>6*3600 else 420
    end = last if last>first else first+.001
    combiner = QueryCombiner(device,first,end,bins)
    for snapshot in snapshots: combiner.add(snapshot)
    source_seconds=SOURCE_LEVELS[source_level]
    result = combiner.finish([], 'offline_csv_gzip' if compressed else 'offline_csv',source_seconds,
                             (time.perf_counter()-began)*1000)
    result['events'] = events.finish()
    result['offline'] = dict(format='CTMC filtered telemetry CSV',bytes=byte_size,rows=count,compressed=compressed,
        rule_source=rule_source,rule_version=rules['version'],mount_confirmed=bool(mount_confirmed),
        persisted=False,limits=dict(max_bytes=limit,max_rows=MAX_ROWS,max_span_days=31))
    result['aggregation']['query_ms'] = round((time.perf_counter()-began)*1000,1)
    result['aggregation']['method'] = f'离线有效数据按三轴合成峰值偏差（≤ {quality.MOTION_IMPACT_THRESHOLD_G:.3f} g 静止，> {quality.MOTION_IMPACT_THRESHOLD_G:.3f} g 运行）和时间桶计算；不补零、不插值；轨迹、区段和候选事件使用当前在线同口径算法'
    return result


def analyze_csv(payload, rule_resolver=None, mount_mode='auto'):
    if not payload: raise ValueError('请选择需要解析的 CSV 文件')
    return analyze_stream(io.BytesIO(payload),len(payload),rule_resolver,mount_mode,False)
