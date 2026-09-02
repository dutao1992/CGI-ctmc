#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check static/map.js
node --test tests/test_map.cjs
python3 deploy/cache-assets.py
release="$(date +%Y%m%d-%H%M%S)"
COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' --exclude='*.bin' -czf tmp/vehicle-release.tgz vehicle static tests docs deploy analysis README.md
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes ctmc-quality "install -d -m 0755 /srv/ctmc-vehicle/releases/$release; install -d -m 0700 /srv/ctmc-vehicle/data /srv/ctmc-vehicle/backups"
scp -q tmp/vehicle-release.tgz "ctmc-quality:/srv/ctmc-vehicle/releases/$release/source.tgz"
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes ctmc-quality "RELEASE_ID=$release bash -s" <<'REMOTE'
set -euo pipefail
release="/srv/ctmc-vehicle/releases/$RELEASE_ID"
tar -xzf "$release/source.tgz" -C "$release"
cd "$release"
/usr/bin/python3.11 -m unittest discover -s tests -v
node --test tests/test_map.cjs
test -d /srv/chcnav-cgi430/logs/raw
previous="$(readlink -f /srv/ctmc-vehicle/current 2>/dev/null || true)"
platform_conf="$(readlink -f /srv/ctmc-quality/current)/deploy/web-standalone.conf"
nginx_backup="/srv/ctmc-vehicle/backups/nginx-before-$RELEASE_ID.conf"
cp -p "$platform_conf" "$nginx_backup"
nginx_changed=0
rollback() {
  if [[ "$nginx_changed" -eq 1 && -f "$nginx_backup" ]]; then
    cp -p "$nginx_backup" "$platform_conf"
    /usr/sbin/nginx -t -c "$platform_conf"
    systemctl reload ctmc-quality-web.service
  fi
  if [[ -f /etc/systemd/system/ctmc-vehicle-retention.timer ]]; then
    systemctl disable --now ctmc-vehicle-retention.timer
    systemctl stop ctmc-vehicle-retention.service
  fi
  if [[ -n "$previous" && -d "$previous" ]]; then
    ln -sfn "$previous" /srv/ctmc-vehicle/current
    install -m 0644 "$previous/deploy/ctmc-vehicle.service" /etc/systemd/system/ctmc-vehicle.service
    systemctl daemon-reload
    systemctl restart ctmc-vehicle.service
    if [[ -f "$previous/deploy/ctmc-vehicle-retention.timer" ]]; then
      install -m 0644 "$previous/deploy/ctmc-vehicle-retention.service" /etc/systemd/system/ctmc-vehicle-retention.service
      install -m 0644 "$previous/deploy/ctmc-vehicle-retention.timer" /etc/systemd/system/ctmc-vehicle-retention.timer
      systemctl daemon-reload
      systemctl enable --now ctmc-vehicle-retention.timer
    fi
  else
    systemctl stop ctmc-vehicle.service
  fi
}
trap rollback ERR HUP INT TERM
nginx_changed=1
/usr/bin/python3.11 deploy/configure-offline-proxy.py --config "$platform_conf"
/usr/sbin/nginx -t -c "$platform_conf"
systemctl reload ctmc-quality-web.service
if [[ -f /etc/systemd/system/ctmc-vehicle-retention.timer ]]; then
  systemctl stop ctmc-vehicle-retention.timer
  systemctl stop ctmc-vehicle-retention.service
fi
# Only parser/API stops briefly. The independent TCP 9000 receiver keeps logging.
systemctl stop ctmc-vehicle.service
/usr/bin/python3.11 deploy/prepare-quality.py --db /srv/ctmc-vehicle/data/vehicle.sqlite \
  --backup "/srv/ctmc-vehicle/backups/quality-before-$RELEASE_ID.sqlite" \
  --context deploy/stationary-6094510-20260826.json \
  --context deploy/stationary-6094510-20260827-gap.json \
  --context deploy/stationary-6094510-20260827-active.json \
  --context deploy/stationary-6094510-20260901-replay-1.json \
  --context deploy/stationary-6094510-20260901-replay-2.json \
  --context deploy/stationary-6094510-20260901-replay-3.json \
  --context deploy/stationary-6094510-20260902-active.json
/usr/bin/python3.11 -m vehicle.retention --prepare-backup "/srv/ctmc-vehicle/backups/retention-before-$RELEASE_ID.sqlite"
/usr/bin/python3.11 deploy/prepare-rollups.py --db /srv/ctmc-vehicle/data/vehicle.sqlite
ln -sfn "$release" /srv/ctmc-vehicle/current
install -m 0644 deploy/ctmc-vehicle.service /etc/systemd/system/ctmc-vehicle.service
install -m 0644 deploy/ctmc-vehicle-retention.service /etc/systemd/system/ctmc-vehicle-retention.service
install -m 0644 deploy/ctmc-vehicle-retention.timer /etc/systemd/system/ctmc-vehicle-retention.timer
if [[ ! -f /etc/ctmc-vehicle-retention.json ]]; then
  install -m 0600 deploy/ctmc-vehicle-retention.json /etc/ctmc-vehicle-retention.json
fi
systemctl daemon-reload
systemctl enable --now ctmc-vehicle.service
systemctl restart ctmc-vehicle.service
ready=0
for attempt in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8790/healthz >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
test "$ready" -eq 1
systemctl enable --now ctmc-vehicle-retention.timer
trap - ERR HUP INT TERM
printf '%s\n' "$release"
REMOTE
