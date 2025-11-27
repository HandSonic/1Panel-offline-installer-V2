#!/bin/bash
# Offline upgrade script for 1Panel v2

set -uo pipefail

CURRENT_DIR=$(cd "$(dirname "$0")" || exit 1; pwd)
LOG_FILE="${CURRENT_DIR}/upgrade.log"
BACKUP_DIR="${CURRENT_DIR}/backup_$(date +%Y%m%d_%H%M%S)"

log() {
    echo "[upgrade] $*" | tee -a "${LOG_FILE}"
}

error_exit() {
    log "ERROR: $1"
    exit 1
}

check_required_files() {
    local missing=0
    for f in 1panel-core 1panel-agent 1pctl GeoIP.mmdb; do
        if [[ ! -f "${CURRENT_DIR}/${f}" ]]; then
            log "ERROR: Required file missing: ${f}"
            missing=1
        fi
    done
    if [[ ! -d "${CURRENT_DIR}/lang" ]]; then
        log "ERROR: Required directory missing: lang/"
        missing=1
    fi
    if [[ ! -d "${CURRENT_DIR}/initscript" ]]; then
        log "WARN: Directory missing: initscript/ (optional for older versions)"
    fi
    if [[ $missing -eq 1 ]]; then
        error_exit "Please ensure all required files exist in ${CURRENT_DIR}"
    fi
}

backup_current() {
    log "Creating backup at ${BACKUP_DIR}..."
    mkdir -p "${BACKUP_DIR}"
    for f in /usr/local/bin/1panel-core /usr/local/bin/1panel-agent /usr/local/bin/1pctl; do
        if [[ -f "$f" ]]; then
            cp -f "$f" "${BACKUP_DIR}/" 2>/dev/null || true
        fi
    done
    if [[ -d /usr/local/bin/lang ]]; then
        cp -rf /usr/local/bin/lang "${BACKUP_DIR}/" 2>/dev/null || true
    fi
}

rollback() {
    log "Rolling back to previous version..."
    if [[ -d "${BACKUP_DIR}" ]]; then
        for f in 1panel-core 1panel-agent 1pctl; do
            if [[ -f "${BACKUP_DIR}/${f}" ]]; then
                cp -f "${BACKUP_DIR}/${f}" /usr/local/bin/
            fi
        done
        if [[ -d "${BACKUP_DIR}/lang" ]]; then
            cp -rf "${BACKUP_DIR}/lang" /usr/local/bin/
        fi
        log "Rollback completed."
    else
        log "WARN: No backup found, cannot rollback."
    fi
}

require_root() {
    if [[ $EUID -ne 0 ]]; then
        echo "Please run as root."
        exit 1
    fi
}

require_installed() {
    if [[ ! -f /usr/local/bin/1pctl ]]; then
        echo "/usr/local/bin/1pctl not found, please run install first."
        exit 1
    fi
}

read_conf() {
    local key=$1
    local line
    line=$(grep -m1 "^${key}=" /usr/local/bin/1pctl 2>/dev/null || true)
    echo "${line#*=}"
}

detect_service_mgr() {
    if command -v systemctl >/dev/null 2>&1; then
        SERVICE_MGR="systemd"
    elif command -v rc-service >/dev/null 2>&1; then
        SERVICE_MGR="openrc"
    else
        SERVICE_MGR="sysvinit"
    fi
}

stop_services() {
    case "${SERVICE_MGR}" in
        systemd)
            systemctl stop 1panel-core.service >/dev/null 2>&1 || true
            systemctl stop 1panel-agent.service >/dev/null 2>&1 || true
            ;;
        openrc)
            rc-service 1panel-core stop >/dev/null 2>&1 || true
            rc-service 1panel-agent stop >/dev/null 2>&1 || true
            ;;
        *)
            service 1panel-core stop >/dev/null 2>&1 || true
            service 1panel-agent stop >/dev/null 2>&1 || true
            ;;
    esac
}

start_services() {
    case "${SERVICE_MGR}" in
        systemd)
            systemctl daemon-reload
            systemctl enable 1panel-core.service >/dev/null 2>&1 || true
            systemctl enable 1panel-agent.service >/dev/null 2>&1 || true
            systemctl start 1panel-core.service
            systemctl start 1panel-agent.service
            ;;
        openrc)
            rc-service 1panel-core start
            rc-service 1panel-agent start
            ;;
        *)
            service 1panel-core start
            service 1panel-agent start
            ;;
    esac
}

