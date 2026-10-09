#!/bin/bash
# Build 1Panel v2 offline installer packages.

set -euo pipefail

BASE_DIR=$(cd "$(dirname "$0")" || exit 1; pwd)
BUILD_ROOT="${BASE_DIR}/build"
CACHE_DIR="${BUILD_ROOT}/cache"

APP_VERSION=""
INSTALL_MODE="stable" # stable | beta | dev
DOCKER_VERSION=""
COMPOSE_VERSION=""
ARCH_LIST="amd64 arm64 armv7 ppc64le s390x loong64 riscv64"
MIN_COMPOSE_SIZE=8000000 # bytes, used to guard against partial downloads
ALLOW_MISSING="false"
SOURCES="official custom" # official | custom | both
CUSTOM_REPO="HandSonic/1Panel-Build-v2" # owner/repo for custom release packages
CUSTOM_PACKAGE_DIR=""
CUSTOM_SOURCE_URL=""
EXPECTED_BUILD_COMMIT=""
PROMPT_APP_VERSION="false"
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
  --custom_repo <owner/repo>    GitHub repo for custom release packages (default: HandSonic/1Panel-Build-v2)
  --custom-package-dir <dir>   Use downloaded, SHA-verified upstream CI archives
  --custom-source-url <url>    Exact upstream Actions run URL for CI provenance
  --expected-build-commit <sha> Required full upstream build commit for local CI inputs
  --docker_version <ver>        Exact Docker static version (default: verified per-arch pins)
  --compose_version <vX.Y.Z>    Exact Compose version (default: verified per-arch pins)
  --arch <list>                 Comma separated arch list, e.g. amd64,arm64 (default: amd64 arm64 armv7 ppc64le s390x loong64 riscv64)
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
        --custom-package-dir)
            CUSTOM_PACKAGE_DIR="$2"; shift 2 ;;
        --custom-source-url)
            CUSTOM_SOURCE_URL="$2"; shift 2 ;;
        --expected-build-commit)
            EXPECTED_BUILD_COMMIT="$2"; shift 2 ;;
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

