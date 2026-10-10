#!/usr/bin/env python3
"""Read-only CI authentication and isolated package validation helpers."""
import json,os,re,shutil,stat,subprocess,sys,tempfile,zipfile
from pathlib import Path,PurePosixPath
from publication_contract import PROOF,REPOS,ROOT,contract_for_repo,read_job_log
from release_asset_repair import GitHub,digest

UPSTREAM=REPOS['upstream7']

def github_json(endpoint):
    result=subprocess.run(['gh','api',endpoint],check=True,capture_output=True,text=True)
    return json.loads(result.stdout)


def check_identity(version,tag,repository):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?',version):raise ValueError('Invalid application version')
    if tag!=version:raise ValueError('Existing-release repair tag must equal the application version')
    return contract_for_repo(repository)


def extract_verified_zip(path,destination):
    destination=Path(destination)
    if destination.exists():raise ValueError('Refusing to overwrite an existing input directory')
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist();names=[i.filename for i in infos]
        if len(names)!=len(set(names)) or sum(i.file_size for i in infos)>2*1024**3:raise ValueError('Duplicate or oversized CI ZIP')
        for info in infos:
            name=PurePosixPath(info.filename);kind=(info.external_attr>>16)&0o170000
            if name.is_absolute() or '..' in name.parts or '\\' in info.filename or len(name.parts)!=1 or info.filename!=name.name or kind not in (0,stat.S_IFREG):
                raise ValueError('Only flat regular CI artifact files are accepted')
        if archive.testzip() is not None:raise ValueError('CI ZIP CRC mismatch')
        destination.mkdir(parents=True)
        for info in infos:
            with archive.open(info) as src,(destination/info.filename).open('wb') as dst:shutil.copyfileobj(src,dst)


def validate_upstream_input(directory,version,expected_commit,contract,provenance=None):
    if contract=='downstream17' and (Path(directory)/'resolved-source.json').is_file():
        from resolved_inventory import object_bytes
        from resolved_transport import ci_controls
        mode=object_bytes((Path(directory)/'resolved-source.json').read_bytes())['mode']
        if provenance is None or provenance['build_repository_commit']!=expected_commit:
            raise ValueError('Resolved CI inputs require their verified producer provenance')
        ci_controls(directory,version,mode,provenance)
        return
    raise ValueError('Authenticated resolved CI source controls required')


class OriginalCIUnavailable(ValueError):
    """Only authenticated expiration/absence permits retained-byte transport."""
    def __init__(self, authentication):
        super().__init__('Original authenticated CI artifact is ' + authentication['availability'])
        self.authentication = authentication


def github_pages(endpoint, field):
    rows = []
    for page in range(1, 101):
        separator = '&' if '?' in endpoint else '?'
        value = github_json(endpoint + separator + f'per_page=100&page={page}')
        entries = value.get(field) if isinstance(value, dict) else None
        if not isinstance(entries, list) or not all(isinstance(row, dict) for row in entries):
            raise ValueError('Malformed original CI catalogue')
        rows.extend(entries)
        if len(entries) < 100:
            if type(value.get('total_count')) is not int or value['total_count'] != len(rows):
                raise ValueError('Incomplete original CI catalogue')
            return rows
    raise ValueError('Original CI catalogue exceeds pagination limit')


def ci_run_identity(run, run_id):
    from resolved_inventory import require
    from native_candidate_input import positive
    require(isinstance(run, dict) and run.get('id') == int(run_id) and positive(run.get('run_attempt')) and
            isinstance(run.get('head_sha'), str) and re.fullmatch('[0-9a-f]{40}', run['head_sha']) and
            run.get('status') == 'completed' and run.get('conclusion') in ('success', 'failure') and
            run.get('path', '').split('@')[0] == '.github/workflows/build.yml' and
            run.get('event') in ('push', 'schedule', 'workflow_dispatch', 'pull_request') and
            all(isinstance(run.get(key), dict) and run[key].get('full_name') == UPSTREAM and
                positive(run[key].get('id')) for key in ('repository', 'head_repository')) and
            run['repository']['id'] == run['head_repository']['id'],
            'Selected upstream workflow is not a completed noncancelled trusted build')
    return {key: run[key] for key in ('id', 'run_attempt', 'head_sha', 'status', 'conclusion',
                                    'path', 'event', 'repository', 'head_repository')}


def verify_artifact_producer(run_id, artifact_id, name, sha, run=None):
    from resolved_inventory import require
    from native_candidate_input import positive
    run = github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}') if run is None else run
    ci_run_identity(run, run_id)
    jobs = github_pages(f'repos/{UPSTREAM}/actions/runs/{run_id}/jobs?filter=all', 'jobs')
    matched = []
    for job in jobs:
        if job.get('name') not in ('build', 'aggregate') or job.get('conclusion') != 'success':
            continue
        require(positive(job.get('id')) and job.get('run_id') == int(run_id) and
                positive(job.get('run_attempt')) and job['run_attempt'] <= run['run_attempt'] and
                job.get('head_sha') == run['head_sha'] and job.get('status') == 'completed',
                'Original artifact upload job identity mismatch')
        logs = read_job_log(GitHub(UPSTREAM, ''), job['id'])
        if re.search(r'Artifact ID (?:is )?' + re.escape(str(artifact_id)) + r'(?![0-9])', logs) and re.search(
                r'SHA256 digest of uploaded artifact zip is ' + re.escape(sha) + r'(?![0-9a-f])', logs) and \
                f'Artifact {name}.zip successfully finalized.' in logs:
            matched.append(job)
    require(len(matched) == 1, 'Selected artifact is not bound to exactly one successful build/aggregate upload')
    job = matched[0]
    attempt = ci_run_identity(github_json(
        f'repos/{UPSTREAM}/actions/runs/{run_id}/attempts/{job["run_attempt"]}'), run_id)
    require(attempt['run_attempt'] == job['run_attempt'] and attempt['head_sha'] == job['head_sha'],
            'Original artifact upload attempt changed')
    return {'run': attempt, 'job_id': job['id'], 'run_attempt': job['run_attempt']}


