#!/bin/bash
# Stable offline entry point; does not rewrite or source upstream installer code.
set -euo pipefail
CURRENT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export PATH="/usr/local/bin:${PATH}"
DOCKER_TEMP=""

log() { printf '[offline] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
require_root() { [[ $EUID -eq 0 ]] || die 'Please run as root'; }
cleanup() { [[ -z "$DOCKER_TEMP" ]] || rm -rf -- "$DOCKER_TEMP"; }

service_manager() {
    if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null; then
        echo systemd
    elif command -v rc-service >/dev/null; then
        echo openrc
    elif command -v service >/dev/null; then
        echo sysv
    else
        echo none
    fi
}

start_docker() {
    case "$(service_manager)" in
        systemd) systemctl daemon-reload && systemctl start docker ;;
        openrc) rc-service docker start || rc-service dockerd start ;;
        sysv) service docker start || service dockerd start ;;
        *) return 1 ;;
    esac
}

wait_for_docker() {
    local i
    for ((i=0; i<30; i++)); do
        docker info >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

install_docker_service() {
    case "$(service_manager)" in
        systemd)
            # Preserve administrator/distro units; supply one only if none exists.
            if ! systemctl cat docker.service >/dev/null 2>&1; then
                [[ -s "$CURRENT_DIR/docker.service" ]] || die 'docker.service is missing'
                install -m 0644 "$CURRENT_DIR/docker.service" /etc/systemd/system/docker.service
            fi
            systemctl daemon-reload
            systemctl enable docker >/dev/null 2>&1 || log 'Docker installed; automatic startup could not be enabled'
            ;;
        openrc)
            if [[ ! -f /etc/init.d/docker && ! -f /etc/init.d/dockerd ]]; then
                cat > /etc/init.d/docker <<'RC'
#!/sbin/openrc-run
name="Docker"
command="/usr/local/bin/dockerd"
command_background="yes"
pidfile="/run/docker-offline.pid"
command_args="--pidfile=${pidfile}"
depend() { need net; }
RC
                chmod 0755 /etc/init.d/docker
            fi
            rc-update add docker default >/dev/null 2>&1 || true
            ;;
        sysv)
            if [[ ! -f /etc/init.d/docker && ! -f /etc/init.d/dockerd ]]; then
                cat > /etc/init.d/docker <<'SYSV'
#!/bin/sh
### BEGIN INIT INFO
# Provides: docker
# Required-Start: $remote_fs $network
# Required-Stop: $remote_fs $network
# Default-Start: 2 3 4 5
# Default-Stop: 0 1 6
# Short-Description: Docker offline engine
### END INIT INFO
case "$1" in
    start)
        /usr/local/bin/docker info >/dev/null 2>&1 && exit 0
        mkdir -p /var/log
        nohup /usr/local/bin/dockerd --pidfile=/run/docker-offline.pid >>/var/log/docker-offline.log 2>&1 &
        ;;
    stop)
        if [ -f /run/docker-offline.pid ]; then
            pid=$(cat /run/docker-offline.pid)
            case "$pid" in ''|*[!0-9]*) exit 1;; esac
            [ "$pid" -gt 1 ] || exit 1
            kill "$pid"
        fi
        ;;
    status) /usr/local/bin/docker info >/dev/null 2>&1 ;;
    restart) "$0" stop && "$0" start ;;
    *) exit 2 ;;
esac
SYSV
                chmod 0755 /etc/init.d/docker
            fi
            if command -v update-rc.d >/dev/null; then update-rc.d docker defaults || true
            elif command -v chkconfig >/dev/null; then chkconfig --add docker || true; fi
            ;;
        *) die 'No supported service manager. Start the supplied dockerd with your service supervisor, then run install.sh again.' ;;
    esac
}

ensure_docker() {
    if command -v docker >/dev/null; then
        if docker info >/dev/null 2>&1; then
            log 'Reusing the existing Docker engine'
            return
        fi
        start_docker || true
        wait_for_docker || die 'Existing Docker is not ready. Check its service log; the installer has preserved its binaries and configuration.'
        return
    fi
    [[ -s "$CURRENT_DIR/docker.tgz" ]] || die 'Offline Docker archive is missing'
    # Do not unpack absolute paths, traversal entries or archive links as root.
    tar -tf "$CURRENT_DIR/docker.tgz" | awk '
      /^\// || /(^|\/)\.\.($|\/)/ {bad=1}
      END {exit bad}' || die 'Invalid Docker archive paths'
    tar -tvf "$CURRENT_DIR/docker.tgz" | awk 'substr($0,1,1)!="-" && substr($0,1,1)!="d" {bad=1} END {exit bad}' || die 'Unexpected Docker archive links'
    DOCKER_TEMP=$(mktemp -d)
    tar -xf "$CURRENT_DIR/docker.tgz" -C "$DOCKER_TEMP" --no-same-owner
    local docker_root="" candidate parent
    # Community builds may wrap binaries in docker/, docker-VERSION/, or another
    # directory. Discover a single directory that contains both client and daemon.
    find "$DOCKER_TEMP" -type f -name dockerd -print0 > "$DOCKER_TEMP/.daemon-candidates"
    while IFS= read -r -d '' candidate; do
        parent=$(dirname "$candidate")
        [[ -s "$parent/docker" ]] || continue
        [[ -z "$docker_root" ]] || die 'Docker archive contains multiple client/daemon directories'
        docker_root=$parent
    done < "$DOCKER_TEMP/.daemon-candidates"
    [[ -n "$docker_root" ]] || die 'Offline archive has no Docker client/daemon'
    "$docker_root/docker" --version >/dev/null || die 'Docker binary cannot run on this host architecture'
    mkdir -p /usr/local/bin
    local executable
    for executable in "$docker_root"/*; do
        [[ -f "$executable" ]] || continue
        install -m 0755 "$executable" "/usr/local/bin/$(basename "$executable")"
    done
    install_docker_service
    start_docker || true
    wait_for_docker || die 'Docker did not become ready. Check its service log and host dependencies (for example iptables/cgroups); no online download was attempted.'
}

ensure_compose() {
    if docker compose version >/dev/null 2>&1; then
        log 'Reusing the existing Docker Compose plugin'
        return
    fi
    [[ -s "$CURRENT_DIR/docker-compose" ]] || die 'Offline Compose binary is missing'
    "$CURRENT_DIR/docker-compose" version >/dev/null || die 'Compose binary cannot run on this host architecture'
    mkdir -p /usr/local/lib/docker/cli-plugins /usr/local/bin
    install -m 0755 "$CURRENT_DIR/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
    if ! command -v docker-compose >/dev/null; then
        install -m 0755 "$CURRENT_DIR/docker-compose" /usr/local/bin/docker-compose
    fi
    docker compose version >/dev/null || die 'Docker cannot load the Compose plugin'
}

main() {
    [[ -f "$CURRENT_DIR/install-upstream.sh" ]] || die 'Upstream installer is missing'
    [[ -f "$CURRENT_DIR/offline-env.sh" ]] || die 'Offline environment is missing'
    # Help must not install or change Docker.
    if [[ "${1:-}" != --help && "${1:-}" != -h ]]; then
        require_root
        ensure_docker
        ensure_compose
    fi
    cd -- "$CURRENT_DIR"
    BASH_ENV="$CURRENT_DIR/offline-env.sh" /bin/bash "$CURRENT_DIR/install-upstream.sh" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    trap cleanup EXIT
    main "$@"
fi
