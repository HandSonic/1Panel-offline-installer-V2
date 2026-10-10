"""Synthetic first-release applicability; never touch the network or services."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import initial_release_applicability as initial
from resolved_inventory import digest

VERSION = 'v2.73.0'
COMMIT = 'a' * 40
ANNOTATED = 'b' * 40


def release(version=VERSION, ident=301, repository=initial.OFFICIAL, **changes):
    result = {'id': ident, 'tag_name': version, 'draft': False,
              'prerelease': initial.channel(version) not in (None, 'stable'),
              'url': f'https://api.github.com/repos/{repository}/releases/{ident}',
              'published_at': '2025-01-02T03:04:05Z'}
    return dict(result, **changes)


def tag(version=VERSION, commit=COMMIT):
    return {'name': version, 'commit': {'sha': commit, 'url': initial.API_ROOT + '/commits/' + commit}}


def git_object(sha=COMMIT, kind='commit'):
    return {'type': kind, 'sha': sha, 'url': initial.API_ROOT + '/git/' + kind + 's/' + sha}


def ref(version=VERSION, sha=COMMIT, kind='commit'):
    return {'ref': 'refs/tags/' + version, 'url': initial.API_ROOT + '/git/refs/tags/' + version,
            'object': git_object(sha, kind)}


def annotation(sha=ANNOTATED, target=COMMIT, kind='commit', name=VERSION):
    return {'sha': sha, 'url': initial.API_ROOT + '/git/tags/' + sha,
            'tag': name, 'object': git_object(target, kind)}


def lower(rows=None):
    return initial.lower_catalogue_snapshot([] if rows is None else rows, catalogue_complete=True)


class GitHub:
    """Faithful endpoint/page simulation with no implicit missing-page success."""
    repo = initial.REPOSITORY

    def __init__(self, releases=None, tags=None, *, annotated=False):
        self.calls = []
        self.responses = {}
        self.catalogue('releases', [release()] if releases is None else releases)
        self.catalogue('tags', [tag()] if tags is None else tags)
        self.responses[f'repos/{initial.OFFICIAL}/releases/301'] = release()
        self.responses[f'repos/{initial.OFFICIAL}/git/ref/tags/{VERSION}'] = (
            ref(sha=ANNOTATED, kind='tag') if annotated else ref())
        self.responses[f'repos/{initial.OFFICIAL}/git/tags/{ANNOTATED}'] = annotation()
        self.responses[f'repos/{initial.OFFICIAL}/git/commits/{COMMIT}'] = {
            'sha': COMMIT, 'url': initial.API_ROOT + '/git/commits/' + COMMIT}

    def catalogue(self, kind, rows):
        for offset in range(0, len(rows) + 1, initial.PER_PAGE):
            self.responses[f'repos/{initial.OFFICIAL}/{kind}?per_page=100&page={offset // 100 + 1}'] = (
                rows[offset:offset + 100])

    def run(self, *args):
        if len(args) != 2 or args[0] != 'api':
            raise AssertionError(args)
        self.calls.append(args[1])
        result = self.responses[args[1]]
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result()
        return result if isinstance(result, str) else json.dumps(result)


def resolve(client=None, catalogue=None, expected=COMMIT):
    return initial.resolve_initial_release(client or GitHub(), VERSION, 'stable',
                                           lower() if catalogue is None else catalogue, expected)


class InitialReleaseTests(unittest.TestCase):
    def test_arbitrary_numeric_first_stable_release_has_exact_immutable_identity(self):
        client = GitHub(releases=[release(), release('v1.99.0', 302), release('v2.72.0-beta.2', 303)],
                        tags=[tag(), tag('v1.99.0'), tag('v2.72.0-beta.2')])
        proof = resolve(client)
        self.assertEqual(proof['source_commit'], COMMIT)
        self.assertEqual(proof['target_release']['id'], 301)
        self.assertEqual(proof['annotated_tags'], [])
        self.assertEqual(initial.validate_initial_release(proof, VERSION, 'stable', COMMIT), proof)
        self.assertNotIn('native_acceptance', proof)
        self.assertEqual(client.calls.count(f'repos/{initial.OFFICIAL}/releases?per_page=100&page=1'), 2)
        self.assertTrue(all(endpoint.startswith('repos/1Panel-dev/1Panel/') for endpoint in client.calls))

    def test_annotated_tag_peels_to_exact_source_commit(self):
        proof = resolve(GitHub(annotated=True))
        self.assertEqual(proof['target_ref']['object']['sha'], ANNOTATED)
        self.assertEqual(proof['annotated_tags'], [annotation()])
        self.assertEqual(proof['source_commit'], COMMIT)

    def test_nested_annotated_tags_are_bounded_and_completely_retained(self):
        client = GitHub(annotated=True)
        nested = 'c' * 40
        client.responses[f'repos/{initial.OFFICIAL}/git/tags/{ANNOTATED}'] = annotation(
            target=nested, kind='tag')
        client.responses[f'repos/{initial.OFFICIAL}/git/tags/{nested}'] = annotation(
            sha=nested, name='earlier-annotation-name')
        proof = resolve(client)
        self.assertEqual(len(proof['annotated_tags']), 2)
        with patch.object(initial, 'MAX_TAG_DEPTH', 1), self.assertRaisesRegex(ValueError, 'depth|excessive'):
            resolve(client)

    def test_complete_pagination_includes_last_partial_and_empty_page(self):
        for count in (100, 101):
            rows = [release()] + [release('historic-' + str(i), 400 + i) for i in range(count - 1)]
            client = GitHub(releases=rows)
            proof = resolve(client)
            self.assertEqual(proof['official_releases']['page_lengths'], [100, count - 100])
            self.assertEqual(len(proof['official_releases']['entries']), count)

    def test_no_lower_release_is_not_proof_of_initial_official_release(self):
        for releases, tags in (([], []), ([release()], []), ([], [tag()])):
            with self.subTest(releases=releases, tags=tags), self.assertRaisesRegex(ValueError, 'both official'):
                resolve(GitHub(releases=releases, tags=tags))

    def test_earlier_official_tag_without_release_blocks_initial_exemption(self):
        client = GitHub(tags=[tag(), tag('v2.72.99')])
        with self.assertRaisesRegex(ValueError, 'Earlier canonical official'):
            resolve(client)

    def test_earlier_official_release_even_draft_without_tag_blocks_initial_exemption(self):
        for draft in (False, True):
            earlier = release('v2.72.99', 302, draft=draft, published_at=None if draft else '2025-01-01T00:00:00Z')
            with self.subTest(draft=draft), self.assertRaisesRegex(ValueError, 'Earlier canonical official'):
                resolve(GitHub(releases=[release(), earlier]))

    def test_missing_failed_cancelled_lower_evidence_never_becomes_initial(self):
        for conclusion in (None, 'failure', 'cancelled'):
            earlier = release('v2.72.99', 300, repository=initial.REPOSITORY)
            if conclusion is not None:
                earlier['receipt_run'] = {'conclusion': conclusion}
            client = GitHub()
            with self.subTest(conclusion=conclusion), self.assertRaisesRegex(ValueError, 'Earlier canonical lower'):
                resolve(client, lower([earlier]))
            self.assertEqual(client.calls, [])

    def test_lower_draft_cannot_be_used_as_absence(self):
        earlier = release('v2.72.99', 300, repository=initial.REPOSITORY, draft=True)
        with self.assertRaisesRegex(ValueError, 'Earlier canonical lower'):
            resolve(catalogue=lower([earlier]))

    def test_lower_complete_identity_digest_and_types_are_required(self):
        for complete in (None, False, 1, 'true'):
            with self.subTest(complete=complete), self.assertRaisesRegex(ValueError, 'Complete lower'):
                initial.lower_catalogue_snapshot([], catalogue_complete=complete)
        variants = [[], {}, dict(lower(), complete=False), dict(lower(), repository=initial.OFFICIAL),
                    dict(lower(), sha256='f' * 64), dict(lower(), releases={})]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve(catalogue=value)

    def test_lower_duplicate_release_id_or_tag_and_wrong_repository_are_rejected(self):
        row = release(repository=initial.REPOSITORY)
        changes = [dict(row), dict(row, id=302, url=f'https://api.github.com/repos/{initial.REPOSITORY}/releases/302'),
                   dict(row, tag_name='v2.74.0'), release()]
        for other in changes:
            with self.subTest(other=other), self.assertRaises(ValueError):
                initial.lower_catalogue_snapshot([row, other], catalogue_complete=True)

    def test_first_prerelease_modes_do_not_claim_cross_channel_compatibility(self):
        for version, mode in (('v2.73.0-beta.0', 'beta'), ('v2.73.0-dev.0', 'dev'), (VERSION, 'beta')):
            client = GitHub()
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                initial.resolve_initial_release(client, version, mode, lower())
            self.assertEqual(client.calls, [])

    def test_failed_malformed_or_incomplete_official_pages_fail_closed(self):
        endpoint = f'repos/{initial.OFFICIAL}/releases?per_page=100&page=1'
        for response in ({'message': 'rate limited'}, None, [None], {}, '[',
                         '[{"id":301,"id":302}]', '[NaN]', RuntimeError('API failed')):
            client = GitHub()
            client.responses[endpoint] = response
            with self.subTest(response=response), self.assertRaises((ValueError, RuntimeError)):
                resolve(client)
        client = GitHub(releases=[release()] + [release('historic-' + str(i), 400 + i) for i in range(99)])
        client.responses[f'repos/{initial.OFFICIAL}/releases?per_page=100&page=2'] = RuntimeError('page 2 failed')
        with self.assertRaisesRegex(RuntimeError, 'page 2 failed'):
            resolve(client)
        with patch.object(initial, 'MAX_PAGES', 1), self.assertRaisesRegex(ValueError, 'bounded pagination'):
            resolve(client)

    def test_official_invalid_duplicate_release_and_tag_fields_fail_closed(self):
        invalid_releases = [dict(release(), id=True), dict(release(), id='301'),
            dict(release(), draft=0), dict(release(), prerelease=0),
            dict(release(), tag_name=None), dict(release(), url='https://api.github.com/repos/attacker/repo/releases/301'),
            dict(release(), published_at=False), dict(release(), published_at='yesterday'),
            dict(release(), published_at='2025-02-30T00:00:00Z')]
        for row in invalid_releases:
            with self.subTest(row=row), self.assertRaises(ValueError):
                resolve(GitHub(releases=[row]))
        for rows in ([release(), release()], [release(), release('v2.74.0')],
                     [release(), release(ident=302)]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'duplicate'):
                resolve(GitHub(releases=rows))
        invalid_tags = [dict(tag(), name=None), dict(tag(), name=''), dict(tag(), commit=None),
                        tag(commit='short'), dict(tag(), commit={'sha': COMMIT, 'url': 'https://example.org'})]
        for row in invalid_tags:
            with self.subTest(row=row), self.assertRaises(ValueError):
                resolve(GitHub(tags=[row]))
        with self.assertRaisesRegex(ValueError, 'duplicate tag'):
            resolve(GitHub(tags=[tag(), tag()]))

    def test_target_must_be_published_non_draft_channel_consistent(self):
        for change in ({'draft': True}, {'published_at': None}, {'prerelease': True}):
            client = GitHub(releases=[dict(release(), **change)])
            with self.subTest(change=change), self.assertRaises(ValueError):
                resolve(client)

    def test_wrong_expected_source_commit_and_tag_commit_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'independently authenticated'):
            resolve(expected='c' * 40)
        with self.assertRaisesRegex(ValueError, 'Immutable commit'):
            resolve(expected=True)
        with self.assertRaisesRegex(ValueError, 'immutable tag commit'):
            resolve(GitHub(tags=[tag(commit='c' * 40)]))

    def test_ref_annotation_and_commit_identity_fail_closed(self):
        ref_endpoint = f'repos/{initial.OFFICIAL}/git/ref/tags/{VERSION}'
        for value in (dict(ref(), ref='refs/tags/v2.74.0'), dict(ref(), url='https://example.org'),
                      dict(ref(), object=git_object(kind='tree')),
                      dict(ref(), object={'type': 'commit', 'sha': COMMIT, 'url': 'https://example.org'})):
            client = GitHub(); client.responses[ref_endpoint] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve(client)
        for changes in ({'tag': 'v2.74.0'}, {'sha': 'c' * 40}, {'url': 'https://example.org'},
                        {'object': git_object(ANNOTATED, 'tag')}):
            client = GitHub(annotated=True)
            client.responses[f'repos/{initial.OFFICIAL}/git/tags/{ANNOTATED}'] = dict(annotation(), **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resolve(client)
        for changes in ({'sha': 'c' * 40}, {'url': 'https://example.org'}):
            client = GitHub()
            endpoint = f'repos/{initial.OFFICIAL}/git/commits/{COMMIT}'
            client.responses[endpoint].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resolve(client)

    def test_target_and_complete_catalogues_are_rechecked_for_mutation(self):
        for fault in ('release', 'ref', 'releases', 'tags'):
            client = GitHub()
            if fault == 'release':
                client.responses[f'repos/{initial.OFFICIAL}/releases/301'] = release(draft=True)
            elif fault == 'ref':
                reads = iter([ref(), ref(sha='c' * 40)])
                client.responses[f'repos/{initial.OFFICIAL}/git/ref/tags/{VERSION}'] = lambda: next(reads)
            else:
                original = [release()] if fault == 'releases' else [tag()]
                changed = original + ([release('v2.74.0', 302)] if fault == 'releases' else [tag('v2.74.0')])
                reads = iter([original, changed])
                client.responses[f'repos/{initial.OFFICIAL}/{fault}?per_page=100&page=1'] = lambda: next(reads)
            with self.subTest(fault=fault), self.assertRaisesRegex(ValueError, 'changed during'):
                resolve(client)

    def test_pure_validator_rejects_tampering_even_with_recomputed_digest(self):
        proof = resolve()
        for fault in ('schema-bool', 'extra', 'repo', 'version', 'mode', 'lower-complete', 'lower-digest',
                      'catalogue-complete', 'page-type', 'page-count', 'page-full', 'extra-release-field',
                      'earlier-tag', 'missing-tag', 'target-release-type', 'target-tag-extra',
                      'source-commit', 'ref-extra', 'commit-extra', 'chain'):
            changed = copy.deepcopy(proof)
            if fault == 'schema-bool': changed['schema'] = True
            elif fault == 'extra': changed['native_acceptance'] = 'success'
            elif fault == 'repo': changed['repository'] = initial.REPOSITORY
            elif fault == 'version': changed['version'] = 'v2.74.0'
            elif fault == 'mode': changed['mode'] = 'beta'
            elif fault == 'lower-complete': changed['lower_catalogue']['complete'] = False
            elif fault == 'lower-digest': changed['lower_catalogue']['sha256'] = 'f' * 64
            elif fault == 'catalogue-complete': changed['official_releases']['complete'] = 1
            elif fault == 'page-type': changed['official_releases']['page_lengths'] = [True]
            elif fault == 'page-count': changed['official_releases']['page_lengths'] = [0]
            elif fault == 'page-full': changed['official_releases']['page_lengths'] = [100]
            elif fault == 'extra-release-field':
                changed['official_releases']['entries'][0]['receipt'] = 'success'
                changed['official_releases']['sha256'] = digest(changed['official_releases']['entries'])
            elif fault in ('earlier-tag', 'missing-tag'):
                rows = [tag('v2.72.99'), tag()] if fault == 'earlier-tag' else []
                changed['official_tags'].update(entries=rows, sha256=digest(rows), page_lengths=[len(rows)])
            elif fault == 'target-release-type': changed['target_release']['draft'] = 0
            elif fault == 'target-tag-extra': changed['target_tag']['trusted'] = True
            elif fault == 'source-commit': changed['source_commit'] = 'c' * 40
            elif fault == 'ref-extra': changed['target_ref']['trusted'] = True
            elif fault == 'commit-extra': changed['commit']['trusted'] = True
            elif fault == 'chain': changed['annotated_tags'] = [annotation()]
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                initial.validate_initial_release(changed, VERSION, 'stable', COMMIT)

    def test_pure_validation_can_bind_current_lower_catalogue_and_expected_commit(self):
        proof = resolve()
        changed_lower = lower([release('v2.74.0', 302, repository=initial.REPOSITORY)])
        with self.assertRaisesRegex(ValueError, 'Lower catalogue changed'):
            initial.validate_initial_release(proof, VERSION, 'stable', COMMIT, lower_catalogue=changed_lower)
        with self.assertRaisesRegex(ValueError, 'independently authenticated'):
            initial.validate_initial_release(proof, VERSION, 'stable', 'c' * 40)


if __name__ == '__main__':
    unittest.main()
