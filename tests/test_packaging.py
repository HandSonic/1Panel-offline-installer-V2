#!/usr/bin/env python3
"""Offline regression tests: missing mirrors, API drift, bad caches and partial builds."""

import hashlib
import http.client
import importlib.util
import io
import json
import os
import struct
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location("download_components", Path(__file__).resolve().parents[1] / "scripts" / "download_components.py")
packaging = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = packaging
SPEC.loader.exec_module(packaging)


def elf(arch="amd64"):
    machine, elf_class, endian, _ = packaging.ARCHES[arch]
    order = "<" if endian == 1 else ">"
    data = bytearray(256)
    data[:7] = b"\x7fELF" + bytes((elf_class, endian, 1))
    struct.pack_into(order + "H", data, 18, machine)
    if elf_class == 2:
        struct.pack_into(order + "Q", data, 32, 64)
        struct.pack_into(order + "HH", data, 54, 56, 1)
        struct.pack_into(order + "I", data, 64, 1)
        struct.pack_into(order + "Q", data, 64 + 32, len(data))
    else:
        struct.pack_into(order + "I", data, 28, 64)
        struct.pack_into(order + "HH", data, 42, 32, 1)
        struct.pack_into(order + "I", data, 64, 1)
        struct.pack_into(order + "I", data, 64 + 16, len(data))
    return bytes(data)


def archive(files):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as handle:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = 0o755
            handle.addfile(entry, io.BytesIO(content))
    return result.getvalue()


def app_archive(arch="amd64", root="changed-upstream-directory"):
    files = {f"{root}/install.sh": b"#!/bin/bash\necho upstream-format-can-change\n",
             f"{root}/1pctl": b"#!/bin/bash\nexit 0\n",
             f"{root}/1panel-core": elf(arch), f"{root}/1panel-agent": elf(arch)}
    for role in ("core", "agent"):
        files[f"{root}/initscript/1panel-{role}.service"] = b"[Service]\nExecStart=/usr/bin/1panel\n"
    return archive(files)


def docker_archive(arch="amd64"):
    return archive({f"docker/{name}": elf(arch) for name in ("docker", "dockerd", "containerd", "runc")})


