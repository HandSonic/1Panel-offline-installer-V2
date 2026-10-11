#!/usr/bin/env python3
"""Authenticate each original producer of a mixed-generation public inventory.

Generation hashes follow the upstream schema's ASCII-escaped canonical JSON.
These inventories never turn an unverified old archive into a current build.
"""
import hashlib
import json

from resolved_inventory import commit_value, exact_keys, hash_value, require
from upstream_outcomes import validate as validate_matrix, verify_jobs

FIELDS = ('schema_version', 'version', 'requested_architectures',
          'resolved_contract_sha256', 'current_generation', 'generations',
          'artifact_generations', 'artifacts')


def generation_id(manifest):
    raw = json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True, allow_nan=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def producer_commit(manifest):
    rows = manifest.get('artifacts')
    require(isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows),
            'Each lineage generation requires a successful artifact')
    commits = {commit_value(row.get('build_repository_commit')) for row in rows}
    require(len(commits) == 1, 'A generation must bind one exact executed producer commit')
    return next(iter(commits))


def validate(value, version):
    exact_keys(value, FIELDS, 'upper publication lineage')
    require(type(value['schema_version']) is int and value['schema_version'] == 3 and
            value['version'] == version, 'Upper publication lineage identity mismatch')
    contract = hash_value(value['resolved_contract_sha256'])
    generations = value['generations']
    require(isinstance(generations, dict) and 1 <= len(generations) <= 8,
            'Invalid upper producer generation inventory')
    current = value['current_generation']
    require(isinstance(current, str) and current in generations, 'Missing current upper generation')
    available = {}
    for key, manifest in generations.items():
        hash_value(key)
        validate_matrix(manifest, version)
        producer_commit(manifest)
        require(key == generation_id(manifest) and
                manifest['requested_architectures'] == value['requested_architectures'],
                'Upper generation hash or requested inventory differs')
        require(all(row.get('resolved_contract_sha256') == contract for row in manifest['artifacts']),
                'Upper generations differ in source contract')
        available[key] = {row['architecture']: row for row in manifest['artifacts']}
    selection = value['artifact_generations']
    require(isinstance(selection, dict) and selection and isinstance(value['artifacts'], list) and
            all(isinstance(row, dict) for row in value['artifacts']), 'Missing selected upper artifacts')
    accepted = [arch for arch in value['requested_architectures'] if arch in selection]
    require(set(accepted) == set(selection) and
            [row.get('architecture') for row in value['artifacts']] == accepted,
            'Selected upper architecture inventory differs')
    for row in value['artifacts']:
        key = selection[row['architecture']]
        require(isinstance(key, str) and key in available and
                available[key].get(row['architecture']) == row,
                'Selected artifact differs from its original successful producer')
    require(set(generations) == set(selection.values()) | {current}, 'Unreferenced upper generation')
    return accepted


def authenticate(value, version, client):
    accepted = validate(value, version)
    for manifest in value['generations'].values():
        verify_jobs(manifest, version, producer_commit(manifest), client)
    return accepted


def receipt_binding(proof, manifest, accepted):
    fields = ('requested_architectures', 'resolved_contract_sha256', 'current_generation',
              'generations', 'artifact_generations')
    require(proof.get('successful_architectures') == accepted and
            all(proof.get(key) == manifest[key] for key in fields),
            'Upstream receipt differs from authenticated producer lineage')
