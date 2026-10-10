"""Genuinely synthetic future versions; no historical records or archive bodies."""
import hashlib
import itertools
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from public_predecessor import (API_ROOT, RECEIPT, REPOSITORY, UPSTREAM, WORKFLOW,
                                bind_predecessor, expected_archives,
                                require_native_acceptance, select_predecessor,
                                semver)
from resolved_inventory import ARCHES, canonical, digest


def facts(raw):
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def release(tag, release_id, **extra):
    return {'id': release_id, 'tag_name': tag, 'draft': False,
            'prerelease': '-' in tag, 'url': API_ROOT + '/releases/' + str(release_id), **extra}


def fixture(version='v2.100.0', target='v2.101.0', mode='stable', enterprise=True):
    matrix = {'official': [a for a in ARCHES if a != 'loong64'], 'custom': list(ARCHES)}
    if enterprise:
        matrix.update({'enterprise-original': ['amd64', 'arm64'],
                       'enterprise-docker': ['amd64', 'arm64']})
    names = expected_archives(version, matrix)
    # These tiny strings are not downloaded data or executable archive fixtures.
    files = {name: facts(('synthetic package placeholder: ' + name).encode()) for name in names}
    checksums = ''.join(files[name]['sha256'] + '  ' + name + '\n' for name in sorted(names)).encode()
    files['checksums.txt'] = facts(checksums)
    proof = {'schema': 1, 'contract': 'downstream17', 'version': version,
             'release_tag': version, 'repository': REPOSITORY,
             'policy_fingerprint': facts(b'synthetic policy claim')['sha256'],
             'workflow_run_id': 900001, 'workflow_commit': 'a' * 40, 'files': files}
    raw = canonical(proof)
    public = release(version, 800001)
    public['assets'] = [{'id': 700001 + i, 'name': name, 'state': 'uploaded',
                         'url': API_ROOT + '/releases/assets/' + str(700001 + i),
                         'size': value['bytes'], 'digest': 'sha256:' + value['sha256']}
                        for i, (name, value) in enumerate(sorted({**files, RECEIPT: facts(raw)}.items()))]
    run = {'id': 900001, 'head_sha': 'a' * 40, 'run_attempt': 2, 'path': WORKFLOW,
           'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success',
           'repository': {'id': 600001, 'full_name': REPOSITORY},
           'head_repository': {'id': 600001, 'full_name': REPOSITORY}}
    selected = select_predecessor(target, mode, [public], catalogue_complete=True)
    return [selected, public, raw, checksums, matrix, run]


def rebind_controls(args, proof, checksums=None):
    """Keep all claimed hashes consistent to expose deeper semantic violations."""
    if checksums is not None:
        args[3] = checksums
        proof['files']['checksums.txt'] = facts(checksums)
    args[2] = canonical(proof)
    controls = {RECEIPT: facts(args[2]), 'checksums.txt': proof['files']['checksums.txt']}
    for asset in args[1]['assets']:
        if asset['name'] in controls:
            value = controls[asset['name']]
            asset.update(size=value['bytes'], digest='sha256:' + value['sha256'])


def synthetic_ci_claim():
    return {'repository': UPSTREAM, 'run_id': 990001, 'artifact_id': 770001,
            'artifact_sha256': facts(b'synthetic upstream ZIP')['sha256'],
            'build_repository_commit': 'b' * 40,
            'run_url': f'https://github.com/{UPSTREAM}/actions/runs/990001'}


def append_backup(args, canonical_name=RECEIPT, token='abcdef123456', asset_id=799999):
    value = facts(b'synthetic unendorsed recovery content ' + token.encode())
    asset = {'id': asset_id, 'name': canonical_name + '.backup-' + token, 'state': 'uploaded',
             'url': API_ROOT + '/releases/assets/' + str(asset_id),
             'size': value['bytes'], 'digest': 'sha256:' + value['sha256']}
    args[1]['assets'].append(asset)
    return asset


