#!/bin/bash
# Build 1Panel v2 offline installer packages.

set -euo pipefail

BASE_DIR=$(cd "$(dirname "$0")" || exit 1; pwd)
BUILD_ROOT="${BASE_DIR}/build"
CACHE_DIR="${BUILD_ROOT}/cache"

APP_VERSION=""
INSTALL_MODE="stable" # stable | beta | dev
DOCKER_VERSION="24.0.7"
COMPOSE_VERSION="v2.23.0"
ARCH_LIST="amd64 arm64 armv7 ppc64le s390x"
MIN_COMPOSE_SIZE=8000000 # bytes, used to guard against partial downloads
ALLOW_MISSING="false"
declare -a BUILT_ARCHES
declare -a SKIPPED_ARCHES
declare -a OFFLINE_TARS

usage() {
    cat <<'EOF'
Usage: ./prepare_offline.sh [OPTIONS]
  --mode <stable|beta|dev>      Download channel (default: stable)
  --app_version <vX.Y.Z>        1Panel version (default: latest for the chosen mode)
  --docker_version <ver>        Docker static version (default: 24.0.7)
  --compose_version <vX.Y.Z>    docker-compose version (default: v2.23.0)
  --arch <list>                 Comma separated arch list, e.g. amd64,arm64 (default: amd64 arm64 armv7 ppc64le s390x)
  --allow-missing               Skip architectures whose artifacts are unavailable instead of failing
  -h, --help                    Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            INSTALL_MODE="$2"
            shift 2
            ;;
        --app_version)
            APP_VERSION="$2"
            shift 2
            ;;
        --docker_version)
            DOCKER_VERSION="$2"
            shift 2
            ;;
        --compose_version)
            COMPOSE_VERSION="$2"
            shift 2
            ;;
        --arch)
            ARCH_LIST=$(echo "$2" | tr ',' ' ')
            shift 2
            ;;
        --allow-missing)
            ALLOW_MISSING="true"
            shift 1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

