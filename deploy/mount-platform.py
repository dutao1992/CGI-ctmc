"""Run on server with an uploaded generated/ folder. Backup, check drift, then mount."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

generated=Path(sys.argv[1]).resolve()
platform=Path('/srv/ctmc-quality/current').resolve()
manifest=json.loads((generated/'manifest.json').read_text())
pairs={
 'integration/original/platform_auth.py':platform/'backend/platform_auth.py',
 'integration/original/web-standalone.conf':platform/'deploy/web-standalone.conf',
 'evidence/portal-before.html':platform/'portal-dist/index.html',
 'evidence/portal-before.js':platform/'portal-dist/assets/index-VDT1Cd3t.js'}
for key,path in pairs.items():
    actual=hashlib.sha256(path.read_bytes()).hexdigest()
    if actual!=manifest['base_sha256'][key]:
        raise RuntimeError('Production drift; stop without changing '+str(path))
with urllib.request.urlopen('http://127.0.0.1:8790/healthz',timeout=5) as r:
    assert json.load(r)['ok']
backup=Path('/srv/ctmc-vehicle/backups')/time.strftime('mount-%Y%m%d-%H%M%S')
backup.mkdir(mode=0o700)
for key,path in pairs.items():shutil.copy2(path,backup/path.name)
db=sqlite3.connect('/srv/ctmc-quality/data/platform-auth.db')
with sqlite3.connect(backup/'platform-auth.db') as target:db.backup(target)
db.close()
(backup/'platform-auth.db').chmod(0o600)
(backup/'platform-path.txt').write_text(str(platform))
rollback=f'''#!/usr/bin/env bash
set -euo pipefail
cp '{backup}/platform_auth.py' '{platform}/backend/platform_auth.py'
cp '{backup}/web-standalone.conf' '{platform}/deploy/web-standalone.conf'
cp '{backup}/index.html' '{platform}/portal-dist/index.html'
/usr/sbin/nginx -t -c '{platform}/deploy/web-standalone.conf'
systemctl restart ctmc-quality-api.service
systemctl reload ctmc-quality-web.service
systemctl disable --now ctmc-vehicle.service
echo 'Rolled back mount. New telemetry database and raw logs preserved.'
'''
(backup/'rollback.sh').write_text(rollback);(backup/'rollback.sh').chmod(0o700)
def run(*args):subprocess.run(args,check=True)
def atomic_copy(src,dest):
    temp=dest.with_name(dest.name+'.vehicle-new')
    shutil.copy2(src,temp);temp.chmod(0o644);temp.replace(dest)
try:
    atomic_copy(generated/'platform_auth.py',platform/'backend/platform_auth.py')
    atomic_copy(generated/'web-standalone.conf',platform/'deploy/web-standalone.conf')
    run('/usr/sbin/nginx','-t','-c',str(platform/'deploy/web-standalone.conf'))
    run('/usr/bin/python3.11','-m','py_compile',str(platform/'backend/platform_auth.py'))
    run('systemctl','restart','ctmc-quality-api.service')
    ready=False
    for _ in range(25):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8010/auth/me',timeout=2) as r:
                ready='vehicle' in json.load(r)['permissions']
            if ready:break
        except Exception:pass
        time.sleep(.4)
    if not ready:raise RuntimeError('Platform API failed readiness')
    atomic_copy(generated/manifest['asset'],platform/'portal-dist/assets'/manifest['asset'])
    atomic_copy(generated/'index.html',platform/'portal-dist/index.html')
    run('systemctl','reload','ctmc-quality-web.service')
except BaseException:
    run('bash',str(backup/'rollback.sh'))
    raise
print(json.dumps({'mounted':True,'backup':str(backup),'platform_release':str(platform)},ensure_ascii=False))
