#!/usr/bin/env python3
"""Import a locally downloaded upstream CI artifact without logging credentials."""
import json
import os
import re
import shutil
import sys
from pathlib import Path
from validate_payload import digest
from validate_upstream import checksum_sidecar

def import_artifact(directory, destination, name, run_url, repository, commit):
    if not re.fullmatch(r'https://github\.com/'+re.escape(repository)+r'/actions/runs/[0-9]+',run_url):
        raise ValueError('Provide the exact public upstream Actions run URL, without credentials/query strings')
    if not re.fullmatch(r'[0-9a-f]{40}',commit):raise ValueError('A full expected build-repository commit is required')
    source=Path(directory)/name;sidecar=Path(directory)/(name+'.sha256')
    expected=checksum_sidecar(sidecar,name)
    if digest(source)['sha256']!=expected:raise ValueError('Local CI artifact differs from its SHA-256 sidecar')
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    part=destination.with_name(destination.name+'.part');part.unlink(missing_ok=True)
    try:
        try:os.link(source,part)
        except OSError:shutil.copyfile(source,part)
        part.replace(destination)
    finally:part.unlink(missing_ok=True)
    Path(str(destination)+'.upstream.sha256').write_bytes(sidecar.read_bytes())
    Path(str(destination)+'.source.json').write_text(json.dumps(dict(digest(destination),url=run_url))+'\n')
    Path(str(destination)+'.origin.json').write_text(json.dumps({'source_kind':'github-actions-artifact','artifact_name':name,
        'checksum_url':run_url,'expected_build_repository_commit':commit})+'\n')

if __name__=='__main__':
    try:import_artifact(*sys.argv[1:])
    except Exception as exc:sys.exit(str(exc))
