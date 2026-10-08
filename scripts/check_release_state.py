#!/usr/bin/env python3
"""A public release is a successful no-op only with a complete verified receipt."""
import argparse,subprocess,sys
from publication_contract import existing_release_state,contract_for_repo
from release_asset_repair import GitHub

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--repository',required=True);parser.add_argument('--version',required=True);parser.add_argument('--tag',required=True);args=parser.parse_args()
    client=GitHub(args.repository,args.tag)
    try:
        # Only a real 404 establishes absence; authentication/network errors fail closed.
        try:client.release()
        except subprocess.CalledProcessError as exc:
            if '(HTTP 404)' in (exc.stderr or ''):print('absent');sys.exit(0)
            raise
        print(existing_release_state(client,contract_for_repo(args.repository),args.version,args.tag))
    except Exception as exc:
        sys.exit('Repair-needed or verification blocked; no release changed: '+str(exc))
