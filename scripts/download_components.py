#!/usr/bin/env python3
"""Discover, validate and assemble offline packages without executing downloaded code.

An explicitly requested component version is a preference: try its mirrors first,
then releases that actually contain a compatible asset, cached releases, and finally
known older versions. One unavailable source/architecture cannot poison other pairs.
Only the app version is exact; silently downgrading 1Panel would be unsafe.
"""

import argparse
import gzip
import hashlib
import http.client
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


BASE_DIR = Path(__file__).resolve().parent.parent
ARCHES = {
    "amd64": (62, 2, 1, ("x86_64", "amd64")),
    "arm64": (183, 2, 1, ("aarch64", "arm64")),
    "armv7": (40, 1, 1, ("armv7", "armhf", "armv7l")),
    "ppc64le": (21, 2, 1, ("ppc64le",)),
    "s390x": (22, 2, 2, ("s390x",)),
    "loong64": (258, 2, 1, ("loong64", "loongarch64")),
    "riscv64": (243, 2, 1, ("riscv64",)),
}
DOCKER_REPOS = {
    "ppc64le": ("ppc64le-cloud/docker-ce-binaries-ppc64le",
                "wojiushixiaobai/docker-ce-binaries-ppc64le",
                "jumpserver-dev/docker-ce-binaries-ppc64le"),
    "s390x": ("obsd90/docker-ce-binaries-s390x", "wojiushixiaobai/docker-ce-binaries-s390x",
              "jumpserver-dev/docker-ce-binaries-s390x"),
    "loong64": ("loong64/docker-ce-packaging", "loongson-community/docker-ce-binaries-loongarch64",
                "wojiushixiaobai/docker-ce-binaries-loong64"),
    "riscv64": ("wojiushixiaobai/docker-ce-binaries-riscv64", "riscv-collab/docker-ce-binaries-riscv64",
                "jumpserver-dev/docker-ce-binaries-riscv64"),
}
LEGACY_DOCKER = ("27.5.1", "27.0.3", "26.1.4", "25.0.5", "24.0.9", "24.0.7", "23.0.6", "20.10.24", "20.10.7")
VERSION_RE = re.compile(r"v?\d+\.\d+\.\d+[A-Za-z0-9._+-]*\Z")


class BuildError(Exception):
    pass


