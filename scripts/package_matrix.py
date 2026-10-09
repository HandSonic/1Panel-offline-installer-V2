#!/usr/bin/env python3
"""Read-only package shards and one complete release aggregation."""
import argparse,json,os,re,shutil,sys
from pathlib import Path
from manual_publication import ROOT,PROOF,check_identity,fetch_ci_bundle,isolated_check
from publication_contract import make_proof,validate_payloads,contract_for_repo
from release_asset_repair import digest
import subprocess


def matrix_rows(version,root=ROOT):
    path=root/f'release-matrix-{version}.json'
    if not path.is_file():raise ValueError(f'No resolved release matrix for {version}; complete source/edition/architecture discovery first')
    matrix=json.loads(path.read_text())
    if matrix.get('enterprise-original',[])!=matrix.get('enterprise-docker',[]):
        raise ValueError('Enterprise original/enhanced architecture sets must agree')
    for source,prefix in [('official','official-sources-'),('enterprise-docker','enterprise-sources-')]:
        if matrix.get(source) and set(json.loads((root/f'{prefix}{version}.json').read_text())) != set(matrix[source]):
            raise ValueError('Release matrix differs from reviewed source architecture inventory')
    if matrix.get('custom'):
        from upstream_validation_contract import validator_root
        validator_root(version,root)
    rows=[]
    for source,arches in matrix.items():
        if source=='enterprise-original':continue
        if source not in ['official','custom','enterprise-docker'] or len(arches)!=len(set(arches)):
            raise ValueError('Invalid or duplicate matrix source/architecture')
        for arch in arches:
            if arch not in json.loads((root/'docker-sources.json').read_text()):raise ValueError('Unsupported architecture')
            rows.append({'source':source,'arch':arch,'key':source+'-'+arch})
    if not rows:raise ValueError('Empty release matrix')
    return rows


def identity(version,repository,tag):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?',version) or not (tag==version or re.fullmatch(re.escape(version)+r'-[A-Za-z0-9._-]+',tag)):raise ValueError('Invalid version/release tag')
    if contract_for_repo(repository)!='downstream17':raise ValueError('Downstream matrix only')
    return {'version':version,'repository':repository,'tag':tag,'workflow_commit':os.environ['GITHUB_SHA'],'workflow_run_id':os.environ['GITHUB_RUN_ID']}


