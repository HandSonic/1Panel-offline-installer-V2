#!/bin/bash
# Transactional offline upgrade. No Docker/Compose changes and no network access.
set -uo pipefail

CURRENT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || exit 1
LOG_FILE="${CURRENT_DIR}/upgrade.log"
BACKUP_DIR=""
TRANSACTION=0
SERVICES_STOPPED=0
BIN_DIR=/usr/local/bin
HEALTH_TIMEOUT=${PANEL_HEALTH_TIMEOUT:-60}
FORCE=${PANEL_UPGRADE_FORCE:-0}
STAGED_CONFIG=""
LOCK_DIR=""
declare -A WAS_ACTIVE=()

log() { printf '[upgrade] %s\n' "$*" | tee -a "$LOG_FILE"; }
error_exit() { log "ERROR: $*"; exit 1; }

# Read literal header assignments only. Never execute an installed shell script
# to obtain configuration; passwords and future settings may contain shell text.
header_assignments() {
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(function[[:space:]]+|[a-zA-Z_][a-zA-Z_0-9]*[[:space:]]*\(\)) ]] && break
        if [[ "$line" =~ ^(export[[:space:]]+)?([A-Z_][A-Z_0-9]*)= ]]; then
            printf '%s\n' "${line#export }"
        fi
    done < "$1"
}

# Decode shell literal quoting without eval/source or variable expansion.
decode_literal() {
    local input=$1 output="" quote="" char next i
    for (( i=0; i<${#input}; i++ )); do
        char=${input:i:1}
        if [[ "$quote" == single ]]; then
            if [[ "$char" == "'" ]]; then quote=""; else output+=$char; fi
        elif [[ "$char" == "\\" ]]; then
            i=$((i + 1)); (( i < ${#input} )) || return 1
            next=${input:i:1}
            if [[ "$quote" == double && "$next" != '$' && "$next" != '`' && "$next" != '"' && "$next" != "\\" ]]; then output+="\\"; fi
            output+=$next
        elif [[ "$char" == '"' ]]; then
            if [[ "$quote" == double ]]; then quote=""; else quote=double; fi
        elif [[ "$char" == "'" && -z "$quote" ]]; then quote=single
        elif [[ "$char" == '$' || "$char" == '`' ]]; then return 1
        else output+=$char; fi
    done
    [[ -z "$quote" ]] || return 1
    printf '%s\n' "$output"
}

read_conf() {
    local line
    while IFS= read -r line; do
        [[ "${line%%=*}" == "$2" ]] || continue
        decode_literal "${line#*=}"
        return $?
    done <<< "$(header_assignments "$1")"
    return 1
}

merge_config() {
    local old=$1 new=$2 output=$3 line key in_header=1
    local -A saved=() used=()
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        key=${line%%=*}
        [[ "$key" == ORIGINAL_VERSION ]] || saved["$key"]=$line
    done <<< "$(header_assignments "$old")"
    # 1Panel's Go ctl_conf reader consumes raw KEY=value text and does not
    # unquote shell strings. Preserve the installed representation verbatim.
    # Overrides are validated as raw shell-safe paths by main before use.
    if [[ -n "${PANEL_BASE_DIR_OVERRIDE:-}" ]]; then
        saved[BASE_DIR]="BASE_DIR=$PANEL_BASE_DIR_OVERRIDE"
    fi
    : > "$output" || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        if (( in_header )) && [[ "$line" =~ ^[[:space:]]*(function[[:space:]]+|[a-zA-Z_][a-zA-Z_0-9]*[[:space:]]*\(\)|if[[:space:]]|case[[:space:]]|source[[:space:]]) ]]; then
            # New upstream versions may remove a setting. Keep it before code
            # that consumes it, instead of appending after the CLI dispatch.
            for key in "${!saved[@]}"; do
                if [[ ! ${used[$key]+present} ]]; then
                    printf '%s\n' "${saved[$key]}" >> "$output" || return 1
                    used[$key]=1
                fi
            done
            in_header=0
        fi
        if (( in_header )) && [[ "$line" =~ ^(export[[:space:]]+)?([A-Z_][A-Z_0-9]*)= ]]; then
            key=${BASH_REMATCH[2]}
            if [[ ${saved[$key]+present} ]]; then line=${saved[$key]}; used[$key]=1; fi
        fi
        printf '%s\n' "$line" >> "$output" || return 1
    done < "$new"
    for key in "${!saved[@]}"; do
        [[ ${used[$key]+present} ]] || printf '%s\n' "${saved[$key]}" >> "$output" || return 1
    done
    bash -n "$output"
}

normalize_arch() {
    case "$1" in
        x86_64|amd64) echo amd64;; aarch64|arm64) echo arm64;;
        armv7*|armhf|armv6*) echo armv7;; loongarch64|loong64) echo loong64;;
        ppc64le|s390x|riscv64) echo "$1";; *) echo "$1";;
    esac
}