class SelectionTests(unittest.TestCase):
    def select(self, tags, target='v2.101.0', mode='stable'):
        return select_predecessor(target, mode, [release(tag, 800001 + i) for i, tag in enumerate(tags)],
                                  catalogue_complete=True)['release']['tag_name']

    def test_numeric_semver_not_publication_date_or_lexical_order(self):
        self.assertEqual(self.select(['v2.9.0', 'v2.99.9', 'v2.100.0', 'v2.101.0', 'v2.102.0']), 'v2.100.0')
        releases = [release('v2.99.0', 800001, published_at='2099-01-01'),
                    release('v2.100.0', 800002, published_at='2000-01-01')]
        results = [select_predecessor('v2.101.0', 'stable', list(rows), catalogue_complete=True)
                   for rows in itertools.permutations(releases)]
        self.assertEqual(results[0], results[1])

    def test_beta_and_dev_numeric_prerelease_precedence(self):
        for mode in ('beta', 'dev'):
            with self.subTest(mode=mode):
                tags = [f'v2.101.0-{mode}.9', f'v2.101.0-{mode}.2', f'v2.101.0-{mode}.10']
                self.assertEqual(self.select(tags, f'v2.101.0-{mode}.11', mode), tags[2])
                self.assertEqual(self.select([f'v2.101.0-{mode}', f'v2.101.0-{mode}.0'],
                                             f'v2.101.0-{mode}.1', mode), f'v2.101.0-{mode}.0')

    def test_historical_patch_numbers_are_numeric(self):
        with self.assertRaisesRegex(ValueError, 'No strictly earlier'):
            self.select(['v2.1.13', 'v2.1.4'], target='v2.1.4')
        # Synthetic catalogue membership does not assert a public release exists.
        self.assertEqual(self.select(['v2.1.13', 'v2.1.3', 'v2.1.4'],
                                     target='v2.1.4'), 'v2.1.3')

    def test_shared_numeric_prerelease_rules(self):
        versions = ['v2.101.0-beta.0', 'v2.101.0-beta.1', 'v2.101.0-beta.2', 'v2.101.0-beta.10']
        keys = [semver(v, 'beta') for v in versions]
        self.assertEqual(keys, sorted(keys))
        self.assertLess(semver('v2.101.0-beta.1', 'beta'), semver('v2.101.0', 'stable'))
        for version in ['v2.101.0-beta.1.1', 'v2.101.0-beta.alpha', 'v2.101.0-beta-1',
                        'v2.101.0-beta-10', 'v2.101.0-beta', 'v2.101.0-beta.01']:
            with self.subTest(version=version), self.assertRaises(ValueError):
                semver(version, 'beta')

    def test_same_channel_policy_never_silently_falls_back(self):
        self.assertEqual(self.select(['v2.99.0', 'v2.100.0-beta.1', 'v2.100.0-dev.1']), 'v2.99.0')
        for mode in ('beta', 'dev'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'No strictly earlier'):
                self.select(['v2.100.0'], f'v2.101.0-{mode}.1', mode)

    def test_equal_newer_draft_and_noncanonical_tags_do_not_qualify(self):
        tags = ['latest', 'v2.100.0-repair.1', 'v2.100.0+build.1', 'v2.099.0',
                'v2.101.0', 'v2.102.0', 'v3.0.0']
        rows = [release(tag, 800001 + i) for i, tag in enumerate(tags)]
        rows.append(release('v2.100.0', 800050, draft=True))
        with self.assertRaisesRegex(ValueError, 'No strictly earlier'):
            select_predecessor('v2.101.0', 'stable', rows, catalogue_complete=True)

    def test_invalid_target_channel_or_numeric_identifier_is_rejected(self):
        for version, mode in [('v2.101.0-beta.01', 'beta'), ('v2.101.0-beta.1', 'stable'),
                              ('v2.101.0', 'beta'), ('v3.0.0', 'stable'),
                              ('v2.101.0+build.1', 'stable'), ('v2.101.0-dev..1', 'dev')]:
            with self.subTest(version=version, mode=mode), self.assertRaises(ValueError):
                self.select([], version, mode)

    def test_incomplete_catalogue_and_ambiguous_id_or_tag_fail_closed(self):
        rows = [release('v2.100.0', 800001)]
        for complete in (False, None, 1):
            with self.subTest(complete=complete), self.assertRaisesRegex(ValueError, 'Complete release catalogue'):
                select_predecessor('v2.101.0', 'stable', rows, catalogue_complete=complete)
        for other in (release('v2.99.0', 800001), release('v2.100.0', 800002)):
            with self.subTest(other=other), self.assertRaisesRegex(ValueError, 'Ambiguous'):
                select_predecessor('v2.101.0', 'stable', rows + [other], catalogue_complete=True)

    def test_metadata_must_match_repository_and_channel(self):
        for mutation in ({'id': True}, {'url': 'https://example.invalid/releases/800001'},
                         {'draft': 'false'}, {'prerelease': True}):
            row = release('v2.100.0', 800001, **mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                select_predecessor('v2.101.0', 'stable', [row], catalogue_complete=True)


class BindingTests(unittest.TestCase):
    def test_future_stable_beta_and_dev_pin_every_canonical_asset(self):
        for version, target, mode in [('v2.100.0', 'v2.101.0', 'stable'),
                                     ('v2.101.0-beta.10', 'v2.101.0-beta.11', 'beta'),
                                     ('v2.101.0-dev.10', 'v2.101.0-dev.11', 'dev')]:
            args = fixture(version, target, mode)
            bound, sha = bind_predecessor(*args)
            with self.subTest(mode=mode):
                self.assertEqual(len(bound['assets']), 19)
                self.assertEqual(bound['release_id'], 800001)
                self.assertEqual(bound['receipt_run']['run_attempt'], 2)
                self.assertEqual(bound['version'], version)
                self.assertEqual(sha, digest(bound))
                for asset in args[1]['assets']:
                    self.assertEqual(bound['assets'][asset['name']],
                                     {'asset_id': asset['id'], 'url': asset['url'], 'bytes': asset['size'],
                                      'sha256': asset['digest'].removeprefix('sha256:')})

    def test_no_enterprise_requires_exact_remaining_membership(self):
        bound, _ = bind_predecessor(*fixture(enterprise=False))
        self.assertEqual(len(bound['assets']), 15)
        self.assertEqual(set(bound['matrix']), {'official', 'custom'})

    def test_asset_and_matrix_order_do_not_change_snapshot_digest(self):
        args = fixture()
        first = bind_predecessor(*args)
        args[1]['assets'].reverse()
        args[4] = {source: list(reversed(arches)) for source, arches in reversed(list(args[4].items()))}
        self.assertEqual(first, bind_predecessor(*args))

    def test_output_has_no_references_to_mutable_input_objects(self):
        args = fixture()
        bound, sha = bind_predecessor(*args)
        args[1]['assets'][0]['id'] = 123
        args[4]['custom'].clear()
        args[5]['id'] = 123
        self.assertEqual(digest(bound), sha)

    def test_selected_release_replacement_and_older_fallback_are_rejected(self):
        for change in ({'id': 800002}, {'tag_name': 'v2.99.0'}, {'draft': True}, {'prerelease': True}):
            args = fixture(); args[1].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_highest_broken_release_does_not_fall_back_to_valid_older_release(self):
        args = fixture()
        older = release('v2.99.0', 800002)
        args[0] = select_predecessor('v2.101.0', 'stable', [older, args[1]], catalogue_complete=True)
        self.assertEqual(args[0]['release']['tag_name'], 'v2.100.0')
        args[1]['assets'].pop()
        with self.assertRaisesRegex(ValueError, 'Incomplete canonical release assets'):
            bind_predecessor(*args)

    def test_asset_membership_ids_sha_size_endpoint_and_state_are_exact(self):
        for fault in ('missing', 'extra', 'duplicate-name', 'duplicate-id', 'id-bool',
                      'size-bool', 'size', 'digest', 'digest-missing', 'url', 'state'):
            args = fixture(); assets = args[1]['assets']
            if fault == 'missing': assets.pop()
            elif fault == 'extra': assets[0]['name'] = 'unexpected.txt'
            elif fault == 'duplicate-name': assets[1]['name'] = assets[0]['name']
            elif fault == 'duplicate-id': assets[1]['id'] = assets[0]['id']
            elif fault == 'id-bool': assets[0]['id'] = True
            elif fault == 'size-bool': assets[0]['size'] = True
            elif fault == 'size': assets[0]['size'] += 1
            elif fault == 'digest': assets[0]['digest'] = 'sha256:' + 'f' * 64
            elif fault == 'digest-missing': assets[0]['digest'] = None
            elif fault == 'url': assets[0]['url'] = 'https://example.invalid/file'
            elif fault == 'state': assets[0]['state'] = 'new'
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_known_optional_upstream_input_formats_are_bound_as_claims_only(self):
        for upstream in ({'source_kind': 'verified-public-release'}, synthetic_ci_claim()):
            args = fixture(); proof = json.loads(args[2]); proof['upstream_input'] = upstream
            original_sha = bind_predecessor(*args)[1]
            rebind_controls(args, proof)
            with self.subTest(upstream=upstream):
                bound, sha = bind_predecessor(*args)
                self.assertEqual(bound['upstream_input_claim'], upstream)
                self.assertNotEqual(sha, original_sha)
                self.assertEqual(bound['native_acceptance'], 'blocked')
                upstream.clear()
                self.assertEqual(digest(bound), sha)
        self.assertNotIn('upstream_input_claim', bind_predecessor(*fixture())[0])

    def test_unknown_incomplete_and_conflicting_upstream_input_claims_fail_closed(self):
        claims = [None, [], {}, {'source_kind': 'unverified-public-release'},
                  {'source_kind': 'verified-public-release', 'approved': True},
                  {'source_kind': 'verified-public-release', **synthetic_ci_claim()}]
        for key, value in [('repository', 'untrusted/repository'), ('run_id', True),
                           ('run_id', '990001'), ('artifact_id', 0), ('artifact_sha256', 'x' * 64),
                           ('build_repository_commit', 'main'),
                           ('run_url', f'https://github.com/{UPSTREAM}/actions/runs/990002')]:
            claims.append({**synthetic_ci_claim(), key: value})
        for key in synthetic_ci_claim():
            claim = synthetic_ci_claim(); claim.pop(key); claims.append(claim)
        for claim in claims:
            args = fixture(); proof = json.loads(args[2]); proof['upstream_input'] = claim
            rebind_controls(args, proof)
            with self.subTest(claim=claim), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_repaired_release_keeps_backups_separate_from_canonical_membership(self):
        args = fixture()
        canonical, _ = bind_predecessor(*args)
        backups = [append_backup(args), append_backup(args, 'checksums.txt', '123456abcdef', 799998),
                   append_backup(args, RECEIPT, '012345abcdef', 799997)]
        bound, sha = bind_predecessor(*args)
        self.assertEqual(bound['assets'], canonical['assets'])
        self.assertEqual(len(bound['recovery_assets']), 3)
        for asset in backups:
            self.assertEqual(bound['recovery_assets'][asset['name']],
                             {'canonical_name': asset['name'].split('.backup-')[0],
                              'asset_id': asset['id'], 'url': asset['url'],
                              'sha256': asset['digest'][7:], 'bytes': asset['size']})
        self.assertEqual(bound['native_acceptance'], 'blocked')
        args[1]['assets'].reverse()
        self.assertEqual(bind_predecessor(*args), (bound, sha))
        backups[0]['digest'] = 'sha256:' + 'e' * 64
        self.assertNotEqual(bind_predecessor(*args)[1], sha)

    def test_staged_unrecognized_duplicate_or_conflicting_recovery_assets_are_rejected(self):
        for fault in ('staged', 'unknown-base', 'uppercase-token', 'short-token', 'long-token',
                      'duplicate-name', 'duplicate-id', 'canonical-id', 'wrong-url', 'size-bool',
                      'size-zero', 'missing-digest', 'invalid-digest', 'wrong-state', 'missing-canonical'):
            args = fixture(); backup = append_backup(args)
            if fault == 'staged': backup['name'] = backup['name'].replace('.backup-', '.staged-')
            elif fault == 'unknown-base': backup['name'] = 'foreign.txt.backup-abcdef123456'
            elif fault == 'uppercase-token': backup['name'] = RECEIPT + '.backup-ABCDEF123456'
            elif fault == 'short-token': backup['name'] = RECEIPT + '.backup-abcdef'
            elif fault == 'long-token': backup['name'] += '0'
            elif fault == 'duplicate-name': append_backup(args, asset_id=799998)
            elif fault == 'duplicate-id': append_backup(args, token='123456abcdef')
            elif fault == 'canonical-id': backup['id'] = args[1]['assets'][0]['id']
            elif fault == 'wrong-url': backup['url'] = 'https://example.invalid/recovery'
            elif fault == 'size-bool': backup['size'] = True
            elif fault == 'size-zero': backup['size'] = 0
            elif fault == 'missing-digest': backup['digest'] = None
            elif fault == 'invalid-digest': backup['digest'] = 'sha256:' + 'x' * 64
            elif fault == 'wrong-state': backup['state'] = 'new'
            elif fault == 'missing-canonical': args[1]['assets'] = [a for a in args[1]['assets'] if a['name'] != RECEIPT]
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_backup_names_are_not_allowed_in_the_canonical_receipt_or_checksums(self):
        for where in ('receipt', 'checksums'):
            args = fixture(); backup = append_backup(args); proof = json.loads(args[2])
            if where == 'receipt':
                proof['files'][backup['name']] = {'sha256': backup['digest'][7:], 'bytes': backup['size']}
                rebind_controls(args, proof)
            else:
                rebind_controls(args, proof, args[3] + (backup['digest'][7:] + '  ' + backup['name'] + '\n').encode())
            with self.subTest(where=where), self.assertRaisesRegex(ValueError, 'canonical membership'):
                bind_predecessor(*args)

    def test_receipt_identity_types_and_membership_are_exact_even_after_rehashing(self):
        for field, value in [('schema', True), ('schema', 2), ('contract', 'upstream7'),
                             ('version', 'v2.99.0'), ('release_tag', 'v2.100.0-repair'),
                             ('repository', 'somewhere/else'), ('workflow_run_id', True),
                             ('workflow_commit', 'main'), ('policy_fingerprint', 'unverified')]:
            args = fixture(); proof = json.loads(args[2]); proof[field] = value
            rebind_controls(args, proof)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                bind_predecessor(*args)
        for fault in ('missing', 'extra', 'bad-size', 'bad-sha', 'unknown-field'):
            args = fixture(); proof = json.loads(args[2]); name = next(iter(proof['files']))
            if fault == 'missing': proof['files'].pop(name)
            elif fault == 'extra': proof['files']['foreign.tar.gz'] = facts(b'foreign')
            elif fault == 'bad-size': proof['files'][name]['bytes'] = True
            elif fault == 'bad-sha': proof['files'][name]['sha256'] = 'x' * 64
            else: proof['approved'] = True
            rebind_controls(args, proof)
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_checksum_membership_fails_even_when_control_hashes_are_consistent(self):
        for fault in ('duplicate', 'missing', 'foreign', 'wrong-version', 'wrong-hash', 'path', 'separator'):
            args = fixture(); proof = json.loads(args[2]); lines = args[3].splitlines(keepends=True)
            if fault == 'duplicate': lines.append(lines[0])
            elif fault == 'missing': lines.pop()
            elif fault == 'foreign': lines.append(('f' * 64 + '  foreign.tar.gz\n').encode())
            elif fault == 'wrong-version': lines[0] = lines[0].replace(b'v2.100.0', b'v2.099.0')
            elif fault == 'wrong-hash': lines[0] = b'f' * 64 + lines[0][64:]
            elif fault == 'path': lines[0] = lines[0].replace(b'  1panel-', b'  ../1panel-')
            elif fault == 'separator': lines[0] = lines[0].replace(b'  1panel-', b' *1panel-')
            rebind_controls(args, proof, b''.join(lines))
            with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, 'checksum|Checksum'):
                bind_predecessor(*args)

    def test_control_duplicate_fields_nonfinite_and_oversize_fail_closed(self):
        for raw in (b'{"schema":1,"schema":1}', b'{"schema":NaN}', b'[]', b'', b' ' * (1024 ** 2 + 1)):
            args = fixture(); args[2] = raw
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_matrix_cannot_hide_source_or_native_architecture(self):
        for fault in ('official', 'custom', 'custom-arch', 'native', 'enterprise-pair',
                      'enterprise-arch', 'duplicate', 'unknown', 'empty'):
            args = fixture(); matrix = args[4]
            if fault == 'official': matrix.pop('official')
            elif fault == 'custom': matrix.pop('custom')
            elif fault == 'custom-arch': matrix['custom'].remove('loong64')
            elif fault == 'native': matrix['official'].remove('arm64')
            elif fault == 'enterprise-pair': matrix.pop('enterprise-original')
            elif fault == 'enterprise-arch': matrix['enterprise-original'].append('armv7')
            elif fault == 'duplicate': matrix['custom'].append('amd64')
            elif fault == 'unknown': matrix['unreviewed'] = ['amd64', 'arm64']
            elif fault == 'empty': matrix['official'] = []
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_cancelled_failed_pending_and_cross_run_provenance_cannot_bind(self):
        for changes in ({'conclusion': 'cancelled'}, {'conclusion': 'failure'},
                        {'status': 'in_progress'}, {'id': 900002}, {'id': True},
                        {'head_sha': 'b' * 40}, {'run_attempt': True}, {'run_attempt': 0},
                        {'event': 'pull_request'}, {'path': '.github/workflows/untrusted.yml'},
                        {'head_repository': {'id': 600002, 'full_name': REPOSITORY}}):
            args = fixture(); args[5].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                bind_predecessor(*args)

    def test_successful_receipt_refresh_remains_blocked_without_native_evidence(self):
        bound, _ = bind_predecessor(*fixture())
        self.assertEqual(bound['native_acceptance'], 'blocked')
        self.assertIn('same-run producer/native', bound['blocker'])
        requirements = ' '.join(bound['required_integration'])
        for expected in ('publication_prepare', 'artifact IDs', 'fresh/existing',
                         'Cancelled writers', 'upload logs', 'rollback/data-preservation'):
            self.assertIn(expected, requirements)
        # A caller cannot turn a successful structural binding into runtime approval.
        for candidate in (bound, {**bound, 'native_acceptance': 'passed'}, {'native_acceptance': 'passed'}):
            with self.assertRaisesRegex(ValueError, 'Native predecessor acceptance blocked'):
                require_native_acceptance(candidate)


if __name__ == '__main__':
    unittest.main()
