#!/usr/bin/env python3
"""Fail closed when upstream's installation contract changes. Never execute it."""
import re
import subprocess
import sys
from pathlib import Path

MARKER = '# OFFLINE_INSTALLER_PATCH_V2'
HELPERS = r'''
# OFFLINE_INSTALLER_PATCH_V2
function install_compose_offline() {
    local compose="${CURRENT_DIR}/docker-compose"
    [[ -s "$compose" ]] || return 1
    mkdir -p /usr/local/lib/docker/cli-plugins || return 1
    cp -f "$compose" /usr/local/lib/docker/cli-plugins/docker-compose || return 1
    cp -f "$compose" /usr/local/bin/docker-compose || return 1
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose || return 1
}

function Install_Docker(){
    local archive="${CURRENT_DIR}/docker.tgz"
    local service="${CURRENT_DIR}/docker.service"
    [[ -s "$archive" && -s "$service" && -s "${CURRENT_DIR}/docker-compose" ]] || { log "Required offline Docker payload missing"; exit 1; }
    local fresh=false
    if ! command -v docker >/dev/null 2>&1; then
        fresh=true
        command -v systemctl >/dev/null 2>&1 && systemctl show-environment >/dev/null 2>&1 || { log "Offline Docker installation requires running systemd"; exit 1; }
        tar -tzf "$archive" >/dev/null || { log "Offline Docker archive is corrupt"; exit 1; }
    elif ! docker version >/dev/null 2>&1; then
        # Do not overwrite a client-only or externally managed Docker installation.
        command -v systemctl >/dev/null 2>&1 && systemctl show-environment >/dev/null 2>&1 && systemctl cat docker.service >/dev/null 2>&1 || { log "Existing Docker is unhealthy and no usable docker.service was found"; exit 1; }
        systemctl start docker || exit 1
        docker version >/dev/null 2>&1 || { log "$TXT_DOCKER_INSTALL_FAIL"; exit 1; }
    fi
    install_compose_offline || { log "Offline Compose installation failed"; exit 1; }
    if [[ "$fresh" == true ]]; then
        local unpack
        unpack=$(mktemp -d) || exit 1
        if ! tar -xzf "$archive" -C "$unpack"; then
            rm -rf "$unpack"
            exit 1
        fi
        cp -f "$unpack"/docker/* /usr/local/bin/ || { rm -rf "$unpack"; exit 1; }
        chmod 755 /usr/local/bin/docker /usr/local/bin/dockerd /usr/local/bin/containerd /usr/local/bin/runc || exit 1
        rm -rf "$unpack"
        cp -f "$service" /etc/systemd/system/docker.service || exit 1
        systemctl daemon-reload || exit 1
        systemctl enable docker || exit 1
        systemctl start docker || exit 1
    fi
    if ! docker version >/dev/null 2>&1; then
        command -v systemctl >/dev/null 2>&1 && systemctl start docker || exit 1
    fi
    docker version >/dev/null 2>&1 || { log "$TXT_DOCKER_INSTALL_FAIL"; exit 1; }
}
'''.lstrip()

def patch(path):
    content = path.read_text()
    if MARKER in content:
        if HELPERS not in content:
            raise ValueError('Existing offline patch is incomplete or changed')
        return
    # Remove obsolete upstream built-in Docker helpers (may run apt-get -f).
    for name in ('Install_Iptables_Offline', 'Install_Docker_Offline'):
        content = re.sub(r'^function ' + name + r'\s*\(\)\s*\{.*?^}[^\S\n]*(?:\n|$)', '', content, flags=re.M | re.S)
    # Replace the complete top-level function, not a whitespace-sensitive prompt.
    pattern = r'^function Install_Docker\s*\(\)\s*\{.*?^}[^\S\n]*(?:\n|$)'
    matches = list(re.finditer(pattern, content, re.M | re.S))
    if len(matches) != 1 or 'CURRENT_DIR=' not in content or 'function log()' not in content:
        raise ValueError('Unsupported upstream installer: expected one Install_Docker, CURRENT_DIR and log')
    old = matches[0].group()
    if not re.search(r'^}\s*$', old, re.M):
        raise ValueError('Unclosed upstream Install_Docker function')
    content = content[:matches[0].start()] + HELPERS + '\n' + content[matches[0].end():]
    content = re.sub(r'PUBLIC_IP=\$\(curl[^\n]*api64\.ipify\.org\)', 'PUBLIC_IP="" # Offline mode', content)
    result = subprocess.run(['bash', '-n'], input=content, text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr)
    temporary = path.with_name(path.name + '.patched')
    temporary.write_text(content)
    temporary.chmod(path.stat().st_mode)
    temporary.replace(path)

if __name__ == '__main__':
    try:
        patch(Path(sys.argv[1]))
    except (ValueError, OSError) as exc:
        sys.exit(str(exc))
