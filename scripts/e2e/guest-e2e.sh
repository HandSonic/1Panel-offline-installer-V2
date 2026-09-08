#!/usr/bin/env bash
# Executed as root exclusively inside the disposable QEMU VM.
set -euo pipefail
export PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=/e2e/results
WORK=/var/tmp/1panel-e2e
BASE=/opt/1panel-e2e
DB_DIR=$BASE/1panel/db
PORT=18080
ENTRANCE=e2eofflinetest
mkdir -p "$OUT" "$WORK"

record() {
    python3 - "$OUT/result.json" "$1" "$2" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
obj = json.loads(p.read_text()) if p.exists() else {'status': 'running', 'checks': {}}
if sys.argv[2] == 'status': obj['status'] = sys.argv[3]
else: obj['checks'][sys.argv[2]] = sys.argv[3]
p.write_text(json.dumps(obj, indent=2) + '\n')
PY
}
diagnostics() {
    local status=$?
    trap - EXIT
    if (( status != 0 )); then record status failed || true; fi
    systemctl --no-pager --full status docker 1panel-core 1panel-agent > "$OUT/services.txt" 2>&1 || true
    journalctl --no-pager -b -u docker -u 1panel-core -u 1panel-agent > "$OUT/services-journal.txt" 2>&1 || true
    ip -j address > "$OUT/network-final.json" 2>&1 || true
    docker info > "$OUT/docker-info.txt" 2>&1 || true
    if [[ -d "$BASE/1panel/log" ]]; then cp -a "$BASE/1panel/log" "$OUT/panel-log" || true; fi
    if [[ -d "$BASE/1panel/logs" ]]; then cp -a "$BASE/1panel/logs" "$OUT/panel-logs" || true; fi
    sync
    exit "$status"
}
trap diagnostics EXIT
record status running

[[ "$(systemd-detect-virt)" == qemu || "$(systemd-detect-virt)" == kvm ]] || {
    echo 'This script must only execute inside the disposable QEMU VM' >&2; exit 1;
}
[[ -f /var/lib/1panel-e2e-provisioned && $EUID -eq 0 ]] || exit 1
[[ ! -e /usr/local/bin/1pctl ]] || { echo 'The initial VM must not contain 1Panel' >&2; exit 1; }
command -v docker >/dev/null && { echo 'The initial VM must not contain Docker' >&2; exit 1; }
ip -j address > "$OUT/network-initial.json"
python3 - "$OUT/network-initial.json" <<'PY'
import json, sys
interfaces = [item['ifname'] for item in json.load(open(sys.argv[1]))]
assert interfaces == ['lo'], interfaces
PY
if curl --noproxy '*' --connect-timeout 2 --max-time 3 http://1.1.1.1 >/dev/null 2>&1; then
    echo 'External networking unexpectedly works' >&2; exit 1
fi
record network 'VM booted with no NIC; only loopback exists; external connect fails'