def log(message):
    print(message, file=sys.stderr, flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".metadata-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version)[:4])


def normalized_version(version):
    version = version.removeprefix("docker-").removeprefix("v")
    return version


@dataclass(frozen=True)
class Candidate:
    version: str
    url: str
    digest: str = ""


@dataclass
class Artifact:
    path: Path
    version: str
    url: str
    digest: str

    def metadata(self, requested=None):
        result = {"version": self.version, "url": self.url, "sha256": self.digest}
        if requested is not None:
            result["requested"] = requested
            result["fallback"] = requested != "latest" and normalized_version(requested) != normalized_version(self.version)
        return result


def validate_elf(stream, size, arch):
    """Check ELF architecture and load segments; no host/foreign binary execution."""
    stream.seek(0)
    header = stream.read(64)
    machine, elf_class, endian, _ = ARCHES[arch]
    if len(header) < 52 or header[:4] != b"\x7fELF" or header[4:7] != bytes((elf_class, endian, 1)):
        raise BuildError(f"not a {arch} ELF executable")
    order = "<" if endian == 1 else ">"
    if struct.unpack_from(order + "H", header, 18)[0] != machine:
        raise BuildError(f"ELF machine does not match {arch}")
    if elf_class == 2:
        if len(header) < 64:
            raise BuildError("truncated ELF header")
        offset = struct.unpack_from(order + "Q", header, 32)[0]
        stride, count = struct.unpack_from(order + "HH", header, 54)
        minimum = 56
    else:
        offset = struct.unpack_from(order + "I", header, 28)[0]
        stride, count = struct.unpack_from(order + "HH", header, 42)
        minimum = 32
    if not count or count > 4096 or stride < minimum or offset + stride * count > size:
        raise BuildError("truncated or invalid ELF program headers")
    loaded = False
    for number in range(count):
        stream.seek(offset + number * stride)
        program = stream.read(minimum)
        if len(program) != minimum:
            raise BuildError("truncated ELF program header")
        if struct.unpack_from(order + "I", program)[0] != 1:
            continue
        loaded = True
        if elf_class == 2:
            start = struct.unpack_from(order + "Q", program, 8)[0]
            length = struct.unpack_from(order + "Q", program, 32)[0]
        else:
            start = struct.unpack_from(order + "I", program, 4)[0]
            length = struct.unpack_from(order + "I", program, 16)[0]
        if start + length > size:
            raise BuildError("truncated ELF load segment")
    if not loaded:
        raise BuildError("ELF has no executable load segments")


def safe_members(archive):
    members = archive.getmembers()
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or member.isdev() or member.isfifo():
            raise BuildError(f"unsafe archive entry: {member.name}")
        if member.issym() or member.islnk():
            target = member.linkname if member.islnk() else posixpath.join(str(name.parent), member.linkname)
            target = posixpath.normpath(target)
            if target.startswith("/") or target == ".." or target.startswith("../"):
                raise BuildError(f"unsafe archive link: {member.name}")
        elif not member.isfile() and not member.isdir():
            raise BuildError(f"unsupported archive entry: {member.name}")
    return members


def archive_layout(path, kind, arch):
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = safe_members(archive)
            files = {str(PurePosixPath(member.name)): member for member in members if member.isfile()}
            required = ("install.sh", "1panel-core", "1panel-agent", "1pctl") if kind == "app" else ("docker", "dockerd", "containerd", "runc")
            roots = {str(PurePosixPath(name).parent) for name in files if PurePosixPath(name).name == required[0]}
            roots = [root for root in roots if all(str(PurePosixPath(root, name)) in files for name in required)]
            if len(roots) != 1:
                raise BuildError(f"{kind} archive lacks an unambiguous package containing {', '.join(required)}")
            root = roots[0]
            executables = ("1panel-core", "1panel-agent") if kind == "app" else required
            for executable in executables:
                member = files[str(PurePosixPath(root, executable))]
                with archive.extractfile(member) as stream:
                    validate_elf(stream, member.size, arch)
            if kind == "app":
                for filename in ("install.sh", "1pctl"):
                    member = files[str(PurePosixPath(root, filename))]
                    with archive.extractfile(member) as stream:
                        prefix = stream.read(4096)
                    if not prefix.strip() or b"\0" in prefix or not prefix.lstrip().startswith(b"#!"):
                        raise BuildError(f"invalid shell entry point: {filename}")
            # tar readers may stop at their end marker before checking gzip's CRC
            # and footer. Consume the stream so a truncated transfer cannot pass.
            with gzip.open(path, "rb") as stream:
                while stream.read(1024 * 1024):
                    pass
            return root
    except (tarfile.TarError, EOFError, OSError, ValueError) as error:
        raise BuildError(f"invalid {kind} archive: {error}") from error


def validate_file(path, kind, arch):
    if kind in ("app", "docker"):
        return archive_layout(path, kind, arch)
    if kind == "compose":
        with Path(path).open("rb") as stream:
            validate_elf(stream, Path(path).stat().st_size, arch)
        return None
    if kind == "service":
        content = Path(path).read_text()
        if "[Service]" not in content or "ExecStart=" not in content:
            raise BuildError("invalid systemd service file")
        return None
    raise BuildError(f"unknown artifact kind: {kind}")


class Downloader:
    def __init__(self, cache_dir, opener=None, sleeper=time.sleep):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.metadata_memory = {}
        self.failed_urls = set()

    def _request(self, url):
        headers = {"User-Agent": "1Panel-offline-installer/3"}
        if urllib.parse.urlparse(url).hostname == "api.github.com":
            headers["Accept"] = "application/vnd.github+json"
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return urllib.request.Request(url, headers=headers)

    def _transfer(self, url, destination):
        if url in self.failed_urls:
            raise BuildError("source already failed in this run")
        error = None
        for attempt in range(3):
            try:
                with self.opener(self._request(url), timeout=45) as response, Path(destination).open("wb") as stream:
                    shutil.copyfileobj(response, stream, length=1024 * 1024)
                    declared_size = response.getheader("Content-Length") if hasattr(response, "getheader") else None
                    if declared_size and declared_size.isdigit() and stream.tell() != int(declared_size):
                        raise OSError("incomplete HTTP response body")
                return
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as failure:
                error = failure
                # Do not spend retries on absent assets, auth errors or API rate limits.
                if isinstance(failure, urllib.error.HTTPError) and failure.code not in (408, 429, 500, 502, 503, 504):
                    break
                if attempt < 2:
                    self.sleeper(attempt + 1)
        self.failed_urls.add(url)
        raise BuildError(f"download failed: {url}: {error}")

    def metadata(self, url, json_data=True):
        if url in self.metadata_memory:
            return self.metadata_memory[url]
        cache = self.cache_dir / "metadata" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache.parent, prefix=".download-", delete=False) as stream:
                temporary = Path(stream.name)
            self._transfer(url, temporary)
            text = temporary.read_text()
            data = json.loads(text) if json_data else text
            if json_data and not isinstance(data, (dict, list)):
                raise ValueError("unexpected API response")
            atomic_json(cache, {"url": url, "data": data})
        except (BuildError, OSError, ValueError) as error:
            log(f"[WARN] Discovery unavailable ({url}): {error}; trying cached metadata/other sources")
            try:
                cached = json.loads(cache.read_text())
                data = cached["data"] if cached["url"] == url else None
            except (OSError, ValueError, KeyError, TypeError):
                data = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.metadata_memory[url] = data
        return data

    def download(self, candidate, kind, arch):
        # Include the complete URL: repositories, release channels and mirrors never
        # share a cache entry merely because their asset filenames are identical.
        identity = hashlib.sha256(candidate.url.encode()).hexdigest()
        filename = Path(urllib.parse.urlparse(candidate.url).path).name or "artifact"
        dest = self.cache_dir / "artifacts" / identity / filename
        sidecar = dest.with_name(dest.name + ".metadata.json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        expected = candidate.digest.removeprefix("sha256:")
        if expected and not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
            expected = ""
        if dest.exists():
            try:
                validate_file(dest, kind, arch)
                digest = sha256(dest)
                if expected and expected.lower() != digest:
                    raise BuildError("cached asset does not match publisher digest")
                if sidecar.exists():
                    old = json.loads(sidecar.read_text())
                    if old.get("url") != candidate.url or old.get("sha256") != digest:
                        raise BuildError("cached asset checksum mismatch")
                log(f"Reuse {filename} ({arch})")
                return Artifact(dest, candidate.version, candidate.url, digest)
            except (BuildError, OSError, ValueError):
                log(f"[WARN] Repairing invalid cached artifact: {filename}")
                dest.unlink(missing_ok=True)
                sidecar.unlink(missing_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".download-", delete=False) as stream:
                temporary = Path(stream.name)
            log(f"Downloading {candidate.url}")
            self._transfer(candidate.url, temporary)
            validate_file(temporary, kind, arch)
            digest = sha256(temporary)
            if expected and expected.lower() != digest:
                raise BuildError("downloaded asset does not match publisher digest")
            os.replace(temporary, dest)
            atomic_json(sidecar, {"version": candidate.version, "url": candidate.url, "sha256": digest, "kind": kind, "arch": arch})
            return Artifact(dest, candidate.version, candidate.url, digest)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def cached_candidates(self, kind, arch):
        candidates = []
        for sidecar in (self.cache_dir / "artifacts").glob("*/*.metadata.json"):
            try:
                entry = json.loads(sidecar.read_text())
                if entry["kind"] == kind and entry["arch"] == arch:
                    candidates.append(Candidate(entry["version"], entry["url"], entry["sha256"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(candidates, key=lambda item: version_key(item.version), reverse=True)


def github_assets(downloader, repo, tag=None):
    suffix = "/tags/" + urllib.parse.quote(tag, safe="") if tag else "?per_page=100"
    releases = downloader.metadata(f"https://api.github.com/repos/{repo}/releases{suffix}")
    if isinstance(releases, dict):
        releases = [releases] if "assets" in releases else []
    for release in releases or []:
        if not isinstance(release, dict) or release.get("draft") or (not tag and release.get("prerelease")):
            continue
        version = release.get("tag_name", "")
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("browser_download_url") and asset.get("name"):
                yield version, asset


def compose_repos(arch):
    return ("docker/compose", "loong64/compose") if arch == "loong64" else ("docker/compose",)


def exact_candidates(kind, arch, version):
    aliases = ARCHES[arch][3]
    if kind == "compose":
        tag = version if version.startswith("v") else "v" + version
        for repo in compose_repos(arch):
            for alias in aliases:
                yield Candidate(tag, f"https://github.com/{repo}/releases/download/{tag}/docker-compose-linux-{alias}")
    else:
        version = normalized_version(version)
        for repo in DOCKER_REPOS.get(arch, ()):
            yield Candidate(version, f"https://github.com/{repo}/releases/download/v{version}/docker-{version}.tgz")
        docker_arch = "armhf" if arch == "armv7" else aliases[0]
        yield Candidate(version, f"https://download.docker.com/linux/static/stable/{docker_arch}/docker-{version}.tgz")


def discovered_candidates(downloader, kind, arch):
    candidates = []
    if kind == "compose":
        names = {f"docker-compose-linux-{alias}" for alias in ARCHES[arch][3]}
        for repo in compose_repos(arch):
            for version, asset in github_assets(downloader, repo):
                if asset["name"] in names and VERSION_RE.fullmatch(version):
                    candidates.append(Candidate(version, asset["browser_download_url"], asset.get("digest") or ""))
    else:
        for repo in DOCKER_REPOS.get(arch, ()):
            for version, asset in github_assets(downloader, repo):
                # Architecture-specific repositories sometimes change tag prefixes
                # and asset suffixes. Read the version from the archive filename.
                match = re.match(r"docker-(?:v)?(\d+\.\d+\.\d+)(?:[-_][A-Za-z0-9_.-]+)?\.(?:tgz|tar\.gz)$", asset["name"])
                if match and "rootless" not in asset["name"]:
                    candidates.append(Candidate(match.group(1), asset["browser_download_url"], asset.get("digest") or ""))
        alias = "armhf" if arch == "armv7" else ARCHES[arch][3][0]
        directory = f"https://download.docker.com/linux/static/stable/{alias}/"
        listing = downloader.metadata(directory, json_data=False)
        if isinstance(listing, str):
            for version in set(re.findall(r'\bdocker-(\d+\.\d+\.\d+)\.tgz\b', listing)):
                candidates.append(Candidate(version, f"{directory}docker-{version}.tgz"))
    # Keep enough older versions for lagging ports, but bound unavailable-asset probes.
    return sorted(candidates, key=lambda item: version_key(item.version), reverse=True)[:36]


def component_candidates(downloader, kind, arch, preferred):
    if preferred != "latest":
        yield from exact_candidates(kind, arch, preferred)
    yield from discovered_candidates(downloader, kind, arch)
    yield from downloader.cached_candidates(kind, arch)
    # Bootstrap fallback only when APIs/indexes are unavailable or their assets fail.
    for version in LEGACY_DOCKER if kind == "docker" else ("v2.23.0",):
        yield from exact_candidates(kind, arch, version)


def resolve_component(downloader, kind, arch, preferred):
    seen = set()
    for candidate in component_candidates(downloader, kind, arch, preferred):
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        try:
            artifact = downloader.download(candidate, kind, arch)
            if preferred != "latest" and normalized_version(preferred) != normalized_version(artifact.version):
                log(f"[WARN] {kind}/{arch}: requested {preferred} unavailable; using {artifact.version}")
            return artifact
        except (BuildError, OSError, ValueError) as error:
            log(f"[WARN] {kind}/{arch}: {error}; trying next candidate")
    raise BuildError(f"no valid {kind} asset available for {arch} (preferred {preferred})")


def resolve_app(downloader, source, arch, args):
    name = f"1panel-{args.app_version}-linux-{arch}.tar.gz"
    if source == "official":
        urls = [Candidate(args.app_version, f"https://resource.fit2cloud.com/1panel/package/v2/{args.mode}/{args.app_version}/release/{name}")]
    else:
        release_base = f"https://github.com/{args.custom_repo}/releases/download/{args.app_version}"
        manifest = downloader.metadata(release_base + "/build-manifest.json")
        if isinstance(manifest, dict) and manifest.get("schema", manifest.get("schema_version")) == 1 and isinstance(manifest.get("packages"), list):
            if manifest.get("version") != args.app_version:
                raise BuildError("custom build manifest does not match the requested app version")
            entries = [entry for entry in manifest["packages"] if isinstance(entry, dict) and entry.get("arch") == arch]
            if not entries or entries[0].get("status") != "built":
                reason = entries[0].get("reason", "not built") if entries else "not included in this build"
                raise BuildError(f"custom build manifest marks {arch} unavailable: {reason}")
            entry = entries[0]
            filename, digest = entry.get("file", ""), entry.get("sha256", "")
            if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith((".tar.gz", ".tgz")) or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                raise BuildError("custom build manifest contains an invalid asset name or checksum")
            # A recognized manifest is authoritative: never fall through to stale
            # attachments from an earlier incomplete/rebuilt release.
            return downloader.download(Candidate(args.app_version, release_base + "/" + urllib.parse.quote(filename), digest), "app", arch)
        if manifest is not None:
            log("[WARN] Unrecognized custom build manifest; using legacy asset discovery")
        urls = [Candidate(args.app_version, f"https://github.com/{args.custom_repo}/releases/download/{args.app_version}/{name}")]
    for candidate in urls:
        try:
            return downloader.download(candidate, "app", arch)
        except (BuildError, OSError, ValueError) as error:
            log(f"[WARN] {source}/{arch}: {error}")
    if source == "custom":
        # Discover aliases or changed release filenames, but never switch app tags.
        aliases = set(ARCHES[arch][3]) | {arch}
        for version, asset in github_assets(downloader, args.custom_repo, args.app_version):
            name = asset["name"]
            if "offline" in name.lower() or not name.endswith((".tar.gz", ".tgz")):
                continue
            if not any(re.search(r"(?:^|[-_])" + re.escape(alias) + r"(?:\.tar\.gz|\.tgz)$", name) for alias in aliases):
                continue
            try:
                return downloader.download(Candidate(args.app_version, asset["browser_download_url"], asset.get("digest") or ""), "app", arch)
            except (BuildError, OSError, ValueError) as error:
                log(f"[WARN] {source}/{arch}: {error}")
    raise BuildError(f"no valid {source} 1Panel {args.app_version} package for {arch}")


def extract_app(artifact, destination, arch):
    root = archive_layout(artifact.path, "app", arch)
    with tarfile.open(artifact.path, "r:gz") as archive:
        # Extract into a fresh private directory only after all names/links were checked.
        options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        archive.extractall(destination, members=safe_members(archive), **options)
    return Path(destination) / root


def ensure_services(downloader, directory, arch, warnings):
    for role in ("core", "agent"):
        filename = f"1panel-{role}.service"
        locations = (directory / "initscript" / filename, directory / filename)
        for location in locations:
            try:
                validate_file(location, "service", arch)
                break
            except (BuildError, OSError, ValueError):
                continue
        else:
            artifact = downloader.download(Candidate("v2", f"https://raw.githubusercontent.com/1Panel-dev/installer/v2/initscript/{filename}"), "service", arch)
            target = directory / "initscript" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact.path, target)
            warnings.append(f"{filename} missing/invalid upstream; restored from installer/v2")


def assemble_package(downloader, source, arch, args, base_dir, version_dir):
    app = resolve_app(downloader, source, arch, args)
    docker = resolve_component(downloader, "docker", arch, args.docker_version)
    compose = resolve_component(downloader, "compose", arch, args.compose_version)
    components = {"app": app.metadata(), "docker": docker.metadata(args.docker_version), "compose": compose.metadata(args.compose_version)}
    warnings = []
    name = f"1panel-{args.app_version}-{source}-offline-linux-{arch}"
    package_dir = version_dir / source
    package_dir.mkdir(parents=True, exist_ok=True)
    target = package_dir / (name + ".tar.gz")
    with tempfile.TemporaryDirectory(dir=package_dir, prefix=".assemble-") as staging:
        directory = extract_app(app, Path(staging) / "upstream", arch)
        ensure_services(downloader, directory, arch, warnings)
        # The upstream script is preserved byte-for-byte. The launcher establishes
        # local Docker/Compose first, so upstream formatting changes need no patch.
        directory.joinpath("install.sh").rename(directory / "install-upstream.sh")
        for source_path, destination in (
            (docker.path, "docker.tgz"), (compose.path, "docker-compose"),
            (base_dir / "docker.service", "docker.service"),
            (base_dir / "upgrade_offline.sh", "upgrade.sh"),
            (base_dir / "scripts" / "install-offline.sh", "install.sh"),
            (base_dir / "scripts" / "offline-env.sh", "offline-env.sh"),
        ):
            shutil.copy2(source_path, directory / destination)
        for filename in ("install.sh", "install-upstream.sh", "upgrade.sh", "1pctl", "docker-compose"):
            (directory / filename).chmod(0o755)
        for filename in ("install.sh", "install-upstream.sh", "upgrade.sh", "offline-env.sh", "1pctl"):
            result = subprocess.run(["bash", "-n", str(directory / filename)], text=True, capture_output=True)
            if result.returncode:
                raise BuildError(f"invalid shell syntax in {filename}: {result.stderr.strip()}")
        manifest = {"schema_version": 1, "fingerprint": os.environ.get("BUILD_FINGERPRINT", ""),
                    "version": args.app_version, "mode": args.mode, "source": source, "arch": arch,
                    "components": components, "warnings": warnings}
        atomic_json(directory / "manifest.json", manifest)
        temporary_tar = Path(staging) / "package.tar.gz"
        with tarfile.open(temporary_tar, "w:gz") as archive:
            archive.add(directory, arcname=name)
        os.replace(temporary_tar, target)
    log(f"Built {target}")
    return {"source": source, "arch": arch, "status": "built", "asset": target.name,
            "path": str(target.relative_to(version_dir)), "sha256": sha256(target), "size": target.stat().st_size,
            "components": components, "warnings": warnings}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("stable", "beta", "dev"), default="stable")
    parser.add_argument("--app_version", default="", help="exact 1Panel version; default: channel latest")
    parser.add_argument("--interactive", action="store_true", help="confirm/override app version when stdin is a TTY")
    parser.add_argument("--source", choices=("official", "custom", "both"), default="both")
    parser.add_argument("--custom_repo", default="HandSonic/1Panel-Build-v2")
    parser.add_argument("--docker_version", default="latest", help="preferred Docker version; automatically fall back to available compatible releases")
    parser.add_argument("--compose_version", default="latest", help="preferred Compose version; automatically fall back to available compatible releases")
    parser.add_argument("--arch", default=" ".join(ARCHES), help="comma/space separated architecture list")
    parser.add_argument("--allow-missing", action="store_true", help="skip unavailable source/architecture pairs; record them for a later retry")
    args = parser.parse_args(argv)
    args.architectures = list(dict.fromkeys("loong64" if item == "loongarch64" else item for item in re.split(r"[,\s]+", args.arch.strip())))
    if not args.architectures or any(arch not in ARCHES for arch in args.architectures):
        parser.error("--arch must contain supported architectures: " + ", ".join(ARCHES))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.custom_repo):
        parser.error("--custom_repo must be owner/repo")
    args.docker_version = normalized_version(args.docker_version)
    for kind in ("docker", "compose"):
        preferred = getattr(args, kind + "_version")
        if preferred != "latest" and not VERSION_RE.fullmatch(preferred):
            parser.error(f"invalid --{kind}_version")
    args.sources = ["official", "custom"] if args.source == "both" else [args.source]
    return args


def build(args, base_dir=BASE_DIR, downloader=None):
    base_dir = Path(base_dir)
    build_root = Path(os.environ.get("ONEPANEL_BUILD_ROOT", str(base_dir / "build")))
    downloader = downloader or Downloader(build_root / "cache")
    if not args.app_version:
        latest = downloader.metadata(f"https://resource.fit2cloud.com/1panel/package/v2/{args.mode}/latest", json_data=False)
        args.app_version = latest.strip() if isinstance(latest, str) else ""
    if args.interactive and sys.stdin.isatty():
        args.app_version = input(f"Detected version: {args.app_version}. Version to use (Enter to keep): ").strip() or args.app_version
    if not VERSION_RE.fullmatch(args.app_version):
        raise BuildError(f"invalid/unavailable 1Panel version for {args.mode}: {args.app_version!r}; specify --app_version to retry")
    version_dir = build_root / args.app_version
    version_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "fingerprint": os.environ.get("BUILD_FINGERPRINT", ""),
                "version": args.app_version, "mode": args.mode,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "requested": {"sources": args.sources, "architectures": args.architectures,
                              "docker_version": args.docker_version, "compose_version": args.compose_version,
                              "custom_repo": args.custom_repo}, "packages": []}
    for arch in args.architectures:
        for source in args.sources:
            # An unavailable current pair must never leave an old tarball looking built.
            target = version_dir / source / f"1panel-{args.app_version}-{source}-offline-linux-{arch}.tar.gz"
            target.unlink(missing_ok=True)
            try:
                entry = assemble_package(downloader, source, arch, args, base_dir, version_dir)
            except (BuildError, OSError, ValueError) as error:
                log(f"[WARN] Skip {source}/{arch}: {error}")
                entry = {"source": source, "arch": arch, "status": "skipped", "reason": str(error)}
            manifest["packages"].append(entry)
    built = [entry for entry in manifest["packages"] if entry["status"] == "built"]
    manifest.update(built_count=len(built), skipped_count=len(manifest["packages"]) - len(built))
    manifest["complete"] = bool(built) and manifest["skipped_count"] == 0
    atomic_json(version_dir / "manifest.json", manifest)
    # Releases flatten directories; checksum entries must use the attachment basenames.
    checksums = "".join(f"{entry['sha256']}  {entry['asset']}\n" for entry in built)
    (version_dir / "checksums.txt").write_text(checksums)
    log(f"Built {manifest['built_count']} package(s), skipped {manifest['skipped_count']}; details: {version_dir / 'manifest.json'}")
    if not built:
        log("No offline packages built. Do not create/publish a release; retry on the next run.")
    return 0 if args.allow_missing or manifest["complete"] else 1


def main(argv=None):
    try:
        return build(parse_args(argv))
    except (BuildError, OSError, ValueError) as error:
        log(f"[ERROR] {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