install_units() {
    local src_core="" src_agent="" dst_core="" dst_agent=""
    local fallback_core="" fallback_agent=""

    case "${SERVICE_MGR}" in
        systemd)
            src_core="${CURRENT_DIR}/initscript/1panel-core.service"
            src_agent="${CURRENT_DIR}/initscript/1panel-agent.service"
            fallback_core="${CURRENT_DIR}/1panel-core.service"
            fallback_agent="${CURRENT_DIR}/1panel-agent.service"
            dst_core="/etc/systemd/system/1panel-core.service"
            dst_agent="/etc/systemd/system/1panel-agent.service"
            ;;
        openrc)
            src_core="${CURRENT_DIR}/initscript/1panel-core.openrc"
            src_agent="${CURRENT_DIR}/initscript/1panel-agent.openrc"
            fallback_core="${CURRENT_DIR}/1panel-core.openrc"
            fallback_agent="${CURRENT_DIR}/1panel-agent.openrc"
            dst_core="/etc/init.d/1panel-core"
            dst_agent="/etc/init.d/1panel-agent"
            ;;
        *)
            src_core="${CURRENT_DIR}/initscript/1panel-core.init"
            src_agent="${CURRENT_DIR}/initscript/1panel-agent.init"
            fallback_core="${CURRENT_DIR}/1panel-core.init"
            fallback_agent="${CURRENT_DIR}/1panel-agent.init"
            dst_core="/etc/init.d/1panel-core"
            dst_agent="/etc/init.d/1panel-agent"
            ;;
    esac

    # Core service: 优先initscript目录，回退到根目录
    if [[ -f "${src_core}" ]]; then
        cp -f "${src_core}" "${dst_core}"
        [[ "${SERVICE_MGR}" != "systemd" ]] && chmod +x "${dst_core}"
    elif [[ -f "${fallback_core}" ]]; then
        log "Using fallback service file: ${fallback_core}"
        cp -f "${fallback_core}" "${dst_core}"
        [[ "${SERVICE_MGR}" != "systemd" ]] && chmod +x "${dst_core}"
    else
        log "WARN: Service unit not found: ${src_core} or ${fallback_core}"
    fi

    # Agent service: 优先initscript目录，回退到根目录
    if [[ -f "${src_agent}" ]]; then
        cp -f "${src_agent}" "${dst_agent}"
        [[ "${SERVICE_MGR}" != "systemd" ]] && chmod +x "${dst_agent}"
    elif [[ -f "${fallback_agent}" ]]; then
        log "Using fallback service file: ${fallback_agent}"
        cp -f "${fallback_agent}" "${dst_agent}"
        [[ "${SERVICE_MGR}" != "systemd" ]] && chmod +x "${dst_agent}"
    else
        log "WARN: Service unit not found: ${src_agent} or ${fallback_agent}"
    fi
}

get_host_ip() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [[ -z "${ip}" ]]; then
        ip=$(ip -4 addr show 2>/dev/null | awk '/inet / && $2 !~ /^127/ {print $2}' | cut -d/ -f1 | head -n1)
    fi
    echo "${ip:-127.0.0.1}"
}

update_1pctl_config() {
    local key="$1"
    local value="$2"
    local file="/usr/local/bin/1pctl"

    # Use python for safe config update (handles special characters properly)
    if command -v python3 >/dev/null 2>&1; then
        python3 - "${file}" "${key}" "${value}" <<'PY'
import sys, re
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, 'r') as f:
    content = f.read()
pattern = rf'^{re.escape(key)}=.*$'
if re.search(pattern, content, re.MULTILINE):
    content = re.sub(pattern, f'{key}={value}', content, flags=re.MULTILINE)
else:
    content += f'\n{key}={value}'
with open(path, 'w') as f:
    f.write(content)
PY
        return
    fi

    # Fallback to sed (escape special chars)
    local escaped_value
    escaped_value=$(printf '%s\n' "${value}" | sed 's/[&/\]/\\&/g')
    if grep -q "^${key}=" "${file}"; then
        sed -i "s|^${key}=.*|${key}=${escaped_value}|g" "${file}"
    else
        echo "${key}=${value}" >> "${file}"
    fi
}