prerelease_is_lower() {
    local new=$1 old=$2 i a b
    new=${new#v}; old=${old#v}
    new=${new%%+*}; old=${old%%+*}
    [[ "$new" =~ [-_] ]] || return 1
    [[ "$old" =~ [-_] ]] || return 0
    new=${new#*[-_]}; old=${old#*[-_]}
    local -a new_tokens old_tokens
    IFS=. read -ra new_tokens <<< "$new"
    IFS=. read -ra old_tokens <<< "$old"
    for (( i=0; i<${#old_tokens[@]} || i<${#new_tokens[@]}; i++ )); do
        (( i < ${#new_tokens[@]} )) || return 0
        (( i < ${#old_tokens[@]} )) || return 1
        a=${new_tokens[i]}; b=${old_tokens[i]}
        [[ "$a" == "$b" ]] && continue
        if [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]]; then
            (( 10#$a < 10#$b )); return $?
        elif [[ "$a" =~ ^[0-9]+$ ]]; then return 0
        elif [[ "$b" =~ ^[0-9]+$ ]]; then return 1
        else [[ "$a" < "$b" ]]; return $?; fi
    done
    return 1
}

check_compatibility() {
    local old_version new_numeric old_numeric host arch="" description name
    old_version=$(read_conf "$BIN_DIR/1pctl" ORIGINAL_VERSION)
    NEW_VERSION=$(read_conf "$CURRENT_DIR/1pctl" ORIGINAL_VERSION)
    [[ "$NEW_VERSION" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+([.+_-][a-zA-Z0-9._-]+)?$ ]] || error_exit "Cannot read a valid package version"
    if [[ "$old_version" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+ ]]; then
        new_numeric=${NEW_VERSION#v}; new_numeric=${new_numeric%%[-+_]*}
        old_numeric=${old_version#v}; old_numeric=${old_numeric%%[-+_]*}
        local -a old_parts new_parts
        IFS=. read -ra old_parts <<< "$old_numeric"
        IFS=. read -ra new_parts <<< "$new_numeric"
        local i lower=0
        for i in 0 1 2; do
            (( 10#${new_parts[i]} < 10#${old_parts[i]} )) && { lower=1; break; }
            (( 10#${new_parts[i]} > 10#${old_parts[i]} )) && break
        done
        if [[ "$new_numeric" == "$old_numeric" ]] && prerelease_is_lower "$NEW_VERSION" "$old_version"; then lower=1; fi
        if (( lower )) && [[ "$FORCE" != 1 ]]; then
            error_exit "Refusing downgrade $old_version -> $NEW_VERSION; use --force only with a compatible backup"
        fi
    else
        log "WARN: Installed version is unknown; relying on package and service checks"
    fi
    host=$(normalize_arch "$(uname -m)")
    name=${CURRENT_DIR##*/}
    if [[ "$name" =~ linux-(amd64|arm64|armv7|ppc64le|s390x|riscv64|loong64)($|-) ]]; then arch=${BASH_REMATCH[1]}; fi
    if [[ -n "$arch" && "$arch" != "$host" ]]; then error_exit "Package architecture $arch does not match host $host"; fi
    # The directory can be renamed: inspect both binaries where file is present.
    if command -v file >/dev/null 2>&1; then
        for name in 1panel-core 1panel-agent; do
            description=$(file -b "$CURRENT_DIR/$name") || continue
            arch=""
            case "$description" in
                *x86-64*) arch=amd64;; *aarch64*|*ARM64*) arch=arm64;;
                *ARM*) arch=armv7;; *LoongArch*) arch=loong64;;
                *RISC-V*) arch=riscv64;; *PowerPC*LSB*|*LSB*PowerPC*) arch=ppc64le;;
                *S/390*|*S390*) arch=s390x;;
            esac
            [[ -z "$arch" || "$arch" == "$host" ]] || error_exit "$name architecture $arch does not match host $host"
        done
    fi
}

detect_service_mgr() {
    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then SERVICE_MGR=systemd
    elif command -v rc-service >/dev/null 2>&1; then SERVICE_MGR=openrc
    elif [[ -x /etc/init.d/1panel-core ]]; then SERVICE_MGR=init
    elif command -v service >/dev/null 2>&1; then SERVICE_MGR=sysvinit
    else error_exit "No service manager found"; fi
}

service_command() {
    local action=$1 name=$2
    case "$SERVICE_MGR" in
        systemd)
            if [[ "$action" == status ]]; then systemctl is-active --quiet "$name.service"
            else systemctl "$action" "$name.service"; fi;;
        openrc) rc-service "$name" "$action";;
        init) "/etc/init.d/$name" "$action";;
        *) service "$name" "$action";;
    esac
}

stop_services() {
    local name failed=0
    for name in 1panel-core 1panel-agent; do
        if ! service_command stop "$name" >> "$LOG_FILE" 2>&1; then
            # Stop may return nonzero for an already stopped service.
            if service_command status "$name" >/dev/null 2>&1; then failed=1; fi
        fi
    done
    for name in 1panel-core 1panel-agent; do
        service_command status "$name" >/dev/null 2>&1 && failed=1
    done
    return "$failed"
}

start_services() {
    local mode=${1:-all} name failed=0
    if [[ "$SERVICE_MGR" == systemd ]]; then systemctl daemon-reload >> "$LOG_FILE" 2>&1 || return 1; fi
    for name in 1panel-agent 1panel-core; do
        [[ "$mode" == all || "${WAS_ACTIVE[$name]:-0}" == 1 ]] || continue
        service_command start "$name" >> "$LOG_FILE" 2>&1 || { log "WARN: $name start returned an error; checking recovery"; failed=1; }
    done
    # A transient start error can recover under Restart=always. Final health,
    # checked independently for both services, decides the transaction result.
    local elapsed=0 consecutive=0
    while (( elapsed <= HEALTH_TIMEOUT )); do
        failed=0
        for name in 1panel-core 1panel-agent; do
            [[ "$mode" == all || "${WAS_ACTIVE[$name]:-0}" == 1 ]] || continue
            service_command status "$name" >/dev/null 2>&1 || failed=1
        done
        if (( failed == 0 )); then
            consecutive=$((consecutive + 1))
            (( consecutive >= 2 )) && return 0
        else consecutive=0; fi
        (( elapsed >= HEALTH_TIMEOUT )) && break
        sleep 2
        elapsed=$((elapsed + 2))
    done
    log "ERROR: Both services did not remain active within ${HEALTH_TIMEOUT}s"
    return 1
}

# Each manifest entry records absence as well as contents, so rollback also
# removes newly created files. cp -a preserves ownership, modes and symlinks.
backup_path() {
    local path=$1 target
    if [[ -d "$path" ]]; then
        target=$(cd "$path" && pwd -P) || return 1
        if [[ "$BACKUP_DIR/" == "$target/"* ]]; then
            log "ERROR: Backup destination is inside $path; move the package outside the data directory"
            return 1
        fi
    fi
    [[ ${BACKED_UP[$path]+present} ]] && return 0
    BACKED_UP[$path]=1
    if [[ -e "$path" || -L "$path" ]]; then
        mkdir -p "$BACKUP_DIR/root$(dirname "$path")" || return 1
        cp -a -- "$path" "$BACKUP_DIR/root$path" || return 1
        printf 'present\t%s\n' "$path" >> "$BACKUP_DIR/manifest" || return 1
        if [[ -L "$path" ]]; then
            target=$(readlink -f "$path") || return 1
            [[ -n "$target" && "$target" != "$path" ]] && backup_path "$target" || return 1
        fi
    else printf 'absent\t%s\n' "$path" >> "$BACKUP_DIR/manifest" || return 1; fi
}

backup_current() {
    local path
    BACKUP_DIR=$(mktemp -d "$CURRENT_DIR/backup_$(date +%Y%m%d_%H%M%S)_XXXXXX") || return 1
    chmod 700 "$BACKUP_DIR" || return 1
    : > "$BACKUP_DIR/manifest" || return 1
    declare -gA BACKED_UP=()
    for path in "$BIN_DIR/1panel-core" "$BIN_DIR/1panel-agent" "$BIN_DIR/1pctl" "$BIN_DIR/lang" \
        "$RUN_BASE_DIR/db" "$RUN_BASE_DIR/conf" "$RUN_BASE_DIR/config" "$RUN_BASE_DIR/geo" \
        /etc/systemd/system/1panel-core.service /etc/systemd/system/1panel-agent.service \
        /etc/systemd/system/1panel-core.service.d /etc/systemd/system/1panel-agent.service.d \
        /etc/init.d/1panel-core /etc/init.d/1panel-agent; do
        backup_path "$path" || return 1
    done
    log "Backup saved at $BACKUP_DIR"
}

rollback() {
    local state path failed=0
    log "Restoring binaries, databases, configuration and service files"
    if ! stop_services; then
        log "ERROR: Cannot stop services safely; restore manually from $BACKUP_DIR after stopping them"
        return 1
    fi
    while IFS=$'\t' read -r state path; do
        if ! rm -rf -- "$path"; then failed=1; continue; fi
        if [[ "$state" == present ]]; then
            mkdir -p "$(dirname "$path")" && cp -a -- "$BACKUP_DIR/root$path" "$path" || failed=1
        fi
    done < "$BACKUP_DIR/manifest"
    (( failed == 0 )) || { log "ERROR: Restoration incomplete; backup retained at $BACKUP_DIR"; return 1; }
    start_services previous || { log "ERROR: Previous files restored but services need attention"; return 1; }
    log "Rollback completed"
}

finish() {
    local code=$?
    trap - EXIT INT TERM
    if (( code != 0 )); then
        if (( TRANSACTION )); then rollback || true
        elif (( SERVICES_STOPPED )); then start_services previous || log "ERROR: Could not restart unchanged installation"; fi
    fi
    [[ -z "$STAGED_CONFIG" ]] || rm -f -- "$STAGED_CONFIG"
    [[ -z "$LOCK_DIR" ]] || rm -rf -- "$LOCK_DIR"
    exit "$code"
}

install_units() {
    local name dst src suffix
    case "$SERVICE_MGR" in systemd) suffix=service;; openrc) suffix=openrc;; init)
        if [[ -f /etc/rc.common ]]; then suffix=procd; else suffix=init; fi;; *) suffix=init;; esac
    for name in 1panel-core 1panel-agent; do
        if [[ "$SERVICE_MGR" == systemd ]]; then
            dst="/etc/systemd/system/$name.service"
            # Preserve administrator overrides and vendor-supplied units too.
            [[ -e "$dst" || -L "$dst" || -f "/usr/lib/systemd/system/$name.service" || -f "/lib/systemd/system/$name.service" ]] && continue
        else dst="/etc/init.d/$name"; [[ -e "$dst" || -L "$dst" ]] && continue; fi
        src="$CURRENT_DIR/initscript/$name.$suffix"
        [[ -s "$src" ]] || src="$CURRENT_DIR/$name.$suffix"
        if [[ ! -s "$src" ]]; then log "WARN: No replacement unit for $name; retaining service manager configuration"; continue; fi
        mkdir -p "$(dirname "$dst")" && cp -f "$src" "$dst" || return 1
        [[ "$SERVICE_MGR" == systemd ]] || chmod +x "$dst" || return 1
    done
}

update_database_versions() {
    local db escaped
    for db in "$RUN_BASE_DIR/db/core.db" "$RUN_BASE_DIR/db/agent.db"; do
        [[ -f "$db" ]] || continue
        if command -v python3 >/dev/null 2>&1; then
            if python3 - "$db" "$NEW_VERSION" <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    # This is metadata; tolerate an upstream schema that no longer has it.
    columns = {row[1] for row in conn.execute('PRAGMA table_info(settings)')}
    if {'key', 'value'} <= columns:
        conn.execute("UPDATE settings SET value=? WHERE key='SystemVersion'", (sys.argv[2],))
PY
            then continue; fi
            log "WARN: Python database metadata update unavailable for ${db##*/}; trying sqlite3"
        fi
        if command -v sqlite3 >/dev/null 2>&1; then
            escaped=${NEW_VERSION//\'/\'\'}
            sqlite3 "$db" "UPDATE settings SET value='$escaped' WHERE key='SystemVersion';" >> "$LOG_FILE" 2>&1 || log "WARN: Keeping existing version metadata in ${db##*/}; service health remains required"
        else log "WARN: No usable database helper; letting 1Panel manage version metadata"; fi
    done
}

main() {
    local option file staged
    for option in "$@"; do
        case "$option" in --force) FORCE=1;; *) error_exit "Usage: $0 [--force]";; esac
    done
    [[ $EUID -eq 0 ]] || error_exit "Please run as root"
    [[ "$HEALTH_TIMEOUT" =~ ^[0-9]+$ && "$HEALTH_TIMEOUT" -ge 2 ]] || error_exit "PANEL_HEALTH_TIMEOUT must be at least 2 seconds"
    [[ -s "$BIN_DIR/1pctl" ]] || error_exit "1pctl not found; install 1Panel first"
    for file in 1panel-core 1panel-agent 1pctl; do
        [[ -s "$CURRENT_DIR/$file" ]] || error_exit "Required file is missing or empty: $file"
    done
    if [[ -n "${PANEL_BASE_DIR_OVERRIDE:-}" && ! "$PANEL_BASE_DIR_OVERRIDE" =~ ^/[a-zA-Z0-9_./:@%+=,-]+$ ]]; then
        error_exit "BASE_DIR override must be a plain absolute path: upstream ctl_conf cannot decode shell quoting"
    fi
    PANEL_BASE_DIR=${PANEL_BASE_DIR_OVERRIDE:-$(read_conf "$BIN_DIR/1pctl" BASE_DIR)}
    [[ "$PANEL_BASE_DIR" == /* && -d "$PANEL_BASE_DIR" && "$PANEL_BASE_DIR" != *$'\n'* && "$PANEL_BASE_DIR" != *$'\t'* ]] || error_exit "Cannot read BASE_DIR; set PANEL_BASE_DIR_OVERRIDE to the installation path"
    PANEL_BASE_DIR=$(cd "$PANEL_BASE_DIR" && pwd -P) || error_exit "Cannot resolve BASE_DIR"
    RUN_BASE_DIR="$PANEL_BASE_DIR/1panel"
    [[ -d "$RUN_BASE_DIR" ]] || error_exit "Missing 1panel data directory"
    for file in db conf config geo; do
        [[ "$CURRENT_DIR/" != "$RUN_BASE_DIR/$file/"* ]] || error_exit "Move the upgrade package outside $RUN_BASE_DIR/$file before upgrading"
    done
    check_compatibility
    detect_service_mgr
    # A portable per-installation lock also covers hosts without flock.
    LOCK_DIR="$RUN_BASE_DIR/.offline-upgrade.lock"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        LOCK_DIR=""
        error_exit "An upgrade lock already exists in $RUN_BASE_DIR; if no upgrade is running, remove .offline-upgrade.lock and retry"
    fi
    trap finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    printf '%s\n' "$$" > "$LOCK_DIR/pid" || error_exit "Cannot write upgrade lock"
    staged=$(mktemp "$CURRENT_DIR/.1pctl_XXXXXX") || error_exit "Cannot stage configuration"
    STAGED_CONFIG=$staged
    chmod 600 "$staged" || { rm -f "$staged"; error_exit "Cannot protect staged configuration"; }
    merge_config "$BIN_DIR/1pctl" "$CURRENT_DIR/1pctl" "$staged" || { rm -f "$staged"; error_exit "Cannot merge 1pctl configuration safely"; }
    log "Upgrading to $NEW_VERSION; Docker and Compose are unchanged"
    for file in 1panel-core 1panel-agent; do
        if service_command status "$file" >/dev/null 2>&1; then WAS_ACTIVE[$file]=1; else WAS_ACTIVE[$file]=0; fi
    done
    SERVICES_STOPPED=1
    stop_services || { rm -f "$staged"; error_exit "Cannot stop both services; no files replaced"; }
    # Database migrations can start immediately with the new process. Snapshot
    # the entire DB directory (including -wal/-shm) only after both have stopped.
    backup_current || { rm -f "$staged"; error_exit "Backup incomplete; no files replaced"; }
    TRANSACTION=1
    for file in 1panel-core 1panel-agent; do
        cp -f "$CURRENT_DIR/$file" "$BIN_DIR/$file" && chmod 700 "$BIN_DIR/$file" || error_exit "Failed to replace $file"
    done
    cp -f "$staged" "$BIN_DIR/1pctl" && chmod 700 "$BIN_DIR/1pctl" || error_exit "Failed to replace 1pctl"
    rm -f "$staged"
    # Missing optional resources retain the previous working copy. A failure
    # after writing starts is transactional, since it may leave partial data.
    if [[ -d "$CURRENT_DIR/lang" ]] && compgen -G "$CURRENT_DIR/lang/*.sh" >/dev/null; then
        mkdir -p "$BIN_DIR/lang" && cp -a "$CURRENT_DIR/lang/." "$BIN_DIR/lang/" || error_exit "Failed to update language files"
    else log "WARN: No packaged language files; retaining installed languages"; fi
    if [[ -s "$CURRENT_DIR/GeoIP.mmdb" ]]; then
        mkdir -p "$RUN_BASE_DIR/geo" && cp -f "$CURRENT_DIR/GeoIP.mmdb" "$RUN_BASE_DIR/geo/GeoIP.mmdb" || error_exit "Failed to update GeoIP"
    else log "WARN: No packaged GeoIP; retaining installed GeoIP"; fi
    install_units || error_exit "Failed to install missing service units"
    update_database_versions
    start_services || error_exit "Upgrade health check failed"
    TRANSACTION=0
    SERVICES_STOPPED=0
    log "Upgrade finished successfully; backup retained at $BACKUP_DIR"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
