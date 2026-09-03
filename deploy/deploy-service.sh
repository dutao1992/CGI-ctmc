#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
node --check static/app.js
node --check static/map.js
node --test tests/test_map.cjs tests/test_chart_peaks.cjs
python3 deploy/cache-assets.py
release="$(date +%Y%m%d-%H%M%S)"
COPYFILE_DISABLE=1 tar --no-xattrs --exclude='__pycache__' --exclude='*.bin' -czf tmp/vehicle-release.tgz vehicle static tests docs deploy analysis README.md
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes ctmc-quality "install -d -m 0755 /srv/ctmc-vehicle/releases/$release; install -d -m 0700 /srv/ctmc-vehicle/data /srv/ctmc-vehicle/backups"
scp -q tmp/vehicle-release.tgz "ctmc-quality:/srv/ctmc-vehicle/releases/$release/source.tgz"
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes ctmc-quality "RELEASE_ID=$release bash -s" <<'REMOTE'
set -euo pipefail
release="/srv/ctmc-vehicle/releases/$RELEASE_ID"
# Serialize the entire remote publish, including backups and rollbacks. An
# interrupted local SSH client must not allow a second migration to overlap.
exec 9>/srv/ctmc-vehicle/data/deploy.lock
if ! flock -n 9; then
  printf '%s\n' 'Another vehicle deployment is still running; inspect it before retrying.' >&2
  exit 1
fi
tar -xzf "$release/source.tgz" -C "$release"
cd "$release"
/usr/bin/python3.11 -m unittest discover -s tests -v
PYTHON=/usr/bin/python3.11 node --test tests/test_map.cjs tests/test_chart_peaks.cjs
test -d /srv/chcnav-cgi430/logs/raw
previous="$(readlink -f /srv/ctmc-vehicle/current 2>/dev/null || true)"
platform_conf="$(readlink -f /srv/ctmc-quality/current)/deploy/web-standalone.conf"
nginx_backup="/srv/ctmc-vehicle/backups/nginx-before-$RELEASE_ID.conf"
cp -p "$platform_conf" "$nginx_backup"
nginx_changed=0
cutover=0
derived_changed=0
stage_db="$release/precomputed.sqlite"
quality_backup="/srv/ctmc-vehicle/backups/quality-before-$RELEASE_ID.sqlite"
rollback() {
  trap - ERR HUP INT TERM
  if [[ "$nginx_changed" -eq 1 && -f "$nginx_backup" ]]; then
    cp -p "$nginx_backup" "$platform_conf"
    /usr/sbin/nginx -t -c "$platform_conf"
    systemctl reload ctmc-quality-web.service
  fi
  if [[ -f /etc/systemd/system/ctmc-vehicle-retention.timer ]]; then
    systemctl disable --now ctmc-vehicle-retention.timer
    systemctl stop ctmc-vehicle-retention.service
  fi
  if [[ "$cutover" -eq 1 && -n "$previous" && -d "$previous" ]] &&
     { ! systemctl is-active --quiet ctmc-vehicle.service || [[ "$(readlink -f /srv/ctmc-vehicle/current)" != "$previous" ]]; }; then
    systemctl stop ctmc-vehicle.service
    if [[ "$derived_changed" -eq 1 ]]; then
      # Restore only derived tables; never overwrite live cursors or annotations.
      if ! /usr/bin/python3.11 deploy/prepare-quality.py --db /srv/ctmc-vehicle/data/vehicle.sqlite --restore-from "$quality_backup"; then
        (cd "$previous" && /usr/bin/python3.11 -m vehicle.quality --db /srv/ctmc-vehicle/data/vehicle.sqlite --backfill)
      fi
    fi
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
  elif [[ "$cutover" -eq 1 ]] && { [[ -z "$previous" ]] || [[ ! -d "$previous" ]]; }; then
    systemctl stop ctmc-vehicle.service
  fi
  printf '%s\n' 'Publish aborted; candidate and backup retained for inspection. Verify health and retention before retrying.' >&2
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
# Heavy work happens on a consistent snapshot, with the old API still serving.
# A bounded stage failure does not stop the working service.
timeout --signal=TERM --kill-after=10s 1200 ionice -c 2 -n 7 nice -n 15 /usr/bin/python3.11 -u deploy/prepare-quality.py \
  --stage-from /srv/ctmc-vehicle/data/vehicle.sqlite --db "$stage_db" \
  --backup "$quality_backup" \
  --context deploy/stationary-6094510-20260826.json \
  --context deploy/stationary-6094510-20260827-gap.json \
  --context deploy/stationary-6094510-20260827-active.json \
  --context deploy/stationary-6094510-20260901-replay-1.json \
  --context deploy/stationary-6094510-20260901-replay-2.json \
  --context deploy/stationary-6094510-20260901-replay-3.json \
  --context deploy/stationary-6094510-20260902-active.json
if [[ -f "$stage_db" ]]; then /usr/bin/python3.11 deploy/prepare-rollups.py --db "$stage_db"; fi
# The independent TCP receiver keeps logging during this bounded cutover.
# A changed raw digest rejects the candidate instead of overwriting newer data.
# Freeze parsing via its file lock but retain the query API during raw checks.
# Only the final copy stops HTTP; its SQL work has a 180 s deadline.
cutover=1
if [[ -f "$quality_backup" ]]; then
  derived_changed=1
  timeout --signal=TERM --kill-after=10s 1200 /usr/bin/python3.11 -u deploy/prepare-quality.py \
    --db /srv/ctmc-vehicle/data/vehicle.sqlite --install-from "$stage_db" --online-cutover
else
  systemctl stop ctmc-vehicle.service
fi
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
for attempt in $(seq 1 180); do
  if curl -fsS http://127.0.0.1:8790/healthz >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.5
done
test "$ready" -eq 1
# These are this deployment's reproducible scratch files, never receiver logs.
rm -f -- "$stage_db" "$stage_db-wal" "$stage_db-shm" "$stage_db.ingest.lock"
if [[ -f "$quality_backup" ]]; then gzip -1 -- "$quality_backup"; fi
systemctl enable --now ctmc-vehicle-retention.timer
trap - ERR HUP INT TERM
printf '%s\n' "$release"
REMOTE