def fetch_ci_bundle(directory,version,run_id,artifact_id,expected_sha,expected_commit,contract,*,evidence=None):
    from resolved_inventory import require
    from native_candidate_input import positive
    if not str(run_id).isdigit() or int(run_id) <= 0 or not str(artifact_id).isdigit() or int(artifact_id) <= 0 or not re.fullmatch('[0-9a-f]{64}',expected_sha) or not re.fullmatch('[0-9a-f]{40}',expected_commit):
        raise ValueError('Exact run/artifact IDs, ZIP hash and built commit are required')
    run=github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}')
    latest_identity=ci_run_identity(run, run_id)
    name=f'verified-1panel-{version}-{expected_commit}'
    producer=verify_artifact_producer(run_id,artifact_id,name,expected_sha,run)
    # PR API heads are branch heads, while the executed commit may be a merge.
    # ci_controls verifies the exact merge and parents through verify_jobs.
    require(run['event'] == 'pull_request' or run['head_sha'] == expected_commit,
            'Original non-PR producer commit mismatch')
    artifacts=github_pages(f'repos/{UPSTREAM}/actions/runs/{run_id}/artifacts', 'artifacts')
    matches=[row for row in artifacts if row.get('id') == int(artifact_id)]
    require(len(matches) <= 1, 'Ambiguous original CI artifact ID')
    authentication={'repository':UPSTREAM, 'artifact_id':int(artifact_id), 'artifact_name':name,
                    'artifact_sha256':expected_sha, 'build_repository_commit':expected_commit,
                    'latest_run':latest_identity, **producer}
    if not matches:
        raise OriginalCIUnavailable(dict(authentication, availability='missing'))
    metadata=github_json(f'repos/{UPSTREAM}/actions/artifacts/{artifact_id}')
    require(metadata == matches[0] and metadata.get('id') == int(artifact_id) and
            isinstance(metadata.get('workflow_run'), dict) and metadata['workflow_run'].get('id') == int(run_id) and
            metadata['workflow_run'].get('head_sha') == run['head_sha'] and
            metadata.get('name') == name and metadata.get('digest') == 'sha256:' + expected_sha and
            positive(metadata.get('size_in_bytes')) and metadata['size_in_bytes'] <= 2 * 1024 ** 3 and
            type(metadata.get('expired')) is bool, 'Selected artifact identity/digest mismatch')
    if metadata['expired']:
        raise OriginalCIUnavailable(dict(authentication, availability='expired', artifact=metadata))
    with tempfile.TemporaryDirectory(dir=Path(directory).parent) as temp:
        archive=Path(temp)/'upstream.zip'
        with archive.open('wb') as stream:
            subprocess.run(['gh','api',f'repos/{UPSTREAM}/actions/artifacts/{artifact_id}/zip'],stdout=stream,check=True)
        if digest(archive)!= {'bytes':metadata['size_in_bytes'],'sha256':expected_sha}:raise ValueError('Downloaded CI ZIP differs from GitHub metadata')
        extract_verified_zip(archive,directory)
    provenance={'repository':UPSTREAM,'run_id':int(run_id),'artifact_id':int(artifact_id),'artifact_sha256':expected_sha,
                'build_repository_commit':expected_commit,'run_url':f'https://github.com/{UPSTREAM}/actions/runs/{run_id}'}
    manifest=json.loads((Path(directory)/'build-manifest.json').read_text())
    if producer['run']['conclusion'] != 'success' or run['event'] == 'pull_request':
        require(manifest.get('schema_version') == 2, 'Failed/PR producer requires authenticated explicit branch outcomes')
    if manifest.get('schema_version') == 2:
        require(manifest.get('producer_run_attempt') == producer['run_attempt'],
                'CI matrix differs from original upload attempt')
    validate_upstream_input(directory,version,expected_commit,contract,provenance)
    require(ci_run_identity(github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}'), run_id) == latest_identity and
            github_json(f'repos/{UPSTREAM}/actions/artifacts/{artifact_id}') == metadata and
            ci_run_identity(github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}/attempts/{producer["run_attempt"]}'), run_id) == producer['run'],
            'Original CI identity changed during acquisition')
    if evidence is not None:
        require(isinstance(evidence, dict) and not evidence, 'CI evidence destination must be empty')
        evidence.update(authentication, availability='available', artifact=metadata)
    return provenance


def isolated_check(directory,version,source,arches):
    from validate_release import validate
    with tempfile.TemporaryDirectory(dir=Path(directory).parent) as temp:
        view=Path(temp);matrix={source:arches}
        if source=='enterprise-docker':matrix['enterprise-original']=arches
        paths=[]
        for item,targets in matrix.items():
            (view/item).mkdir()
            for arch in targets:
                original=Path(directory)/item/f'1panel-{version}-{item}-offline-linux-{arch}.tar.gz'
                dest=view/item/original.name;os.link(original,dest);paths.append(dest)
        (view/'checksums.txt').write_text(''.join(digest(p)['sha256']+'  '+p.name+'\n' for p in paths))
        matrix_path=view/'matrix.json';matrix_path.write_text(json.dumps(matrix))
        validate(view,version,matrix_path)



if __name__=='__main__':
    raise SystemExit('Use package_matrix.py for preparation and runtime_publication.py for native-admitted publication')
