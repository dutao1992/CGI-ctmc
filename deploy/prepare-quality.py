"""Offline reversible migration; run with vehicle parser/timer stopped, receiver on."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle import quality
from vehicle.store import Store


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
    # A code-only release must not create another full-size SQLite backup when
    # the immutable contexts and all quality decisions are already current.
    with store.connect() as c:
        c.execute('BEGIN')
        installed_contexts = [dict(id=scope['id'],installed=install_scope(c,scope)) for scope in scopes]
        pending = c.execute('SELECT COUNT(*) FROM ('+quality.JOIN+' WHERE q.version IS NULL OR q.version!=?)',(quality.VERSION,)).fetchone()[0]
        c.rollback()
    if not any(item['installed'] for item in installed_contexts) and not pending:
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
    assessed = quality.backfill(store)
    with store.connect() as c:
        after = raw_digest(c)
        if after != before:
            raise ValueError('原始采样摘要变化，停止发布')
        if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('迁移后完整性检查失败')
        pending = c.execute('SELECT COUNT(*) FROM ('+quality.JOIN+' WHERE q.version IS NULL OR q.version!=?)',(quality.VERSION,)).fetchone()[0]
        if pending:
            raise ValueError('仍有未判定采样')
    result = dict(version=quality.VERSION,backup=str(backup),contexts=installed_contexts,assessed=assessed,
                  raw_count=before[0],raw_sha256=before[1],raw_unchanged=True,
                  duration_s=round(time.time()-started,3),finished_at=time.time())
    store.meta('quality_migration',result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--backup',required=True)
    parser.add_argument('--context',required=True,action='append')
    args = parser.parse_args()
    print(json.dumps(prepare(args.db,args.backup,args.context),ensure_ascii=False))
