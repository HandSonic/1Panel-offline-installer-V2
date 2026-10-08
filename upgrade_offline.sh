#!/bin/bash
# Community offline upgrade. Never source package configuration or execute package binaries.
set -uo pipefail
umask 077
CURRENT_DIR=$(cd "$(dirname "$0")" && pwd) || exit 1
LOG_FILE="${CURRENT_DIR}/upgrade.log"
WORK_DIR=""
STOPPED=0
MUTATED=0
BACKED_UP=0
log() { printf '[upgrade] %s\n' "$*" | tee -a "$LOG_FILE"; }

# All filesystem stages explicitly return errors; Python exceptions are deliberately
# redacted because configuration, paths, and SQLite errors can contain secrets.
files() {
    python3 - "$1" "$CURRENT_DIR" "$WORK_DIR" "$SERVICE_MGR" <<'PY'
import os, sys, pathlib, shutil, re, shlex, sqlite3, struct, platform, json
mode, package, work, manager = sys.argv[1:]
pkg, work = pathlib.Path(package), pathlib.Path(work)
bin_dir = pathlib.Path('/usr/local/bin')
keys = ('BASE_DIR','ORIGINAL_PORT','ORIGINAL_USERNAME','ORIGINAL_PASSWORD',
        'ORIGINAL_ENTRANCE','LANGUAGE','CHANGE_USER_INFO')
def assignments(path):
    return dict(re.findall(r'^([A-Z_]+)=(.*)$', path.read_text(), re.M))
def literal(raw):
    words = shlex.split(raw, comments=True)
    if len(words) != 1: raise ValueError('invalid literal')
    return words[0]
def version(raw):
    value = literal(raw)
    if value == 'v2.nightly': return value, 'nightly', ()
    m = re.fullmatch(r'v2\.(\d+)\.(\d+)(?:-alpha\.(\d+))?', value)
    if not m: raise ValueError('unsupported version')
    return value, 'alpha' if m[3] else 'stable', tuple(int(x or 0) for x in m.groups())
def remove(path):
    if path.is_symlink() or path.is_file(): path.unlink()
    elif path.exists(): shutil.rmtree(path)
def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir(): shutil.copytree(src, dst, symlinks=True)
    else: shutil.copy2(src, dst, follow_symlinks=False)
def db_connect(path, readonly=False):
    return sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True)
