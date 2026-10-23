"""Real installer recipe inputs; only source HTTP transport is synthetic."""
from functools import lru_cache
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = (ROOT / 'tests/fixtures/historical-installers/6b5ea4cb6f443a1626c2fc0fe2dc37c6d31a746d6f9f2745d14a19fdd7d31eb1.sh').read_bytes()


def facts(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


@lru_cache(maxsize=None)
def source_archive(version, arch):
    stream = io.BytesIO()
    with gzip.GzipFile(fileobj=stream, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            member = tarfile.TarInfo(f'1panel-{version}-linux-{arch}/install.sh')
            member.size = len(ORIGINAL); member.mode = 0o755
            archive.addfile(member, io.BytesIO(ORIGINAL))
    return stream.getvalue()


def source_contract():
    return {'installer': {'repository': '1Panel-dev/installer', 'commit': 'd' * 40,
                          'resources': {'install.sh': facts(ORIGINAL)}}}


def download_source(pin, path):
    arch = pin['url'].rsplit('/', 1)[-1]
    Path(path).write_bytes(source_archive(pin['version'], arch))


def mock_sources(test):
    download = patch('native_upgrade_input.download_url', side_effect=download_source)
    read = patch('resolved_transport.canonical_read', return_value={'status': 200, 'body': ORIGINAL})
    test.addCleanup(download.stop); test.addCleanup(read.stop)
    return download.start(), read.start()


def current_installer():
    from patch_installer import render
    return render(ORIGINAL.decode()).encode()
