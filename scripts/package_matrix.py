#!/usr/bin/env python3
"""Read-only package shards and one complete release aggregation."""
import argparse,json,os,re,shutil,sys
from pathlib import Path
from manual_publication import ROOT,PROOF,fetch_ci_bundle,isolated_check
from publication_contract import make_proof,validate_payloads,contract_for_repo
from release_asset_repair import digest
import subprocess
from release_inventory import native_rows


def matrix_rows(version,root=ROOT):
    from runtime_contract import require_selected
    return require_selected(version, root)['inventory']['rows']


def identity(version,repository,tag):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?',version) or not (tag==version or re.fullmatch(re.escape(version)+r'-[A-Za-z0-9._-]+',tag)):raise ValueError('Invalid version/release tag')
    if contract_for_repo(repository)!='downstream17':raise ValueError('Downstream matrix only')
    return {'version':version,'repository':repository,'tag':tag,'workflow_commit':os.environ['GITHUB_SHA'],'workflow_run_id':os.environ['GITHUB_RUN_ID']}


def plan(args):
    facts=identity(args.version,args.repository,args.tag)
    from native_candidate_input import predecessor_context
    facts.update(predecessor_context(args.version,os.environ))
    mode=getattr(args,'mode','stable')
    if mode not in ['stable','beta','dev']:raise ValueError('Invalid build mode')
    facts['mode']=mode
    work=Path(args.work);work.mkdir(parents=True,exist_ok=False)
    provenance={'source_kind':'verified-public-release'}
    source_directory=None;custom_error=None
    if args.upstream_source=='verified-ci':
        try:
            provenance=fetch_ci_bundle(work/'upstream-input',args.version,args.run_id,args.artifact_id,args.artifact_sha256,args.build_commit,'downstream17')
            source_directory=work/'upstream-input'
        except (OSError,ValueError,subprocess.SubprocessError) as error:
            from upstream_outcomes import safe_pr_failure_reason
            custom_error='Verified CI input authentication failed: '+type(error).__name__
            reason=safe_pr_failure_reason(error)
            if reason:custom_error+=': '+reason
            provenance={'source_kind':'failed-verified-ci-input'}
    from resolved_transport import resolve
    from resolved_inventory import canonical
    from runtime_contract import PLAN_PATH,PLAN_SHA
    runtime=resolve(args.version,mode,ROOT,upstream_directory=source_directory,provenance=provenance,**({'custom_error':custom_error} if custom_error else {}))
    rows=runtime['inventory']['rows']
    facts.update(upstream_input=provenance,rows=rows,native_rows=runtime['inventory']['native_rows'],resolved=runtime)
    (work/'plan.json').write_bytes(canonical(facts))
    plan_sha=digest(work/'plan.json')['sha256']
    os.environ[PLAN_PATH]=str((work/'plan.json').resolve());os.environ[PLAN_SHA]=plan_sha
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as out:
            out.write('matrix='+json.dumps({'include':rows},separators=(',',':'))+'\n')
            out.write('native_matrix='+json.dumps({'include':facts['native_rows']},separators=(',',':'))+'\n')
            out.write('plan_sha256='+plan_sha+'\n')
            out.write('upstream_ready='+str(source_directory is not None).lower()+'\n')
            out.write('upgrade_matrix='+json.dumps({'include':runtime['inventory']['upgrade_rows']},separators=(',',':'))+'\n')
    print(json.dumps({'packages':len(rows),'jobs':len(rows)}))


def load_plan(args):
    work=Path(args.work);facts=json.loads((work/'plan.json').read_text())
    from runtime_contract import require_selected
    if require_selected(args.version) != facts.get('resolved'):
        raise ValueError('Resolved plan is not bound to the exact planner output')
    expected=identity(args.version,args.repository,args.tag)
    expected['mode']=getattr(args,'mode','stable')
    if any(facts.get(k)!=v for k,v in expected.items()) or facts.get('rows')!=matrix_rows(args.version) or facts.get('native_rows')!=native_rows(args.version):raise ValueError('Matrix plan identity changed')
    return work,facts