try:
    if mode == 'prepare':
        package_manifest = pkg/'offline-manifest.json'
        if package_manifest.exists():
            if package_manifest.is_symlink(): raise ValueError('invalid package manifest')
            declared = json.loads(package_manifest.read_text())
            if declared.get('source') not in ('official','custom'): raise ValueError('community upgrade requires community package')
        old = assignments(bin_dir/'1pctl'); new = assignments(pkg/'1pctl')
        base = pathlib.Path(os.environ.get('PANEL_BASE_DIR_OVERRIDE') or literal(old['BASE_DIR']))
        if not base.is_absolute() or not base.is_dir(): raise ValueError('invalid install directory')
        run = base/'1panel'
        dbs = [run/'db'/name for name in ('core.db','agent.db')]
        current = []
        for db in dbs:
            if not db.is_file(): raise ValueError('missing database')
            with db_connect(db, True) as con:
                rows = con.execute("SELECT value FROM settings WHERE key='SystemVersion'").fetchall()
                if len(rows) != 1: raise ValueError('missing or ambiguous version row')
                current.append(version(rows[0][0]))
        if current[0] != current[1]: raise ValueError('inconsistent installed database versions')
        target = version(new['ORIGINAL_VERSION'])
        if current[0][1] != target[1]: raise ValueError('cross-channel upgrade refused')
        if target[1] != 'nightly' and current[0][2] >= target[2]: raise ValueError('same version or downgrade refused')
        expected = {'x86_64':(2,62),'amd64':(2,62),'aarch64':(2,183),'arm64':(2,183),
                    'armv7l':(1,40),'ppc64le':(2,21),'s390x':(2,22),'riscv64':(2,243),
                    'loongarch64':(2,258),'loong64':(2,258)}.get(platform.machine())
        if expected is None: raise ValueError('unsupported architecture')
        for name in ('1panel-core','1panel-agent'):
            path = pkg/name
            if path.is_symlink(): raise ValueError('symlink binary')
            data = path.read_bytes()[:64]
            if len(data)<52 or data[:4]!=b'\x7fELF' or data[5] not in (1,2): raise ValueError('invalid ELF')
            machine = struct.unpack(('<' if data[5]==1 else '>')+'H', data[18:20])[0]
            if data[5] != (1 if sys.byteorder == 'little' else 2) or (data[4],machine)!=expected: raise ValueError('architecture mismatch')
        for name in ('1pctl','GeoIP.mmdb','lang'):
            path=pkg/name
            if path.is_symlink() or not path.exists(): raise ValueError('missing resource')
            if name=='lang' and not path.is_dir(): raise ValueError('invalid language directory')
            if path.is_dir():
                if not any(path.iterdir()) or any(p.is_symlink() for p in path.rglob('*')): raise ValueError('invalid language tree')
            elif not path.stat().st_size: raise ValueError('empty resource')
        stage = work/'stage'; stage.mkdir()
        paths=[]
        for name in ('1panel-core','1panel-agent','1pctl','lang'):
            copy(pkg/name, stage/name); paths.append([str(bin_dir/name),name])
        text=(stage/'1pctl').read_text()
        for key in keys:
            if key not in old: continue
            raw = shlex.quote(str(base)) if key=='BASE_DIR' else old[key]
            # Callable replacement keeps backslashes, $, &, quotes and delimiters literal.
            line=key+'='+raw
            if re.search(r'^'+key+r'=.*$',text,re.M): text=re.sub(r'^'+key+r'=.*$',lambda m:line,text,flags=re.M)
            else: text+='\n'+line+'\n'
        (stage/'1pctl').write_text(text)
        for name in ('1panel-core','1panel-agent','1pctl'): (stage/name).chmod(0o700)
        copy(pkg/'GeoIP.mmdb',stage/'GeoIP.mmdb'); paths.append([str(run/'geo'/'GeoIP.mmdb'),'GeoIP.mmdb'])
        suffix = {'systemd':'service','openrc':'openrc','sysvinit':'init'}[manager]
        for service in ('1panel-core','1panel-agent'):
            name=service+'.'+suffix
            src=pkg/'initscript'/name
            if not src.exists(): src=pkg/name
            dst=pathlib.Path('/etc/systemd/system')/(service+'.service') if manager=='systemd' else pathlib.Path('/etc/init.d')/service
            if src.exists():
                if src.is_symlink() or not src.is_file() or not src.stat().st_size: raise ValueError('invalid service file')
                copy(src,stage/name); (stage/name).chmod(0o644 if manager=='systemd' else 0o755)
                paths.append([str(dst),name])
            elif not dst.is_file(): raise ValueError('no installed or packaged service file')
        paths.append([str(run/'db'),'db'])
        (work/'context.json').write_text(json.dumps({'paths':paths,'dbs':[str(p) for p in dbs],'version':target[0]}))
    else:
        ctx=json.loads((work/'context.json').read_text())
        backup=work/'backup'
        if mode=='backup':
            backup.mkdir(); manifest=[]
            for path,name in ctx['paths']:
                path=pathlib.Path(path)
                if path.is_symlink(): raise ValueError('symlink destination')
                exists=path.exists(); manifest.append([str(path),name,exists])
                if exists: copy(path,backup/name)
            (backup/'manifest.json').write_text(json.dumps(manifest))
        elif mode=='install':
            for path,name in ctx['paths']:
                if name=='db': continue
                path=pathlib.Path(path); remove(path); copy(work/'stage'/name,path)
        elif mode=='database':
            for db in ctx['dbs']:
                with db_connect(pathlib.Path(db)) as con:
                    cur=con.execute("UPDATE settings SET value=? WHERE key='SystemVersion'",(ctx['version'],))
                    if cur.rowcount!=1: raise ValueError('database version update failed')
                    if con.execute("SELECT value FROM settings WHERE key='SystemVersion'").fetchall()!=[(ctx['version'],)]: raise ValueError('database verification failed')
        elif mode=='verify':
            for db in ctx['dbs']:
                with db_connect(pathlib.Path(db), True) as con:
                    if con.execute("SELECT value FROM settings WHERE key='SystemVersion'").fetchall()!=[(ctx['version'],)]: raise ValueError('post-start version mismatch')
        elif mode=='restore':
            for path,name,existed in json.loads((backup/'manifest.json').read_text()):
                path=pathlib.Path(path); remove(path)
                if existed: copy(backup/name,path)
        else: raise ValueError('invalid stage')
