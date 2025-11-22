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
ARCH_LIST="amd64 arm64 armv7 ppc64le s390x loong64 riscv64"
MIN_COMPOSE_SIZE=8000000 # bytes, used to guard against partial downloads
ALLOW_MISSING="false"
SOURCES="official custom" # official | custom | both
CUSTOM_REPO="HandSonic/test1v2" # owner/repo for custom release packages
PROMPT_APP_VERSION="false"
SQLITE_URL_BASE="https://github.com/sqldevnoarch/sqlite-autobuild/releases/download" # placeholder base, override per arch
SQLITE_VERSION="3460100"
declare -a BUILT_ARCHES=()
declare -a SKIPPED_ARCHES=()
declare -a OFFLINE_TARS=()
declare -a SKIPPED_SOURCES=()

usage() {
    cat <<'EOF'
Usage: ./prepare_offline.sh [OPTIONS]
  --mode <stable|beta|dev>      Download channel (default: stable)
  --app_version <vX.Y.Z>        1Panel version (default: latest for the chosen mode)
  --interactive                 Prompt to confirm/override version (default: disabled)
  --source <official|custom|both>  Choose package source (default: both)
  --custom_repo <owner/repo>    GitHub repo for custom release packages (default: wojiushixiaobai/1Panel)
  --docker_version <ver>        Docker static version (default: 24.0.7)
  --compose_version <vX.Y.Z>    docker-compose version (default: v2.23.0)
  --arch <list>                 Comma separated arch list, e.g. amd64,arm64 (default: amd64 arm64 armv7 ppc64le s390x loong64)
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
        --interactive)
            PROMPT_APP_VERSION="true"
            shift 1
            ;;
        --docker_version)
            DOCKER_VERSION="$2"
            shift 2
            ;;
        --compose_version)
            COMPOSE_VERSION="$2"
            shift 2
            ;;
        --source)
            case "$2" in
                official|custom|both)
                    SOURCES="$2"
                    ;;
                *)
                    echo "Invalid source: $2"
                    exit 1
                    ;;
            esac
            [[ "${SOURCES}" == "both" ]] && SOURCES="official custom"
            shift 2
            ;;
        --custom_repo)
            CUSTOM_REPO="$2"
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

if [[ "${PROMPT_APP_VERSION}" == "true" ]] && [[ -t 0 ]]; then
    read -rp "Detected version: ${APP_VERSION}. Enter version to use (leave empty to keep): " input_ver
    if [[ -n "${input_ver}" ]]; then
        APP_VERSION="${input_ver}"
        echo "Using version: ${APP_VERSION}"
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

download_with_candidates() {
    local dest="$1"
    local type="${2:-archive}"
    local min_size="${3:-0}"
    shift 3
    local urls=("$@")

    for url in "${urls[@]}"; do
        if download_if_missing "${url}" "${dest}" "${type}" "${min_size}"; then
            return 0
        fi
        echo "Try next source for ${dest}..."
    done
    return 1
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
    print("WARNING: PASSWORD_MASK marker missing, skip offline docker patch", file=sys.stderr)
    sys.exit(0)

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
    print("WARNING: Install_Docker marker missing, skip offline docker patch", file=sys.stderr)
    sys.exit(0)

content = content.replace(install_marker, helpers + "\n\n" + install_marker, 1)

prompt_marker = '    else\n        while true; do\n        read -p "$TXT_INSTALL_DOCKER_CONFIRM" install_docker_choice\n'
prompt_replacement = '    else\n        if [[ -f "${OFFLINE_DOCKER_TGZ}" ]]; then\n            install_docker_offline\n            return\n        fi\n        while true; do\n        read -p "$TXT_INSTALL_DOCKER_CONFIRM" install_docker_choice\n'
if prompt_marker not in content:
    print("WARNING: Docker install prompt marker missing, skip offline docker patch", file=sys.stderr)
    sys.exit(0)

content = content.replace(prompt_marker, prompt_replacement, 1)

tail_marker = '    fi\n}\n\nfunction Set_Port(){'
tail_replacement = '    fi\n    install_compose_offline\n}\n\nfunction Set_Port(){'
if tail_marker not in content:
    print("WARNING: Set_Port marker missing, skip offline docker patch", file=sys.stderr)
    sys.exit(0)

content = content.replace(tail_marker, tail_replacement, 1)

path.write_text(content)
PY
}