main() {
    require_root
    require_installed
    check_required_files
    detect_service_mgr

    # Read existing config before any changes
    PANEL_BASE_DIR=${PANEL_BASE_DIR_OVERRIDE:-$(read_conf BASE_DIR)}
    PANEL_PORT=$(read_conf ORIGINAL_PORT)
    PANEL_USER=$(read_conf ORIGINAL_USERNAME)
    PANEL_PASSWORD=$(read_conf ORIGINAL_PASSWORD)
    PANEL_ENTRANCE=$(read_conf ORIGINAL_ENTRANCE)
    PANEL_LANG=$(read_conf LANGUAGE)
    CHANGE_USER_INFO=$(read_conf CHANGE_USER_INFO)

    if [[ -z "${PANEL_BASE_DIR}" || ! -d "${PANEL_BASE_DIR}" ]]; then
        error_exit "Cannot detect install directory (BASE_DIR). Set PANEL_BASE_DIR_OVERRIDE to the correct path and re-run."
    fi

    # Get new version from the package (not from installed 1pctl)
    NEW_VERSION=$(grep -m1 "^ORIGINAL_VERSION=" "${CURRENT_DIR}/1pctl" 2>/dev/null | cut -d= -f2)
    if [[ -z "${NEW_VERSION}" ]]; then
        error_exit "Cannot determine new version from package 1pctl"
    fi

    log "Upgrade to ${NEW_VERSION}"
    log "Detected config: dir=${PANEL_BASE_DIR} port=${PANEL_PORT} user=${PANEL_USER} entrance=${PANEL_ENTRANCE} lang=${PANEL_LANG}"

    # Create backup before making changes
    backup_current

    log "Stopping 1Panel services..."
    stop_services

    log "Updating binaries and resources..."
    if ! cp -f "${CURRENT_DIR}/1panel-core" /usr/local/bin ||
       ! cp -f "${CURRENT_DIR}/1panel-agent" /usr/local/bin ||
       ! cp -f "${CURRENT_DIR}/1pctl" /usr/local/bin; then
        log "ERROR: Failed to copy binaries"
        rollback
        start_services
        error_exit "Upgrade failed during binary copy"
    fi
    chmod 700 /usr/local/bin/1panel-core /usr/local/bin/1panel-agent /usr/local/bin/1pctl
    cp -rf "${CURRENT_DIR}/lang" /usr/local/bin

    RUN_BASE_DIR="${PANEL_BASE_DIR}/1panel"
    mkdir -p "${RUN_BASE_DIR}/geo"
    cp -f "${CURRENT_DIR}/GeoIP.mmdb" "${RUN_BASE_DIR}/geo/GeoIP.mmdb"

    # Preserve existing config into new 1pctl (using safe update function)
    log "Restoring configuration..."
    update_1pctl_config "BASE_DIR" "${PANEL_BASE_DIR}"
    update_1pctl_config "ORIGINAL_PORT" "${PANEL_PORT}"
    update_1pctl_config "ORIGINAL_USERNAME" "${PANEL_USER}"
    update_1pctl_config "ORIGINAL_PASSWORD" "${PANEL_PASSWORD}"
    update_1pctl_config "ORIGINAL_ENTRANCE" "${PANEL_ENTRANCE}"
    [[ -n "${PANEL_LANG}" ]] && update_1pctl_config "LANGUAGE" "${PANEL_LANG}"
    [[ -n "${CHANGE_USER_INFO}" ]] && update_1pctl_config "CHANGE_USER_INFO" "${CHANGE_USER_INFO}"

    # Update SystemVersion in database
    CORE_DB="${PANEL_BASE_DIR}/1panel/db/core.db"
    AGENT_DB="${PANEL_BASE_DIR}/1panel/db/agent.db"
    DB_UPDATED=0

    if command -v python3 >/dev/null 2>&1; then
        for DB in "${CORE_DB}" "${AGENT_DB}"; do
            if [[ -f "${DB}" ]]; then
                if python3 - "$DB" "$NEW_VERSION" <<'PY'
import sqlite3, sys
try:
    db, ver = sys.argv[1], sys.argv[2]
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("UPDATE settings SET value=? WHERE key='SystemVersion'", (ver,))
    conn.commit()
    conn.close()
except Exception as e:
    print(f"DB update error: {e}", file=sys.stderr)
    sys.exit(1)
PY
                then
                    DB_UPDATED=1
                fi
            fi
        done
    fi

    # Fallback to system sqlite3 command
    if [[ $DB_UPDATED -eq 0 ]]; then
        if command -v sqlite3 >/dev/null 2>&1; then
            for DB in "${CORE_DB}" "${AGENT_DB}"; do
                if [[ -f "${DB}" ]]; then
                    sqlite3 "${DB}" "UPDATE settings SET value='${NEW_VERSION}' WHERE key='SystemVersion';" 2>/dev/null || true
                fi
            done
        else
            log "WARN: python3/sqlite3 not found; skip updating SystemVersion in DB"
        fi
    fi

    log "Updating service units..."
    install_units

    log "Starting 1Panel services..."
    if ! start_services; then
        log "WARN: Service start may have issues, check status manually"
    fi

    # Verify services are running
    sleep 2
    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl is-active --quiet 1panel-core.service; then
            log "WARN: 1panel-core service may not be running properly"
        fi
    fi

    PANEL_HOST=$(get_host_ip)
    log "Upgrade finished successfully."
    log "Panel: http://${PANEL_HOST}:${PANEL_PORT}/${PANEL_ENTRANCE}"
    log "User: ${PANEL_USER}"
    log "Backup saved at: ${BACKUP_DIR}"
}

main
