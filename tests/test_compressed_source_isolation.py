"""Malformed published bytes retain the existing source/architecture isolation."""
import hashlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from resolved_inventory import ARCHES
from test_package_public_source import Publisher


def truncate_published_archive(publisher, arch):
    name = f'1panel-{publisher.tag}-linux-{arch}.tar.gz'
    publisher.bodies[name] = publisher.bodies[name][:-8]
    # The published bytes themselves are corrupt; transport pins still match.
    # A network truncation instead fails the existing size/SHA validation.
    publisher.bodies[name + '.sha256'] = (
        hashlib.sha256(publisher.bodies[name]).hexdigest() + '  ' + name + '\n'
    ).encode()
    publisher.refresh()


class CompressedSourceIsolationTests(unittest.TestCase):
    def test_truncated_first_archive_uses_healthy_sibling(self):
        publisher = Publisher()
        truncate_published_archive(publisher, ARCHES[0])
        runtime = publisher.runtime()
        self.assertIsNotNone(runtime['source_contract'])
        self.assertEqual(publisher.downloaded[:2], [
            f'1panel-{publisher.tag}-linux-{arch}.tar.gz' for arch in ARCHES[:2]
        ])
        self.assertTrue(any(row['source'] == 'official' for row in runtime['inventory']['rows']))

    def test_all_truncated_custom_archives_preserve_vendor_plan(self):
        publisher = Publisher()
        for arch in ARCHES:
            truncate_published_archive(publisher, arch)
        runtime = publisher.runtime()
        self.assertIsNone(runtime['source_contract'])
        self.assertIn('Invalid compressed custom archive', runtime['custom_failure'])
        self.assertEqual(len(publisher.downloaded), len(ARCHES))
        self.assertEqual(set(runtime['inventory']['official']['archives']), set(ARCHES) - {'loong64'})
        self.assertEqual(set(runtime['inventory']['enterprise']['archives']), {'amd64', 'arm64'})
        self.assertEqual({row['source'] for row in runtime['inventory']['rows']},
                         {'custom', 'official', 'enterprise-docker', 'enterprise-original'})


if __name__ == '__main__':
    unittest.main()
