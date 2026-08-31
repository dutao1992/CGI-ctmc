"""Rebuild and verify derived minute rollups while the parser is stopped."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle.aggregate import ROLLUP_LEVELS, ROLLUP_VERSION
from vehicle.store import Store


def prepare(db, verify_current=False):
    path = Path(db)
    if not path.is_file():
        raise ValueError('拒绝为不存在的数据库创建空聚合')
    store = Store(path)
    with store.connect() as c:
        raw_before = c.execute('SELECT COUNT(*) FROM points').fetchone()[0]
        devices_before = c.execute('SELECT COALESCE(SUM(point_count),0) FROM devices').fetchone()[0]
        status_before = store._rollup_status(c)
        if raw_before != devices_before:
            raise ValueError(f'设备累计点数不一致 raw={raw_before} devices={devices_before}，拒绝重建派生聚合')
        current = status_before['ready']
        check_before = c.execute('PRAGMA quick_check').fetchone()[0] if current and verify_current else 'not_run_current_rollups'
    if current:
        if verify_current and check_before != 'ok':
            raise ValueError(f'数据库完整性检查失败 quick_check={check_before}')
        return dict(version=ROLLUP_VERSION,skipped=True,reason='all_levels_current',raw_points=raw_before,
                    verified_levels=status_before['levels'],quick_check=check_before)
    result = store.rebuild_rollups()
    with store.connect() as c:
        raw = c.execute('SELECT COUNT(*) FROM points').fetchone()[0]
        levels = []
        for seconds in ROLLUP_LEVELS:
            row = c.execute('''SELECT COUNT(*),COALESCE(SUM(point_count),0),
                    COALESCE(SUM(version!=?),0) FROM point_rollups WHERE bucket_s=?''',
                    (ROLLUP_VERSION,seconds)).fetchone()
            levels.append(dict(resolution_s=seconds,buckets=row[0],points=row[1],invalid_buckets=row[2]))
        check = c.execute('PRAGMA quick_check').fetchone()[0]
    if any(level['points'] != raw or level['invalid_buckets'] for level in levels) or check != 'ok':
        raise ValueError(f'聚合核验失败 raw={raw} levels={levels} quick_check={check}')
    return dict(result,raw_points=raw,verified_levels=levels,quick_check=check)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('--verify-current',action='store_true',help='当前聚合完整时仍执行耗时的全库 quick_check')
    args = parser.parse_args()
    print(json.dumps(prepare(args.db,args.verify_current),ensure_ascii=False))
