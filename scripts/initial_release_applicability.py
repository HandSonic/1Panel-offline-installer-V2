#!/usr/bin/env python3
"""Prove when a stable v2 target has no possible canonical predecessor.

Only ``resolve_initial_release`` performs transport. Its authenticated GitHub
client must fail on unsuccessful API requests; errors are never absence. The
pure validator checks the retained inventories and identity bindings, but JSON
and digests alone cannot authenticate their origin. Callers must bind this proof
to trusted same-run inputs and independently require fresh native installation.
This is neither native acceptance nor permission to publish a package.

The initial prerelease channels remain unsupported: first beta/dev membership
does not justify compatibility with stable or another prerelease channel.
"""
import json
import re
from datetime import datetime
from urllib.parse import quote

from public_predecessor import REPOSITORY, channel, release_identity, semver
from resolved_inventory import (commit_value, digest, exact_keys,
                                hash_value, require)

OFFICIAL = '1Panel-dev/1Panel'
API_ROOT = 'https://api.github.com/repos/' + OFFICIAL
PER_PAGE = 100
MAX_PAGES = 100
MAX_TAG_DEPTH = 8
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
KIND = 'initial-official-release-upgrade-applicability'


def _version(version, mode):
    key = semver(version, mode)
    require(mode == 'stable', 'Initial prerelease applicability is not established')
    return key


def _name(value):
    require(isinstance(value, str) and 0 < len(value) <= 512 and
            not any(ord(char) < 32 or ord(char) == 127 for char in value),
            'Malformed catalogue tag name')
    return value


def _release(value, repository, *, published=False):
    result = release_identity(value, repository)
    _name(result['tag_name'])
    mode = channel(result['tag_name'])
    if mode is not None:
        require(result['prerelease'] == (mode != 'stable'),
                'Canonical release prerelease/channel mismatch')
    if published:
        stamp = value.get('published_at')
        require(stamp is None or isinstance(stamp, str), 'Malformed release publication time')
        if stamp is not None:
            require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', stamp),
                    'Malformed release publication time')
            datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')
        require(result['draft'] or stamp is not None, 'Release is not published')
        result['published_at'] = stamp
    return result


def _releases(rows, repository, *, published=False):
    require(isinstance(rows, list), 'Release catalogue must be a list')
    normalized = [_release(row, repository, published=published) for row in rows]
    require(len({row['id'] for row in normalized}) == len(normalized) and
            len({row['tag_name'] for row in normalized}) == len(normalized),
            'Ambiguous duplicate release ID or tag')
    return sorted(normalized, key=lambda row: row['id'])


def _object(value):
    require(isinstance(value, dict) and value.get('type') in ('commit', 'tag'),
            'Tag must resolve to a commit or annotated tag')
    sha = commit_value(value.get('sha'))
    kind = value['type']
    require(value.get('url') == API_ROOT + '/git/' + kind + 's/' + sha,
            'Git object repository/identity mismatch')
    return {'type': kind, 'sha': sha, 'url': value['url']}


def _tag(value):
    require(isinstance(value, dict), 'Malformed tag catalogue entry')
    name = _name(value.get('name'))
    commit = value.get('commit')
    require(isinstance(commit, dict), 'Missing tag commit')
    sha = commit_value(commit.get('sha'))
    require(commit.get('url') == API_ROOT + '/commits/' + sha,
            'Tag commit repository/identity mismatch')
    return {'name': name, 'commit': {'sha': sha, 'url': commit['url']}}


def _tags(rows):
    require(isinstance(rows, list), 'Tag catalogue must be a list')
    normalized = [_tag(row) for row in rows]
    require(len({row['name'] for row in normalized}) == len(normalized),
            'Ambiguous duplicate tag')
    return sorted(normalized, key=lambda row: row['name'])


def lower_catalogue_snapshot(releases, *, catalogue_complete=False):
    """Normalize the lower catalogue only after every authenticated page succeeds.

    Completeness is an adapter assertion, never a replacement for transport or
    an excuse to interpret a failed catalogue/predecessor read as empty.
    """
    require(catalogue_complete is True, 'Complete lower release catalogue required')
    rows = _releases(releases, REPOSITORY)
    return {'repository': REPOSITORY, 'complete': True, 'releases': rows,
            'sha256': digest(rows)}


def _lower(value, version, mode):
    exact_keys(value, ('repository', 'complete', 'releases', 'sha256'), 'lower catalogue')
    require(value['repository'] == REPOSITORY, 'Wrong lower catalogue repository')
    normalized = lower_catalogue_snapshot(value['releases'],
                                          catalogue_complete=value['complete'])
    require(value == normalized, 'Lower catalogue normalization/digest mismatch')
    target_key = semver(version, mode)
    for row in normalized['releases']:
        if channel(row['tag_name']) == mode:
            require(semver(row['tag_name'], mode) >= target_key,
                    'Earlier canonical lower release exists; no initial-release fallback')
    return normalized


