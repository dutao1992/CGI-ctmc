"""Rebuild and verify derived minute rollups while the parser is stopped."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from vehicle.aggregate import ROLLUP_LEVELS, ROLLUP_VERSION
from vehicle.store import Store


def prepare(db):
    path = Path(db)
    if not path.is_file():
        raise ValueError('拒绝为不存在的数据库创建空聚合')
    store = Store(path)
    with store.connect() as c:
        raw_before = c.execute('SELECT COUNT(*) FROM points').fetchone()[0]
        devices_before = c.execute('SELECT COALESCE(SUM(point_count),0) FROM devices').fetchone()[0]
        status_before = store._rollup_status(c)
        check_before = c.execute('PRAGMA quick_check').fetchone()[0]
    if raw_before == devices_before and status_before['ready'] and check_before == 'ok':
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
    args = parser.parse_args()
    print(json.dumps(prepare(args.db),ensure_ascii=False))
