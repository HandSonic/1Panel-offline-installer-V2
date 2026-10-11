"""Candidate source acquisition uses synthetic bytes and mocked network only."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import native_upgrade_input as upgrade
from resolved_inventory import ARCHES, canonical, digest
from test_validate_resolved_custom import Fixture as RawFixture
from test_runtime_contract import runtime


class SourceFixture:
    def __init__(self, directory, ci=True):
        self.directory = directory
        self.runtime = runtime()
        self.version = self.runtime['version']
        self.producer = self.runtime['upstream']['producer_commit']
        self.contract = self.runtime['source_contract']
        self.raws = directory/'source-fixtures'; self.raws.mkdir()
        records = self.runtime['upstream']['records']
        for arch in ARCHES:
            fixture = RawFixture(arch)
            assert fixture.contract == self.contract
            name = records[arch]['file']
            pin = fixture.archive(self.raws/name)
            records[arch].update(size=pin['bytes'], sha256=pin['sha256'])
            (self.raws/(name+'.sha256')).write_text(pin['sha256']+'  '+name+'\n')
        self.provenance = {'repository': upgrade.ControlGitHub('HandSonic/1Panel-Build-v2',self.version).repo,
            'run_id': 555, 'artifact_id': 666, 'artifact_sha256': '9'*64,
            'build_repository_commit': self.producer,
            'run_url': 'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/555'}
        if ci:
            self.runtime['upstream'].update(source_kind='verified-ci-artifact', validation_run_id=555,
                validation_sha256='9'*64, validation_commit=self.producer)
        self.plan = {'version': self.version, 'mode': 'stable', 'resolved': self.runtime,
            'upstream_input': self.provenance if ci else {'source_kind':'verified-public-release'}}
        (self.raws/'resolved-source.json').write_bytes(canonical(self.contract))
        (self.raws/'build-manifest.json').write_bytes(canonical({'schema_version':1,'artifacts':list(records.values())}))
        (self.raws/'build-inputs.env').write_text('SYNTHETIC=unit-test-only\n')
        (self.raws/'checksums.txt').write_text(''.join(r['sha256']+'  '+r['file']+'\n' for r in records.values()))
        self.configs = {part:text.encode() for part,text in self.runtime['configuration_sources'].items()}
        self.work = directory/'work'; self.work.mkdir()
        self.calls = []

    def fetch(self, directory, version, run_id, artifact_id, sha, commit, contract):
        self.calls.append((version,run_id,artifact_id,sha,commit,contract))
        shutil.copytree(self.raws,directory)
        return dict(self.provenance)

    def read(self,url):
        for part,profile in self.contract['configuration'].items():
            expected='https://raw.githubusercontent.com/1Panel-dev/1Panel/'+self.contract['source']['commit']+'/'+profile['path']
            if url==expected:return {'status':200,'body':self.configs[part]}
        raise AssertionError('Unexpected unpinned configuration request')

    def execute_ci(self):
        with patch('manual_publication.fetch_ci_bundle',side_effect=self.fetch), \
                patch.object(upgrade,'canonical_read',side_effect=self.read), \
                patch.object(upgrade,'public_controls',side_effect=AssertionError('No current contract discovery')), \
                patch.object(upgrade,'ControlGitHub',side_effect=AssertionError('No current release discovery')):
            return upgrade.source_archive_from_candidate(self.plan,self.runtime,'custom','amd64',self.work)


class CandidateSourceAcquisitionTests(unittest.TestCase):
    def test_selected_raw_producer_is_separate_from_ci_validation_commit(self):
        # The schema3 consumer is integrated separately. Its transport result may
        # have no global producer; the actual archive still has one exact row pin.
        for wrong_row in (False, True):
            with self.subTest(wrong_row=wrong_row),tempfile.TemporaryDirectory() as td:
                fixture=SourceFixture(Path(td))
                fixture.runtime['upstream'].update(producer_commit=None,validation_commit='7'*40)
                fixture.provenance['build_repository_commit']='7'*40
                if wrong_row:fixture.runtime['upstream']['records']['amd64']['build_repository_commit']='6'*40
                with patch('resolved_transport.ci_controls',return_value=(fixture.contract,
                        fixture.runtime['source_contract_sha256'],fixture.runtime['upstream'])):
                    if wrong_row:
                        with self.assertRaisesRegex(ValueError,'manifest'):
                            fixture.execute_ci()
                    else:
                        body,_=fixture.execute_ci()
                        self.assertIn('1panel-core',body)
                self.assertEqual(fixture.calls[0][4],'7'*40)

    def test_exact_ci_only_source_passes_real_raw_archive_and_contract_validation(self):
        with tempfile.TemporaryDirectory() as td:
            fixture=SourceFixture(Path(td))
            body,proof=fixture.execute_ci()
            self.assertIn('1panel-core',body)
            self.assertEqual(proof['upstream'],fixture.runtime['upstream'])
            self.assertEqual(proof['acquisition']['provenance'],fixture.provenance)
            self.assertEqual(fixture.calls,[(fixture.version,555,666,'9'*64,fixture.producer,'downstream17')])

    def test_ci_wrong_transport_contract_record_bytes_or_configuration_never_falls_back(self):
        for fault in ('run','artifact','commit','contract','record','raw','config'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as td:
                fixture=SourceFixture(Path(td))
                if fault=='run':fixture.provenance['run_id']+=1
                if fault=='artifact':fixture.provenance['artifact_sha256']='3'*64
                if fault=='commit':fixture.provenance['build_repository_commit']='4'*40
                if fault=='contract':(fixture.raws/'resolved-source.json').write_bytes(b'{}')
                if fault=='record':
                    file=fixture.raws/'build-manifest.json';value=json.loads(file.read_text());value['artifacts'][0]['size']+=1;file.write_bytes(canonical(value))
                if fault=='raw':
                    path=fixture.raws/fixture.runtime['upstream']['records']['amd64']['file'];path.write_bytes(path.read_bytes()+b'changed')
                if fault=='config':fixture.configs['core']=b'wrong configuration'
                with self.assertRaises((ValueError,KeyError)):fixture.execute_ci()

    def test_official_uses_only_candidate_vendor_pin_without_custom_or_discovery(self):
        with tempfile.TemporaryDirectory() as td:
            fixture=SourceFixture(Path(td));row=fixture.runtime['upstream']['records']['amd64']
            raw=fixture.raws/row['file']
            pin=dict(fixture.runtime['inventory']['official']['archives']['amd64'],sha256=row['sha256'],bytes=row['size'])
            fixture.runtime['inventory']['official']['archives']['amd64']=pin
            def download(actual,destination):
                self.assertEqual(actual,pin);destination.write_bytes(raw.read_bytes())
            with patch.object(upgrade,'download_url',side_effect=download), \
                    patch.object(upgrade,'discover_vendor',side_effect=AssertionError('No rediscovery')), \
                    patch('manual_publication.fetch_ci_bundle',side_effect=AssertionError('Official must not read custom CI')):
                _,proof=upgrade.source_archive_from_candidate(fixture.plan,fixture.runtime,'official','amd64',fixture.work)
            self.assertEqual(proof['pin'],pin)
            self.assertEqual(proof['kind'],'canonical-vendor')

    def test_public_custom_downloads_exact_asset_id_only_when_candidate_pin_matches(self):
        for fault in ('none','row-producer','old-public-bytes','changed-after-download'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as td:
                fixture=SourceFixture(Path(td),ci=False)
                if fault=='row-producer':fixture.runtime['upstream']['producer_commit']=None
                repo='HandSonic/1Panel-Build-v2';row=fixture.runtime['upstream']['records']['amd64']
                asset={'id':777,'name':row['file'],'state':'uploaded','size':row['size'],'digest':'sha256:'+row['sha256'],
                       'url':f'https://api.github.com/repos/{repo}/releases/assets/777'}
                release={'id':888,'tag_name':fixture.version,'draft':False,'prerelease':False,'assets':[asset]}
                original=copy.deepcopy(release)
                if fault=='old-public-bytes':asset['digest']='sha256:'+'1'*64
                after=copy.deepcopy(release)
                if fault=='changed-after-download':after['assets'][0]['id']+=1
                downloaded=[]
                def download(selected,path):
                    downloaded.append(selected['id']);path.write_bytes((fixture.raws/row['file']).read_bytes())
                with patch.object(upgrade,'ControlGitHub',return_value=SimpleNamespace(repo=repo,release=lambda:release)), \
                        patch.object(upgrade,'array_pages',return_value=release['assets']), \
                        patch.object(upgrade,'PredecessorGitHub',return_value=SimpleNamespace(repo=repo,download_asset=download)), \
                        patch.object(upgrade,'public_release',return_value=after), \
                        patch.object(upgrade,'canonical_read',side_effect=fixture.read), \
                        patch.object(upgrade,'public_controls',side_effect=AssertionError('No current contract discovery')), \
                        patch('manual_publication.fetch_ci_bundle',side_effect=AssertionError('Public source must not use CI')):
                    if fault in ('none','row-producer'):
                        _,proof=upgrade.source_archive_from_candidate(fixture.plan,fixture.runtime,'custom','amd64',fixture.work)
                        self.assertEqual(proof['acquisition']['asset']['asset_id'],777)
                    else:
                        with self.assertRaises(ValueError):upgrade.source_archive_from_candidate(fixture.plan,fixture.runtime,'custom','amd64',fixture.work)
                self.assertEqual(downloaded,[] if fault=='old-public-bytes' else [777])


if __name__=='__main__':unittest.main()
