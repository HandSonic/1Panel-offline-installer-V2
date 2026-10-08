import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from import_ci_artifact import import_artifact
from validate_upstream import verify_directory
import test_upstream_manifest as fixtures

class CIArtifactImportTests(unittest.TestCase):
    def fixture(self,root):
        source=root/'download';source.mkdir();name='1panel-v2.3.2-linux-amd64.tar.gz';(source/name).write_bytes(b'verified artifact bytes')
        (source/(name+'.sha256')).write_text(hashlib.sha256((source/name).read_bytes()).hexdigest()+'  '+name+'\n')
        return source,name,root/'cache/custom-package.tar.gz'
    def test_exact_ci_artifact_provenance(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source,name,dest=self.fixture(root);url='https://github.com/HandSonic/1Panel-Build-v2/actions/runs/37800320709'
            import_artifact(source,dest,name,url,'HandSonic/1Panel-Build-v2','a'*40)
            self.assertEqual(dest.read_bytes(),(source/name).read_bytes())
            facts=json.loads(Path(str(dest)+'.source.json').read_text());origin=json.loads(Path(str(dest)+'.origin.json').read_text())
            self.assertEqual(facts['url'],url);self.assertEqual(origin['expected_build_repository_commit'],'a'*40)
            self.assertNotIn(str(source),json.dumps(facts)+json.dumps(origin))
    def test_credentials_in_source_url_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source,name,dest=self.fixture(root)
            with self.assertRaises(ValueError):import_artifact(source,dest,name,'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/123?token=secret','HandSonic/1Panel-Build-v2','a'*40)
            self.assertFalse(dest.exists())
    def test_changed_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);source,name,dest=self.fixture(root);(source/name).write_bytes(b'changed')
            with self.assertRaises(ValueError):import_artifact(source,dest,name,'https://github.com/HandSonic/1Panel-Build-v2/actions/runs/123','HandSonic/1Panel-Build-v2','a'*40)
    def test_wrong_expected_build_commit_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);fixtures.UpstreamManifestTests().fixture(root)
            with self.assertRaises(ValueError):verify_directory(root,'amd64','v2.3.2','a'*40)

if __name__=='__main__':unittest.main()
