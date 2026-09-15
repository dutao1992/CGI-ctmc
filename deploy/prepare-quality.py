"""Offline reversible migration; run with vehicle parser/timer stopped, receiver on."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle import quality, event_projection
from vehicle.store import Store, Connection
from vehicle.aggregate import ROLLUP_LEVELS, ROLLUP_VERSION
from vehicle.rules import THRESHOLD_EVENT_KINDS


def install_scope(connection, scope):
    historical = scope.get('profile', {}).get('historical_replay')
    if historical:
        return quality.install_context(connection,scope,'system/historical-replay',
                                       'quality.stationary_context.historical_replay')
    return quality.install_context(connection,scope)


def raw_digest(connection):
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute('SELECT * FROM points ORDER BY device_id,t,protocol'):
        digest.update(json.dumps(tuple(row),separators=(',',':')).encode())
        count += 1
    return count, digest.hexdigest()


def prepare(db, backup, context_paths):
    if not Path(db).is_file():
        raise ValueError('拒绝迁移不存在的数据库')
    if isinstance(context_paths, (str, Path)):
        context_paths = [context_paths]
    scopes = [json.loads(Path(path).read_text()) for path in context_paths]
    store = Store(db)
    policy_devices = event_projection.migrate_default_rules(store)
    # A code-only release must not create another full-size SQLite backup when
    # the immutable contexts and all quality decisions are already current.
    with store.connect() as c:
        c.execute('BEGIN')
        installed_contexts = [dict(id=scope['id'],installed=install_scope(c,scope)) for scope in scopes]
        pending = c.execute('SELECT COUNT(*) FROM ('+quality.JOIN+' WHERE q.version IS NULL OR q.version!=? OR g.estimate IS NULL)',(quality.VERSION,)).fetchone()[0]
        c.rollback()
    with store.connect() as c:
        events_current = event_projection.is_current(c)
    if not any(item['installed'] for item in installed_contexts) and not pending and events_current:
        return dict(version=quality.VERSION,skipped=True,reason='contexts_and_assessments_current',
                    contexts=installed_contexts,assessed=0,backup=None,finished_at=time.time())
    backup = Path(backup)
    if backup.exists():
        raise ValueError('拒绝覆盖已有备份')
    partial = backup.with_suffix(backup.suffix+'.partial')
    fd = os.open(partial,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.close(fd)
    source = sqlite3.connect('file:'+str(Path(db).resolve())+'?mode=ro',uri=True)
    target = sqlite3.connect(partial)
    try:
        source.backup(target)
        if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('备份完整性检查失败')
        before = raw_digest(target)
    finally:
        source.close();target.close()
    partial.rename(backup)
    started = time.time()
    installed_contexts = []
    with store.ingestion_lock(), store.connect() as c:
        for scope in scopes:
            existing = c.execute('SELECT 1 FROM quality_contexts WHERE id=?',(scope['id'],)).fetchone()
            if not existing:
                train_start = scope['profile'].get('training_start',scope['start'])
                train_end = scope['profile'].get('training_end',scope['end'])
                actual = c.execute('SELECT COUNT(*) FROM points WHERE device_id=? AND t BETWEEN ? AND ?',
                                   (scope['device_id'],train_start,train_end)).fetchone()[0]
                if actual != scope['profile']['population']:
                    raise ValueError(f'参考区间实际有 {actual} 条采样，与训练快照 {scope["profile"]["population"]} 条不一致；先核对再发布')
            installed_contexts.append(dict(id=scope['id'],installed=install_scope(c,scope)))
    def progress(event):
        print(json.dumps(dict(event,elapsed_s=round(time.time()-started,3))), flush=True)

    assessed = quality.backfill(store, progress=progress)
    threshold_events = event_projection.rebuild(
        store, progress=progress,
        force=bool(assessed or policy_devices or any(item['installed'] for item in installed_contexts)))
    with store.connect() as c:
        after = raw_digest(c)
        if after != before:
            raise ValueError('原始采样摘要变化，停止发布')
        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('迁移后完整性检查失败')
        pending = c.execute('SELECT COUNT(*) FROM ('+quality.JOIN+' WHERE q.version IS NULL OR q.version!=? OR g.estimate IS NULL)',(quality.VERSION,)).fetchone()[0]
        if pending:
            raise ValueError('仍有未判定采样')
    result = dict(version=quality.VERSION,backup=str(backup),contexts=installed_contexts,assessed=assessed,
                  threshold_events=threshold_events,
                  threshold_policy_devices=policy_devices,
                  raw_count=before[0],raw_sha256=before[1],raw_unchanged=True,
                  duration_s=round(time.time()-started,3),finished_at=time.time())
    store.meta('quality_migration',result)
    return result


def copy_snapshot(source_db, db, progress=None):
    """Hold one WAL read snapshot so heartbeat writes cannot restart copying."""
    if not Path(source_db).is_file():
        raise ValueError('源数据库不存在')
    fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    print(json.dumps(dict(stage='snapshot',source=str(source_db),destination=str(db))), flush=True)
    with closing(sqlite3.connect('file:'+str(Path(source_db).resolve())+'?mode=ro',uri=True)) as source:
        source.execute('BEGIN')
        source.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()
        with closing(sqlite3.connect(db)) as target:
            source.backup(target, pages=256, sleep=.05, progress=progress)


def stage(source_db, db, backup, context_paths):
    """Compute on a consistent snapshot while the old API remains available."""
    # Preserve the code-only deployment fast path without making a full copy.
    scopes = [json.loads(Path(path).read_text()) for path in context_paths]
    with closing(sqlite3.connect(Path(source_db).resolve().as_uri()+'?mode=ro',uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'points','point_quality','point_ground_speed','quality_contexts','point_rollups','devices'} <= tables:
            installed = all(c.execute('SELECT 1 FROM quality_contexts WHERE id=?',(scope['id'],)).fetchone() for scope in scopes)
            if installed:
                for scope in scopes:
                    install_scope(c, scope)  # Existing contexts validate without writes.
                pending = c.execute(quality.JOIN+' WHERE q.version IS NULL OR q.version!=? OR g.estimate IS NULL LIMIT 1',
                                    (quality.VERSION,)).fetchone()
                if pending is None and Store._rollup_status(c)['ready'] and event_projection.is_current(c):
                    return dict(skipped=True,reason='quality_and_rollups_current',version=quality.VERSION)
    copy_snapshot(source_db, db, progress=lambda *_: time.sleep(.01))
    print(json.dumps(dict(stage='prepare_snapshot')), flush=True)
    return prepare(db, backup, context_paths)


def install_derived(db, prepared, restore=False, before_install=None):
    """Atomically replace only derived tables; refuse stale/incompatible raw data.

    The parser must be stopped at cutover. A failed/stale install rolls back
    without changing raw points, cursors, devices, events, or annotations.
    """
    if not Path(db).is_file() or not Path(prepared).is_file():
        raise ValueError('数据库不存在')
    if Path(db).resolve() == Path(prepared).resolve():
        raise ValueError('拒绝从同一个数据库安装')
    store = Store(db)
    # URI mode must be enabled on the main connection too: SQLite 3.26 does
    # not recognize a read-only ATTACH URI on a plain-path connection.
    with store.ingestion_lock(), sqlite3.connect(
            Path(db).resolve().as_uri()+'?mode=rw', uri=True,
            timeout=30, factory=Connection) as c:
        c.row_factory = sqlite3.Row
        c.execute('ATTACH DATABASE ? AS prepared',
                  ('file:'+str(Path(prepared).resolve())+'?mode=ro',))
        # A deferred transaction permits a read-only attached source on SQLite
        # 3.26. The ingestion lock and stopped parser protect the live target.
        c.execute('BEGIN')
        # Reserve only the writable main database, not the read-only ATTACH.
        # WAL readers (the old query API) remain available during verification.
        c.execute("UPDATE main.meta SET value=value WHERE key='query_cache_epoch'")
        policy_devices = [] if restore else event_projection.migrate_default_rules_connection(c)
        print(json.dumps(dict(stage='verify_raw_before_cutover')), flush=True)
        with closing(sqlite3.connect('file:'+str(Path(prepared).resolve())+'?mode=ro',uri=True)) as source:
            expected = raw_digest(source)
        if raw_digest(c) != expected:
            raise ValueError('原始数据与预计算快照不一致，拒绝切换；旧版数据未修改')
        count = expected[0]
        for table in ('point_quality', 'point_ground_speed'):
            rows, bad = c.execute(f'SELECT COUNT(*),COALESCE(SUM(version!=?),0) FROM prepared.{table}',
                                  (quality.VERSION,)).fetchone()
            if not restore and (rows != count or bad):
                raise ValueError('预计算质量版本或记录数不完整')
        for seconds in ROLLUP_LEVELS:
            points, bad = c.execute('SELECT COALESCE(SUM(point_count),0),COALESCE(SUM(version!=?),0) '
                                   'FROM prepared.point_rollups WHERE bucket_s=?',
                                   (ROLLUP_VERSION,seconds)).fetchone()
            if not restore and (points != count or bad):
                raise ValueError('预计算聚合不完整')
        # Contexts are immutable manifests, not the user's editable records.
        if not restore:
            for scope in c.execute('SELECT * FROM prepared.quality_contexts').fetchall():
                existing = c.execute('SELECT * FROM quality_contexts WHERE id=?',(scope['id'],)).fetchone()
                if existing and tuple(existing) != tuple(scope):
                    raise ValueError('质量上下文已变化，拒绝安装旧快照')
                if not existing:
                    c.execute('INSERT INTO quality_contexts SELECT * FROM prepared.quality_contexts WHERE id=?',(scope['id'],))
        if before_install:
            before_install()
        deadline = time.monotonic()+180
        c.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
        print(json.dumps(dict(stage='install_derived',raw_count=count)), flush=True)
        for table in ('point_quality', 'point_ground_speed', 'point_rollups'):
            c.execute(f'DELETE FROM {table}')
            c.execute(f'INSERT INTO {table} SELECT * FROM prepared.{table}')
        event_floor = c.execute("SELECT MAX(COALESCE((SELECT MAX(id) FROM events),0),COALESCE(CAST((SELECT value FROM meta WHERE key='event_id_floor') AS INTEGER),0),COALESCE(CAST((SELECT value FROM prepared.meta WHERE key='event_id_floor') AS INTEGER),0))").fetchone()[0]
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('event_id_floor',?)", (str(event_floor),))
        placeholders = ','.join('?' for _ in THRESHOLD_EVENT_KINDS)
        c.execute(f'DELETE FROM events WHERE kind IN ({placeholders})', THRESHOLD_EVENT_KINDS)
        c.execute(f'INSERT INTO events SELECT * FROM prepared.events WHERE kind IN ({placeholders})', THRESHOLD_EVENT_KINDS)
        event_receipt = c.execute('SELECT value FROM prepared.meta WHERE key=?',
                                  (event_projection.META_KEY,)).fetchone()
        if not restore and not event_receipt:
            raise ValueError('阈值事件预计算不完整')
        if event_receipt:
            c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                      (event_projection.META_KEY, event_receipt[0]))
        else:
            c.execute('DELETE FROM meta WHERE key=?', (event_projection.META_KEY,))
        entry = c.execute("SELECT value FROM prepared.meta WHERE key='quality_migration'").fetchone()
        if restore:
            if entry:
                c.execute("INSERT OR REPLACE INTO meta VALUES ('quality_migration',?)",(entry[0],))
            else:
                c.execute("DELETE FROM meta WHERE key='quality_migration'")
        else:
            # A stage may finish all derived tables before its final reporting
            # step times out. Record the checks actually performed at install,
            # never inherit an older version's migration receipt.
            receipt = json.loads(entry[0]) if entry else {}
            if receipt.get('version') != quality.VERSION:
                receipt = {}
            receipt.update(version=quality.VERSION,raw_count=count,raw_sha256=expected[1],
                           raw_unchanged=True,prepared=str(prepared),installed_at=time.time())
            c.execute("INSERT OR REPLACE INTO meta VALUES ('quality_migration',?)",(json.dumps(receipt),))
        store.bump_query_cache_epoch(c)
    return dict(installed=True,restored=restore,raw_count=count,raw_sha256=expected[1],raw_unchanged=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--backup')
    parser.add_argument('--context',action='append',default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--stage-from',help='Online source; --db must be a new isolated snapshot path')
    mode.add_argument('--install-from',help='Install verified derived tables after stopping the parser')
    mode.add_argument('--restore-from',help='Restore old derived tables with identical raw data')
    parser.add_argument('--online-cutover',action='store_true',help='Keep old query API until checks pass, then stop it before installing')
    args = parser.parse_args()
    if args.install_from or args.restore_from:
        def stop_service():
            print(json.dumps(dict(stage='stop_query_service',at=time.time())), flush=True)
            subprocess.run(['systemctl','stop','ctmc-vehicle.service'],check=True)
        result = install_derived(args.db,args.install_from or args.restore_from,bool(args.restore_from),
                                 stop_service if args.online_cutover else None)
    elif not args.backup:
        parser.error('--backup is required for preparation')
    elif args.stage_from:
        result = stage(args.stage_from,args.db,args.backup,args.context)
    else:
        result = prepare(args.db,args.backup,args.context)
    print(json.dumps(result,ensure_ascii=False),flush=True)