def shard(args):
    work,facts=load_plan(args)
    row={'source':args.source,'arch':args.arch,'key':args.source+'-'+args.arch}
    if row not in facts['rows']:raise ValueError('Unrequested matrix shard')
    runtime=facts['resolved']
    if args.source=='custom':
        if runtime['source_contract'] is None:raise ValueError(runtime['custom_failure'])
        if args.arch not in runtime['upstream']['records']:raise ValueError('Upstream compile branch failed for '+args.arch)
    else:
        vendor=runtime['inventory']['official' if args.source=='official' else 'enterprise']
        if args.arch not in vendor['archives']:raise ValueError(vendor.get('failures',{}).get(args.arch,'Vendor archive is not authenticated'))
    output=ROOT/'build'/args.version
    if output.exists() and any(output.rglob('*.tar.gz')):raise ValueError('Stale shard output')
    if args.source in ('enterprise-docker','enterprise-original'):
        command=[sys.executable,str(ROOT/'scripts/prepare_enterprise.py'),'--version',args.version,'--arch',args.arch,
                 '--variant','original' if args.source=='enterprise-original' else 'docker']
    else:
        command=['bash',str(ROOT/'prepare_offline.sh'),'--app_version',args.version,'--mode',facts['mode'],'--source',args.source,'--arch',args.arch]
        if args.source=='custom' and facts['upstream_input'].get('run_url'):
            command+=['--custom-package-dir',str(work/'upstream-input'),'--custom-source-url',facts['upstream_input']['run_url'],'--expected-build-commit',facts['upstream_input']['build_repository_commit']]
    subprocess.run(command,check=True,cwd=ROOT)
    isolated_check(output,args.version,args.source,[args.arch])
    if not args.source.startswith('enterprise-'):shutil.rmtree(output/args.source/f'1panel-{args.version}-{args.source}-offline-linux-{args.arch}')
    target=Path(args.output);target.mkdir(parents=True,exist_ok=False)
    files={};companions={}
    paths=sorted((output/args.source).glob('*.tar.gz'))
    for path in paths:
        relative=path.relative_to(output);files[str(relative)]=digest(path)
        (target/relative.parent).mkdir(exist_ok=True);shutil.move(path,target/relative)
    if args.source=='enterprise-docker':
        original=output/'enterprise-original'/f'1panel-{args.version}-enterprise-original-offline-linux-{args.arch}.tar.gz'
        relative='companions/'+original.name
        companions[relative]=digest(original)
        (target/'companions').mkdir();shutil.move(original,target/relative)
    receipt={'identity':{k:facts[k] for k in ['version','repository','tag','workflow_commit','workflow_run_id']},'row':row,'files':files,'plan_sha256':digest(work/'plan.json')['sha256']}
    receipt['companions']=companions
    (target/'shard.json').write_text(json.dumps(receipt,sort_keys=True)+'\n')


def package_outcomes(facts):
    from native_candidate_input import CandidateGitHub,listed
    from publication_outcomes import collect,products
    client=CandidateGitHub(facts['repository'],facts['tag'])
    jobs=listed(client,f'repos/{client.repo}/actions/runs/{facts["workflow_run_id"]}/jobs?filter=all','jobs')
    return collect(jobs,products(facts['resolved']['inventory']['matrix']),int(facts['workflow_run_id']),
                   int(os.environ.get('GITHUB_RUN_ATTEMPT','1')),facts['workflow_commit'])


