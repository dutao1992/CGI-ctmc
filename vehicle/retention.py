"""Oldest-first vehicle retention. No forced disk override exists in the CLI.

A durable tombstone precedes unlink, so a crash never reimports retired files.
Raw deletion and small SQL batches resume independently. Receiver is never stopped.
"""
import argparse
import collections
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .maintenance import disk_usage, file_lock
from .store import Store

DB = '/srv/ctmc-vehicle/data/vehicle.sqlite'
RAW = '/srv/chcnav-cgi430/logs/raw'
CONFIG = '/etc/ctmc-vehicle-retention.json'
DEFAULT_POLICY = dict(trigger_pct=80, target_pct=75, protect_hours=24, max_run_seconds=600)


def policy_checked(given):
    if set(given)-set(DEFAULT_POLICY):
        raise ValueError('unknown retention policy key')
    policy = dict(DEFAULT_POLICY, **given)
    if not (1 <= policy['target_pct'] < policy['trigger_pct'] <= 95):
        raise ValueError('invalid disk thresholds')
    if not (24 <= policy['protect_hours'] <= 8760 and 10 <= policy['max_run_seconds'] <= 1800):
        raise ValueError('invalid protection or runtime limit')
    return policy


def receiver_open_inodes():
    """Fail closed if the known receiver cannot be inspected, including fd permissions."""
    opened, found = set(), False
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = (proc/'cmdline').read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if b'/srv/chcnav-cgi430/' not in command or b'/src/server.js' not in command:
            continue
        found = True
        try:
            for fd in (proc/'fd').iterdir():
                try:
                    s = fd.stat()
                    opened.add((s.st_dev, s.st_ino))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            raise RuntimeError('receiver exited during open-file inspection')
    if not found:
        raise RuntimeError('cannot verify receiver open files; refusing raw deletion')
    return opened


def prepare_incremental(store, backup):
    """One-time offline preparation. Caller stops only the vehicle parser, never TCP."""
    with store.ingestion_lock():
        with store.connect() as c:
            if c.execute('PRAGMA auto_vacuum').fetchone()[0] == 2:
                return {'changed':False}
            size = Path(store.path).stat().st_size
            if disk_usage(store.path)['available_bytes'] < 4*size+256*1024*1024:
                raise RuntimeError('insufficient headroom for backup and VACUUM migration')
            if Path(backup).exists():
                raise ValueError('backup already exists')
            with closing(sqlite3.connect(str(backup))) as dest:
                c.backup(dest)
                if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('backup integrity failed')
            c.execute('PRAGMA auto_vacuum=INCREMENTAL')
            c.execute('VACUUM')
            if c.execute('PRAGMA auto_vacuum').fetchone()[0] != 2 or c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('incremental migration failed')
        return {'changed':True, 'backup':str(backup)}


