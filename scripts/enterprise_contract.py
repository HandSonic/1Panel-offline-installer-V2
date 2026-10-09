#!/usr/bin/env python3
"""Runtime-resolved enterprise archive requirements, independent of community upgrades."""
import hashlib
from pathlib import Path
from validate_payload import APP_REQUIRED

ROOT = Path(__file__).resolve().parents[1]


def source(version, arch, root=ROOT):
    from runtime_contract import require_selected
    pin = require_selected(version, root)['inventory']['enterprise']['archives'].get(arch)
    if pin is None:
        raise ValueError('Enterprise architecture is absent from authenticated discovery')
    return pin


def validate_layout(entries, prefix, installer, version, root=ROOT):
    from runtime_contract import require_selected
    from installer_capabilities import inspect_installer
    require_selected(version, root)
    names = {name[len(prefix):] for name in entries if name.startswith(prefix)}
    capabilities = inspect_installer(installer, names, 'enterprise')
    row = {'installer_sha256': capabilities['installer_sha256'],
           'appstore_required': capabilities['appstore_required']}
    required = APP_REQUIRED + ['install.sh', 'upgrade.sh']
    if row['appstore_required']:
        required += ['appstore.tar.gz']
    for name in required:
        member = entries.get(prefix + name)
        if member is None or not member.isfile() or member.size <= 0:
            raise ValueError('Enterprise input missing regular payload: ' + name)
    if hashlib.sha256(installer).hexdigest() != row['installer_sha256']:
        raise ValueError('Enterprise installer differs from its reviewed version')
    return row
