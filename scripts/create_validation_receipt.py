#!/usr/bin/env python3
import argparse,json,os
from pathlib import Path
from publication_contract import PROOF,contract_for_repo,make_proof,validate_payloads

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--repository',required=True);p.add_argument('--version',required=True);p.add_argument('--tag',required=True);p.add_argument('--directory',type=Path,required=True);args=p.parse_args()
    contract=contract_for_repo(args.repository);files=validate_payloads(args.directory,contract,args.version)
    proof=make_proof(files,contract,args.version,args.tag,args.repository,os.environ['GITHUB_RUN_ID'],os.environ['GITHUB_SHA'])
    (args.directory/PROOF).write_text(json.dumps(proof,sort_keys=True,indent=2)+'\n')
    import hashlib
    print('VERIFIED_RELEASE_RECEIPT_SHA256='+hashlib.sha256((args.directory/PROOF).read_bytes()).hexdigest())
