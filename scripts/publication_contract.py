#!/usr/bin/env python3
"""Independent upstream7/downstream17 publication contracts and remote receipts."""
import hashlib,json,os,re,subprocess,sys,tempfile
from pathlib import Path
from release_asset_repair import digest

ROOT=Path(__file__).resolve().parents[1]
ARCHES=['amd64','arm64','armv7','ppc64le','s390x','loong64','riscv64']
REPOS={'upstream7':'HandSonic/1Panel-Build-v2','downstream17':'HandSonic/1Panel-offline-installer-V2'}
PROOF='release-validation.json'


def contract_for_repo(repository):
    for name,repo in REPOS.items():
        if repository==repo:return name
    raise ValueError('Unsupported publication repository')


def policy_fingerprint(contract,version,root=ROOT):
    if contract=='upstream7':
        paths=['scripts/validate_artifacts.py','scripts/resolve_inputs.py','scripts/configure_release.py','scripts/build_release.sh','Dockerfile','config/sources.json']
    elif contract=='downstream17':
        paths=['scripts/validate_release.py','scripts/validate_payload.py','scripts/patch_installer.py',
               'scripts/validate_upstream.py','upgrade_offline.sh','docker.service',
               'docker-sources.json','compose-sources.json',f'release-matrix-{version}.json',
               ]
        from release_inventory import resolved_matrix
        matrix=resolved_matrix(version,root)
        if any(name.startswith('enterprise-') for name in matrix):
            paths += [f'enterprise-sources-{version}.json','scripts/enterprise_contract.py']
        if not matrix.get('enterprise-docker'):
            paths += [f'enterprise-sources-{version}.json',f'config/source-availability/{version}.json','scripts/release_inventory.py']
        if matrix.get('official'):
            paths += [f'official-sources-{version}.json','scripts/official_source.py']
    else:raise ValueError('Unknown publication contract')
    paths.append('scripts/embedded_configuration.py')
    facts={p:digest(root/p)['sha256'] for p in paths}
    facts['embedded-config-version']=hashlib.sha256(json.dumps(json.loads((root/'config/embedded-configs.json').read_text())[version],sort_keys=True).encode()).hexdigest()
    if contract=='downstream17':
        if any(name.startswith('enterprise-') for name in matrix):
            from enterprise_contract import contract as enterprise_contract
            facts['enterprise-contract-version']=hashlib.sha256(json.dumps(enterprise_contract(version,root),sort_keys=True).encode()).hexdigest()
        if matrix.get('custom'):
            from upstream_validation_contract import validator_root
            selected=validator_root(version,root)
            facts['upstream-validator-source']=digest(selected/'SOURCE.json')['sha256']
            facts['upstream-validator-selector']=digest(root/'scripts/upstream_validation_contract.py')['sha256']
            facts['upstream-package-gate']=digest(root/'scripts/validate_upstream_package.py')['sha256']
            if (selected/'scripts/semantic_configuration.py').is_file():
                facts['upstream-validator-requirements']=digest(root/'requirements-validation.txt')['sha256']
    if contract=='upstream7':
        # Adding an unrelated historical version must not invalidate this version's receipt.
        facts['config/sources.json']=hashlib.sha256(json.dumps(json.loads((root/'config/sources.json').read_text())[version],sort_keys=True).encode()).hexdigest()
    return hashlib.sha256(json.dumps(facts,sort_keys=True).encode()).hexdigest()


