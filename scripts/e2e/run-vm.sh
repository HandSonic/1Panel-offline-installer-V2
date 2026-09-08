#!/usr/bin/env bash
# Disposable VM only. Boot 1 prepares the OS; boot 2 has no virtual NIC.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TARGET=${1:?Usage: run-vm.sh TARGET_OFFLINE_TGZ [BASELINE_OFFLINE_TGZ] [RESULT_DIR]}
BASELINE=${2:-$TARGET}
RESULT_DIR=${3:-$PWD/e2e-vm-results}
TARGET=$(realpath "$TARGET")
BASELINE=$(realpath "$BASELINE")
mkdir -p "$RESULT_DIR"
RESULT_DIR=$(realpath "$RESULT_DIR")
for archive in "$TARGET" "$BASELINE"; do
    [[ -s "$archive" ]] || { echo "Missing archive: $archive" >&2; exit 2; }
done
for executable in qemu-system-x86_64 qemu-img cloud-localds curl sha256sum python3; do
    command -v "$executable" >/dev/null || {
        echo "Missing $executable. Install qemu-system-x86 qemu-utils cloud-image-utils curl python3 on the disposable runner." >&2
        exit 2
    }
done

STATE=$(mktemp -d "${RUNNER_TEMP:-$PWD}/1panel-e2e-vm.XXXXXX")
SHARE="$STATE/share"
mkdir -p "$SHARE/results"
chmod 0777 "$SHARE/results"
cp "$SCRIPT_DIR/guest-e2e.sh" "$SHARE/guest-e2e.sh"
cp --reflink=auto "$TARGET" "$SHARE/target.tar.gz"
if [[ "$BASELINE" == "$TARGET" ]]; then
    ln "$SHARE/target.tar.gz" "$SHARE/baseline.tar.gz"
else
    cp --reflink=auto "$BASELINE" "$SHARE/baseline.tar.gz"
fi
printf '%s\n' "$(basename "$TARGET")" > "$SHARE/target-name"
printf '%s\n' "$(basename "$BASELINE")" > "$SHARE/baseline-name"

cleanup() {
    local code=$?
    cp -a "$SHARE/results/." "$RESULT_DIR/" || true
    # Guest disk contains only generated CI credentials and disposable data.
    rm -rf -- "$STATE"
    exit "$code"
}
trap cleanup EXIT