def _decode(raw):
    require(isinstance(raw, (str, bytes)) and 0 < len(raw) <= MAX_RESPONSE_BYTES,
            'Invalid or oversized GitHub response')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate GitHub JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          ValueError('Nonfinite GitHub JSON value')))


def _api(client, endpoint):
    return _decode(client.run('api', endpoint))


def _inventory(client, collection):
    endpoint = f'repos/{OFFICIAL}/{collection}'
    rows, lengths = [], []
    for page in range(1, MAX_PAGES + 1):
        values = _api(client, endpoint + f'?per_page={PER_PAGE}&page={page}')
        require(isinstance(values, list) and len(values) <= PER_PAGE and
                all(isinstance(row, dict) for row in values),
                'Malformed or failed official GitHub pagination')
        rows.extend(values)
        lengths.append(len(values))
        if len(values) < PER_PAGE:
            normalized = (_releases(rows, OFFICIAL, published=True) if collection == 'releases'
                          else _tags(rows))
            return {'repository': OFFICIAL, 'endpoint': endpoint, 'complete': True,
                    'page_lengths': lengths, 'entries': normalized, 'sha256': digest(normalized)}
    raise ValueError('Official GitHub catalogue exceeds bounded pagination')


def _validate_inventory(value, collection):
    exact_keys(value, ('repository', 'endpoint', 'complete', 'page_lengths', 'entries', 'sha256'),
               'official catalogue')
    require(value['repository'] == OFFICIAL and value['endpoint'] == f'repos/{OFFICIAL}/{collection}' and
            value['complete'] is True, 'Complete trusted official catalogue required')
    lengths = value['page_lengths']
    require(isinstance(lengths, list) and 0 < len(lengths) <= MAX_PAGES and
            all(type(size) is int and 0 <= size <= PER_PAGE for size in lengths) and
            all(size == PER_PAGE for size in lengths[:-1]) and lengths[-1] < PER_PAGE,
            'Incomplete official catalogue pagination')
    rows = (_releases(value['entries'], OFFICIAL, published=True) if collection == 'releases'
            else _tags(value['entries']))
    require(sum(lengths) == len(rows) and rows == value['entries'] and
            digest(rows) == hash_value(value['sha256']),
            'Official catalogue normalization/count/digest mismatch')
    return rows


def _ref(value, version):
    require(isinstance(value, dict) and value.get('ref') == 'refs/tags/' + version and
            value.get('url') == API_ROOT + '/git/refs/tags/' + quote(version, safe=''),
            'Target tag ref repository/identity mismatch')
    return {'ref': value['ref'], 'url': value['url'], 'object': _object(value.get('object'))}


def _annotation(value, sha):
    require(isinstance(value, dict) and value.get('sha') == sha and
            value.get('url') == API_ROOT + '/git/tags/' + sha,
            'Annotated tag repository/identity mismatch')
    return {'sha': sha, 'url': value['url'], 'tag': _name(value.get('tag')),
            'object': _object(value.get('object'))}


def _commit(value, sha):
    require(isinstance(value, dict) and value.get('sha') == sha and
            value.get('url') == API_ROOT + '/git/commits/' + sha,
            'Target commit repository/identity mismatch')
    commit_value(sha)
    return {'sha': sha, 'url': value['url']}