# normalize docker version like "docker-v29.0.2" or "v29.0.2" -> "29.0.2"
DOCKER_VERSION=${DOCKER_VERSION#docker-}
DOCKER_VERSION=${DOCKER_VERSION#v}

if [[ -z "${APP_VERSION}" ]]; then
    APP_VERSION=$(curl -fsSL "https://resource.fit2cloud.com/1panel/package/v2/${INSTALL_MODE}/latest")
    if [[ -z "${APP_VERSION}" ]]; then
        echo "Failed to fetch latest version for mode: ${INSTALL_MODE}"
        exit 1
    fi
fi

mkdir -p "${CACHE_DIR}"

download_if_missing() {
    local url="$1"
    local dest="$2"
    local type="${3:-archive}" # archive | binary
    local min_size="${4:-0}"

    if [[ -f "${dest}" ]]; then
        if [[ "${type}" == "archive" ]]; then
            if tar -tf "${dest}" >/dev/null 2>&1; then
                echo "Reuse ${dest}"
                return
            fi
        else
            local current_size
            current_size=$(stat -c %s "${dest}" 2>/dev/null || echo 0)
            if [[ ${current_size} -ge ${min_size} && ${current_size} -gt 0 ]]; then
                echo "Reuse ${dest}"
                return
            fi
        fi
        echo "Cached ${dest} looks invalid, re-downloading..."
        rm -f "${dest}"
    fi

    echo "Downloading ${url}"
    if ! curl --retry 3 --retry-delay 2 --progress-bar -fL "${url}" -o "${dest}"; then
        status=$?
        echo "Download failed for ${url}"
        rm -f "${dest}"
        return ${status}
    fi

    if [[ "${type}" == "archive" ]]; then
        if ! tar -tf "${dest}" >/dev/null 2>&1; then
            echo "Archive ${dest} is invalid, removing it"
            rm -f "${dest}"
            return 1
        fi
    else
        local current_size
        current_size=$(stat -c %s "${dest}" 2>/dev/null || echo 0)
        if [[ ${current_size} -lt ${min_size} || ${current_size} -eq 0 ]]; then
            echo "Downloaded file ${dest} is smaller than expected, removing it"
            rm -f "${dest}"
            return 1
        fi
    fi

    return 0
}

handle_missing_arch() {
    local arch="$1"
    local reason="$2"
    if [[ "${ALLOW_MISSING}" == "true" ]]; then
        echo "[WARN] Skip ${arch}: ${reason}"
        SKIPPED_ARCHES+=("${arch}")
        return 0
    fi
    echo "[ERROR] ${reason}"
    exit 1
}

patch_install_script() {
    local file="$1"

    if ! command -v python3 >/dev/null 2>&1; then
        echo "python3 is required to patch ${file}"
        exit 1
    fi

    python3 - "${file}" <<'PY'
from pathlib import Path
import sys
import textwrap

path = Path(sys.argv[1])
content = path.read_text()

if "OFFLINE_DOCKER_TGZ" in content:
    sys.exit(0)

marker = 'PASSWORD_MASK="**********"'
if marker not in content:
    sys.exit("Expected PASSWORD_MASK marker is missing; install.sh layout changed.")

offline_vars = textwrap.dedent("""
OFFLINE_DOCKER_TGZ="${CURRENT_DIR}/docker.tgz"
OFFLINE_COMPOSE_BIN="${CURRENT_DIR}/docker-compose"
OFFLINE_DOCKER_SERVICE="${CURRENT_DIR}/docker.service"
""").strip()

content = content.replace(marker, marker + "\n\n" + offline_vars, 1)

helpers = textwrap.dedent("""
function install_compose_offline() {
    if [ -f "${OFFLINE_COMPOSE_BIN}" ]; then
        log "docker-compose offline package detected, installing..."
        mkdir -p /usr/local/lib/docker/cli-plugins
        cp -f "${OFFLINE_COMPOSE_BIN}" /usr/local/lib/docker/cli-plugins/docker-compose
        cp -f "${OFFLINE_COMPOSE_BIN}" /usr/local/bin/docker-compose
        chmod +x /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose
    fi
}

function install_docker_offline() {
    log "docker offline package detected, installing..."
    if [ ! -f "${OFFLINE_DOCKER_TGZ}" ]; then
        log "offline docker package missing: ${OFFLINE_DOCKER_TGZ}"
        return 1
    fi

    rm -rf "${CURRENT_DIR}/docker"
    tar -xf "${OFFLINE_DOCKER_TGZ}" || return 1
    chown -R root:root "${CURRENT_DIR}/docker"
    chmod -R 755 "${CURRENT_DIR}/docker"
    cp -f "${CURRENT_DIR}/docker"/* /usr/local/bin
    rm -rf "${CURRENT_DIR}/docker"

    if command -v systemctl &>/dev/null; then
        if [ -f "${OFFLINE_DOCKER_SERVICE}" ]; then
            cp -f "${OFFLINE_DOCKER_SERVICE}" /etc/systemd/system/docker.service
        fi
        systemctl daemon-reload
        systemctl enable docker >/dev/null 2>&1 || true
        systemctl start docker >/dev/null 2>&1 || true
    elif command -v service &>/dev/null; then
        service dockerd start >/dev/null 2>&1 || true
    fi

    install_compose_offline
    log "$TXT_DOCKER_RESTARTED"
}
""").strip()

install_marker = "function Install_Docker(){"
if install_marker not in content:
    sys.exit("Install_Docker definition not found; install.sh layout changed.")

content = content.replace(install_marker, helpers + "\n\n" + install_marker, 1)

prompt_marker = '    else\n        while true; do\n        read -p "$TXT_INSTALL_DOCKER_CONFIRM" install_docker_choice\n'
prompt_replacement = '    else\n        if [[ -f "${OFFLINE_DOCKER_TGZ}" ]]; then\n            install_docker_offline\n            return\n        fi\n        while true; do\n        read -p "$TXT_INSTALL_DOCKER_CONFIRM" install_docker_choice\n'
if prompt_marker not in content:
    sys.exit("Docker install prompt block not found; install.sh layout changed.")

content = content.replace(prompt_marker, prompt_replacement, 1)

tail_marker = '    fi\n}\n\nfunction Set_Port(){'
tail_replacement = '    fi\n    install_compose_offline\n}\n\nfunction Set_Port(){'
if tail_marker not in content:
    sys.exit("Set_Port marker not found; install.sh layout changed.")

content = content.replace(tail_marker, tail_replacement, 1)

path.write_text(content)
PY
}

build_package_for_arch() {
    local arch="$1"

    local APP_ARCH=""
    local DOCKER_ARCH=""
    local COMPOSE_ARCH=""

    case "${arch}" in
        amd64)
            APP_ARCH="amd64"
            DOCKER_ARCH="x86_64"
            COMPOSE_ARCH="x86_64"
            ;;
        arm64)
            APP_ARCH="arm64"
            DOCKER_ARCH="aarch64"
            COMPOSE_ARCH="aarch64"
            ;;
        armv7)
            APP_ARCH="armv7"
            DOCKER_ARCH="armhf"
            COMPOSE_ARCH="armv7"
            ;;
        ppc64le)
            APP_ARCH="ppc64le"
            DOCKER_ARCH="ppc64le"
            COMPOSE_ARCH="ppc64le"
            ;;
        s390x)
            APP_ARCH="s390x"
            DOCKER_ARCH="s390x"
            COMPOSE_ARCH="s390x"
            ;;
        *)
            echo "Unsupported arch: ${arch}"
            exit 1
            ;;
    esac

    local package_dir="${BUILD_ROOT}/${APP_VERSION}"
    local offline_dir="${package_dir}/1panel-${APP_VERSION}-offline-linux-${APP_ARCH}"
    local offline_tar="${offline_dir}.tar.gz"

    mkdir -p "${package_dir}"
    rm -rf "${offline_dir}"
    mkdir -p "${offline_dir}"

    local app_tar="${CACHE_DIR}/1panel-${APP_VERSION}-linux-${APP_ARCH}.tar.gz"
    local app_url="https://resource.fit2cloud.com/1panel/package/v2/${INSTALL_MODE}/${APP_VERSION}/release/1panel-${APP_VERSION}-linux-${APP_ARCH}.tar.gz"
    if ! download_if_missing "${app_url}" "${app_tar}"; then
        handle_missing_arch "${arch}" "failed to download app package"
        return
    fi

    local docker_tgz="${CACHE_DIR}/docker-${DOCKER_VERSION}-${DOCKER_ARCH}.tgz"
    local docker_url="https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${DOCKER_VERSION}.tgz"
    if ! download_if_missing "${docker_url}" "${docker_tgz}"; then
        handle_missing_arch "${arch}" "failed to download docker ${DOCKER_VERSION} for ${DOCKER_ARCH}"
        return
    fi

    local compose_bin="${CACHE_DIR}/docker-compose-${COMPOSE_VERSION}-${COMPOSE_ARCH}"
    local compose_url="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}"
    if ! download_if_missing "${compose_url}" "${compose_bin}" "binary" "${MIN_COMPOSE_SIZE}"; then
        handle_missing_arch "${arch}" "failed to download docker-compose ${COMPOSE_VERSION} for ${COMPOSE_ARCH}"
        return
    fi

    tar -xf "${app_tar}" -C "${offline_dir}" --strip-components=1

    cp -f "${docker_tgz}" "${offline_dir}/docker.tgz"
    cp -f "${compose_bin}" "${offline_dir}/docker-compose"
    chmod +x "${offline_dir}/docker-compose"
    cp -f "${BASE_DIR}/docker.service" "${offline_dir}/docker.service"

    patch_install_script "${offline_dir}/install.sh"

    tar -zcf "${offline_tar}" -C "${package_dir}" "$(basename "${offline_dir}")"
    echo "Built ${offline_tar}"
    OFFLINE_TARS+=("${offline_tar}")
    BUILT_ARCHES+=("${arch}")
}

for arch in ${ARCH_LIST}; do
    build_package_for_arch "${arch}"
done

if [[ ${#OFFLINE_TARS[@]} -eq 0 ]]; then
    echo "No offline packages were built."
    exit 1
fi

cd "${BUILD_ROOT}/${APP_VERSION}"
sha256sum "${OFFLINE_TARS[@]}" | sed "s@${BUILD_ROOT}/${APP_VERSION}/@@" > checksums.txt
ls -lh .

echo "Built arches: ${BUILT_ARCHES[*]}"
if [[ ${#SKIPPED_ARCHES[@]} -gt 0 ]]; then
    echo "Skipped arches (missing artifacts): ${SKIPPED_ARCHES[*]}"
fi
