#!/usr/bin/env python3
"""Read-only, finite recovery transport. Never rebuild, execute, admit or publish."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

from native_candidate_input import (CandidateGitHub, api, download_verified,
    extract_zip, require, verify_artifact, verify_upload_log, WORKFLOW)
from publication_contract import read_job_log
from release_asset_repair import digest

REPOSITORY = 'HandSonic/1Panel-offline-installer-V2'
MANIFEST = Path(__file__).resolve().parents[1] / 'docs/recovery/2026-10-artifact-transfer.json'
LIMIT = 512 * 1024 ** 2
CONTROL_LIMIT = 4 * 1024 ** 2


def selected_tar(archive, output, row):
    """Stream one regular ZIP member; do not unpack or execute its tar contents."""
    filename, pin = row['filename'], row['tar']
    require(re.fullmatch(r'1panel-v[0-9]+\.[0-9]+\.[0-9]+-enterprise-docker-offline-linux-(amd64|arm64)\.tar\.gz', filename),
            'Unexpected target filename')
    require(0 < pin['bytes'] < LIMIT - 1024 * 1024, 'Target exceeds transport limit')
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        require(len(infos) == len({i.filename for i in infos}), 'Duplicate ZIP members')
        for info in infos:
            path = PurePosixPath(info.filename)
            require(not path.is_absolute() and '..' not in path.parts and '\\' not in info.filename and
                    str(path) == info.filename and info.orig_filename == info.filename and
                    not (info.flag_bits & 1) and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) and
                    (info.external_attr >> 16) & 0o170000 in (0, stat.S_IFREG, stat.S_IFDIR), 'Unsafe ZIP member')
        matches = [i for i in infos if PurePosixPath(i.filename).name == filename]
        require(len(matches) == 1, 'Missing or ambiguous target member')
        info = matches[0]
        require(not info.is_dir() and not (info.external_attr & 0x10) and
                (info.external_attr >> 16) & 0o170000 in (0, stat.S_IFREG) and
                info.file_size == pin['bytes'], 'Target ZIP member size/type differs')
        output.mkdir(parents=True, exist_ok=False)
        target = output / filename
        with source.open(info) as src, target.open('xb') as dst:
            count = 0
            while block := src.read(min(1024 * 1024, pin['bytes'] + 1 - count)):
                count += len(block)
                require(count <= pin['bytes'], 'Target exceeds admitted size')
                dst.write(block)
        require(digest(target) == pin, 'Target differs from original admitted bytes')
        (output / (filename + '.sha256')).write_bytes((pin['sha256'] + '  ' + filename + '\n').encode('ascii'))


def authenticate_controls(directory, row, log):
    receipt_bytes = (directory / 'release-validation.json').read_bytes()
    native_bytes = (directory / 'native-acceptance.json').read_bytes()
    require(hashlib.sha256(receipt_bytes).hexdigest() == row['receipt_sha256'] and
            hashlib.sha256(native_bytes).hexdigest() == row['native_sha256'], 'Original acceptance bytes changed')
    for marker, wanted in [('VERIFIED_RELEASE_RECEIPT_SHA256', row['receipt_sha256']),
                           ('NATIVE_ACCEPTANCE_SHA256', row['native_sha256'])]:
        require(re.findall(r'(?m)^(?:[0-9T:.-]+Z )?' + marker + r'=([0-9a-f]{64})[ \t]*$', log) == [wanted],
                'Original successful acceptance log marker differs')
    receipt, native = json.loads(receipt_bytes), json.loads(native_bytes)
    for document in (receipt, native):
        require(document['repository'] == REPOSITORY and document['workflow_run_id'] == row['run_id'] and
                document['workflow_run_attempt'] == row['run_attempt'] and document['workflow_commit'] == row['head_sha'] and
                document['version'] == row['version'], 'Original acceptance identity differs')
    require(receipt['files'][row['filename']] == row['tar'] and
            row['product_key'] in native['admission']['accepted_keys'], 'Target was not originally admitted')
    outcomes = [v for v in receipt['outcomes'] if v['key'] == row['product_key']]
    require(len(outcomes) == 1 and outcomes[0]['status'] == 'success', 'Original target outcome failed')


def transfer(client, row, output):
    require(client.repo == REPOSITORY, 'Unexpected repository')
    identity = {'run_id': row['run_id'], 'run_attempt': row['run_attempt'], 'head_sha': row['head_sha']}
    run = api(client, f'repos/{client.repo}/actions/runs/{row["run_id"]}')
    require(run['id'] == row['run_id'] and run['head_sha'] == row['head_sha'] and
            run['path'].split('@')[0] == WORKFLOW and
            all(run[k]['full_name'] == REPOSITORY for k in ('repository', 'head_repository')), 'Original workflow differs')
    job = api(client, f'repos/{client.repo}/actions/jobs/{row["acceptance_job_id"]}')
    require(job['id'] == row['acceptance_job_id'] and job['run_id'] == row['run_id'] and
            job['run_attempt'] == row['run_attempt'] and job['head_sha'] == row['head_sha'] and
            job['name'] == 'publication_acceptance' and job['status'] == 'completed' and
            job['conclusion'] == 'success', 'Original acceptance job did not succeed')
    artifacts = {}
    for kind in ('controls', 'shard'):
        pin = row[kind]
        value = api(client, f'repos/{client.repo}/actions/artifacts/{pin["id"]}')
        require(all(value.get(k) == v for k, v in pin.items()), 'Pinned original artifact differs')
        verify_artifact(value, pin['name'], identity, run, CONTROL_LIMIT if kind == 'controls' else 2 * 1024 ** 3)
        artifacts[kind] = value
    log = read_job_log(client, job['id'])
    verify_upload_log(log, artifacts['controls'])
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        download_verified(client, artifacts['controls'], work / 'controls.zip')
        extract_zip(work / 'controls.zip', work / 'controls',
                    {name: CONTROL_LIMIT for name in ('release-validation.json', 'native-acceptance.json', 'checksums.txt')}, CONTROL_LIMIT)
        authenticate_controls(work / 'controls', row, log)
        download_verified(client, artifacts['shard'], work / 'shard.zip')
        selected_tar(work / 'shard.zip', output, row)
        for value in artifacts.values():
            require(api(client, f'repos/{client.repo}/actions/artifacts/{value["id"]}') == value,
                    'Original artifact changed during transfer')
    # Provenance remains the original run. This is not a new acceptance marker.
    print(json.dumps({'transport_only': True, 'original_run_id': row['run_id'],
                      'original_receipt_sha256': row['receipt_sha256'], 'file': row['filename'], **row['tar']}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--key', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads(MANIFEST.read_text())
    matches = [row for row in rows if row['key'] == args.key]
    require(len(matches) == 1, 'Unknown or ambiguous approved transfer')
    transfer(CandidateGitHub(REPOSITORY, ''), matches[0], args.output)


if __name__ == '__main__':
    main()
