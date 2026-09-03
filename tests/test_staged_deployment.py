"""Exercise production staging/cutover/rollback on real temporary databases."""
import sqlite3
import json
import unittest
from pathlib import Path
import subprocess

import test_vehicle as fixtures
from vehicle import quality


class StagedDeploymentTests(unittest.TestCase):
    setUp = fixtures.StoreTests.setUp
    tearDown = fixtures.StoreTests.tearDown
    ingest = fixtures.StoreTests.ingest

    def test_stage_failure_restores_old_services_retention_timer(self):
        source = (Path(__file__).resolve().parents[1]/'deploy/deploy-service.sh').read_text()
        rollback = source[source.index('rollback() {'):source.index('trap rollback ERR')]
        # Exercise the actual shell function with service actions stubbed; no
        # systemd command or production path is touched by this local test.
        script = '''nginx_changed=0; cutover=0; derived_changed=0; previous=/old
systemctl() { printf 'service-action:%s\\n' "$*"; return 0; }
'''+rollback+'\nrollback\n'
        result = subprocess.run(['bash','-c',script], capture_output=True, text=True, check=True)
        self.assertIn('service-action:enable --now ctmc-vehicle-retention.timer', result.stdout)
        self.assertNotIn('service-action:stop ctmc-vehicle.service', result.stdout)

    def prepared(self):
        self.ingest(*fixtures.LIVE)
        with self.store.connect() as c:
            c.execute('UPDATE point_quality SET version=6')
            c.execute('DELETE FROM point_ground_speed')
            c.execute('UPDATE point_rollups SET version=607')
        module = fixtures.DeployPreparationTests.module('prepare-quality.py')
        candidate, backup = self.root/'candidate.sqlite', self.root/'before.sqlite'
        module.stage(self.store.path, str(candidate), backup, [])
        return module, candidate, backup

    def test_stage_install_and_restore_preserve_live_annotations_and_raw(self):
        module, candidate, backup = self.prepared()
        with self.store.connect() as c:
            raw = module.raw_digest(c)
            self.assertEqual(c.execute('SELECT MIN(version) FROM point_quality').fetchone()[0], 6)
            c.execute("INSERT INTO audit(t,actor,action,target,detail) VALUES (1,'user','review','event','keep after snapshot')")
        result = module.install_derived(self.store.path, candidate)
        self.assertTrue(result['raw_unchanged'])
        with self.store.connect() as c:
            self.assertEqual(module.raw_digest(c), raw)
            self.assertEqual(c.execute('SELECT MIN(version) FROM point_quality').fetchone()[0], quality.VERSION)
            self.assertEqual(c.execute("SELECT detail FROM audit WHERE actor='user'").fetchone()[0], 'keep after snapshot')
        self.assertTrue(module.install_derived(self.store.path, backup, restore=True)['restored'])
        with self.store.connect() as c:
            self.assertEqual(module.raw_digest(c), raw)
            self.assertEqual(c.execute('SELECT MIN(version) FROM point_quality').fetchone()[0], 6)
            self.assertEqual(c.execute("SELECT detail FROM audit WHERE actor='user'").fetchone()[0], 'keep after snapshot')

    def test_stale_or_incomplete_preparation_is_rejected_without_modification(self):
        module, candidate, backup = self.prepared()
        with sqlite3.connect(candidate) as c:
            c.execute('DELETE FROM point_ground_speed')
        with self.assertRaisesRegex(ValueError, '不完整'):
            module.install_derived(self.store.path, candidate)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT MIN(version) FROM point_quality').fetchone()[0], 6)

            c.execute('UPDATE points SET speed=speed+1 WHERE t=(SELECT MIN(t) FROM points)')
        with self.assertRaisesRegex(ValueError, '原始数据'):
            module.install_derived(self.store.path, backup, restore=True)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT MIN(version) FROM point_quality').fetchone()[0], 6)

    def test_online_snapshot_does_not_restart_when_source_changes(self):
        self.ingest(*fixtures.LIVE)
        module = fixtures.DeployPreparationTests.module('prepare-quality.py')
        with self.store.connect() as c:
            c.execute('CREATE TABLE backup_padding(value BLOB)')
            c.executemany('INSERT INTO backup_padding VALUES (?)',[(b'x'*4096,)]*600)
            before = module.raw_digest(c)
        remaining_pages = []

        def concurrent_writer(status, remaining, total):
            remaining_pages.append(remaining)
            if len(remaining_pages) == 1:
                with self.store.connect() as writer:
                    writer.execute('UPDATE points SET speed=speed+1')

        copy = self.root/'snapshot.sqlite'
        module.copy_snapshot(self.store.path, str(copy), concurrent_writer)
        self.assertGreater(len(remaining_pages), 1)
        self.assertTrue(all(b<a for a,b in zip(remaining_pages,remaining_pages[1:])), remaining_pages)
        with sqlite3.connect(copy) as snapshot:
            self.assertEqual(module.raw_digest(snapshot), before)
        with self.store.connect() as c:
            self.assertNotEqual(module.raw_digest(c), before)

    def test_code_only_stage_skips_snapshot_and_backup(self):
        self.ingest(*fixtures.LIVE)
        module = fixtures.DeployPreparationTests.module('prepare-quality.py')
        candidate, backup = self.root/'unused.sqlite', self.root/'unused-backup.sqlite'
        self.assertTrue(module.stage(self.store.path, str(candidate), backup, [])['skipped'])
        self.assertFalse(candidate.exists())
        self.assertFalse(backup.exists())

    def test_completed_tables_without_final_receipt_can_be_verified_at_install(self):
        module, candidate, backup = self.prepared()
        with sqlite3.connect(candidate) as c:
            c.execute("DELETE FROM meta WHERE key='quality_migration'")
        installed = module.install_derived(self.store.path, candidate)
        with self.store.connect() as c:
            receipt = json.loads(c.execute("SELECT value FROM meta WHERE key='quality_migration'").fetchone()[0])
        self.assertEqual(receipt['version'], quality.VERSION)
        self.assertEqual(receipt['raw_sha256'], installed['raw_sha256'])
        self.assertTrue(receipt['raw_unchanged'])

    def test_query_service_stop_callback_runs_only_after_successful_checks(self):
        module, candidate, backup = self.prepared()
        calls = []
        with sqlite3.connect(candidate) as c:
            c.execute('UPDATE points SET speed=speed+1')
        with self.assertRaisesRegex(ValueError, '原始数据'):
            module.install_derived(self.store.path, candidate, before_install=lambda: calls.append('stop'))
        self.assertEqual(calls, [])
        with sqlite3.connect(candidate) as c:
            # Restore the exact test fixture values, not a floating subtraction.
            with sqlite3.connect(backup) as original:
                for t, speed in original.execute('SELECT t,speed FROM points'):
                    c.execute('UPDATE points SET speed=? WHERE t=?',(speed,t))
        module.install_derived(self.store.path, candidate, before_install=lambda: calls.append('stop'))
        self.assertEqual(calls, ['stop'])


if __name__ == '__main__':
    unittest.main()
