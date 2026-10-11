"""Typed first-release applicability, separate from predecessor upgrade proof."""
import hashlib

from initial_release_applicability import validate_initial_release
from native_candidate_input import recorded_context
from public_predecessor import channel
from resolved_inventory import canonical, commit_value, exact_keys, hash_value, require

KIND = 'initial-release-native-install'
NOT_APPLICABLE = 'not-applicable; first official stable v2 release'
REQUIRED = 'actual target installation in this run; predecessor upgrade and rollback not applicable'


def validate_input(proof, candidate):
    fields = ('schema', 'kind', 'target', 'target_manifest_sha256', 'target_source_commit',
              'applicability', 'applicability_sha256', 'target_package_path', 'required_acceptance')
    exact_keys(proof, fields + (('_file_sha256',) if '_file_sha256' in proof else ()),
               'initial installation input')
    require(type(proof['schema']) is int and proof['schema'] == 3 and proof['kind'] == KIND and
            proof['target'] == candidate and candidate.get('source') in ('official', 'custom') and
            candidate.get('arch') in ('amd64', 'arm64') and channel(candidate['version']) == 'stable' and
            proof['required_acceptance'] == REQUIRED and not recorded_context(proof, candidate['version']),
            'Initial installation identity/context mismatch')
    hash_value(proof['target_manifest_sha256'])
    require(isinstance(proof['target_package_path'], str) and proof['target_package_path'].startswith('/'),
            'Initial installation target path missing')
    expected = proof['target_source_commit']
    if candidate['source'] == 'custom':
        commit_value(expected)
    else:
        require(expected is None, 'Official initial installation has no custom source claim')
    validate_initial_release(proof['applicability'], candidate['version'], 'stable', expected)
    require(hash_value(proof['applicability_sha256']) == hashlib.sha256(canonical(proof['applicability'])).hexdigest(),
            'Initial-release applicability bytes changed')
    return proof
