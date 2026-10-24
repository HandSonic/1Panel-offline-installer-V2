"""Genuinely synthetic future versions; no historical records or archive bodies."""
import hashlib
import itertools
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from public_predecessor import (API_ROOT, RECEIPT, REPOSITORY, WORKFLOW,
                                select_predecessor, semver)
from resolved_inventory import ARCHES, canonical


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
    names = {f'1panel-{version}-{source}-offline-linux-{arch}.tar.gz'
             for source, arches in matrix.items() for arch in arches}
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


if __name__ == '__main__':
    unittest.main()