def validate_initial_release(proof, version, mode, expected_source_commit=None, *, lower_catalogue=None):
    """Strict pure validation; caller must authenticate the proof's producer.

    If a custom/source contract is available, ``expected_source_commit`` must be
    its independently authenticated source commit. A stored proof cannot supply
    its own expected identity. No absence of runtime evidence is considered.
    """
    target_key = _version(version, mode)
    exact_keys(proof, ('schema', 'kind', 'repository', 'version', 'mode', 'source_commit',
                      'lower_catalogue', 'official_releases', 'official_tags', 'target_release',
                      'target_tag', 'target_ref', 'annotated_tags', 'commit'), 'initial-release proof')
    require(type(proof['schema']) is int and proof['schema'] == 1 and proof['kind'] == KIND and
            proof['repository'] == OFFICIAL and proof['version'] == version and proof['mode'] == mode,
            'Initial-release proof identity mismatch')
    lower = _lower(proof['lower_catalogue'], version, mode)
    if lower_catalogue is not None:
        require(lower == _lower(lower_catalogue, version, mode), 'Lower catalogue changed')
    releases = _validate_inventory(proof['official_releases'], 'releases')
    tags = _validate_inventory(proof['official_tags'], 'tags')
    for rows, field in ((releases, 'tag_name'), (tags, 'name')):
        for row in rows:
            if channel(row[field]) == mode:
                require(semver(row[field], mode) >= target_key,
                        'Earlier canonical official release or tag exists')
    matched_releases = [row for row in releases if row['tag_name'] == version]
    matched_tags = [row for row in tags if row['name'] == version]
    require(len(matched_releases) == len(matched_tags) == 1,
            'Exact target must exist in both official release and tag catalogues')
    release, tag = matched_releases[0], matched_tags[0]
    require(_release(proof['target_release'], OFFICIAL, published=True) == release and
            proof['target_release'] == release and not release['draft'] and
            release['published_at'] is not None and not release['prerelease'],
            'Target official release must be published stable and non-draft')
    require(_tag(proof['target_tag']) == tag and proof['target_tag'] == tag,
            'Target tag catalogue identity mismatch')
    ref = _ref(proof['target_ref'], version)
    require(ref == proof['target_ref'], 'Unexpected target tag ref fields')
    annotations = proof['annotated_tags']
    require(isinstance(annotations, list) and len(annotations) <= MAX_TAG_DEPTH,
            'Invalid or excessive annotated tag depth')
    current, visited = ref['object'], set()
    for index, annotation in enumerate(annotations):
        require(current['type'] == 'tag' and current['sha'] not in visited,
                'Unexpected or cyclic annotated tag chain')
        visited.add(current['sha'])
        normalized = _annotation(annotation, current['sha'])
        require(annotation == normalized and (index != 0 or annotation['tag'] == version),
                'Annotated target tag name/fields mismatch')
        current = normalized['object']
    require(current['type'] == 'commit', 'Incomplete annotated tag chain')
    commit = _commit(proof['commit'], current['sha'])
    require(commit == proof['commit'] and commit['sha'] == commit_value(proof['source_commit']) and
            commit['sha'] == tag['commit']['sha'], 'Target immutable tag commit mismatch')
    if expected_source_commit is not None:
        require(commit['sha'] == commit_value(expected_source_commit),
                'Target tag differs from independently authenticated source commit')
    return proof


def resolve_initial_release(client, version, mode, lower_catalogue, expected_source_commit=None):
    """Fetch complete official inventories and bind the exact first stable tag.

    This may be called only for a successfully read complete lower catalogue.
    It does not catch predecessor failures or substitute for native evidence.
    Explicit official endpoints avoid inheriting the client's lower repository.
    """
    _version(version, mode)
    lower = _lower(lower_catalogue, version, mode)
    if expected_source_commit is not None:
        commit_value(expected_source_commit)
    releases = _inventory(client, 'releases')
    tags = _inventory(client, 'tags')
    matched_releases = [row for row in releases['entries'] if row['tag_name'] == version]
    matched_tags = [row for row in tags['entries'] if row['name'] == version]
    require(len(matched_releases) == len(matched_tags) == 1,
            'Exact target must exist in both official release and tag catalogues')
    release, tag = matched_releases[0], matched_tags[0]
    ref_endpoint = f'repos/{OFFICIAL}/git/ref/tags/' + quote(version, safe='')
    ref = _ref(_api(client, ref_endpoint), version)
    current, annotations, visited = ref['object'], [], set()
    while current['type'] == 'tag':
        require(len(annotations) < MAX_TAG_DEPTH and current['sha'] not in visited,
                'Cyclic or excessive annotated tag chain')
        visited.add(current['sha'])
        annotation = _annotation(_api(client, f'repos/{OFFICIAL}/git/tags/' + current['sha']),
                                 current['sha'])
        annotations.append(annotation)
        current = annotation['object']
    commit = _commit(_api(client, f'repos/{OFFICIAL}/git/commits/' + current['sha']), current['sha'])
    proof = {'schema': 1, 'kind': KIND, 'repository': OFFICIAL, 'version': version, 'mode': mode,
             'source_commit': commit['sha'], 'lower_catalogue': lower,
             'official_releases': releases, 'official_tags': tags,
             'target_release': release, 'target_tag': tag, 'target_ref': ref,
             'annotated_tags': annotations, 'commit': commit}
    validate_initial_release(proof, version, mode, expected_source_commit,
                             lower_catalogue=lower_catalogue)
    fresh_release = _release(_api(client, f'repos/{OFFICIAL}/releases/' + str(release['id'])),
                             OFFICIAL, published=True)
    require(fresh_release == release and _ref(_api(client, ref_endpoint), version) == ref,
            'Target official release or tag changed during resolution')
    # Repeating both full inventories detects pagination shifts and concurrent
    # additions/deletions, rather than relying on a mutable latest pointer.
    require(_inventory(client, 'releases') == releases and _inventory(client, 'tags') == tags,
            'Official catalogues changed during resolution')
    return proof
