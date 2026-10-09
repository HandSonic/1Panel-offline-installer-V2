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


def verify_artifact_producer(run_id,artifact_id,name,sha):
    jobs=github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}/jobs?filter=all&per_page=100')['jobs']
    for job in jobs:
        if job.get('name') not in ['build','aggregate'] or job.get('conclusion')!='success':continue
        logs=read_job_log(GitHub(UPSTREAM,''),job['id'])
        # These values are emitted by upload-artifact after upload finalization.
        if re.search(r'Artifact ID (?:is )?'+re.escape(str(artifact_id))+r'(?![0-9])',logs) and re.search(r'SHA256 digest of uploaded artifact zip is '+re.escape(sha)+r'(?![0-9a-f])',logs) and f'Artifact {name}.zip successfully finalized.' in logs:
            return
    raise ValueError('Selected artifact is not bound to a successful build/aggregate upload')


def fetch_ci_bundle(directory,version,run_id,artifact_id,expected_sha,expected_commit,contract):
    if not str(run_id).isdigit() or not str(artifact_id).isdigit() or not re.fullmatch('[0-9a-f]{64}',expected_sha) or not re.fullmatch('[0-9a-f]{40}',expected_commit):
        raise ValueError('Exact run/artifact IDs, ZIP hash and built commit are required')
    run=github_json(f'repos/{UPSTREAM}/actions/runs/{run_id}')
    if run.get('status')!='completed' or run.get('conclusion') not in ('success','failure'):raise ValueError('Selected upstream workflow is not a completed noncancelled build')
    if run.get('path','').split('@')[0]!='.github/workflows/build.yml':raise ValueError('Unexpected upstream workflow')
    metadata=github_json(f'repos/{UPSTREAM}/actions/artifacts/{artifact_id}')
    if metadata.get('expired') or metadata.get('workflow_run',{}).get('id')!=int(run_id):raise ValueError('Artifact/run mismatch or expired artifact')
    if metadata.get('name')!=f'verified-1panel-{version}-{expected_commit}' or metadata.get('digest')!='sha256:'+expected_sha:
        raise ValueError('Selected artifact identity/digest mismatch')
    verify_artifact_producer(run_id,artifact_id,metadata['name'],expected_sha)
    with tempfile.TemporaryDirectory(dir=Path(directory).parent) as temp:
        archive=Path(temp)/'upstream.zip'
        with archive.open('wb') as stream:
            subprocess.run(['gh','api',f'repos/{UPSTREAM}/actions/artifacts/{artifact_id}/zip'],stdout=stream,check=True)
        if digest(archive)!= {'bytes':metadata['size_in_bytes'],'sha256':expected_sha}:raise ValueError('Downloaded CI ZIP differs from GitHub metadata')
        extract_verified_zip(archive,directory)
    provenance={'repository':UPSTREAM,'run_id':int(run_id),'artifact_id':int(artifact_id),'artifact_sha256':expected_sha,
                'build_repository_commit':expected_commit,'run_url':f'https://github.com/{UPSTREAM}/actions/runs/{run_id}'}
    if run.get('conclusion')!='success':
        manifest=json.loads((Path(directory)/'build-manifest.json').read_text())
        if not (Path(directory)/'resolved-source.json').is_file() or manifest.get('schema_version')!=2:
            raise ValueError('Failed producer requires authenticated explicit branch outcomes')
    validate_upstream_input(directory,version,expected_commit,contract,provenance)
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