def plan(args):
    facts=identity(args.version,args.repository,args.tag);rows=matrix_rows(args.version)
    mode=getattr(args,'mode','stable')
    if mode not in ['stable','beta','dev']:raise ValueError('Invalid build mode')
    facts['mode']=mode
    work=Path(args.work);work.mkdir(parents=True,exist_ok=False)
    provenance={'source_kind':'verified-public-release'}
    if args.upstream_source=='verified-ci':
        provenance=fetch_ci_bundle(work/'upstream-input',args.version,args.run_id,args.artifact_id,args.artifact_sha256,args.build_commit,'downstream17')
    facts.update(upstream_input=provenance,rows=rows)
    (work/'plan.json').write_text(json.dumps(facts,sort_keys=True)+'\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as out:out.write('matrix='+json.dumps({'include':rows},separators=(',',':'))+'\n')
    print(json.dumps({'packages':sum(2 if r['source']=='enterprise-docker' else 1 for r in rows),'jobs':len(rows)}))


def load_plan(args):
    work=Path(args.work);facts=json.loads((work/'plan.json').read_text())
    expected=identity(args.version,args.repository,args.tag)
    expected['mode']=getattr(args,'mode','stable')
    if any(facts.get(k)!=v for k,v in expected.items()) or facts.get('rows')!=matrix_rows(args.version):raise ValueError('Matrix plan identity changed')
    return work,facts


def shard(args):
    work,facts=load_plan(args)
    row={'source':args.source,'arch':args.arch,'key':args.source+'-'+args.arch}
    if row not in facts['rows']:raise ValueError('Unrequested matrix shard')
    output=ROOT/'build'/args.version
    if output.exists() and any(output.rglob('*.tar.gz')):raise ValueError('Stale shard output')
    if args.source=='enterprise-docker':
        command=[sys.executable,str(ROOT/'scripts/prepare_enterprise.py'),'--version',args.version,'--arch',args.arch]
    else:
        command=['bash',str(ROOT/'prepare_offline.sh'),'--app_version',args.version,'--mode',facts['mode'],'--source',args.source,'--arch',args.arch]
        if args.source=='custom' and facts['upstream_input'].get('run_url'):
            command+=['--custom-package-dir',str(work/'upstream-input'),'--custom-source-url',facts['upstream_input']['run_url'],'--expected-build-commit',facts['upstream_input']['build_repository_commit']]
    subprocess.run(command,check=True,cwd=ROOT)
    isolated_check(output,args.version,args.source,[args.arch])
    if args.source!='enterprise-docker':shutil.rmtree(output/args.source/f'1panel-{args.version}-{args.source}-offline-linux-{args.arch}')
    target=Path(args.output);target.mkdir(parents=True,exist_ok=False)
    files={}
    for path in sorted(output.glob('*/*.tar.gz')):
        relative=path.relative_to(output);files[str(relative)]=digest(path)
        (target/relative.parent).mkdir(exist_ok=True);shutil.move(path,target/relative)
    receipt={'identity':{k:facts[k] for k in ['version','repository','tag','workflow_commit','workflow_run_id']},'row':row,'files':files,'plan_sha256':digest(work/'plan.json')['sha256']}
    (target/'shard.json').write_text(json.dumps(receipt,sort_keys=True)+'\n')


def aggregate(args):
    work,facts=load_plan(args);shards=Path(args.shards);dest=Path(args.output)
    if dest.exists():raise ValueError('Aggregation destination exists')
    expected_keys={r['key'] for r in facts['rows']}
    selected={}
    for folder in shards.iterdir():
        match=re.fullmatch(r'package-shard-([1-9][0-9]*)-(.+)',folder.name)
        if not match or match[2] not in expected_keys or not folder.is_dir() or folder.is_symlink():raise ValueError('Unexpected package shard')
        attempt,key=int(match[1]),match[2]
        if attempt>int(os.environ.get('GITHUB_RUN_ATTEMPT','1')):raise ValueError('Future shard attempt')
        if key not in selected or attempt>selected[key][0]:selected[key]=(attempt,folder)
    if set(selected)!=expected_keys:raise ValueError('Missing package shard')
    release=dest/'release';control=dest/'control';release.mkdir(parents=True);control.mkdir()
    seen=set()
    for row in facts['rows']:
        folder=selected[row['key']][1];record=json.loads((folder/'shard.json').read_text())
        identity_fields={k:facts[k] for k in ['version','repository','tag','workflow_commit','workflow_run_id']}
        if record.get('row')!=row or record.get('identity')!=identity_fields or record.get('plan_sha256')!=digest(work/'plan.json')['sha256']:raise ValueError('Shard provenance mismatch')
        sources=[row['source']]+(['enterprise-original'] if row['source']=='enterprise-docker' else [])
        names={f'{s}/1panel-{args.version}-{s}-offline-linux-{row["arch"]}.tar.gz' for s in sources}
        actual={str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and p.name!='shard.json'}
        if set(record.get('files',{}))!=names or actual!=names:raise ValueError('Shard payload set mismatch')
        for name in names:
            source=folder/name
            if source.is_symlink() or not source.is_file() or digest(source)!=record['files'][name] or name in seen:raise ValueError('Shard bytes changed or duplicated')
            seen.add(name);target=release/name;target.parent.mkdir(exist_ok=True);shutil.copyfile(source,target)
    paths=sorted(release.glob('*/*.tar.gz'))
    (release/'checksums.txt').write_text(''.join(digest(p)['sha256']+'  '+p.name+'\n' for p in paths))
    files=validate_payloads(release,'downstream17',args.version)
    proof=make_proof(files,'downstream17',args.version,args.tag,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'])
    proof['upstream_input']=facts['upstream_input'];(control/PROOF).write_text(json.dumps(proof,indent=2,sort_keys=True)+'\n')
    print('Full package matrix independently validated')

def revalidate(args):
    # Existing release bytes only: deliberately bypass plan/shard/aggregate.
    from receipt_migration import revalidate as revalidate_existing
    revalidate_existing(args)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['plan','shard','aggregate','revalidate'])
    for name in ['version','repository','tag','work']:p.add_argument('--'+name,required=True)
    for name in ['source','arch','output','shards','run-id','artifact-id','artifact-sha256','build-commit']:p.add_argument('--'+name,default='')
    p.add_argument('--mode',choices=['stable','beta','dev'],default='stable')
    p.add_argument('--upstream-source',choices=['release','verified-ci'],default='release')
    a=p.parse_args();globals()[a.operation](a)