def expected_names(contract,version,root=ROOT):
    if contract=='upstream7':
        names={f'1panel-{version}-linux-{a}.tar.gz' for a in ARCHES}
        return names|{name+'.sha256' for name in names}|{'checksums.txt','build-manifest.json','build-inputs.env'}
    if contract=='downstream17':
        from release_inventory import resolved_matrix
        matrix=resolved_matrix(version,root)
        return {f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz' for source,arches in matrix.items() for arch in arches}|{'checksums.txt'}
    raise ValueError('Unknown publication contract')


def validate_payloads(directory,contract,version,root=ROOT):
    directory=Path(directory)
    if contract=='upstream7':
        # Upstream's own validator; no offline-installer assumptions are substituted.
        subprocess.run([sys.executable,str(root/'scripts/validate_artifacts.py'),str(directory),version,' '.join(ARCHES)],check=True)
        from embedded_configuration import validate_archive
        for arch in ARCHES:validate_archive(directory/f'1panel-{version}-linux-{arch}.tar.gz',version,arch,root)
        files=[p for p in directory.iterdir() if p.is_file()]
    elif contract=='downstream17':
        from validate_release import validate
        validate(directory,version,root/f'release-matrix-{version}.json',lock_root=root)
        files=list(directory.glob('*/*.tar.gz'))+[directory/'checksums.txt']
    else:raise ValueError('Unknown publication contract')
    if {p.name for p in files}!=expected_names(contract,version,root) or len(files)!=len({p.name for p in files}):
        raise ValueError('Publication payload matrix mismatch')
    return sorted(files,key=lambda p:p.name)


def make_proof(files,contract,version,tag,repository,workflow_run_id,workflow_commit,root=ROOT):
    if repository!=REPOS[contract] or not (tag==version or tag.startswith(version+'-')):raise ValueError('Publication target/version mismatch')
    if not str(workflow_run_id).isdigit() or not re.fullmatch('[0-9a-f]{40}',workflow_commit):raise ValueError('Immutable workflow identity required')
    return {'schema':1,'contract':contract,'version':version,'release_tag':tag,'repository':repository,
            'policy_fingerprint':policy_fingerprint(contract,version,root),
            'workflow_run_id':int(workflow_run_id),'workflow_commit':workflow_commit,
            'files':{p.name:digest(p) for p in files}}


def verify_receipt(proof,checksums,assets,run,contract,version,tag,repository,root=ROOT):
    expected=expected_names(contract,version,root)
    identity={'schema':1,'contract':contract,'version':version,'release_tag':tag,'repository':repository,
              'policy_fingerprint':policy_fingerprint(contract,version,root)}
    if any(proof.get(k)!=v for k,v in identity.items()):raise ValueError('Repair-needed: validation receipt identity is missing or stale')
    if set(proof.get('files',{}))!=expected:raise ValueError('Repair-needed: receipt matrix mismatch')
    if run.get('id')!=proof.get('workflow_run_id') or run.get('head_sha')!=proof.get('workflow_commit') or run.get('status')!='completed' or run.get('conclusion')!='success':
        raise ValueError('Repair-needed: receipt workflow is not a completed successful exact commit')
    allowed={'.github/workflows/build-offline-v2.yml' if contract=='downstream17' else '.github/workflows/build.yml'}
    if run.get('path','').split('@')[0] not in allowed:raise ValueError('Repair-needed: unrecognized receipt workflow')
    by_name={a['name']:a for a in assets}
    if len(by_name)!=len(assets):raise ValueError('Repair-needed: duplicate remote asset names')
    canonical=expected|{PROOF}
    if not canonical<=set(by_name):raise ValueError('Repair-needed: incomplete canonical asset matrix')
    for name in set(by_name)-canonical:
        # Known noncanonical recovery names are retained deliberately, never deleted.
        if not any(re.fullmatch(re.escape(base)+r'\.(backup|staged)-[0-9a-f]{12}',name) for base in canonical):
            raise ValueError('Repair-needed: unexpected remote canonical asset')
    for name,facts in proof['files'].items():
        a=by_name[name]
        if a.get('digest')!='sha256:'+facts['sha256'] or a.get('size')!=facts['bytes'] or facts['bytes']<=0:
            raise ValueError(f'Repair-needed: remote digest/size mismatch: {name}')
    if digest_bytes(checksums)!=proof['files']['checksums.txt']:raise ValueError('Repair-needed: checksum file content mismatch')
    sums={}
    for line in checksums.decode().splitlines():
        match=re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)',line)
        if not match or match[2] in sums:raise ValueError('Repair-needed: malformed flat checksums')
        sums[match[2]]=match[1]
    archives={n for n in expected if n.endswith('.tar.gz')}
    if set(sums)!=archives or any(sums[n]!=proof['files'][n]['sha256'] for n in archives):raise ValueError('Repair-needed: checksum matrix mismatch')
    return True


def digest_bytes(data):return {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}


