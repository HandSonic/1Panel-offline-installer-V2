#!/usr/bin/env python3
"""Validate downstream offline assets, then optionally perform recoverable repair."""
import argparse,json,re
from pathlib import Path
from validate_release import validate
from release_asset_repair import GitHub, Journal, digest, repair

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',required=True);parser.add_argument('--tag',required=True)
    parser.add_argument('--version',required=True);parser.add_argument('--matrix',required=True,type=Path)
    parser.add_argument('--directory',required=True,type=Path);parser.add_argument('--journal',required=True,type=Path)
    parser.add_argument('--execute',action='store_true',help='Explicitly perform reviewed remote asset replacement')
    args=parser.parse_args()
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',args.repo):parser.error('Invalid repository')
    validate(args.directory,args.version,args.matrix)
    files=sorted(args.directory.glob('*/*.tar.gz'))+[args.directory/'checksums.txt']
    if not args.execute:
        print(json.dumps({'action':'plan_only','repo':args.repo,'tag':args.tag,'assets':[{**digest(p),'name':p.name} for p in files],
                          'warning':'Execution stages and verifies downloads, retains old assets as backups, then renames. This is not atomic.'},indent=2))
    else:
        if args.journal.exists():parser.error('Journal exists: review prior repair before starting another')
        print(json.dumps(repair(GitHub(args.repo,args.tag),files,args.journal),indent=2))