class Retention:
    def __init__(self, store, root, policy=None, *, usage=disk_usage, opened=receiver_open_inodes, clock=time.time):
        self.store, self.root = store, Path(root)
        self.policy = policy_checked(policy or {})
        self.usage, self.opened, self.clock = usage, opened, clock
        if self.root.is_symlink() or self.root.resolve() != self.root.absolute() or not self.root.is_dir():
            raise ValueError('raw root must be an existing canonical directory')
        if self.root.stat().st_dev != Path(store.path).stat().st_dev:
            raise ValueError('raw and database must be on the same monitored filesystem')
        self.deadline = 0

    def _path(self, rel):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}/[^/]+\.log', rel):
            raise ValueError('outside raw file allowlist')
        dt.date.fromisoformat(rel[:10])
        p = self.root/rel
        if p.parent.is_symlink() or p.is_symlink() or p.resolve() != p:
            raise ValueError('symlink or noncanonical raw path')
        return p

    def _safe(self, rel, cutoff, opened):
        p = self._path(rel)
        s = p.stat()
        # Both UTC and Shanghai current day are protected, regardless of receiver TZ.
        protected_day = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).date().isoformat()
        if rel[:10] >= protected_day or s.st_mtime >= cutoff:
            return None
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_dev != self.root.stat().st_dev:
            return None
        if (s.st_dev, s.st_ino) in opened:
            return None
        with self.store.connect() as c:
            cur = c.execute('SELECT * FROM cursors WHERE path=?', (rel,)).fetchone()
            if cur and (cur['inode'] != s.st_ino or cur['size'] != s.st_size or cur['mtime'] != s.st_mtime):
                return None
            remaining = s.st_size-(cur['offset'] if cur else 0)
            if remaining < 0 or remaining > 8192:
                return None
            # A small closed tail with no newline is the receiver's unclosed fragment.
            if remaining:
                with p.open('rb') as f:
                    f.seek(s.st_size-remaining)
                    if b'\n' in f.read(8192):
                        return None
            if c.execute('SELECT 1 FROM points WHERE source=? AND (t>=? OR ingested_at>=?) LIMIT 1',
                         (rel,cutoff,cutoff)).fetchone():
                return None
        return s, remaining

    def _reclaim(self):
        with self.store.ingestion_lock():
            with self.store.connect() as c:
                # SQLite 3.26 on the deployed host releases only one page per statement
                # here; newer runtimes release up to N. Check actual freelist progress.
                remaining = 512
                free = c.execute('PRAGMA freelist_count').fetchone()[0]
                while remaining > 0 and free:
                    c.execute(f'PRAGMA incremental_vacuum({remaining})').fetchall()
                    after = c.execute('PRAGMA freelist_count').fetchone()[0]
                    if after >= free:
                        raise RuntimeError('incremental vacuum made no progress')
                    remaining -= free-after
                    free = after
                c.commit()
                result = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if result[0]:
                    raise RuntimeError('WAL reader blocked space reclamation; retry next hour')

    def _retire(self, rel, cutoff):
        with self.store.ingestion_lock():
            with self.store.connect() as c:
                row = c.execute('SELECT * FROM retention_files WHERE path=?', (rel,)).fetchone()
            if not row:
                safe = self._safe(rel, cutoff, self.opened())
                if safe is None:
                    return False
                s, tail = safe
                with self.store.connect() as c:
                    c.execute('INSERT INTO retention_files(path,inode,size,mtime_ns,status,started,tail_bytes) VALUES (?,?,?,?,?,?,?)',
                              (rel,s.st_ino,s.st_size,s.st_mtime_ns,'pending',self.clock(),tail))
                    c.execute('INSERT INTO audit(t,actor,action,target,detail) VALUES (?,?,?,?,?)',
                              (self.clock(),'system.retention','retention.begin',rel,json.dumps(dict(bytes=s.st_size,tail_bytes=tail,policy=self.policy))))
                row = dict(inode=s.st_ino,size=s.st_size,mtime_ns=s.st_mtime_ns,status='pending')
            if row['status'] == 'pending':
                p = self._path(rel)
                if p.exists():
                    safe = self._safe(rel, cutoff, self.opened())
                    if safe is None:
                        raise RuntimeError('pending raw file is no longer safe to remove: '+rel)
                    s, _ = safe
                    if (s.st_ino,s.st_size,s.st_mtime_ns) != (row['inode'],row['size'],row['mtime_ns']):
                        raise RuntimeError('pending raw identity changed: '+rel)
                    # Directory fd + O_NOFOLLOW avoids following a swapped parent symlink.
                    fd = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        check = os.stat(p.name,dir_fd=fd,follow_symlinks=False)
                        if (check.st_ino,check.st_size,check.st_mtime_ns) != (s.st_ino,s.st_size,s.st_mtime_ns):
                            raise RuntimeError('raw changed before unlink')
                        os.unlink(p.name, dir_fd=fd)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                with self.store.connect() as c:
                    c.execute("UPDATE retention_files SET status='raw_deleted' WHERE path=?", (rel,))
        # Yield ingestion between transactions. A partial file is resumed even below threshold.
        while time.monotonic() < self.deadline:
            with self.store.ingestion_lock():
                with self.store.connect() as c:
                    rows = c.execute('SELECT device_id,t,protocol FROM points WHERE source=? LIMIT 2000', (rel,)).fetchall()
                    if not rows:
                        c.execute("UPDATE retention_files SET status='done',finished=? WHERE path=?", (self.clock(),rel))
                        cur = c.execute('SELECT counters FROM cursors WHERE path=?',(rel,)).fetchone()
                        old = c.execute("SELECT value FROM meta WHERE key='retired_counters'").fetchone()
                        counts = collections.Counter(json.loads(old[0]) if old else {})
                        if cur: counts.update(json.loads(cur[0]))
                        c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('retired_counters',json.dumps(counts)))
                        c.execute('DELETE FROM cursors WHERE path=?', (rel,))
                        c.execute('INSERT INTO audit(t,actor,action,target,detail) VALUES (?,?,?,?,?)',
                                  (self.clock(),'system.retention','retention.complete',rel,'{}'))
                        break
                    counts = collections.Counter(r['device_id'] for r in rows)
                    rollup_buckets = collections.defaultdict(set)
                    for row in rows:
                        rollup_buckets[row['device_id']].add(int(row['t']//60*60))
                    c.executemany('DELETE FROM point_quality WHERE device_id=? AND t=? AND protocol=?', [tuple(r) for r in rows])
                    c.executemany('DELETE FROM point_ground_speed WHERE device_id=? AND t=? AND protocol=?', [tuple(r) for r in rows])
                    c.executemany('DELETE FROM points WHERE device_id=? AND t=? AND protocol=?', [tuple(r) for r in rows])
                    for sn,starts in rollup_buckets.items():
                        self.store.rebuild_rollup_buckets(c,sn,starts)
                    self.store.bump_query_cache_epoch(c)
                    c.execute('UPDATE retention_files SET deleted_points=deleted_points+? WHERE path=?', (len(rows),rel))
                    for sn,n in counts.items():
                        first = c.execute('SELECT t FROM points WHERE device_id=? ORDER BY t LIMIT 1',(sn,)).fetchone()
                        c.execute('UPDATE devices SET point_count=MAX(0,point_count-?),first_t=COALESCE(?,last_t) WHERE id=?',
                                  (n,first[0] if first else None,sn))
                        # Keep cross-boundary episode summaries and all operator audit rows.
                        boundary = min(cutoff,first[0]) if first else cutoff
                        # Never reuse event ids referenced by durable operator audit rows.
                        c.execute("INSERT OR REPLACE INTO meta(key,value) SELECT 'event_id_floor',CAST(MAX(COALESCE((SELECT MAX(id) FROM events),0),COALESCE(CAST((SELECT value FROM meta WHERE key='event_id_floor') AS INTEGER),0)) AS TEXT)")
                        c.execute('DELETE FROM events WHERE device_id=? AND end<?', (sn,boundary))
            self._reclaim()
            time.sleep(.02)
        self._reclaim()
        with self.store.connect() as c:
            return c.execute('SELECT status FROM retention_files WHERE path=?',(rel,)).fetchone()[0] == 'done'

    def run(self, apply=False):
        now = self.clock()
        cutoff = now-self.policy['protect_hours']*3600
        self.deadline = time.monotonic()+self.policy['max_run_seconds']
        before = self.usage(self.store.path)
        result = dict(checked_at=now,mode='apply' if apply else 'dry_run',policy=self.policy,
                      before=before,after=before,status='below_threshold',files_completed=0,
                      deleted_points=0,raw_bytes_retired=0,warning=None)
        with file_lock(self.store.path+'.retention.lock',blocking=False) as acquired:
            if not acquired:
                return dict(result,status='already_running')
            with self.store.connect() as c:
                pending = [r[0] for r in c.execute("SELECT path FROM retention_files WHERE status!='done' ORDER BY path")]
                start_counts = c.execute('SELECT COALESCE(SUM(deleted_points),0),COALESCE(SUM(CASE WHEN status!=\'pending\' THEN size ELSE 0 END),0) FROM retention_files').fetchone()
                if apply and c.execute('PRAGMA auto_vacuum').fetchone()[0] != 2:
                    raise RuntimeError('incremental vacuum migration required before enabling retention')
                previous = c.execute("SELECT value FROM meta WHERE key='retention'").fetchone()
                active = bool(previous and json.loads(previous[0]).get('pressure_active'))
            try:
                # Exercise this guard even below threshold: permission regressions must
                # surface now, not only on the first high-pressure deletion attempt.
                opened = self.opened()
                if pending or before['used_pct'] >= self.policy['trigger_pct'] or (active and before['used_pct'] > self.policy['target_pct']):
                    result['status'] = 'cleaning' if apply else 'would_clean'
                    if apply:
                        for rel in pending:
                            if time.monotonic() >= self.deadline: break
                            result['files_completed'] += int(self._retire(rel,cutoff))
                        # Finish outstanding reclamation before considering any new raw files.
                        self._reclaim()
                    opened = self.opened()
                    candidates = []
                    with self.store.connect() as c:
                        retired = {r[0] for r in c.execute('SELECT path FROM retention_files')}
                    for p in self.root.glob('????-??-??/*.log'):
                        if time.monotonic() >= self.deadline: break
                        rel = str(p.relative_to(self.root))
                        if rel in retired: continue
                        try:
                            safe = self._safe(rel,cutoff,opened)
                        except (ValueError,FileNotFoundError):
                            continue
                        if safe:
                            candidates.append((rel[:10],safe[0].st_mtime,rel,safe[0].st_size))
                    candidates.sort()
                    result['eligible_files'] = len(candidates)
                    result['eligible_raw_bytes'] = sum(x[3] for x in candidates)
                    for _,_,rel,_ in candidates:
                        if not apply or time.monotonic() >= self.deadline or self.usage(self.store.path)['used_pct'] <= self.policy['target_pct']:
                            break
                        result['files_completed'] += int(self._retire(rel,cutoff))
                        time.sleep(.02)
                    result['after'] = self.usage(self.store.path)
                    if apply:
                        result['status'] = 'target_reached' if result['after']['used_pct'] <= self.policy['target_pct'] else 'pressure_remaining'
                        if result['status'] == 'pressure_remaining':
                            result['warning'] = '旧车载数据不足、受保护或本轮时间预算耗尽；仍高于目标，需检查或扩容。未清理其他数据。'
            except Exception as e:
                result.update(status='error',warning=str(e)[:500],after=self.usage(self.store.path))
            if apply:
                with self.store.connect() as c:
                    end_counts = c.execute('SELECT COALESCE(SUM(deleted_points),0),COALESCE(SUM(CASE WHEN status!=\'pending\' THEN size ELSE 0 END),0) FROM retention_files').fetchone()
                    result['deleted_points'] = end_counts[0]-start_counts[0]
                    result['raw_bytes_retired'] = end_counts[1]-start_counts[1]
                    result['pending_files'] = c.execute("SELECT COUNT(*) FROM retention_files WHERE status!='done'").fetchone()[0]
                    result['total_files_retired'] = c.execute('SELECT COUNT(*) FROM retention_files').fetchone()[0]
                result['finished_at'] = self.clock()
                result['pressure_active'] = bool((active or pending or before['used_pct'] >= self.policy['trigger_pct'])
                                                and result['after']['used_pct'] > self.policy['target_pct']
                                                and result['status'] in ('pressure_remaining','error'))
                result['filesystem_used_delta_bytes'] = before['used_bytes']-result['after']['used_bytes']
                self.store.meta('retention',result)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=CONFIG)
    parser.add_argument('--apply',action='store_true',help='Allow deletion, only at configured disk threshold')
    parser.add_argument('--prepare-backup',help='Prepare incremental vacuum offline; backup destination must not exist')
    args = parser.parse_args()
    if not Path(DB).is_file():
        raise SystemExit('vehicle database missing; refusing to create an empty replacement')
    store = Store(DB)
    if args.prepare_backup:
        print(json.dumps(prepare_incremental(store,args.prepare_backup),ensure_ascii=False))
        return
    policy = json.loads(Path(args.config).read_text())
    result = Retention(store,RAW,policy).run(apply=args.apply)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if result['status'] in ('error','pressure_remaining'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