def read_job_log(client,job_id):
    if type(job_id) is not int or job_id<=0:raise ValueError('Invalid workflow job identity')
    endpoint=f'repos/{client.repo}/actions/jobs/{job_id}/logs'
    try:
        logs=client.run('api',endpoint)
    except subprocess.CalledProcessError as exc:
        # New gh versions guard ANSI even when stdout is captured. This is a
        # data-only read, never terminal output; do not retry access/HTTP errors.
        if 'the response contains terminal escape sequences' not in (exc.stderr or ''):raise
        logs=client.run('api','--allow-escape-sequences',endpoint)
    escape=r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))'
    def neutralize(match):
        left=logs[match.start()-1:match.start()] if match.start() else ''
        right=logs[match.end():match.end()+1]
        # Color around a token is harmless. Do not join token fragments or
        # interpret cursor/erase/OSC controls as if they were trusted text.
        inside=bool(left and right and re.match(r'[A-Za-z0-9_=]',left) and re.match(r'[A-Za-z0-9_=]',right))
        return '' if re.fullmatch(r'\x1b\[[0-9;]*m',match[0]) and not inside else '[CONTROL]'
    logs=re.sub(escape,neutralize,logs)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]','[CONTROL]',logs)


def verify_validation_log(client,proof,receipt_sha):
    run_id=proof.get('workflow_run_id')
    if type(run_id) is not int or run_id<=0 or not re.fullmatch('[0-9a-f]{40}',proof.get('workflow_commit','')):
        raise ValueError('Repair-needed: invalid workflow identity')
    jobs=json.loads(client.run('api',f'repos/{client.repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100'))['jobs']
    for job in jobs:
        if job.get('name') not in ['build','publication_prepare','publication_revalidate'] or job.get('conclusion')!='success':continue
        if type(job.get('id')) is not int:continue
        try:
            logs=read_job_log(client,job['id'])
        except subprocess.CalledProcessError as exc:
            # Never print redirect URLs, credentials or arbitrary server content.
            error=exc.stderr or '';status=re.search(r'HTTP [0-9]{3}',error)
            reasons=[word for word in ['Resource not accessible by integration','Must have admin rights','Not Found','Forbidden','expired','redirect','TLS','timeout'] if word.lower() in error.lower()]
            output=exc.stdout or ''
            try:
                parsed=json.loads(output)
                message=parsed.get('message','') if isinstance(parsed,dict) else ''
            except (ValueError,TypeError):message=''
            safe=error+(' JSON message: '+message if isinstance(message,str) and message else '')
            for key in ['GH_TOKEN','GITHUB_TOKEN']:
                secret=os.environ.get(key)
                if secret:safe=safe.replace(secret,'[redacted]')
            safe=re.sub(r'https?://[^\s]+','[redacted-url]',safe)
            safe=re.sub(r'(?i)(authorization|token|password|secret)[=: ]+[^\s]+',r'\1=[redacted]',safe)
            safe=re.sub(r'[A-Za-z0-9_+/=-]{20,}','[redacted-opaque]',safe)
            safe=' '.join(safe.split())[:400] or 'empty stderr'
            detail=', '.join(([status[0]] if status else ['status unavailable'])+reasons)+f'; exception={type(exc).__name__}; exit={exc.returncode}; stdout_length={len(output)}; stderr_length={len(error)}; '+safe
            raise ValueError(f'Validation job {job["id"]} log read blocked ({detail}); release remains unchanged') from None
        hashes=set(re.findall(r'(?m)^(?:[0-9T:.-]+Z )?VERIFIED_RELEASE_RECEIPT_SHA256=([0-9a-f]{64})[ \t]*$',logs))
        if len(hashes)>1:raise ValueError('Ambiguous validation receipt markers')
        if hashes=={receipt_sha}:return True
    raise ValueError('Repair-needed: published receipt is not bound to a successful validation job')

def existing_release_state(client,contract,version,tag,root=ROOT):
    release=client.release()
    if release.get('draft'):return 'draft'
    with tempfile.TemporaryDirectory() as temporary:
        directory=Path(temporary)
        client.download(PROOF,directory);client.download('checksums.txt',directory)
        proof_bytes=(directory/PROOF).read_bytes();proof=json.loads(proof_bytes)
        assets=release['assets'];asset=next(a for a in assets if a['name']==PROOF)
        if asset.get('digest')!='sha256:'+hashlib.sha256(proof_bytes).hexdigest() or asset['size']!=len(proof_bytes):raise ValueError('Repair-needed: receipt byte mismatch')
        verify_validation_log(client,proof,hashlib.sha256(proof_bytes).hexdigest())
        run=json.loads(client.run('api',f'repos/{client.repo}/actions/runs/{proof["workflow_run_id"]}'))
        if run.get('event') in ['pull_request','pull_request_target']:
            raise ValueError('Repair-needed: read-only PR tests are not package publication validation')
        verify_receipt(proof,(directory/'checksums.txt').read_bytes(),assets,run,contract,version,tag,client.repo,root)
    return 'verified'
