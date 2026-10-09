#!/usr/bin/env python3
"""Versioned enterprise archive requirements, independent of community upgrades."""
import hashlib
import json
from pathlib import Path
import re
from validate_payload import APP_REQUIRED

ROOT = Path(__file__).resolve().parents[1]


def contract(version, root=ROOT):
    records = json.loads((root / 'config/enterprise-contracts.json').read_text())
    if version not in records:
        raise ValueError('Unreviewed enterprise payload contract: ' + version)
    row = records[version]
    if set(row) != {'installer_sha256', 'appstore_required'} or \
            not re.fullmatch('[0-9a-f]{64}', row.get('installer_sha256', '')) or \
            type(row.get('appstore_required')) is not bool:
        raise ValueError('Malformed enterprise payload contract')
    return row


def validate_layout(entries, prefix, installer, version, root=ROOT):
    row = contract(version, root)
    required = APP_REQUIRED + ['install.sh', 'upgrade.sh']
    if row['appstore_required']:
        required += ['appstore.tar.gz']
    for name in required:
        member = entries.get(prefix + name)
        if member is None or not member.isfile() or member.size <= 0:
            raise ValueError('Enterprise input missing regular payload: ' + name)
    if hashlib.sha256(installer).hexdigest() != row['installer_sha256']:
        raise ValueError('Enterprise installer differs from its reviewed version')
    appstore_step = bool(re.search(rb'(?m)^function Install_AppStore\s*\(\)\s*\{', installer))
    if appstore_step != row['appstore_required'] or \
            (prefix + 'appstore.tar.gz' in entries) != row['appstore_required']:
        raise ValueError('Enterprise AppStore capability/resource contract mismatch')
    return row
