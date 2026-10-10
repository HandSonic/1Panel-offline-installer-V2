#!/usr/bin/env python3
"""Check a custom archive with its exact producer contract before any repacking."""
import sys


def validate(path, version, arch):
    from runtime_contract import require_selected, ROOT
    from resolved_transport import acquire_origin
    from validate_resolved_custom import verify_archive
    from installer_capabilities import inspect_installer
    runtime = require_selected(version, ROOT)
    contract = runtime['source_contract']
    if contract is None:
        raise ValueError('Authenticated custom source contract required')
    row = runtime['upstream']['records'][arch]
    origin = contract['resources']['geoip'].get('archive')
    origin_path = acquire_origin(origin, ROOT / 'build/cache') if origin else None
    return verify_archive(path, version, runtime['mode'], arch, contract,
        runtime['source_contract_sha256'], {'sha256': row['sha256'], 'bytes': row['size']},
        row['build_repository_commit'],
        {part: text.encode('utf-8') for part, text in runtime['configuration_sources'].items()},
        row, origin_path, capability_check=inspect_installer)


if __name__ == '__main__':
    validate(*sys.argv[1:4])