IMAGE=noble-server-cloudimg-amd64.img
IMAGE_URL=${E2E_UBUNTU_IMAGE_BASE:-https://cloud-images.ubuntu.com/noble/current}
curl -fL --retry 4 --retry-all-errors --connect-timeout 20 --max-time 1800 \
    "$IMAGE_URL/$IMAGE" -o "$STATE/$IMAGE"
curl -fL --retry 4 --retry-all-errors --connect-timeout 20 --max-time 120 \
    "$IMAGE_URL/SHA256SUMS" -o "$STATE/SHA256SUMS"
python3 - "$STATE" "$IMAGE" <<'PY'
import hashlib, pathlib, sys
root, filename = pathlib.Path(sys.argv[1]), sys.argv[2]
matches = [line.split()[0] for line in (root / 'SHA256SUMS').read_text().splitlines()
           if len(line.split()) == 2 and line.split()[1].lstrip('*') == filename]
if len(matches) != 1:
    raise SystemExit('Ubuntu image checksum is missing or ambiguous')
digest = hashlib.file_digest((root / filename).open('rb'), 'sha256').hexdigest()
if digest != matches[0]:
    raise SystemExit('Ubuntu cloud image checksum mismatch')
print('Ubuntu cloud image verified:', digest)
PY
qemu-img resize "$STATE/$IMAGE" 24G

cat > "$STATE/meta-data" <<'EOF'
instance-id: 1panel-offline-e2e
local-hostname: 1panel-offline-e2e
EOF
cat > "$STATE/user-data" <<'EOF'
#cloud-config
ssh_pwauth: false
disable_root: true
write_files:
  - path: /etc/systemd/system/1panel-e2e.service
    permissions: '0644'
    content: |
      [Unit]
      Description=Disposable 1Panel offline acceptance test
      After=cloud-final.service
      ConditionPathExists=/var/lib/1panel-e2e-provisioned
      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/1panel-e2e-boot
      TimeoutStartSec=infinity
      StandardOutput=journal+console
      StandardError=journal+console
      [Install]
      WantedBy=multi-user.target
  - path: /usr/local/sbin/1panel-e2e-boot
    permissions: '0755'
    content: |
      #!/bin/bash
      set -uo pipefail
      mkdir -p /e2e
      if mount -t 9p -o trans=virtio,version=9p2000.L e2e /e2e; then
          bash /e2e/guest-e2e.sh 2>&1 | tee /e2e/results/guest.log
          status=${PIPESTATUS[0]}
          printf '%s\n' "$status" > /e2e/results/exit-code
          sync
      fi
      systemctl poweroff --no-block
runcmd:
  - [bash, -c, 'set -euxo pipefail; export DEBIAN_FRONTEND=noninteractive; apt-get -o Acquire::Retries=4 update; apt-get -o Acquire::Retries=4 install -y --no-install-recommends iptables curl sqlite3 file xz-utils busybox-static; mkdir -p /e2e; mount -t 9p -o trans=virtio,version=9p2000.L e2e /e2e; touch /var/lib/1panel-e2e-provisioned; systemctl enable 1panel-e2e.service; touch /e2e/results/provisioned; sync']
power_state:
  delay: now
  mode: poweroff
  message: OS prerequisites installed; the next boot removes the virtual NIC
  timeout: 30
  condition: true
EOF
cloud-localds "$STATE/seed.img" "$STATE/user-data" "$STATE/meta-data"

ACCEL=tcg
CPU=max
if [[ -r /dev/kvm && -w /dev/kvm ]]; then ACCEL=kvm; CPU=host; fi
python3 - "$RESULT_DIR/host.json" "$ACCEL" "$TARGET" "$BASELINE" <<'PY'
import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    'hypervisor': 'qemu', 'acceleration': sys.argv[2],
    'os': 'Ubuntu 24.04 amd64 cloud image',
    'target_archive': pathlib.Path(sys.argv[3]).name,
    'baseline_archive': pathlib.Path(sys.argv[4]).name,
    'offline_boot_network': '-nic none',
}, indent=2) + '\n')
PY
QEMU=(qemu-system-x86_64 -accel "$ACCEL" -cpu "$CPU" -smp 2 -m "${E2E_VM_MEMORY_MB:-4096}"
      -display none -serial stdio -monitor none -no-reboot
      -drive "file=$STATE/$IMAGE,if=virtio,format=qcow2"
      -drive "file=$STATE/seed.img,if=virtio,format=raw,readonly=on"
      -fsdev "local,id=e2eshare,path=$SHARE,security_model=none,multidevs=remap"
      -device virtio-9p-pci,fsdev=e2eshare,mount_tag=e2e)

echo "Provisioning a disposable Ubuntu VM (acceleration=$ACCEL)."
timeout --foreground --kill-after=30s "${E2E_PROVISION_TIMEOUT:-30m}" \
    "${QEMU[@]}" -nic user,model=virtio-net-pci 2>&1 | tee "$RESULT_DIR/provision-console.log"
[[ -f "$SHARE/results/provisioned" ]] || { echo 'VM provisioning did not finish' >&2; exit 1; }

echo 'Starting acceptance boot with -nic none; the VM has no network adapter.'
timeout --foreground --kill-after=30s "${E2E_OFFLINE_TIMEOUT:-60m}" \
    "${QEMU[@]}" -nic none 2>&1 | tee "$RESULT_DIR/offline-console.log"
[[ -f "$SHARE/results/exit-code" ]] || { echo 'VM produced no result; inspect console logs' >&2; exit 1; }
[[ "$(cat "$SHARE/results/exit-code")" == 0 ]] || { echo 'Offline acceptance failed; inspect guest.log and service journals' >&2; exit 1; }
python3 - "$SHARE/results/result.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
assert report.get('status') == 'passed', report
print(json.dumps(report, indent=2))
PY