except Exception:
    print('Upgrade filesystem stage failed: '+mode, file=sys.stderr)
    sys.exit(1)
PY
}
service_action() {
    local action=$1 name=$2
    case "$SERVICE_MGR" in
        systemd) if [[ $action == status ]]; then systemctl is-active --quiet "$name.service"; else systemctl "$action" "$name.service"; fi ;;
        openrc) rc-service "$name" "$action" ;;
        *) service "$name" "$action" ;;
    esac
}
stop_services() {
    local failed=0 name status
    for name in 1panel-core 1panel-agent; do service_action stop "$name" >/dev/null 2>&1 || failed=1; done
    # A successful stop must also leave both services inactive.
    for name in 1panel-core 1panel-agent; do
        service_action status "$name" >/dev/null 2>&1; status=$?
        case "$SERVICE_MGR:$status" in
            systemd:3|openrc:1|openrc:3|sysvinit:1|sysvinit:2|sysvinit:3) ;;
            *) failed=1 ;;
        esac
    done
    return "$failed"
}
start_services() {
    local failed=0 name
    if [[ $SERVICE_MGR == systemd ]]; then systemctl daemon-reload >/dev/null 2>&1 || return 1; fi
    for name in 1panel-agent 1panel-core; do service_action start "$name" >/dev/null 2>&1 || failed=1; done
    return "$failed"
}
healthy() {
    local count=0 attempt
    for attempt in {1..30}; do
        if service_action status 1panel-core >/dev/null 2>&1 && service_action status 1panel-agent >/dev/null 2>&1; then
            count=$((count+1)); [[ $count -ge 2 ]] && return 0
        else count=0; fi
        sleep 2
    done
    return 1
}
finish() {
    local result=$?
    trap - EXIT INT TERM
    if [[ $result -ne 0 && $STOPPED -eq 1 ]]; then
        if [[ $MUTATED -eq 1 && $BACKED_UP -eq 1 ]]; then
            log 'Upgrade failed; stopping new services before restoring the complete snapshot.'
            if ! stop_services; then
                log "ERROR: Cannot safely restore while services may be running. Keep services stopped and recover manually from ${WORK_DIR}/backup."
                exit 1
            fi
            if ! files restore; then
                log "ERROR: Restore failed. Services remain stopped; recover manually from ${WORK_DIR}/backup."
                exit 1
            fi
        fi
        if start_services && healthy; then log 'Previous installation restarted.'
        else log 'ERROR: Previous services could not be recovered; manual intervention required.'; fi
    fi
    [[ $result -eq 0 ]] || log 'ERROR: Upgrade did not complete.'
    exit "$result"
}
main() {
    [[ $EUID -eq 0 ]] || { log 'ERROR: Run as root.'; return 1; }
    [[ -f /usr/local/bin/1pctl ]] || { log 'ERROR: Existing community installation not found.'; return 1; }
    command -v python3 >/dev/null && command -v flock >/dev/null || { log 'ERROR: python3 (with sqlite3) and flock are required.'; return 1; }
    exec 9>/usr/local/bin/.1panel-upgrade.lock || return 1
    flock -n 9 || { log 'ERROR: Another upgrade is running.'; return 1; }
    if command -v systemctl >/dev/null 2>&1; then SERVICE_MGR=systemd
    elif command -v rc-service >/dev/null 2>&1; then SERVICE_MGR=openrc
    else SERVICE_MGR=sysvinit; fi
    WORK_DIR=$(mktemp -d "${CURRENT_DIR}/upgrade-backup.XXXXXXXX") || return 1
    files prepare || return 1
    bash -n "$WORK_DIR/stage/1pctl" >/dev/null 2>&1 || { log 'ERROR: Invalid staged control script syntax.'; return 1; }
    trap finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    log 'Package staged and validated. Stopping services for a consistent backup.'
    STOPPED=1
    stop_services || return 1
    files backup || return 1
    BACKED_UP=1
    MUTATED=1
    files install || return 1
    files database || return 1
    start_services || return 1
    healthy || return 1
    files verify || return 1
    log "Upgrade finished successfully. Protected backup saved at: ${WORK_DIR}/backup"
}
main "$@"
