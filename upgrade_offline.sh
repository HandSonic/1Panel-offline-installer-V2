#!/bin/bash
# Offline upgrade script for 1Panel v2

set -euo pipefail

CURRENT_DIR=$(cd "$(dirname "$0")" || exit 1; pwd)
LOG_FILE="${CURRENT_DIR}/upgrade.log"

log() {
    echo "[upgrade] $*" | tee -a "${LOG_FILE}"
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
    case "${SERVICE_MGR}" in
        systemd)
            cp -f "${CURRENT_DIR}/initscript/1panel-core.service" /etc/systemd/system
            cp -f "${CURRENT_DIR}/initscript/1panel-agent.service" /etc/systemd/system
            ;;
        openrc)
            cp -f "${CURRENT_DIR}/initscript/1panel-core.openrc" /etc/init.d/1panel-core
            cp -f "${CURRENT_DIR}/initscript/1panel-agent.openrc" /etc/init.d/1panel-agent
            chmod +x /etc/init.d/1panel-core /etc/init.d/1panel-agent
            ;;
        *)
            cp -f "${CURRENT_DIR}/initscript/1panel-core.init" /etc/init.d/1panel-core
            cp -f "${CURRENT_DIR}/initscript/1panel-agent.init" /etc/init.d/1panel-agent
            chmod +x /etc/init.d/1panel-core /etc/init.d/1panel-agent
            ;;
    esac
}

get_host_ip() {
    local ip
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [[ -z "${ip}" ]]; then
        ip=$(ip -4 addr show 2>/dev/null | awk '/inet / && $2 !~ /^127/ {print $2}' | cut -d/ -f1 | head -n1)
    fi
    echo "${ip:-127.0.0.1}"
}

main() {
    require_root
    require_installed
    detect_service_mgr

    # Allow manual override for non-standard installs
    PANEL_BASE_DIR=${PANEL_BASE_DIR_OVERRIDE:-$(read_conf BASE_DIR)}
    PANEL_PORT=$(read_conf ORIGINAL_PORT)
    PANEL_USER=$(read_conf ORIGINAL_USERNAME)
    PANEL_PASSWORD=$(read_conf ORIGINAL_PASSWORD)
    PANEL_ENTRANCE=$(read_conf ORIGINAL_ENTRANCE)
    PANEL_LANG=$(read_conf LANGUAGE)
    CHANGE_USER_INFO=$(read_conf CHANGE_USER_INFO)

    if [[ -z "${PANEL_BASE_DIR}" || ! -d "${PANEL_BASE_DIR}" ]]; then
        echo "Cannot detect install directory (BASE_DIR). Set PANEL_BASE_DIR_OVERRIDE to the correct path and re-run."
        exit 1
    fi

    log "Detected config: dir=${PANEL_BASE_DIR} port=${PANEL_PORT} user=${PANEL_USER} entrance=${PANEL_ENTRANCE} lang=${PANEL_LANG}"

    log "Stopping 1Panel services..."
    stop_services

    log "Updating binaries and resources..."
    cp -f "${CURRENT_DIR}/1panel-core" /usr/local/bin
    cp -f "${CURRENT_DIR}/1panel-agent" /usr/local/bin
    cp -f "${CURRENT_DIR}/1pctl" /usr/local/bin
    chmod 700 /usr/local/bin/1panel-core /usr/local/bin/1panel-agent /usr/local/bin/1pctl
    cp -rf "${CURRENT_DIR}/lang" /usr/local/bin

    RUN_BASE_DIR="${PANEL_BASE_DIR}/1panel"
    mkdir -p "${RUN_BASE_DIR}/geo"
    cp -f "${CURRENT_DIR}/GeoIP.mmdb" "${RUN_BASE_DIR}/geo/GeoIP.mmdb"

    # Preserve existing config into new 1pctl
    sed -i \
        -e "s#BASE_DIR=.*#BASE_DIR=${PANEL_BASE_DIR}#g" \
        -e "s#ORIGINAL_PORT=.*#ORIGINAL_PORT=${PANEL_PORT}#g" \
        -e "s#ORIGINAL_USERNAME=.*#ORIGINAL_USERNAME=${PANEL_USER}#g" \
        -e "s#ORIGINAL_PASSWORD=.*#ORIGINAL_PASSWORD=${PANEL_PASSWORD}#g" \
        -e "s#ORIGINAL_ENTRANCE=.*#ORIGINAL_ENTRANCE=${PANEL_ENTRANCE}#g" \
        /usr/local/bin/1pctl
    if [[ -n "${PANEL_LANG}" ]]; then
        sed -i -e "s#LANGUAGE=.*#LANGUAGE=${PANEL_LANG}#g" /usr/local/bin/1pctl
    fi
    if grep -q "^CHANGE_USER_INFO=" /usr/local/bin/1pctl; then
        if [[ -n "${CHANGE_USER_INFO}" ]]; then
            sed -i -e "s#^CHANGE_USER_INFO=.*#CHANGE_USER_INFO=${CHANGE_USER_INFO}#g" /usr/local/bin/1pctl
        fi
    elif [[ -n "${CHANGE_USER_INFO}" ]]; then
        echo "CHANGE_USER_INFO=${CHANGE_USER_INFO}" >> /usr/local/bin/1pctl
    fi

    NEW_VERSION=$(read_conf ORIGINAL_VERSION)
    CORE_DB="${PANEL_BASE_DIR}/1panel/db/core.db"
    AGENT_DB="${PANEL_BASE_DIR}/1panel/db/agent.db"
    SQLITE_BIN="sqlite3"
    if ! command -v sqlite3 >/dev/null 2>&1 && [[ -x "${CURRENT_DIR}/sqlite3" ]]; then
        SQLITE_BIN="${CURRENT_DIR}/sqlite3"
    fi
    if command -v "${SQLITE_BIN}" >/dev/null 2>&1; then
        if [[ -f "${CORE_DB}" ]]; then
            "${SQLITE_BIN}" "${CORE_DB}" "UPDATE settings SET value='${NEW_VERSION}' WHERE key='SystemVersion';"
        fi
        if [[ -f "${AGENT_DB}" ]]; then
            "${SQLITE_BIN}" "${AGENT_DB}" "UPDATE settings SET value='${NEW_VERSION}' WHERE key='SystemVersion';"
        fi
    else
        log "WARN: sqlite3 not found; skip updating SystemVersion in DB"
    fi

    log "Updating service units..."
    install_units

    log "Starting 1Panel services..."
    start_services

    PANEL_HOST=$(get_host_ip)
    log "Upgrade finished."
    log "Panel: http://${PANEL_HOST}:${PANEL_PORT}/${PANEL_ENTRANCE}"
    log "User: ${PANEL_USER}"
}

main
