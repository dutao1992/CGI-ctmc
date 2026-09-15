"""Deterministic threshold-event projection from quality-filtered samples."""
import collections
import json
import time

from . import quality
from .rules import (LEGACY_THRESHOLD_DEFAULTS, THRESHOLD_DEFAULTS,
                    THRESHOLD_EVENT_KINDS, THRESHOLD_POLICY_VERSION,
                    threshold_conditions)


VERSION = 2
META_KEY = 'threshold_event_projection'
_POLICY_KEYS = tuple(THRESHOLD_DEFAULTS)


def expected_receipt():
    return {'version': VERSION, 'quality_version': quality.VERSION,
            'threshold_policy': THRESHOLD_POLICY_VERSION}


def is_current(connection):
    row = connection.execute('SELECT value FROM meta WHERE key=?', (META_KEY,)).fetchone()
    if not row:
        return False
    try:
        receipt = json.loads(row[0])
    except (TypeError, ValueError):
        return False
    return all(receipt.get(key) == value for key, value in expected_receipt().items())


def _same_number(left, right):
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def migrate_default_rules_connection(connection):
    """Move untouched legacy defaults to the calibrated policy once.

    Custom device rules are deliberately left alone.  The migration only
    recognizes the exact defaults shipped before policy v2, records the
    change in the existing audit table, and invalidates the event receipt so
    the caller can rebuild the historical threshold projection.
    """
    changed = []
    rows = connection.execute('SELECT id,rules FROM devices ORDER BY id').fetchall()
    for row in rows:
        rules = json.loads(row['rules'])
        if int(rules.get('threshold_policy', 0)) >= THRESHOLD_POLICY_VERSION:
            continue
        if not all(_same_number(rules.get(key), value)
                   for key, value in LEGACY_THRESHOLD_DEFAULTS.items()):
            continue
        previous = dict(rules)
        rules.update(THRESHOLD_DEFAULTS)
        rules['threshold_policy'] = THRESHOLD_POLICY_VERSION
        rules['version'] = int(rules.get('version', 1)) + 1
        connection.execute('UPDATE devices SET rules=? WHERE id=?',
                           (json.dumps(rules, ensure_ascii=False), row['id']))
        connection.execute(
            'INSERT INTO audit(t,actor,action,target,detail) VALUES (?,?,?,?,?)',
            (time.time(), 'system/threshold-policy-v2', 'device.rules.migrate',
             row['id'], json.dumps(dict(previous=previous, current=rules), ensure_ascii=False)))
        changed.append(row['id'])
    if changed:
        connection.execute('DELETE FROM meta WHERE key=?', (META_KEY,))
        connection.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)',
                           ('query_cache_epoch', json.dumps(str(time.time_ns()))))
    return changed


def migrate_default_rules(store):
    """Apply the one-time default-policy migration under the ingest lock."""
    with store.ingestion_lock(), store.connect() as connection:
        return migrate_default_rules_connection(connection)


class Detector:
    """Stream episodes without retaining the full data set in memory."""
    def __init__(self, rules, mount_confirmed, emit):
        self.rules = rules
        self.mount_confirmed = mount_confirmed
        self.emit = emit
        self.previous = None
        self.history = collections.deque()
        self.active = {}

    def _finish(self, kind):
        entry = self.active.pop(kind, None)
        if not entry or entry['last'] - entry['start'] + 1e-6 < entry['dwell']:
            return
        self.emit(dict(kind=kind, severity=entry['severity'], start=entry['start'],
                       end=entry['last'], peak=entry['peak'], threshold=entry['threshold'],
                       samples=entry['samples'], rule_version=self.rules['version'],
                       point_t=entry['point_t']))

    def add(self, p):
        while self.history and self.history[0]['t'] < p['t'] - 1.5:
            self.history.popleft()
        baseline = next((item for item in self.history if p['t'] - item['t'] >= .8), None)
        signals = threshold_conditions(p, self.previous, baseline, self.rules, self.mount_confirmed)
        for kind in list(self.active):
            if kind not in signals or p['t'] < self.active[kind]['last'] or p['t'] - self.active[kind]['last'] > self.rules['gap_s']:
                self._finish(kind)
        for kind, (value, threshold, severity, dwell) in signals.items():
            entry = self.active.get(kind)
            if entry is None:
                entry = dict(start=p['t'], last=p['t'], peak=value, samples=0,
                             point_t=p['t'], threshold=threshold, severity=severity, dwell=dwell)
                self.active[kind] = entry
            entry['last'] = p['t']
            entry['samples'] += 1
            if value > entry['peak']:
                entry['peak'], entry['point_t'] = value, p['t']
        self.previous = p
        self.history.append(p)

    def finish(self):
        for kind in list(self.active):
            self._finish(kind)


def rebuild(store, progress=None, force=False):
    """Rebuild automatic threshold rows once per projection/quality version."""
    with store.ingestion_lock():
        with store.connect() as c:
            if not force and is_current(c):
                return 0
            devices = c.execute('SELECT id,rules,mount_confirmed FROM devices ORDER BY id').fetchall()
        written = 0
        for device in devices:
            sn = device['id']
            rules = json.loads(device['rules'])
            with store.connect() as c:
                high_water = c.execute("SELECT MAX(COALESCE((SELECT MAX(id) FROM events),0),COALESCE(CAST((SELECT value FROM meta WHERE key='event_id_floor') AS INTEGER),0))").fetchone()[0]
                c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('event_id_floor',?)", (str(high_water),))
                c.execute('DELETE FROM events WHERE device_id=? AND kind IN (' +
                          ','.join('?' for _ in THRESHOLD_EVENT_KINDS) + ')',
                          (sn, *THRESHOLD_EVENT_KINDS))
                next_id = c.execute("SELECT MAX(COALESCE((SELECT MAX(id) FROM events),0),COALESCE(CAST((SELECT value FROM meta WHERE key='event_id_floor') AS INTEGER),0))+1").fetchone()[0]

                def emit(event):
                    nonlocal next_id, written
                    c.execute('''INSERT INTO events(id,device_id,kind,severity,start,end,peak,threshold,samples,
                                 rule_version,point_t,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                              (next_id, sn, event['kind'], event['severity'], event['start'], event['end'],
                               event['peak'], event['threshold'], event['samples'], event['rule_version'],
                               event['point_t'], time.time()))
                    next_id += 1
                    written += 1

                detector = Detector(rules, bool(device['mount_confirmed']), emit)
                for row in c.execute(quality.JOIN + ' WHERE p.device_id=? ORDER BY p.t,p.protocol', (sn,)):
                    detector.add(quality.project(row))
                detector.finish()
            if progress:
                progress(dict(stage='threshold_events', device_id=sn, events=written))
        receipt = dict(expected_receipt(), events=written, rebuilt_at=time.time())
        store.meta(META_KEY, receipt)
        return written
