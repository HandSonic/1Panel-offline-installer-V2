#!/usr/bin/env python3
"""Replay the authenticated producer's per-run lock; never resolve dependencies."""
import hashlib
import json
import os
from pathlib import Path
import stat

from resolved_inventory import commit_value, exact_keys, hash_value, require
from frontend_lock_origins import validate_lock_origins

SIDECAR = 'resolved-frontend-package-lock.json'
MAX_MANIFEST = 65536
MAX_LOCK = 4 * 1024 * 1024
GENERATOR = {'node': '22.22.1', 'npm': '10.9.4',
             'registry': 'https://registry.npmjs.org', 'lifecycle_scripts': False}


def data_object(raw, bound, label):
    require(isinstance(raw, bytes) and 0 < len(raw) <= bound, 'Invalid ' + label + ' size')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'Duplicate ' + label + ' field')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
    require(isinstance(value, dict), label + ' must be an object')
    return value


def validate_descriptor(lock, source):
    exact_keys(lock, ('kind', 'sha256', 'bytes', 'manifest_sha256', 'manifest', 'generator'), 'resolved frontend lock')
    require(lock['kind'] == 'resolved', 'Expected automatically resolved lock')
    hash_value(lock['sha256']); hash_value(lock['manifest_sha256'])
    require(type(lock['bytes']) is int and 0 < lock['bytes'] <= MAX_LOCK, 'Invalid resolved lock size')
    exact_keys(lock['generator'], GENERATOR, 'lock generator')
    require(lock['generator'] == GENERATOR and lock['generator']['lifecycle_scripts'] is False,
            'Unsupported automatic lock generator')
    require(source['repository'] == '1Panel-dev/1Panel', 'Unexpected automatic lock source')
    commit_value(source['commit'])
    require(source['absent'] == ['frontend/package-lock.json'] and 'frontend/package-lock.json' not in source['files'],
            'Automatic lock requires immutable source-lock absence')
    require(isinstance(lock['manifest'], str), 'Missing resolved source manifest bytes')
    raw = lock['manifest'].encode('utf-8')
    manifest = data_object(raw, MAX_MANIFEST, 'frontend manifest')
    actual = hashlib.sha256(raw).hexdigest()
    require(actual == lock['manifest_sha256'] and source['files']['frontend/package.json'] ==
            {'sha256': actual, 'bytes': len(raw)}, 'Resolved manifest differs from immutable source')
    return manifest


def validate_payload(raw, contract):
    descriptor = contract['frontend_lock']
    manifest = validate_descriptor(descriptor, contract['source'])
    require(isinstance(raw, bytes) and len(raw) == descriptor['bytes'] and
            hashlib.sha256(raw).hexdigest() == descriptor['sha256'], 'Resolved frontend lock bytes mismatch')
    lock = data_object(raw, MAX_LOCK, 'frontend lock')
    require(type(lock.get('lockfileVersion')) is int and lock['lockfileVersion'] == 3 and
            isinstance(lock.get('packages'), dict) and isinstance(lock['packages'].get(''), dict),
            'Unsupported resolved lock schema')
    root = lock['packages']['']
    for key in ('name', 'version'):
        require(lock.get(key) == manifest.get(key) and root.get(key) == manifest.get(key), 'Resolved lock root identity differs')
    for key in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
        require(root.get(key, {}) == manifest.get(key, {}), 'Resolved lock root manifest differs: ' + key)
    validate_lock_origins(lock)
    return raw


def required_sidecars(contract):
    return (SIDECAR,) if contract['frontend_lock']['kind'] == 'resolved' else ()


def read_payload(directory, contract):
    path = Path(directory) / SIDECAR
    if not required_sidecars(contract):
        require(not path.exists() and not path.is_symlink(), 'Unexpected resolved frontend lock sidecar')
        return None
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_LOCK, 'Unsafe resolved frontend lock file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        require(stat.S_ISREG(before.st_mode) and identity(info) == identity(before), 'Resolved lock changed before opening')
        raw = stream.read(MAX_LOCK + 1)
        require(identity(before) == identity(os.fstat(stream.fileno())) and identity(before) == identity(path.lstat()),
                'Resolved lock changed while reading')
    return validate_payload(raw, contract)
