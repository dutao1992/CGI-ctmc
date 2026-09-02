import collections
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .protocol import FRAME, NUMERIC, parse, checksum, describe
from .rules import DEFAULTS, LABELS, conditions
from .maintenance import file_lock, disk_usage
from . import quality
from .aggregate import (METRICS, ROLLUP_LEVELS, ROLLUP_SECONDS, ROLLUP_VERSION, QueryCombiner,
                        RollupBuilder, bucket_start, decode as decode_rollup,
                        encode as encode_rollup)
from .vibration import MAX_GAP_S, MAX_WINDOW_S, MIN_SAMPLES, analyze as analyze_vibration, unavailable as vibration_unavailable


def _sum_existing_sizes(paths):
    total=0
    for path in paths:
        try: total += path.stat().st_size
        except FileNotFoundError: pass  # SQLite may remove transient -shm after globbing.
    return total

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS devices (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, vehicle TEXT NOT NULL DEFAULT '', fleet TEXT NOT NULL DEFAULT '',
 mount_confirmed INTEGER NOT NULL DEFAULT 0, rules TEXT NOT NULL, first_t REAL NOT NULL, last_t REAL NOT NULL,
 last_received REAL NOT NULL, point_count INTEGER NOT NULL DEFAULT 0, latest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cursors (
 path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER NOT NULL DEFAULT 0, size INTEGER DEFAULT 0,
 mtime REAL DEFAULT 0, counters TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
 start REAL NOT NULL, end REAL NOT NULL, peak REAL NOT NULL, threshold REAL NOT NULL,
 samples INTEGER NOT NULL, rule_version INTEGER NOT NULL, point_t REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', note TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL DEFAULT '',
 updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS event_range ON events(device_id,start,end);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, t REAL, actor TEXT, action TEXT, target TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS audit_action_target ON audit(action,target);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS retention_files (
 path TEXT PRIMARY KEY, inode INTEGER NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 status TEXT NOT NULL, started REAL NOT NULL, finished REAL,
 deleted_points INTEGER NOT NULL DEFAULT 0, tail_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS point_rollups (
 device_id TEXT NOT NULL, bucket_start INTEGER NOT NULL, bucket_s INTEGER NOT NULL,
 version INTEGER NOT NULL, point_count INTEGER NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(device_id,bucket_s,bucket_start)) WITHOUT ROWID;
'''

VIBRATION_RAW_SCAN_MAX_S = 6 * 3600
VIBRATION_ROLLUP_CANDIDATES = 24


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path):
        self.path = str(path)
        self._query_cache = collections.OrderedDict()
        self._cache_lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA auto_vacuum=INCREMENTAL')
            c.executescript(SCHEMA)
            cols = ','.join(f'{k} REAL' for k in NUMERIC)
            c.execute(f'''CREATE TABLE IF NOT EXISTS points (
                device_id TEXT NOT NULL,t REAL NOT NULL, protocol TEXT NOT NULL, week INTEGER,tow REAL,
                {cols}, status_text TEXT,fix_mode INTEGER,nav_mode INTEGER,warning INTEGER,valid_pos INTEGER,
                source TEXT,source_offset INTEGER,source_length INTEGER,ingested_at REAL,
                PRIMARY KEY(device_id,t,protocol)) WITHOUT ROWID''')
            c.execute('CREATE INDEX IF NOT EXISTS points_source ON points(source)')
            c.executescript(quality.SCHEMA)
            quality.ensure_schema(c)

    def ingestion_lock(self, blocking=True):
        return file_lock(self.path+'.ingest.lock', blocking)

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, factory=Connection)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=30000')
        return c

    def clear_query_cache(self, device_id=None, from_t=None):
        with self._cache_lock:
            if device_id is None:
                self._query_cache.clear()
                return
            for key in list(self._query_cache):
                _, sn, _, end, _ = key
                if sn == device_id and (from_t is None or end >= from_t):
                    self._query_cache.pop(key,None)

    def _cache_epoch(self, connection):
        row = connection.execute("SELECT value FROM meta WHERE key='query_cache_epoch'").fetchone()
        return row[0] if row else '0'

    def bump_query_cache_epoch(self, connection=None):
        if connection is None:
            with self.connect() as c:
                return self.bump_query_cache_epoch(c)
        value = str(time.time_ns())
        connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('query_cache_epoch',json.dumps(value)))
        self.clear_query_cache()
        return value

    def _cache_get(self, key):
        now = time.monotonic()
        with self._cache_lock:
            item = self._query_cache.get(key)
            if not item or item[0] <= now:
                self._query_cache.pop(key,None)
                return None
            self._query_cache.move_to_end(key)
            # Query results are immutable after insertion.  Copy only the two
            # dictionaries whose cache metadata changes; deep-copying ~1 MB of
            # chart arrays made a cache hit needlessly expensive.
            result = dict(item[1])
            result['aggregation'] = dict(item[1]['aggregation'])
        result['aggregation']['cache_hit'] = True
        result['aggregation']['cache_age_s'] = round(max(0,time.time()-item[2]),1)
        return result

    def _cache_put(self, key, result, ttl):
        with self._cache_lock:
            self._query_cache[key] = (time.monotonic()+ttl,result,time.time())
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > 16:
                self._query_cache.popitem(last=False)

    @staticmethod
    def _rollup_rows(connection, sn, start, end):
        return connection.execute(quality.JOIN+' WHERE p.device_id=? AND p.t>=? AND p.t<? ORDER BY p.t,p.protocol',(sn,start,end))

    @staticmethod
    def _write_rollup(connection, sn, start, seconds, snapshot):
        if snapshot['count']:
            connection.execute('INSERT OR REPLACE INTO point_rollups VALUES (?,?,?,?,?,?)',
                               (sn,start,seconds,ROLLUP_VERSION,snapshot['count'],encode_rollup(snapshot)))
        else:
            connection.execute('DELETE FROM point_rollups WHERE device_id=? AND bucket_s=? AND bucket_start=?',
                               (sn,seconds,start))

    def _rebuild_base_rollup(self, connection, sn, start):
        start = bucket_start(start)
        builder = RollupBuilder(sn,start)
        for row in self._rollup_rows(connection,sn,start,start+ROLLUP_SECONDS):
            builder.add(row)
        snapshot = builder.snapshot()
        self._write_rollup(connection,sn,start,ROLLUP_SECONDS,snapshot)
        return snapshot['count']

    def _rebuild_derived_rollup(self, connection, sn, start, seconds):
        start = bucket_start(start,seconds)
        rows = connection.execute('''SELECT * FROM point_rollups WHERE device_id=? AND bucket_s=?
                AND bucket_start>=? AND bucket_start<? ORDER BY bucket_start''',
                (sn,ROLLUP_SECONDS,start,start+seconds)).fetchall()
        if any(row['version'] != ROLLUP_VERSION for row in rows):
            connection.execute('DELETE FROM point_rollups WHERE device_id=? AND bucket_s=? AND bucket_start=?',
                               (sn,seconds,start))
            return 0
        combiner = QueryCombiner(sn,start,start+seconds,1)
        for row in rows:
            combiner.add(decode_rollup(row['payload']))
        snapshot = combiner.snapshot(start,seconds)
        self._write_rollup(connection,sn,start,seconds,snapshot)
        return snapshot['count']

    def rebuild_rollup_buckets(self, connection, sn, starts):
        """Rebuild changed minute buckets and each affected coarser parent once."""
        base_starts = sorted({bucket_start(start) for start in starts})
        for start in base_starts:
            self._rebuild_base_rollup(connection,sn,start)
        for seconds in ROLLUP_LEVELS[1:]:
            for start in sorted({bucket_start(value,seconds) for value in base_starts}):
                self._rebuild_derived_rollup(connection,sn,start,seconds)

    def rebuild_rollup_bucket(self, connection, sn, start):
        """Compatibility wrapper for callers that changed one minute bucket."""
        self.rebuild_rollup_buckets(connection,sn,[start])
        row = connection.execute('SELECT point_count FROM point_rollups WHERE device_id=? AND bucket_s=? AND bucket_start=?',
                                 (sn,ROLLUP_SECONDS,bucket_start(start))).fetchone()
        return row[0] if row else 0

    def rebuild_rollups(self):
        started = time.perf_counter()
        rows = []
        with self.ingestion_lock():
            with self.connect() as reader:
                current = builder = None
                for row in reader.execute(quality.JOIN+' ORDER BY p.device_id,p.t,p.protocol'):
                    key = (row['device_id'],bucket_start(row['t']))
                    if key != current:
                        if builder:
                            snapshot = builder.snapshot()
                            rows.append((current[0],current[1],ROLLUP_SECONDS,ROLLUP_VERSION,snapshot['count'],encode_rollup(snapshot)))
                        current = key
                        builder = RollupBuilder(key[0],key[1])
                    builder.add(row)
                if builder:
                    snapshot = builder.snapshot()
                    rows.append((current[0],current[1],ROLLUP_SECONDS,ROLLUP_VERSION,snapshot['count'],encode_rollup(snapshot)))
            base_rows = list(rows)
            for seconds in ROLLUP_LEVELS[1:]:
                derived = []
                current = combiner = None
                for row in base_rows:
                    key = (row[0],bucket_start(row[1],seconds))
                    if key != current:
                        if combiner:
                            snapshot = combiner.snapshot(current[1],seconds)
                            derived.append((current[0],current[1],seconds,ROLLUP_VERSION,snapshot['count'],encode_rollup(snapshot)))
                        current = key
                        combiner = QueryCombiner(key[0],key[1],key[1]+seconds,1)
                    combiner.add(decode_rollup(row[5]))
                if combiner:
                    snapshot = combiner.snapshot(current[1],seconds)
                    derived.append((current[0],current[1],seconds,ROLLUP_VERSION,snapshot['count'],encode_rollup(snapshot)))
                rows.extend(derived)
            with self.connect() as writer:
                writer.execute('DELETE FROM point_rollups')
                writer.executemany('INSERT INTO point_rollups VALUES (?,?,?,?,?,?)',rows)
                self.bump_query_cache_epoch(writer)
        levels = [dict(resolution_s=seconds,buckets=sum(r[2]==seconds for r in rows),
                       points=sum(r[4] for r in rows if r[2]==seconds)) for seconds in ROLLUP_LEVELS]
        return dict(version=ROLLUP_VERSION,levels=levels,duration_s=round(time.perf_counter()-started,3))

    @staticmethod
    def _rollup_status(connection, device_id=None):
        if device_id is None:
            raw = connection.execute('SELECT COALESCE(SUM(point_count),0) FROM devices').fetchone()[0]
            where, params = '', ()
        else:
            row = connection.execute('SELECT point_count FROM devices WHERE id=?',(device_id,)).fetchone()
            raw = row[0] if row else 0
            where, params = ' AND device_id=?', (device_id,)
        levels = []
        for seconds in ROLLUP_LEVELS:
            row = connection.execute('''SELECT COUNT(*),COALESCE(SUM(point_count),0),
                    COALESCE(SUM(version!=?),0) FROM point_rollups WHERE bucket_s=?'''+where,
                    (ROLLUP_VERSION,seconds,*params)).fetchone()
            levels.append(dict(resolution_s=seconds,buckets=row[0],points=row[1],invalid_buckets=row[2],
                               ready=row[1]==raw and row[2]==0))
        return dict(version=ROLLUP_VERSION,raw_points=raw,ready=all(level['ready'] for level in levels),levels=levels)

    @staticmethod
    def _snapshots(rows, sn, start, end, bins):
        builder = current = None
        for row in rows:
            key = min(bins-1,max(0,int((row['t']-start)/(end-start)*bins)))
            if key != current:
                if builder:
                    yield builder.snapshot()
                current = key
                builder = RollupBuilder(sn,int(row['t']),max(1,int((end-start)/bins)))
            builder.add(row)
        if builder:
            yield builder.snapshot()

    def devices(self):
        with self.connect() as c:
            rows = c.execute('SELECT * FROM devices ORDER BY last_t DESC').fetchall()
            out = []
            for row in rows:
                d = dict(row)
                latest = c.execute(quality.JOIN+' WHERE p.device_id=? ORDER BY p.t DESC LIMIT 1',(d['id'],)).fetchone()
                # Retained device metadata may outlive all its samples. Fail closed.
                d['latest'] = describe(quality.project(latest or json.loads(d['latest']), info=True))
                d['rules'] = json.loads(d['rules'])
                d['online'] = 0 <= time.time() - d['last_received'] < 30 and abs(time.time()-d['last_t']) < 60
                out.append(d)
        return out

    def probe(self):
        """Cheap liveness/readiness probe; detailed health remains an authenticated API."""
        with self.connect() as c:
            meta = {r['key']: json.loads(r['value']) for r in c.execute(
                "SELECT key,value FROM meta WHERE key IN ('heartbeat','error')")}
        heartbeat = meta.get('heartbeat',0)
        return dict(ok=bool(heartbeat and time.time()-heartbeat < 30 and not meta.get('error')),
                    heartbeat=heartbeat,error=meta.get('error'))

    def health(self):
        with self.connect() as c:
            meta = {r['key']: json.loads(r['value']) for r in c.execute('SELECT * FROM meta')}
            counters = collections.Counter()
            for r in c.execute('SELECT counters FROM cursors'):
                counters.update(json.loads(r[0]))
            counters.update(meta.get('retired_counters', {}))
            pending = c.execute('SELECT COALESCE(SUM(MAX(size-offset,0)),0) FROM cursors').fetchone()[0]
            rollup = self._rollup_status(c)
        heartbeat = meta.get('heartbeat', 0)
        return dict(ok=bool(heartbeat and time.time()-heartbeat < 30 and not meta.get('error')),
                    heartbeat=heartbeat, error=meta.get('error'), counters=dict(counters), pending_bytes=pending,
                    db_bytes=_sum_existing_sizes(Path(self.path).parent.glob(Path(self.path).name+'*')),
                    aggregation=rollup,
                    raw_retention='每小时检查，磁盘达 80% 清理最旧车载数据至 75%；保护最近 24 小时及活动文件',
                    retention=meta.get('retention'), disk=disk_usage(self.path),
                    leap_seconds=18, devices=len(self.devices()))

    def meta(self, key, value):
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def save_device(self, sn, data, actor):
        with self.connect() as c:
            row = c.execute('SELECT * FROM devices WHERE id=?', (sn,)).fetchone()
            if not row:
                raise ValueError('设备不存在')
            rules = json.loads(row['rules'])
            given = data.get('rules', {})
            bounds = {'speed_kmh':(5,200),'accel_ms2':(.5,15),'brake_ms2':(.5,15),'roll_deg':(3,60),
                      'pitch_deg':(3,60),'shock_g':(.1,5),'age_s':(1,120),'position_std_m':(.1,100),
                      'gap_s':(.3,60),'dwell_s':(.2,30)}
            for key, value in given.items():
                if key not in bounds:
                    raise ValueError('未知规则')
                number = float(value)
                lo, hi = bounds[key]
                if not math.isfinite(number) or not lo <= number <= hi:
                    raise ValueError(f'{key} 应在 {lo} 至 {hi} 之间')
                rules[key] = number
            mount = data.get('mount_confirmed', bool(row['mount_confirmed']))
            if not isinstance(mount, bool):
                raise ValueError('安装确认必须为布尔值')
            changed = any(rules[k] != json.loads(row['rules'])[k] for k in bounds) or int(mount) != row['mount_confirmed']
            rules['version'] += int(changed)
            values = [str(data.get(k,row[k])).strip()[:80] for k in ['name','vehicle','fleet']]
            if not values[0]:
                raise ValueError('设备名称不能为空')
            c.execute('UPDATE devices SET name=?,vehicle=?,fleet=?,mount_confirmed=?,rules=? WHERE id=?',
                      (*values,int(mount),json.dumps(rules),sn))
            c.execute('INSERT INTO audit(t,actor,action,target,detail) VALUES (?,?,?,?,?)',
                      (time.time(),actor,'device.update',sn,json.dumps(data,ensure_ascii=False)))
        return {'ok':True, 'rule_version':rules['version'], 'note':'新规则仅用于后续采样；历史事件保留原规则版本'}

    def review_event(self, event_id, data, actor):
        status = data.get('status')
        if status not in ('open','acknowledged','resolved'):
            raise ValueError('事件状态无效')
        note = str(data.get('note','')).strip()[:1000]
        if status == 'resolved' and not note:
            raise ValueError('关闭事件需要填写处置说明')
        with self.connect() as c:
            result = c.execute('UPDATE events SET status=?,note=?,actor=?,updated=? WHERE id=?',
                               (status,note,actor,time.time(),event_id))
            if not result.rowcount:
                raise ValueError('事件不存在')
            c.execute('INSERT INTO audit(t,actor,action,target,detail) VALUES (?,?,?,?,?)',
                      (time.time(),actor,'event.review',str(event_id),json.dumps(data,ensure_ascii=False)))
        return {'ok':True}

    def events(self, sn, start, end):
        # Historical measurement events are operational only if their peak is
        # still valid under the current filter. Status/transport diagnostics stay.
        event_fields = {'overspeed':['speed'],'acceleration':['speed'],'braking':['speed'],
                        'roll':['roll'],'pitch':['pitch'],'shock':['ax','ay','az'],'position_jump':['lat','lon']}
        cases = ' '.join("WHEN '"+kind+"' THEN "+str(sum(quality.BITS[k] for k in fields)) for kind,fields in event_fields.items())
        gate = f''' AND (e.kind NOT IN ({','.join('?' for _ in event_fields)}) OR EXISTS (
            SELECT 1 FROM point_quality q WHERE q.device_id=e.device_id AND q.t=e.point_t
            AND q.version={quality.VERSION} AND q.context_id IS NULL
            AND (q.mask & CASE e.kind {cases} ELSE 0 END)=0))'''
        params = (sn,end,start,*event_fields)
        with self.connect() as c:
            base = ' FROM events e WHERE e.device_id=? AND e.start<=? AND e.end>=?'+gate
            count = c.execute('SELECT COUNT(*)'+base, params).fetchone()[0]
            rows = c.execute('SELECT e.*'+base+' ORDER BY e.start DESC LIMIT 2000',params).fetchall()
        return dict(total=count, items=[dict(r, label=LABELS.get(r['kind'],r['kind'])) for r in rows], truncated=count>2000)

    def quality_summary(self, sn, start, end, connection=None):
        if connection is None:
            with self.connect() as c:
                return self.quality_summary(sn,start,end,c)
        c = connection
        reason_counts, field_counts = collections.Counter(), collections.Counter()
        total = excluded = anomalies = unavailable = status = pending = 0
        for r in c.execute('''SELECT q.version,q.mask,q.reasons,COUNT(*) n FROM points p LEFT JOIN point_quality q
                ON q.device_id=p.device_id AND q.t=p.t AND q.protocol=p.protocol
                WHERE p.device_id=? AND p.t BETWEEN ? AND ? GROUP BY q.version,q.mask,q.reasons''', (sn,start,end)):
            n = r['n']; total += n
            if r['version'] != quality.VERSION:
                pending += n
                continue
            mask, reasons = r['mask'], r['reasons']
            excluded += n if mask else 0
            anomalies += n if reasons & quality.ANOMALY_BITS else 0
            unavailable += n if reasons & quality.UNAVAILABLE_BITS else 0
            status += n if reasons & quality.STATUS_BITS else 0
            for key, bit in quality.REASON_BITS.items():
                if reasons & bit: reason_counts[key] += n
            for key, bit in quality.BITS.items():
                if mask & bit: field_counts[key] += n
        scopes = [s for s in quality.contexts(c,sn) if s['start']<=end and quality.context_end(s)>=start]
        return dict(version=quality.VERSION,total=total,excluded_samples=excluded,anomaly_samples=anomalies,
                    unavailable_samples=unavailable,status_samples=status,pending_samples=pending,excluded_fields=dict(field_counts),
                    reasons=[dict(code=k,label=label,category=kind,count=reason_counts[k]) for k,(label,kind) in quality.REASONS.items()],
                    contexts=scopes,
                    policy=f'运动/静止仅按三轴合成峰值偏差 |√(ax²+ay²+az²)-1|：≤ {quality.MOTION_IMPACT_THRESHOLD_G:.3f} g 为静止，> {quality.MOTION_IMPACT_THRESHOLD_G:.3f} g 为运行；导航初始化、定向未就绪、静止或低速航迹角只提示不剔除。仅隔离超过 {quality.MAX_VALID_VEHICLE_SPEED_KMH:.0f} km/h 的失真导航解和明显位置跳点，原始值保留')

    def quality_records(self, sn, start, end, reason='anomaly', offset=0, limit=50):
        if reason == 'anomaly': bits = quality.ANOMALY_BITS
        elif reason == 'unavailable': bits = quality.UNAVAILABLE_BITS
        elif reason == 'all': bits = quality.ANOMALY_BITS | quality.UNAVAILABLE_BITS
        elif reason in quality.REASON_BITS: bits = quality.REASON_BITS[reason]
        else: raise ValueError('未知过滤原因')
        offset = max(0,min(1_000_000,int(offset))); limit = max(1,min(100,int(limit)))
        args = (sn,start,end,bits)
        where = ' WHERE p.device_id=? AND p.t BETWEEN ? AND ? AND q.version='+str(quality.VERSION)+' AND q.mask!=0 AND (q.reasons & ?)!=0'
        with self.connect() as c:
            if c.execute('SELECT COUNT(*) FROM points WHERE device_id=? AND t BETWEEN ? AND ?', (sn,start,end)).fetchone()[0] > 1_000_000:
                raise ValueError('所选范围超过 100 万条采样，请缩小时间范围')
            count = c.execute('SELECT COUNT(*) FROM ('+quality.JOIN+where+')',args).fetchone()[0]
            rows = c.execute(quality.JOIN+where+' ORDER BY p.t DESC,p.protocol LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
            scopes = quality.contexts(c,sn)
        items = []
        with self.connect() as detail_connection:
            for row in rows:
                p = dict(row)
                context = quality.context_for(p,scopes)
                previous = detail_connection.execute(
                    'SELECT * FROM points WHERE device_id=? AND '
                    '(t<? OR (t=? AND protocol<?)) ORDER BY t DESC,protocol DESC LIMIT 1',
                    (p['device_id'],p['t'],p['t'],p['protocol'])).fetchone()
                _, _, details = quality.assess(p,context,explain=True,
                                               previous=dict(previous) if previous else None)
                items.append(dict(device_id=sn,t=p['t'],protocol=p['protocol'],version=p['q_version'],context_id=p['q_context'],
                                  details=details,raw_values={k:p[k] for k in quality.BITS if p['q_mask'] & quality.BITS[k]},
                                  source=p['source'],source_offset=p['source_offset']))
        return dict(total=count,offset=offset,limit=limit,items=items,has_more=offset+len(items)<count)

    def point(self, sn, t, protocol=None, raw=False):
        with self.connect() as c:
            row = c.execute(quality.JOIN+' WHERE p.device_id=? AND p.t=?'+(' AND p.protocol=?' if protocol else '')+' ORDER BY p.protocol LIMIT 1',
                            (sn,t,protocol) if protocol else (sn,t)).fetchone()
            if row is None: return None
            p = quality.project(row,info=True)
            if raw:
                original = dict(row)
                for key in ('q_version','q_context','q_mask','q_reasons','q_motion_state'): original.pop(key,None)
                original['quality'] = p['quality']
                previous = c.execute(
                    'SELECT * FROM points WHERE device_id=? AND '
                    '(t<? OR (t=? AND protocol<?)) ORDER BY t DESC,protocol DESC LIMIT 1',
                    (sn,original['t'],original['t'],original['protocol'])).fetchone()
                original['quality']['details'] = quality.assess(
                    original, quality.context_for(original,quality.contexts(c,sn)), True,
                    dict(previous) if previous else None)[2]
                original['filtered_values'] = {k:p[k] for k in NUMERIC}
                original['data_view'] = 'raw_evidence'
                return describe(original)
            p['data_view'] = 'filtered'
            return describe(p)

    @staticmethod
    def _vibration_rows(connection, sn, start, end):
        rows = connection.execute(
            quality.JOIN + ' WHERE p.device_id=? AND p.t>=? AND p.t<=? ORDER BY p.t,p.protocol',
            (sn, start, end),
        )
        # Multiple enabled protocols may share one timestamp. Prefer GPCHCX,
        # while still allowing a single GPCHC stream to use the same projection.
        by_time = {}
        for row in rows:
            point = quality.project(row)
            if not all(point.get(key) is not None for key in ('ax', 'ay', 'az')):
                continue
            current = by_time.get(point['t'])
            if current is None or point['protocol'] == 'GPCHCX':
                by_time[point['t']] = point
        return list(by_time.values())

    @staticmethod
    def _vibration_rollup_candidates(connection, sn, start, end):
        """Rank 60-second rollups, then let raw samples verify the winner.

        Long queries must not scan every raw row just to find a diagnostic
        window.  Rollup dynamic RMS provides a deterministic shortlist; raw
        samples around those buckets preserve the exact Hann FFT and gap rules.
        """
        rows = connection.execute('''SELECT bucket_start,payload,version FROM point_rollups
                WHERE device_id=? AND bucket_s=? AND bucket_start<? AND bucket_start+?>?
                ORDER BY bucket_start''',
                (sn, 60, end, 60, start)).fetchall()
        ranked = []
        for row in rows:
            if row['version'] != ROLLUP_VERSION:
                continue
            try:
                vibration = decode_rollup(row['payload']).get('vibration') or []
                lo, hi, total, squares, count = vibration
                if not count or count < MIN_SAMPLES or lo is None or hi is None:
                    continue
                mean = total / count
                rms = math.sqrt(max(0.0, squares / count - mean * mean))
                peak = max(abs(lo - mean), abs(hi - mean))
                ranked.append((rms, peak, int(row['bucket_start'])))
            except (TypeError, ValueError, KeyError):
                continue
        ranked.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
        selected = set()
        for _, _, bucket in ranked[:VIBRATION_ROLLUP_CANDIDATES]:
            # Neighbor buckets let the raw verifier evaluate a true sliding
            # 60-second window that straddles a rollup boundary.
            selected.update((bucket - 60, bucket, bucket + 60))
        if ranked:
            # Include both selection edges so a high-amplitude 60-second
            # window clipped by a custom range is still considered.
            edge_starts = {
                bucket_start(start), bucket_start(start) + 60,
                bucket_start(max(start, end - MAX_WINDOW_S)),
                bucket_start(max(start, end - MAX_WINDOW_S)) - 60,
            }
            selected.update(edge_starts)
        return sorted(value for value in selected if value < end + MAX_GAP_S and value + 60 > start - MAX_GAP_S)

    @staticmethod
    def _vibration(connection, sn, start, end):
        if end is None or end < start:
            return vibration_unavailable('所选时段无采样')
        span = end - start
        if span <= VIBRATION_RAW_SCAN_MAX_S:
            return analyze_vibration(Store._vibration_rows(connection, sn, start, end))
        candidate_starts = Store._vibration_rollup_candidates(connection, sn, start, end)
        if not candidate_starts:
            return vibration_unavailable('筛选时段没有可用于诊断的 60 秒振动聚合候选')
        samples = []
        for bucket in candidate_starts:
            samples.extend(Store._vibration_rows(
                connection, sn, max(start, bucket - MAX_GAP_S), min(end, bucket + MAX_WINDOW_S + MAX_GAP_S)
            ))
        result = analyze_vibration(samples)
        if result.get('available'):
            result.setdefault('selection', {})['rollup_candidates'] = len(candidate_starts)
        return result

    def query(self, sn, start, end, bins=700):
        if not math.isfinite(start+end) or end <= start or end-start > 31*86400:
            raise ValueError('请选择有效时间范围，单次不超过 31 天')
        bins = max(50,min(1500,int(bins)))
        started = time.perf_counter()
        with self.connect() as c:
            epoch = self._cache_epoch(c)
            cache_key = (epoch,sn,start,end,bins)
            cached = self._cache_get(cache_key)
            if cached is not None:
                cached['aggregation']['computed_query_ms'] = cached['aggregation'].get('query_ms')
                cached['aggregation']['query_ms'] = round((time.perf_counter()-started)*1000,1)
                return cached
            device_row = c.execute('SELECT last_t FROM devices WHERE id=?',(sn,)).fetchone()
            device_latest = device_row[0] if device_row else None
            contexts = quality.contexts(c,sn)
            combiner = QueryCombiner(sn,start,end,bins)
            source = 'raw'
            resolution = 0
            span = end-start
            resolution = 600 if span >= 2*86400 else ROLLUP_SECONDS if span >= 6*3600 else 0
            count = None
            if resolution:
                status = self._rollup_status(c,sn)
                level = next(item for item in status['levels'] if item['resolution_s']==resolution)
            else:
                level = None
            if resolution and level['ready']:
                full_start = int(math.ceil(start/resolution)*resolution)
                full_end = int(math.floor(end/resolution)*resolution)
                snapshots = []
                if start < full_start:
                    snapshots.extend(self._snapshots(c.execute(quality.JOIN+' WHERE p.device_id=? AND p.t>=? AND p.t<? ORDER BY p.t,p.protocol',(sn,start,full_start)),sn,start,end,bins))
                rollup_rows = c.execute('''SELECT * FROM point_rollups WHERE device_id=? AND bucket_s=?
                        AND bucket_start>=? AND bucket_start<? ORDER BY bucket_start''',(sn,resolution,full_start,full_end)).fetchall()
                rollup_ok = all(r['version']==ROLLUP_VERSION for r in rollup_rows)
                if rollup_ok:
                    snapshots.extend(decode_rollup(r['payload']) for r in rollup_rows)
                if full_end <= end:
                    snapshots.extend(self._snapshots(c.execute(quality.JOIN+' WHERE p.device_id=? AND p.t>=? AND p.t<=? ORDER BY p.t,p.protocol',(sn,full_end,end)),sn,start,end,bins))
                if rollup_ok:
                    source = 'rollup'
                    for snapshot in sorted(snapshots,key=lambda s:s['first']['t']):
                        combiner.add(snapshot)
                    count = combiner.count
            if count is None:
                count = c.execute('SELECT COUNT(*) FROM points WHERE device_id=? AND t>=? AND t<=?', (sn,start,end)).fetchone()[0]
                if resolution and count > 1_000_000:
                    raise ValueError('长时间范围的聚合索引尚未就绪，请稍后重试；原始采样未丢失')
                combiner = QueryCombiner(sn,start,end,bins)
                rows = c.execute(quality.JOIN+' WHERE p.device_id=? AND p.t>=? AND p.t<=? ORDER BY p.t,p.protocol',(sn,start,end))
                for snapshot in self._snapshots(rows,sn,start,end,bins):
                    combiner.add(snapshot)
                source = 'raw'
                resolution = 0
            vibration = self._vibration(c, sn, start, end)
        result = combiner.finish(contexts,source,resolution,(time.perf_counter()-started)*1000)
        vibration['range'] = result.pop('vibration_range')
        result['vibration'] = vibration
        result['events'] = self.events(sn,start,end)
        result['aggregation']['query_ms'] = round((time.perf_counter()-started)*1000,1)
        ttl = 10 if device_latest is not None and end >= device_latest-120 else 60
        self._cache_put(cache_key,result,ttl)
        return result


class Ingestor:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root)
        self.states = {}
        self.file_cache = {}
        self.quality_contexts = []

    def _event(self, c, p, state, kind, value, threshold, severity, dwell, rules):
        active = state['active']
        entry = active.get(kind)
        if not entry or p['t']-entry['last'] > rules['gap_s'] or p['t'] < entry['last']:
            entry = {'start':p['t'],'last':p['t'],'peak':value,'samples':0,'id':None,'point_t':p['t']}
            active[kind] = entry
        entry['last'] = p['t']
        entry['samples'] += 1
        if value > entry['peak']:
            entry['peak'], entry['point_t'] = value,p['t']
        if p['t']-entry['start']+1e-6 < dwell:
            return
        if entry['id']:
            c.execute('UPDATE events SET end=?,peak=?,samples=?,point_t=? WHERE id=?',
                      (p['t'],entry['peak'],entry['samples'],entry['point_t'],entry['id']))
        else:
            next_id = c.execute("SELECT MAX(COALESCE((SELECT MAX(id) FROM events),0),COALESCE(CAST((SELECT value FROM meta WHERE key='event_id_floor') AS INTEGER),0))+1").fetchone()[0]
            result = c.execute('INSERT INTO events(id,device_id,kind,severity,start,end,peak,threshold,samples,rule_version,point_t,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (next_id,p['device_id'],kind,severity,entry['start'],p['t'],entry['peak'],threshold,entry['samples'],rules['version'],entry['point_t'],time.time()))
            entry['id'] = result.lastrowid

    def _point(self, c, p, stat):
        sn = p['device_id']
        device_row = c.execute('SELECT * FROM devices WHERE id=?', (sn,)).fetchone()
        if not device_row:
            c.execute('INSERT INTO devices(id,name,rules,first_t,last_t,last_received,latest) VALUES (?,?,?,?,?,?,?)',
                      (sn,'CGI-430 · '+sn,json.dumps(DEFAULTS),p['t'],p['t'],stat.st_mtime,json.dumps(p)))
            device_row = c.execute('SELECT * FROM devices WHERE id=?',(sn,)).fetchone()
        rules = json.loads(device_row['rules'])
        # Read the preceding immutable sample before inserting the current
        # one.  It is used only for the conservative severe-jump check; the
        # tri-axis motion state itself is independent of navigation fields.
        previous = c.execute(
            'SELECT * FROM points WHERE device_id=? AND '
            '(t<? OR (t=? AND protocol<?)) ORDER BY t DESC,protocol DESC LIMIT 1',
            (sn, p['t'], p['t'], p['protocol'])).fetchone()
        keys = list(p)
        result = c.execute(f'INSERT OR IGNORE INTO points ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})',list(p.values()))
        if not result.rowcount:
            return False
        raw_point = p
        late = p['t'] < device_row['last_t']
        # Lifecycle contexts from v1-v4 are audit records only.  The current
        # rule is evaluated point-by-point and never opens a speed/position
        # based stationary context.
        mask, reasons, context_id = quality.write_assessment(
            c, p, self.quality_contexts, previous=dict(previous) if previous else None)
        p = quality.project(dict(p,q_version=quality.VERSION,q_mask=mask,q_reasons=reasons,q_context=context_id))
        if sn not in self.states or self.states[sn]['version'] != rules['version']:
            prev = c.execute(quality.JOIN+' WHERE p.device_id=? AND p.t<? ORDER BY p.t DESC LIMIT 1',(sn,p['t'])).fetchone()
            self.states[sn] = {'prev':quality.project(prev) if prev else None,'history':collections.deque(), 'active':{},'version':rules['version']}
            # Recover persisted episode identity after restart, never replay side effects.
            for e in c.execute('SELECT * FROM events WHERE device_id=? AND end>=? AND end<=? AND rule_version=?',
                               (sn,p['t']-rules['gap_s'],p['t'],rules['version'])):
                self.states[sn]['active'][e['kind']] = {'id':e['id'],'start':e['start'],'last':e['end'],'peak':e['peak'],'samples':e['samples'],'point_t':e['point_t']}
        state = self.states[sn]
        history = state['history']
        while history and history[0]['t'] < p['t']-1.5:
            history.popleft()
        baseline = next((x for x in history if p['t']-x['t']>=.8),None)
        signals = conditions(p,state['prev'],baseline,rules,bool(device_row['mount_confirmed']))
        for kind in list(state['active']):
            if kind not in signals:
                del state['active'][kind]
        for kind, (value,threshold,severity,dwell) in signals.items():
            self._event(c,p,state,kind,value,threshold,severity,dwell,rules)
        if state['prev'] is None or p['t'] >= state['prev']['t']:
            state['prev'] = p
            history.append(p)
        c.execute('UPDATE devices SET first_t=CASE WHEN point_count=0 THEN ? ELSE MIN(first_t,?) END,last_t=MAX(last_t,?),last_received=MAX(last_received,?),point_count=point_count+1,latest=CASE WHEN last_t<=? THEN ? ELSE latest END WHERE id=?',
                  (p['t'],p['t'],p['t'],stat.st_mtime,p['t'],json.dumps(raw_point),sn))
        return True

    def scan(self):
        with self.store.ingestion_lock(blocking=False) as acquired:
            if acquired:
                return self._scan()
        return 0

    def _scan(self):
        # Full discovery supports late/reconnected sessions and historical backfill. Never follows symlinks.
        inserted_total = 0
        with self.store.connect() as c:
            retired = {r[0] for r in c.execute('SELECT path FROM retention_files')}
            self.quality_contexts = quality.contexts(c)
        for path in sorted(self.root.glob('????-??-??/*.log')):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            rel = str(path.relative_to(self.root))
            if rel in retired:
                self.file_cache.pop(rel, None)
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if self.file_cache.get(rel) == fingerprint:
                continue
            with self.store.connect() as c:
                cur = c.execute('SELECT * FROM cursors WHERE path=?',(rel,)).fetchone()
                offset = cur['offset'] if cur and cur['inode']==stat.st_ino and cur['offset']<=stat.st_size else 0
                if offset == stat.st_size:
                    self.file_cache[rel] = fingerprint
                    continue
                counts = collections.Counter(json.loads(cur['counters'])) if cur else collections.Counter()
                with path.open('rb') as f:
                    f.seek(offset)
                    data = f.read(2*1024*1024)
                boundary = data.rfind(b'\n')+1
                if not boundary:
                    boundary = max(0,len(data)-8192)
                if not boundary:
                    self.file_cache[rel] = fingerprint
                    continue
                block = data[:boundary]
                matched = 0
                touched = collections.defaultdict(set)
                for match in FRAME.finditer(block):
                    frame = match.group(1)
                    matched += len(match.group(0))
                    name = frame.split(b',')[0].decode('ascii')
                    try:
                        p = parse(frame)
                        counts['valid_ascii'] += 1
                        counts['protocol:'+name] += 1
                        if p:
                            # Reject impossible device clocks; old historical data is allowed.
                            if p['t'] > time.time()+300 or p['t'] < 1262304000:
                                raise ValueError('clock_range')
                            p.update(source=rel,source_offset=offset+match.start(),source_length=len(frame),ingested_at=time.time())
                            inserted = self._point(c,p,stat)
                            counts['points' if inserted else 'duplicates'] += 1
                            if inserted:
                                inserted_total += 1
                                touched[p['device_id']].add(bucket_start(p['t']))
                        else:
                            counts['auxiliary_ascii'] += 1
                    except (ValueError,IndexError,OverflowError) as e:
                        counts['rejected'] += 1
                        counts['error:'+str(e)[:60]] += 1
                counts['unparsed_bytes'] += boundary-matched
                counts['read_bytes'] += boundary
                for sn, buckets in touched.items():
                    self.store.rebuild_rollup_buckets(c,sn,buckets)
                c.execute('INSERT OR REPLACE INTO cursors VALUES (?,?,?,?,?,?)',
                          (rel,stat.st_ino,offset+boundary,stat.st_size,stat.st_mtime,json.dumps(counts)))
            for sn,buckets in touched.items():
                self.store.clear_query_cache(sn,min(buckets))
            if offset+len(data) >= stat.st_size:
                self.file_cache[rel] = fingerprint
        with self.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('heartbeat',json.dumps(time.time())))
            c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('error',json.dumps(None)))
        return inserted_total