unpack() {
    local name=$1
    mkdir -p "$WORK/$name"
    tar -xzf "/e2e/$name.tar.gz" -C "$WORK/$name"
    local -a entries=()
    mapfile -t entries < <(find "$WORK/$name" -type f -name install.sh)
    [[ ${#entries[@]} -eq 1 ]] || { echo "Expected one install.sh in $name archive" >&2; return 1; }
    dirname "${entries[0]}"
}
BASELINE_DIR=$(unpack baseline)
TARGET_DIR=$(unpack target)
package_version() { sed -n 's/^ORIGINAL_VERSION=//p' "$1/1pctl" | head -n 1 | tr -d "\"'"; }
BASELINE_VERSION=$(package_version "$BASELINE_DIR")
TARGET_VERSION=$(package_version "$TARGET_DIR")
[[ -n "$BASELINE_VERSION" && -n "$TARGET_VERSION" ]] || exit 1
record baseline_version "$BASELINE_VERSION"
record target_version "$TARGET_VERSION"
if [[ "$BASELINE_VERSION" == "$TARGET_VERSION" ]]; then
    record upgrade_scope 'same-version replacement; cross-version migrations not exercised'
else
    record upgrade_scope "cross-version $BASELINE_VERSION -> $TARGET_VERSION"
fi

health() {
    local attempt code
    for attempt in $(seq 1 90); do
        if systemctl is-active --quiet docker && systemctl is-active --quiet 1panel-core && systemctl is-active --quiet 1panel-agent; then
            code=$(curl --noproxy '*' --max-time 3 -sS -o "$OUT/panel-last.html" -w '%{http_code}' "http://127.0.0.1:$PORT/$ENTRANCE" || true)
            if [[ "$code" == 200 ]] && grep -Eqi '<!doctype html|<html' "$OUT/panel-last.html"; then
                sleep 3
                systemctl is-active --quiet 1panel-core && systemctl is-active --quiet 1panel-agent && return 0
            fi
        fi
        sleep 2
    done
    echo 'Panel service/API readiness timed out' >&2
    return 1
}

echo "Installing $BASELINE_VERSION with no existing Docker and no virtual NIC"
PANEL_PASSWORD=CiOnly_Offline_4729 timeout 15m bash "$BASELINE_DIR/install.sh" \
    --non-interactive --lang en --install-dir "$BASE" --port "$PORT" \
    --entrance "$ENTRANCE" --username e2eadmin --install-docker n \
    --configure-accelerator n --replace-daemon-json n < /dev/null 2>&1 | tee "$OUT/install.log"
health
docker info >/dev/null
docker compose version | tee "$OUT/compose-version.txt"
for database in core agent; do
    [[ -s "$DB_DIR/$database.db" ]]
    [[ "$(sqlite3 "$DB_DIR/$database.db" 'PRAGMA integrity_check;')" == ok ]]
done
record fresh_install 'installer exited 0; Docker/core/agent active; panel HTML returns HTTP 200; both SQLite DBs valid'

# Use a static binary provided by the prepared OS. No registry or image pull.
mkdir -p "$WORK/container-root/bin"
cp /usr/bin/busybox "$WORK/container-root/bin/busybox"
tar -C "$WORK/container-root" -cf "$WORK/container-root.tar" .
docker import "$WORK/container-root.tar" 1panel-e2e:local >/dev/null
docker run --rm --network none 1panel-e2e:local /bin/busybox echo DOCKER_OFFLINE_OK | tee "$OUT/container.txt"
grep -qx DOCKER_OFFLINE_OK "$OUT/container.txt"
cat > "$WORK/compose.yaml" <<'EOF'
services:
  verify:
    image: 1panel-e2e:local
    pull_policy: never
    network_mode: none
    command: ["/bin/busybox", "echo", "COMPOSE_OFFLINE_OK"]
EOF
docker compose -p offline-e2e -f "$WORK/compose.yaml" run --rm verify | tee "$OUT/compose-container.txt"
grep -qx COMPOSE_OFFLINE_OK "$OUT/compose-container.txt"
docker compose -p offline-e2e -f "$WORK/compose.yaml" down
record containers 'Docker and Compose ran real containers from a locally imported image, network none'

# Real durable data that must survive successful upgrade and failed startup.
for database in core agent; do
    sqlite3 "$DB_DIR/$database.db" "CREATE TABLE e2e_rollback_probe (id INTEGER PRIMARY KEY, value TEXT); INSERT INTO e2e_rollback_probe VALUES (1,'before-upgrade');"
done
mkdir -p "$BASE/1panel/conf"
printf 'preserve-custom-config\n' > "$BASE/1panel/conf/e2e-preserve.conf"
sha256sum /usr/local/bin/docker /usr/local/bin/dockerd /usr/local/lib/docker/cli-plugins/docker-compose > "$OUT/docker-before.sha256"
sed -n '/^BASE_DIR=/p; /^ORIGINAL_PORT=/p; /^ORIGINAL_USERNAME=/p; /^ORIGINAL_ENTRANCE=/p; /^LANGUAGE=/p' /usr/local/bin/1pctl > "$OUT/config-before.txt"

echo "Upgrading $BASELINE_VERSION -> $TARGET_VERSION with no virtual NIC"
PANEL_HEALTH_TIMEOUT=90 timeout 15m bash "$TARGET_DIR/upgrade.sh" < /dev/null 2>&1 | tee "$OUT/upgrade-success.log"
health
for binary in 1panel-core 1panel-agent; do cmp "$TARGET_DIR/$binary" "/usr/local/bin/$binary"; done
sha256sum -c "$OUT/docker-before.sha256"
sed -n '/^BASE_DIR=/p; /^ORIGINAL_PORT=/p; /^ORIGINAL_USERNAME=/p; /^ORIGINAL_ENTRANCE=/p; /^LANGUAGE=/p' /usr/local/bin/1pctl > "$OUT/config-after.txt"
cmp "$OUT/config-before.txt" "$OUT/config-after.txt"
grep -qx preserve-custom-config "$BASE/1panel/conf/e2e-preserve.conf"
for database in core agent; do
    [[ "$(sqlite3 "$DB_DIR/$database.db" 'SELECT value FROM e2e_rollback_probe WHERE id=1;')" == before-upgrade ]]
    [[ "$(sqlite3 "$DB_DIR/$database.db" 'PRAGMA integrity_check;')" == ok ]]
done
record upgrade 'upgrade.sh exited 0; compiled target binaries installed; HTTP 200; DB sentinels/config preserved; Docker and Compose unchanged'

sha256sum /usr/local/bin/1panel-core /usr/local/bin/1panel-agent /usr/local/bin/1pctl \
    /etc/systemd/system/1panel-core.service /etc/systemd/system/1panel-agent.service > "$OUT/before-failure.sha256"
for database in core agent; do
    sqlite3 "$DB_DIR/$database.db" "SELECT value FROM settings WHERE key='SystemVersion';" > "$OUT/$database-version-before.txt"
done

# Change only the proposed core executable. The real systemd unit starts it;
# it dirties both actual DBs and config, then exits 42 to exercise real rollback.
FAILED_DIR="$WORK/failing-linux-amd64"
mkdir -p "$FAILED_DIR"
for item in 1panel-agent 1pctl upgrade.sh; do cp "$TARGET_DIR/$item" "$FAILED_DIR/$item"; done
cat > "$FAILED_DIR/1panel-core" <<'EOF'
#!/bin/bash
for database in core agent; do
    sqlite3 "/opt/1panel-e2e/1panel/db/$database.db" "UPDATE e2e_rollback_probe SET value='failed-core-start' WHERE id=1;"
done
printf 'dirty-config-from-failed-core\n' > /opt/1panel-e2e/1panel/conf/e2e-preserve.conf
touch /var/tmp/e2e-failed-core-executed
exit 42
EOF
chmod 0755 "$FAILED_DIR/1panel-core"
python3 - "$FAILED_DIR/1pctl" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
def increment(m):
    return 'ORIGINAL_VERSION=v' + m[1] + '.' + m[2] + '.' + str(int(m[3]) + 1)
s, count = re.subn(r'^ORIGINAL_VERSION=[\"\x27]?v?(\d+)\.(\d+)\.(\d+)[^\n]*', increment, s, flags=re.M)
assert count == 1, 'Expected a single package version'
p.write_text(s)
PY
echo 'Injecting a real failing service executable and checking transactional rollback'
set +e
PANEL_HEALTH_TIMEOUT=35 timeout 10m bash "$FAILED_DIR/upgrade.sh" < /dev/null 2>&1 | tee "$OUT/upgrade-failure.log"
failure_status=${PIPESTATUS[0]}
set -e
[[ "$failure_status" -ne 0 && "$failure_status" -ne 124 && "$failure_status" -ne 137 ]]
[[ -f /var/tmp/e2e-failed-core-executed ]]
grep -q 'Rollback completed' "$OUT/upgrade-failure.log"
sha256sum -c "$OUT/before-failure.sha256"
health
grep -qx preserve-custom-config "$BASE/1panel/conf/e2e-preserve.conf"
for database in core agent; do
    [[ "$(sqlite3 "$DB_DIR/$database.db" 'SELECT value FROM e2e_rollback_probe WHERE id=1;')" == before-upgrade ]]
    [[ "$(sqlite3 "$DB_DIR/$database.db" 'PRAGMA integrity_check;')" == ok ]]
    sqlite3 "$DB_DIR/$database.db" "SELECT value FROM settings WHERE key='SystemVersion';" > "$OUT/$database-version-after.txt"
    cmp "$OUT/$database-version-before.txt" "$OUT/$database-version-after.txt"
done
sha256sum -c "$OUT/docker-before.sha256"
record rollback "real core exited 42 after dirtying both DBs/config; upgrade returned $failure_status; binary/unit hashes, DB values/version and config restored; HTTP 200"
record status passed
echo 'PASS: offline fresh install, real containers, upgrade and failed-start rollback'