def aggregate(args):
    work,facts=load_plan(args);shards=Path(args.shards);dest=Path(args.output)
    if dest.exists():raise ValueError('Aggregation destination exists')
    expected_keys={r['key'] for r in facts['rows']}
    outcomes=package_outcomes(facts)
    accepted={row['key']:row for row in outcomes if row['status']=='success'}
    from publication_outcomes import report
    report(outcomes)
    if not accepted:raise ValueError('Every package branch failed; there are no verified assets to publish')
    selected={}
    for folder in shards.iterdir():
        match=re.fullmatch(r'package-shard-([1-9][0-9]*)-(.+)',folder.name)
        if not match or match[2] not in expected_keys or not folder.is_dir() or folder.is_symlink():raise ValueError('Unexpected package shard')
        attempt,key=int(match[1]),match[2]
        if attempt>int(os.environ.get('GITHUB_RUN_ATTEMPT','1')):raise ValueError('Future shard attempt')
        if key not in accepted or attempt!=accepted[key]['producer_run_attempt']:continue
        if key not in selected or attempt>selected[key][0]:selected[key]=(attempt,folder)
    if set(selected)!=set(accepted):raise ValueError('Missing successful package shard')
    release=dest/'release';control=dest/'control';release.mkdir(parents=True);control.mkdir()
    seen=set()
    for row in facts['rows']:
        if row['key'] not in accepted:continue
        folder=selected[row['key']][1];record=json.loads((folder/'shard.json').read_text())
        identity_fields={k:facts[k] for k in ['version','repository','tag','workflow_commit','workflow_run_id']}
        if record.get('row')!=row or record.get('identity')!=identity_fields or record.get('plan_sha256')!=digest(work/'plan.json')['sha256']:raise ValueError('Shard provenance mismatch')
        sources=[row['source']]
        names={f'{s}/1panel-{args.version}-{s}-offline-linux-{row["arch"]}.tar.gz' for s in sources}
        actual={str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and p.name!='shard.json'}
        companions={}
        if row['source']=='enterprise-docker':
            name=f'companions/1panel-{args.version}-enterprise-original-offline-linux-{row["arch"]}.tar.gz'
            pin=facts['resolved']['inventory']['enterprise']['archives'][row['arch']]
            companions[name]={k:pin[k] for k in ('bytes','sha256')}
        if record.get('companions')!=companions:raise ValueError('Unexpected enterprise verification companion')
        if set(record.get('files',{}))!=names or actual!=names|set(companions):raise ValueError('Shard payload set mismatch')
        for name,pin in companions.items():
            source=folder/name
            if source.is_symlink() or digest(source)!=pin:raise ValueError('Enterprise verification companion changed')
            target=dest/'verification/enterprise-original'/Path(name).name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
        for name in names:
            source=folder/name
            if source.is_symlink() or not source.is_file() or digest(source)!=record['files'][name] or name in seen:raise ValueError('Shard bytes changed or duplicated')
            seen.add(name);target=release/name;target.parent.mkdir(exist_ok=True);shutil.copyfile(source,target)
    paths=sorted(release.glob('*/*.tar.gz'))
    (release/'checksums.txt').write_text(''.join(digest(p)['sha256']+'  '+p.name+'\n' for p in paths))
    files=validate_payloads(release,'downstream17',args.version,outcomes=outcomes)
    proof=make_proof(files,'downstream17',args.version,args.tag,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'],
                     plan=facts,outcomes=outcomes)
    proof['upstream_input']=facts['upstream_input'];(control/PROOF).write_text(json.dumps(proof,indent=2,sort_keys=True)+'\n')
    shutil.copyfile(work/'plan.json',control/'plan.json')
    if os.environ.get('GITHUB_OUTPUT'):
        included={(r['source'],r['arch']) for r in accepted.values()}
        native=[r for r in facts['resolved']['inventory']['native_rows'] if (r['source'],r['arch']) in included]
        upgrades=[r for r in facts['resolved']['inventory']['upgrade_rows'] if (r['source'],r['arch']) in included]
        with open(os.environ['GITHUB_OUTPUT'],'a') as out:
            for kind,rows in [('native',native),('upgrade',upgrades)]:
                out.write(kind+'_matrix='+json.dumps({'include':rows},separators=(',',':'))+'\n')
                out.write(kind+'_count='+str(len(rows))+'\n')
    print('Accepted package branches independently validated; failures retained in Actions')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('operation',choices=['plan','shard','aggregate'])
    for name in ['version','repository','tag','work']:p.add_argument('--'+name,required=True)
    for name in ['source','arch','output','shards','run-id','artifact-id','artifact-sha256','build-commit']:p.add_argument('--'+name,default='')
    p.add_argument('--mode',choices=['stable','beta','dev'],default='stable')
    p.add_argument('--upstream-source',choices=['release','verified-ci'],default='release')
    a=p.parse_args();globals()[a.operation](a)