class FakeNetwork:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        value = self.responses.get(request.full_url)
        if value is None:
            raise urllib.error.HTTPError(request.full_url, 404, "fixture missing", {}, None)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.network = FakeNetwork()
        self.downloader = packaging.Downloader(self.root / "cache", opener=self.network, sleeper=lambda _: None)

    def test_download_404_is_failure_without_partial_file(self):
        with self.assertRaises(packaging.BuildError):
            self.downloader.download(packaging.Candidate("v2.0.0", "https://example.test/missing"), "compose", "amd64")
        self.assertEqual(len(self.network.calls), 1)
        self.assertEqual(list((self.root / "cache").rglob(".download-*")), [])

    def test_html_and_wrong_architecture_never_count_as_binaries(self):
        for content in (b"<html>error</html>" * 1_000_000, elf("arm64"), elf()[:160]):
            path = self.root / "invalid"
            path.write_bytes(content)
            with self.assertRaises(packaging.BuildError):
                packaging.validate_file(path, "compose", "amd64")

    def test_interrupted_http_transfer_retries_then_recovers(self):
        url = "https://example.test/interrupted"
        attempts = []

        def interrupted_then_valid(request, timeout):
            attempts.append(request)
            if len(attempts) == 1:
                raise http.client.IncompleteRead(b"partial", 2048)
            return io.BytesIO(elf())

        downloader = packaging.Downloader(self.root / "cache", opener=interrupted_then_valid, sleeper=lambda _: None)
        artifact = downloader.download(packaging.Candidate("v2.1.0", url), "compose", "amd64")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(artifact.path.read_bytes(), elf())

    def test_correct_architecture_elf_all_ports(self):
        for arch in packaging.ARCHES:
            with self.subTest(arch=arch):
                data = elf(arch)
                packaging.validate_elf(io.BytesIO(data), len(data), arch)

    def test_bad_cached_file_is_replaced_and_source_cache_isolated(self):
        first = "https://example.test/repo-a/stable/compose"
        second = "https://example.test/repo-b/dev/compose"
        self.network.responses.update({first: elf(), second: elf()})
        original = self.downloader.download(packaging.Candidate("v2.1.0", first), "compose", "amd64")
        original.path.write_bytes(b"partial cached file")
        fixed = self.downloader.download(packaging.Candidate("v2.1.0", first), "compose", "amd64")
        other = self.downloader.download(packaging.Candidate("v2.1.0", second), "compose", "amd64")
        self.assertEqual(fixed.path.read_bytes(), elf())
        self.assertNotEqual(other.path, fixed.path)
        self.assertEqual(len(self.network.calls), 3)

    def test_publisher_hash_mismatch_does_not_promote_download(self):
        url = "https://example.test/compose"
        self.network.responses[url] = elf()
        with self.assertRaisesRegex(packaging.BuildError, "publisher digest"):
            self.downloader.download(packaging.Candidate("v2.1.0", url, "sha256:" + "0" * 64), "compose", "amd64")
        self.assertEqual(list((self.root / "cache").rglob("compose")), [])

    def test_missing_preferred_compose_discovers_alias_from_release_assets(self):
        api = "https://api.github.com/repos/loong64/compose/releases?per_page=100"
        url = "https://github.com/loong64/compose/releases/download/rebuilt/docker-compose-linux-loongarch64"
        self.network.responses[api] = json.dumps([{"tag_name": "v9.1.4", "assets": [
            {"name": "docker-compose-linux-loongarch64", "browser_download_url": url,
             "digest": "sha256:" + hashlib.sha256(elf("loong64")).hexdigest()}]}]).encode()
        self.network.responses[url] = elf("loong64")
        artifact = packaging.resolve_component(self.downloader, "compose", "loong64", "v9.9.9")
        self.assertEqual(artifact.version, "v9.1.4")
        self.assertEqual(artifact.url, url)
        self.assertTrue(artifact.metadata("v9.9.9")["fallback"])

    def test_docker_discovers_available_version_from_directory_index(self):
        directory = "https://download.docker.com/linux/static/stable/x86_64/"
        self.network.responses[directory] = b'<a href="docker-99.4.1.tgz">future asset</a>'
        self.network.responses[directory + "docker-99.4.1.tgz"] = docker_archive()
        artifact = packaging.resolve_component(self.downloader, "docker", "amd64", "99.9.9")
        self.assertEqual(artifact.version, "99.4.1")

    def test_api_outage_uses_previous_metadata(self):
        url = "https://api.github.com/repos/docker/compose/releases?per_page=100"
        self.network.responses[url] = b'[{"tag_name":"v9.0.0", "assets":[]}]'
        previous = self.downloader.metadata(url)
        disconnected = packaging.Downloader(self.root / "cache", opener=FakeNetwork(), sleeper=lambda _: None)
        self.assertEqual(disconnected.metadata(url), previous)

    def test_auth_only_sent_to_github_api(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "fixture-token"}):
            self.assertEqual(self.downloader._request("https://api.github.com/repos/a/b/releases").get_header("Authorization"), "Bearer fixture-token")
            self.assertIsNone(self.downloader._request("https://github.com/a/b/releases/download/v1/file").get_header("Authorization"))
            self.assertIsNone(self.downloader._request("https://example.test/file").get_header("Authorization"))

    def test_unsafe_and_incomplete_archives_are_rejected(self):
        for files in ({"../escape": b"unsafe"}, {"docker/docker": elf()}):
            path = self.root / "bad.tgz"
            path.write_bytes(archive(files))
            with self.assertRaises(packaging.BuildError):
                packaging.validate_file(path, "docker", "amd64")

    def test_truncated_gzip_footer_is_rejected(self):
        path = self.root / "truncated.tgz"
        path.write_bytes(docker_archive()[:-4])
        with self.assertRaises(packaging.BuildError):
            packaging.validate_file(path, "docker", "amd64")

    def test_custom_manifest_selects_built_asset_with_publisher_checksum(self):
        args = packaging.parse_args(["--app_version", "v2.2.5", "--source", "custom", "--arch", "amd64"])
        base = "https://github.com/HandSonic/1Panel-Build-v2/releases/download/v2.2.5"
        data = app_archive()
        self.network.responses[base + "/build-manifest.json"] = json.dumps({
            "schema": 1, "version": "v2.2.5", "packages": [
                {"arch": "amd64", "status": "built", "file": "renamed-compatible-package.tgz", "sha256": hashlib.sha256(data).hexdigest()}]}).encode()
        self.network.responses[base + "/renamed-compatible-package.tgz"] = data
        artifact = packaging.resolve_app(self.downloader, "custom", "amd64", args)
        self.assertEqual(artifact.url, base + "/renamed-compatible-package.tgz")

    def test_custom_manifest_skipped_arch_never_reuses_stale_release_asset(self):
        args = packaging.parse_args(["--app_version", "v2.2.5", "--source", "custom", "--arch", "amd64"])
        base = "https://github.com/HandSonic/1Panel-Build-v2/releases/download/v2.2.5"
        self.network.responses[base + "/build-manifest.json"] = json.dumps({
            "schema": 1, "version": "v2.2.5", "packages": [{"arch": "amd64", "status": "skipped", "reason": "retry pending"}]}).encode()
        self.network.responses[base + "/1panel-v2.2.5-linux-amd64.tar.gz"] = app_archive()
        with self.assertRaisesRegex(packaging.BuildError, "retry pending"):
            packaging.resolve_app(self.downloader, "custom", "amd64", args)
        self.assertEqual(len(self.network.calls), 1)

    def fixture_build(self, extra_args=()):
        args = packaging.parse_args(["--app_version", "v2.2.5", "--arch", "amd64", "--docker_version", "29.0.0", "--compose_version", "v2.40.0", *extra_args])
        (self.root / "scripts").mkdir(exist_ok=True)
        for name in ("scripts/install-offline.sh", "scripts/offline-env.sh", "upgrade_offline.sh"):
            (self.root / name).write_text("#!/bin/bash\nexit 0\n")
        (self.root / "docker.service").write_text("[Service]\nExecStart=/usr/bin/dockerd\n")
        self.network.responses.update({
            "https://resource.fit2cloud.com/1panel/package/v2/stable/v2.2.5/release/1panel-v2.2.5-linux-amd64.tar.gz": app_archive(),
            "https://download.docker.com/linux/static/stable/x86_64/docker-29.0.0.tgz": docker_archive(),
            "https://github.com/docker/compose/releases/download/v2.40.0/docker-compose-linux-x86_64": elf(),
        })
        return args

    def test_partial_build_is_isolated_manifest_records_retry_and_flat_checksums(self):
        args = self.fixture_build(("--allow-missing",))
        old_custom = self.root / "build/v2.2.5/custom/1panel-v2.2.5-custom-offline-linux-amd64.tar.gz"
        old_custom.parent.mkdir(parents=True)
        old_custom.write_bytes(b"stale")
        with mock.patch.dict(os.environ, {"BUILD_FINGERPRINT": "fixture-fingerprint"}):
            self.assertEqual(packaging.build(args, self.root, self.downloader), 0)
        output = self.root / "build/v2.2.5"
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual((manifest["built_count"], manifest["skipped_count"], manifest["complete"]), (1, 1, False))
        self.assertEqual(manifest["packages"][1]["source"], "custom")
        self.assertFalse(old_custom.exists())
        built = manifest["packages"][0]
        self.assertEqual((output / "checksums.txt").read_text(), f"{built['sha256']}  {built['asset']}\n")
        self.assertEqual(built["components"]["docker"]["version"], "29.0.0")
        with tarfile.open(output / built["path"]) as package:
            prefix = built["asset"].removesuffix(".tar.gz")
            upstream = package.extractfile(prefix + "/install-upstream.sh").read()
            self.assertEqual(upstream, b"#!/bin/bash\necho upstream-format-can-change\n")
            package_manifest = json.load(package.extractfile(prefix + "/manifest.json"))
            self.assertEqual(package_manifest["fingerprint"], "fixture-fingerprint")
            self.assertEqual(package_manifest["arch"], "amd64")
            self.assertIn(prefix + "/offline-env.sh", package.getnames())

    def test_missing_compose_skips_whole_pair_and_never_emits_incomplete_package(self):
        args = self.fixture_build(("--source", "official", "--allow-missing"))
        del self.network.responses["https://github.com/docker/compose/releases/download/v2.40.0/docker-compose-linux-x86_64"]
        self.assertEqual(packaging.build(args, self.root, self.downloader), 0)
        output = self.root / "build/v2.2.5"
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(manifest["built_count"], 0)
        self.assertFalse(manifest["complete"])
        self.assertEqual((output / "checksums.txt").read_text(), "")
        self.assertEqual(list(output.rglob("*.tar.gz")), [])

    def test_strict_cli_reports_partial_failure_after_recording_all_pairs(self):
        args = self.fixture_build()
        self.assertEqual(packaging.build(args, self.root, self.downloader), 1)
        manifest = json.loads((self.root / "build/v2.2.5/manifest.json").read_text())
        self.assertEqual(len(manifest["packages"]), 2)


if __name__ == "__main__":
    unittest.main()