build_package_for_arch() {
    local source="$1"
    local arch="$2"

    local source_label="${source}"
    [[ "${source}" == "official" ]] && source_label="official" || source_label="custom"

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
        riscv64)
            APP_ARCH="riscv64"
            DOCKER_ARCH="riscv64"
            COMPOSE_ARCH="riscv64"
            ;;
        loong64|loongarch64)
            APP_ARCH="loong64"
            DOCKER_ARCH="loong64"
            COMPOSE_ARCH="loong64"
            ;;
        *)
            echo "Unsupported arch: ${arch}"
            exit 1
            ;;
    esac

    local package_dir="${BUILD_ROOT}/${APP_VERSION}/${source_label}"
    local offline_dir="${package_dir}/1panel-${APP_VERSION}-${source_label}-offline-linux-${APP_ARCH}"
    local offline_tar="${offline_dir}.tar.gz"

    mkdir -p "${package_dir}"
    rm -rf "${offline_dir}"
    mkdir -p "${offline_dir}"

    local app_tar="${CACHE_DIR}/${source_label}-1panel-${APP_VERSION}-linux-${APP_ARCH}.tar.gz"
    local app_url=""
    if [[ "${source}" == "official" ]]; then
        app_url="https://resource.fit2cloud.com/1panel/package/v2/${INSTALL_MODE}/${APP_VERSION}/release/1panel-${APP_VERSION}-linux-${APP_ARCH}.tar.gz"
    else
        app_url="https://github.com/${CUSTOM_REPO}/releases/download/${APP_VERSION}/1panel-${APP_VERSION}-linux-${APP_ARCH}.tar.gz"
    fi
    if ! download_if_missing "${app_url}" "${app_tar}"; then
        if [[ "${ALLOW_MISSING}" == "true" ]]; then
            echo "[WARN] Skip ${source_label}/${arch}: app package not found"
            SKIPPED_ARCHES+=("${source_label}/${arch}")
            return
        else
            handle_missing_arch "${arch}" "failed to download app package from ${source}"
            return
        fi
    fi

    local docker_versions=("${DOCKER_VERSION}")
    case "${DOCKER_ARCH}" in
        ppc64le|s390x|riscv64|loong64|loongarch64)
            docker_versions+=("24.0.7" "20.10.7")
            ;;
    esac

    local docker_tgz=""
    local chosen_docker_version=""
    for dv in "${docker_versions[@]}"; do
        local candidate_tgz="${CACHE_DIR}/docker-${dv}-${DOCKER_ARCH}.tgz"
        local docker_urls=()
        case "${DOCKER_ARCH}" in
            ppc64le)
                docker_urls+=(
                    "https://github.com/ppc64le-cloud/docker-ce-binaries-ppc64le/releases/download/v${dv}/docker-${dv}.tgz"
                    "https://github.com/wojiushixiaobai/docker-ce-binaries-ppc64le/releases/download/v${dv}/docker-${dv}.tgz"
                )
                ;;
            s390x)
                docker_urls+=(
                    "https://github.com/obsd90/docker-ce-binaries-s390x/releases/download/v${dv}/docker-${dv}.tgz"
                    "https://github.com/wojiushixiaobai/docker-ce-binaries-s390x/releases/download/v${dv}/docker-${dv}.tgz"
                )
                ;;
            loong64|loongarch64)
                docker_urls+=(
                    "https://github.com/loong64/docker-ce-packaging/releases/download/v${dv}/docker-${dv}.tgz"
                    "https://github.com/loongson-community/docker-ce-binaries-loongarch64/releases/download/v${dv}/docker-${dv}.tgz"
                )
                ;;
            riscv64)
                docker_urls+=(
                    "https://github.com/wojiushixiaobai/docker-ce-binaries-riscv64/releases/download/v${dv}/docker-${dv}.tgz"
                    "https://github.com/riscv-collab/docker-ce-binaries-riscv64/releases/download/v${dv}/docker-${dv}.tgz"
                )
                ;;
        esac
        # Always try official static tarball as a fallback at the end
        docker_urls+=("https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-${dv}.tgz")

        if download_with_candidates "${candidate_tgz}" "archive" "0" "${docker_urls[@]}"; then
            docker_tgz="${candidate_tgz}"
            chosen_docker_version="${dv}"
            break
        fi
    done

    if [[ -z "${docker_tgz}" ]]; then
        if [[ "${ALLOW_MISSING}" == "true" ]]; then
            echo "[WARN] Skip ${source_label}/${arch}: failed to download docker ${DOCKER_VERSION}"
            SKIPPED_ARCHES+=("${source_label}/${arch}")
            return
        else
            handle_missing_arch "${arch}" "failed to download docker ${DOCKER_VERSION} for ${DOCKER_ARCH}"
            return
        fi
    fi

    local compose_versions=("${COMPOSE_VERSION}")
    case "${COMPOSE_ARCH}" in
        ppc64le|s390x|armv7|armv6|loong64|loongarch64|riscv64)
            compose_versions+=("v2.23.0")
            ;;
    esac
    local compose_bin=""
    for cv in "${compose_versions[@]}"; do
        local candidate_bin="${CACHE_DIR}/docker-compose-${cv}-${COMPOSE_ARCH}"
        local compose_urls=(
            "https://github.com/docker/compose/releases/download/${cv}/docker-compose-linux-${COMPOSE_ARCH}"
        )
        case "${COMPOSE_ARCH}" in
            loong64|loongarch64)
                compose_urls+=("https://github.com/loong64/compose/releases/download/${cv}/docker-compose-linux-${COMPOSE_ARCH}")
                ;;
        esac
        if download_with_candidates "${candidate_bin}" "binary" "${MIN_COMPOSE_SIZE}" "${compose_urls[@]}"; then
            compose_bin="${candidate_bin}"
            break
        else
            echo "Try next compose version for ${COMPOSE_ARCH}..."
        fi
    done
    if [[ -z "${compose_bin}" ]]; then
        if [[ "${ALLOW_MISSING}" == "true" ]]; then
            echo "[WARN] Skip ${source_label}/${arch}: failed to download docker-compose for ${COMPOSE_ARCH}"
            SKIPPED_ARCHES+=("${source_label}/${arch}")
            return
        else
            handle_missing_arch "${arch}" "failed to download docker-compose for ${COMPOSE_ARCH}"
            return
        fi
    fi

    if ! tar -tf "${app_tar}" >/dev/null 2>&1; then
        if [[ "${ALLOW_MISSING}" == "true" ]]; then
            echo "[WARN] Skip ${source_label}/${arch}: app package invalid or unreadable at ${app_tar}"
            SKIPPED_ARCHES+=("${source_label}/${arch}")
            return
        else
            echo "[ERROR] App package invalid at ${app_tar}"
            exit 1
        fi
    fi

    tar -xf "${app_tar}" -C "${offline_dir}" --strip-components=1
    # embed sqlite3 fallback
    local sqlite_arch=""
    case "${APP_ARCH}" in
        amd64) sqlite_arch="x86_64" ;;
        arm64) sqlite_arch="aarch64" ;;
        armv7) sqlite_arch="armv7" ;;
        ppc64le) sqlite_arch="ppc64le" ;;
        s390x) sqlite_arch="s390x" ;;
        riscv64) sqlite_arch="riscv64" ;;
        loong64) sqlite_arch="loongarch64" ;;
    esac
    if [[ -n "${sqlite_arch}" ]]; then
        local sqlite_bin="${CACHE_DIR}/sqlite3-${sqlite_arch}-${SQLITE_VERSION}"
        local sqlite_urls=(
            "${SQLITE_URL_BASE}/v${SQLITE_VERSION}/sqlite3-linux-${sqlite_arch}.tar.gz"
        )
        if download_with_candidates "${sqlite_bin}.tar.gz" "archive" "0" "${sqlite_urls[@]}"; then
            mkdir -p "${CACHE_DIR}/sqlite-${sqlite_arch}"
            if tar -xf "${sqlite_bin}.tar.gz" -C "${CACHE_DIR}/sqlite-${sqlite_arch}"; then
                if [[ -f "${CACHE_DIR}/sqlite-${sqlite_arch}/sqlite3" ]]; then
                    cp -f "${CACHE_DIR}/sqlite-${sqlite_arch}/sqlite3" "${offline_dir}/sqlite3"
                    chmod +x "${offline_dir}/sqlite3"
                else
                    echo "[WARN] sqlite3 archive for ${sqlite_arch} missing sqlite3 binary"
                fi
            else
                echo "[WARN] sqlite3 archive for ${sqlite_arch} failed to extract"
            fi
        else
            echo "[WARN] sqlite3 download failed for ${sqlite_arch}, continuing without embedded sqlite3"
        fi
    fi
    if [[ -f "${docker_tgz}" ]]; then
        cp -f "${docker_tgz}" "${offline_dir}/docker.tgz"
    fi
    if [[ -f "${compose_bin}" ]]; then
        cp -f "${compose_bin}" "${offline_dir}/docker-compose"
        chmod +x "${offline_dir}/docker-compose"
    fi
    if [[ -f "${BASE_DIR}/docker.service" ]]; then
        cp -f "${BASE_DIR}/docker.service" "${offline_dir}/docker.service"
    fi
    if [[ -f "${BASE_DIR}/upgrade_offline.sh" ]]; then
        cp -f "${BASE_DIR}/upgrade_offline.sh" "${offline_dir}/upgrade.sh"
        chmod +x "${offline_dir}/upgrade.sh"
    fi

    patch_install_script "${offline_dir}/install.sh"

    tar -zcf "${offline_tar}" -C "${package_dir}" "$(basename "${offline_dir}")"
    echo "Built ${offline_tar}"
    OFFLINE_TARS+=("${offline_tar}")
    BUILT_ARCHES+=("${source_label}/${arch}")
}

for arch in ${ARCH_LIST}; do
    for source in ${SOURCES}; do
        build_package_for_arch "${source}" "${arch}"
    done
done

if [[ ${#OFFLINE_TARS[@]} -eq 0 ]]; then
    if [[ "${ALLOW_MISSING}" == "true" ]]; then
        echo "No offline packages were built (all sources/arches skipped)."
        exit 0
    else
        echo "No offline packages were built."
        exit 1
    fi
fi

cd "${BUILD_ROOT}/${APP_VERSION}"
sha256sum "${OFFLINE_TARS[@]}" | sed "s@${BUILD_ROOT}/${APP_VERSION}/@@" > checksums.txt
ls -lh .

echo "Built arches: ${BUILT_ARCHES[*]}"
if [[ ${#SKIPPED_ARCHES[@]} -gt 0 ]]; then
    echo "Skipped arches (missing artifacts): ${SKIPPED_ARCHES[*]}"
else
    echo "Skipped arches (missing artifacts): none"
fi