[[ "${INSTALL_MODE}" =~ ^(stable|beta|dev)$ ]] || { echo "Invalid mode"; exit 1; }
[[ -z "${APP_VERSION}" || "${APP_VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || { echo "Invalid app version"; exit 1; }
[[ "${CUSTOM_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "Invalid repository"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

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

[[ "${APP_VERSION}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || { echo "Invalid resolved app version"; exit 1; }

mkdir -p "${CACHE_DIR}"

download_if_missing() {
    local url="$1"
    local dest="$2"
    local type="${3:-archive}" # archive | binary
    local min_size="${4:-0}"

    if [[ -f "${dest}" && -s "${dest}.source.json" ]] && python3 "${BASE_DIR}/scripts/validate_payload.py" cached "${dest}" "${url}"; then
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
    local status
    if curl --retry 3 --retry-delay 2 --progress-bar -fL "${url}" -o "${dest}.part"; then
        if ! mv -f "${dest}.part" "${dest}"; then
            rm -f "${dest}.part"
            return 1
        fi
    else
        status=$?
        echo "Download failed for ${url}"
        rm -f "${dest}" "${dest}.part" "${dest}.source.json"
        return "${status}"
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

    python3 "${BASE_DIR}/scripts/validate_payload.py" provenance "${dest}" "${url}" || return 1
    return 0
}

download_with_candidates() {
    local dest="$1"
    local type="${2:-archive}"
    local min_size="${3:-0}"
    shift 3
    local urls=("$@")
    local url

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
    python3 "${BASE_DIR}/scripts/patch_installer.py" "$1"
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
    local expected_app_sha=""
    if [[ "${source}" == "official" ]]; then
        local pinned_app_url=""
        read -r pinned_app_url expected_app_sha < <(python3 "${BASE_DIR}/scripts/official_source.py" pin "${APP_VERSION}" "${APP_ARCH}")
        [[ "${pinned_app_url}" == "${app_url}" && "${expected_app_sha}" =~ ^[0-9a-f]{64}$ ]] || { echo "No reviewed official source contract for requested version/channel"; exit 1; }
        if [[ -f "${app_tar}" ]] && [[ "$(sha256sum "${app_tar}" | cut -d' ' -f1)" != "${expected_app_sha}" ]]; then
            rm -f "${app_tar}" "${app_tar}.source.json"
        fi
    fi
    if [[ "${source}" == "custom" && -n "${CUSTOM_PACKAGE_DIR}" ]]; then
        python3 "${BASE_DIR}/scripts/import_ci_artifact.py" "${CUSTOM_PACKAGE_DIR}" "${app_tar}" \
            "1panel-${APP_VERSION}-linux-${APP_ARCH}.tar.gz" "${CUSTOM_SOURCE_URL}" "${CUSTOM_REPO}" "${EXPECTED_BUILD_COMMIT}"
    else
        if [[ "${source}" == "custom" ]]; then
            # Repaired upstream assets can retain the URL: authoritative hash precedes cache reuse.
            local checksum_file="${app_tar}.upstream.sha256"
            curl --retry 3 --connect-timeout 15 --max-time 90 -fsSL "${app_url}.sha256" -o "${checksum_file}.part"
            expected_app_sha=$(python3 "${BASE_DIR}/scripts/validate_upstream.py" checksum "${checksum_file}.part" "1panel-${APP_VERSION}-linux-${APP_ARCH}.tar.gz")
            mv -f "${checksum_file}.part" "${checksum_file}"
            if [[ -f "${app_tar}" ]] && [[ "$(sha256sum "${app_tar}" | cut -d' ' -f1)" != "${expected_app_sha}" ]]; then
                rm -f "${app_tar}" "${app_tar}.source.json"
            fi
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

        if [[ -n "${expected_app_sha}" ]] && [[ "$(sha256sum "${app_tar}" | cut -d' ' -f1)" != "${expected_app_sha}" ]]; then
            echo "App package differs from reviewed upstream SHA-256"
            exit 1
        fi
        if [[ "${source}" == "official" ]]; then
            python3 "${BASE_DIR}/scripts/official_source.py" verify "${app_tar}" "${APP_VERSION}" "${APP_ARCH}" "${app_url}"
        fi

        if [[ "${source}" == "custom" ]]; then
            printf '%s\n' '{"source_kind":"release"}' > "${app_tar}.origin.json"
        fi
    fi

    if [[ "${source}" == "custom" ]]; then
        # Both canonical-release and verified-CI inputs use the same pinned producer gate.
        python3 "${BASE_DIR}/scripts/validate_upstream_package.py" "${app_tar}" "${APP_VERSION}" "${APP_ARCH}"
    fi

    local docker_tgz=""
    local chosen_docker_version=""
    local docker_url="" docker_sha=""
    read -r chosen_docker_version docker_url docker_sha < <(python3 - "${BASE_DIR}/docker-sources.json" "${APP_ARCH}" <<'PYLOCK'
import json,sys
p=json.load(open(sys.argv[1]))[sys.argv[2]]
print(p['version'],p['url'],p['sha256'])
PYLOCK
    )
    if [[ -n "${DOCKER_VERSION}" && "${DOCKER_VERSION}" != "${chosen_docker_version}" ]]; then
        # An explicit version never silently falls back to a different version.
        docker_url="${docker_url//${chosen_docker_version}/${DOCKER_VERSION}}"
        chosen_docker_version="${DOCKER_VERSION}"
        docker_sha=""
    fi
    local candidate_tgz="${CACHE_DIR}/docker-${chosen_docker_version}-${DOCKER_ARCH}.tgz"
    if download_if_missing "${docker_url}" "${candidate_tgz}" &&
        python3 "${BASE_DIR}/scripts/validate_payload.py" docker "${candidate_tgz}" "${APP_ARCH}" >/dev/null; then
        if [[ -z "${docker_sha}" ]] || [[ "$(sha256sum "${candidate_tgz}" | cut -d' ' -f1)" == "${docker_sha}" ]]; then
            docker_tgz="${candidate_tgz}"
        else
            echo "Docker checksum mismatch: ${candidate_tgz}"
        fi
    fi

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

    local compose_bin="" chosen_compose_version="" compose_url="" compose_sha=""
    read -r chosen_compose_version compose_url compose_sha < <(python3 - "${BASE_DIR}/compose-sources.json" "${APP_ARCH}" <<'PYLOCK'
import json,sys
p=json.load(open(sys.argv[1]))[sys.argv[2]]
print(p['version'],p['url'],p['sha256'])
PYLOCK
    )
    if [[ -n "${COMPOSE_VERSION}" && "${COMPOSE_VERSION}" != "${chosen_compose_version}" ]]; then
        compose_url="${compose_url//${chosen_compose_version}/${COMPOSE_VERSION}}"
        chosen_compose_version="${COMPOSE_VERSION}"
        compose_sha=""
    fi
    local candidate_bin="${CACHE_DIR}/docker-compose-${chosen_compose_version}-${COMPOSE_ARCH}"
    if download_if_missing "${compose_url}" "${candidate_bin}" binary "${MIN_COMPOSE_SIZE}" &&
        python3 "${BASE_DIR}/scripts/validate_payload.py" elf "${candidate_bin}" "${APP_ARCH}"; then
        if [[ -z "${compose_sha}" ]] || [[ "$(sha256sum "${candidate_bin}" | cut -d' ' -f1)" == "${compose_sha}" ]]; then
            compose_bin="${candidate_bin}"
        else
            echo "Compose checksum mismatch: ${candidate_bin}"
        fi
    fi
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

    python3 "${BASE_DIR}/scripts/validate_payload.py" archive "${app_tar}" "1panel-${APP_VERSION}-linux-${APP_ARCH}"
    tar -xf "${app_tar}" -C "${offline_dir}" --strip-components=1

    if [[ "${source}" == "custom" ]]; then
        python3 "${BASE_DIR}/scripts/validate_upstream.py" "${offline_dir}" "${APP_ARCH}" "${APP_VERSION}" "${EXPECTED_BUILD_COMMIT}"
    fi

    # Do not mix mutable, unversioned init scripts into a versioned package.
    # Required application and init payloads are checked by manifest validation.

    # Every advertised offline package must contain all mandatory payloads.
    cp -f "${docker_tgz}" "${offline_dir}/docker.tgz"
    cp -f "${compose_bin}" "${offline_dir}/docker-compose"
    chmod +x "${offline_dir}/docker-compose"
    cp -f "${BASE_DIR}/docker.service" "${offline_dir}/docker.service"
    cp -f "${BASE_DIR}/upgrade_offline.sh" "${offline_dir}/upgrade.sh"
    chmod +x "${offline_dir}/upgrade.sh"

    patch_install_script "${offline_dir}/install.sh"

    python3 "${BASE_DIR}/scripts/validate_payload.py" manifest "${offline_dir}" "${APP_ARCH}" "${source_label}" \
        "${APP_VERSION}" "${chosen_docker_version}" "${chosen_compose_version}" "${app_tar}" "${docker_tgz}" "${compose_bin}"
    bash -n "${offline_dir}/install.sh"
    tar -zcf "${offline_tar}.part" -C "${package_dir}" "$(basename "${offline_dir}")"
    tar -tzf "${offline_tar}.part" >/dev/null
    mv -f "${offline_tar}.part" "${offline_tar}"
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
        exit 1
    else
        echo "No offline packages were built."
        exit 1
    fi
fi

cd "${BUILD_ROOT}/${APP_VERSION}"
# Release assets are flat: checksum names must be basenames, not source paths.
: > checksums.txt
for file in "${OFFLINE_TARS[@]}"; do
    hash=$(sha256sum "$file")
    printf '%s  %s\n' "${hash%% *}" "$(basename "$file")" >> checksums.txt
done
ls -lh .

echo "Built arches: ${BUILT_ARCHES[*]}"
if [[ ${#SKIPPED_ARCHES[@]} -gt 0 ]]; then
    echo "Skipped arches (missing artifacts): ${SKIPPED_ARCHES[*]}"
else
    echo "Skipped arches (missing artifacts): none"
fi
