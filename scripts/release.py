#!/usr/bin/env python3
"""Plan, validate and publish offline releases. All network operations are bounded.

CNB uses the same plan/staging commands and its native attachment uploader.
A tag or one existing archive is never a completion marker.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"v2\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")


def warn(message):
    print(f"[release] {message}", file=sys.stderr)


def fetch(url, as_json=False):
    headers = {"User-Agent": "1Panel-offline-builder"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as response:
                text = response.read().decode().strip()
                return json.loads(text) if as_json else text
        except (OSError, ValueError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                return None
            if attempt < 2:
                time.sleep(attempt + 1)
    warn(f"Temporarily unavailable: {url}")
    return None


def gh(*args, check=True):
    # Uploads can be large; still bound the operation so schedules can recover.
    timeout = 900 if args[:2] == ("release", "upload") else 180
    retryable = args[:2] in {("release", "view"), ("release", "download"), ("release", "upload"), ("release", "edit")}
    for attempt in range(3 if retryable else 1):
        try:
            result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            if not retryable or attempt == 2:
                raise
        else:
            transient = re.search(r"429|50[234]|timeout|timed out|connection|eof|temporar|tls handshake", result.stderr, re.I)
            if not result.returncode or not retryable or not transient or attempt == 2:
                if check:
                    result.check_returncode()
                return result
        time.sleep(attempt + 1)


def release_metadata(repo, version):
    return fetch(f"https://api.github.com/repos/{repo}/releases/tags/{version}", True)


def fingerprint(plan, custom_release):
    h = hashlib.sha256()
    # Code/content, not a mutable branch label, determines whether repacking is needed.
    paths = [ROOT / "prepare_offline.sh", ROOT / "upgrade_offline.sh", ROOT / "docker.service"]
    paths += sorted((ROOT / "scripts").glob("*"))
    for path in paths:
        if path.is_file():
            h.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    inputs = {key: plan[key] for key in ("version", "mode", "docker_version", "compose_version", "custom_repo")}
    inputs["custom_assets"] = sorted(
        [(a.get("name"), a.get("size"), a.get("digest"), a.get("updated_at"))
         for a in (custom_release or {}).get("assets", [])]
    )
    h.update(json.dumps(inputs, sort_keys=True).encode())
    return h.hexdigest()


def manifest_complete(manifest, metadata, expected_fingerprint):
    if not manifest or manifest.get("fingerprint") != expected_fingerprint or not manifest.get("complete"):
        return False
    assets = {a["name"]: a for a in metadata.get("assets", [])}
    if "manifest.json" not in assets or "checksums.txt" not in assets:
        return False
    packages = manifest.get("packages", [])
    requested = manifest.get("requested", {})
    expected = {(s, a) for s in requested.get("sources", []) for a in requested.get("architectures", [])}
    actual = {(p.get("source"), p.get("arch")) for p in packages if p.get("status") == "built"}
    if not expected or expected != actual or len(actual) != len(packages):
        return False
    for package in packages:
        asset = assets.get(package.get("asset"))
        if not asset or asset.get("size", 0) <= 0 or not package.get("sha256"):
            return False
        digest = asset.get("digest")
        if digest and digest != "sha256:" + package["sha256"]:
            return False
    return True


def outputs(values, provider):
    if provider == "cnb":
        for key, value in values.items():
            print(f"##[set-output {key.upper()}={value}]")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(values, ensure_ascii=False))


def plan_release(provider):
    mode = os.environ.get("MODE") or "stable"
    if mode not in {"stable", "beta", "dev"}:
        raise ValueError("MODE must be stable, beta or dev")
    custom_repo = os.environ.get("CUSTOM_REPO", "HandSonic/1Panel-Build-v2")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", custom_repo):
        raise ValueError("Invalid CUSTOM_REPO")
    version = os.environ.get("INPUT_VERSION", "").strip()
    if not version:
        version = fetch(f"https://resource.fit2cloud.com/1panel/package/v2/{mode}/latest")
        if not version and mode == "stable":
            version = (fetch("https://api.github.com/repos/1Panel-dev/1Panel/releases/latest", True) or {}).get("tag_name")
    if not version:
        warn("No version could be resolved. Deferred without creating a release; the next schedule will retry.")
        outputs({"build": "0", "reason": "version-unavailable"}, provider)
        return
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid upstream version: {version!r}")
    docker = os.environ.get("DOCKER_VERSION") or (fetch("https://api.github.com/repos/moby/moby/releases/latest", True) or {}).get("tag_name") or "27.5.1"
    docker = docker.removeprefix("docker-").removeprefix("v")
    compose = os.environ.get("COMPOSE_VERSION") or (fetch("https://api.github.com/repos/docker/compose/releases/latest", True) or {}).get("tag_name") or "v2.30.3"
    plan = dict(version=version, mode=mode, docker_version=docker, compose_version=compose, custom_repo=custom_repo)
    plan["fingerprint"] = fingerprint(plan, release_metadata(custom_repo, version))
    plan["build"] = "1"
    # CNB retries automatically because tags cannot certify attachment completeness.
    if provider == "github" and os.environ.get("FORCE_BUILD", "0").lower() not in {"1", "true"}:
        repo = os.environ.get("GITHUB_REPOSITORY", "HandSonic/1Panel-offline-installer-V2")
        metadata = release_metadata(repo, version)
        if metadata and not metadata.get("draft") and shutil.which("gh"):
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "manifest.json"
                result = gh("release", "download", version, "-R", repo, "-p", "manifest.json", "-O", str(target), "--clobber", check=False)
                if result.returncode == 0:
                    try:
                        if manifest_complete(json.loads(target.read_text()), metadata, plan["fingerprint"]):
                            plan["build"] = "0"
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build/ci-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    outputs(plan, provider)


def load_plan():
    return json.loads((ROOT / "build/ci-plan.json").read_text())


def stage_release(provider):
    plan = load_plan()
    version_dir = ROOT / "build" / plan["version"]
    manifest_path = version_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("version") != plan["version"] or manifest.get("fingerprint") != plan["fingerprint"]:
        raise ValueError("Manifest version/fingerprint differs from the current build plan")
    stage = ROOT / "build/release-assets"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    checksums = []
    for package in manifest["packages"]:
        if package["status"] != "built":
            continue
        file = (version_dir / package["path"]).resolve()
        if not file.is_relative_to(version_dir.resolve()) or file.name != package["asset"]:
            raise ValueError("Package path escapes version directory")
        digest = sha256_file(file)
        if digest != package["sha256"]:
            raise ValueError(f"Archive changed since packaging: {file.name}")
        shutil.copy2(file, stage / file.name)
        checksums.append(f"{digest}  {file.name}\n")
    (stage / "checksums.txt").write_text("".join(sorted(checksums)))
    shutil.copy2(manifest_path, stage / "manifest.json")
    outputs({"has_packages": "1" if checksums else "0", "built_count": len(checksums)}, provider)
    if not checksums:
        warn("No usable packages this run; no release will be created. The next run can retry.")


def sha256_file(file):
    h = hashlib.sha256()
    with file.open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def publish_release():
    plan = load_plan()
    stage_release("github")
    stage = ROOT / "build/release-assets"
    manifest = json.loads((stage / "manifest.json").read_text())
    assets = [stage / p["asset"] for p in manifest["packages"] if p["status"] == "built"]
    if not assets:
        return
    repo = os.environ["GITHUB_REPOSITORY"]
    version = plan["version"]
    existing = gh("release", "view", version, "-R", repo, "--json", "isDraft", check=False)
    notes = f"Offline 1Panel {version}; channel: {plan['mode']}. Built {len(assets)} package(s). See manifest.json for actual dependency versions and deferred architectures."
    if existing.returncode:
        gh("release", "create", version, "-R", repo, "--draft", "--title", version, "--notes", notes)
    else:
        # Invalidate the old completion marker before replacing any public asset.
        # Missing marker means retry, even if this run is interrupted halfway.
        metadata = release_metadata(repo, version)
        if metadata is None:
            raise RuntimeError("Cannot inspect existing release before replacing assets")
        if any(asset["name"] == "manifest.json" for asset in metadata.get("assets", [])):
            # A temporary outage for one architecture must not discard a valid
            # package from the same inputs. A changed fingerprint never reuses it.
            with tempfile.TemporaryDirectory() as temp:
                previous_file = Path(temp) / "manifest.json"
                previous_result = gh("release", "download", version, "-R", repo, "-p", "manifest.json", "-O", str(previous_file), "--clobber", check=False)
                try:
                    previous = json.loads(previous_file.read_text()) if previous_result.returncode == 0 else {}
                except (OSError, ValueError):
                    previous = {}
                if previous.get("fingerprint") == manifest.get("fingerprint") and previous.get("version") == version:
                    known = {a["name"]: a for a in metadata.get("assets", [])}
                    old_pairs = {(p.get("source"), p.get("arch")): p for p in previous.get("packages", []) if p.get("status") == "built"}
                    for index, package in enumerate(manifest["packages"]):
                        old = old_pairs.get((package.get("source"), package.get("arch")))
                        if package["status"] == "built" or not old:
                            continue
                        asset = known.get(old.get("asset"), {})
                        valid = asset.get("digest") == "sha256:" + old.get("sha256", "")
                        if not asset.get("digest"):
                            valid = bool(old.get("size")) and asset.get("size") == old["size"]
                        if valid:
                            manifest["packages"][index] = old
            manifest["built_count"] = sum(p["status"] == "built" for p in manifest["packages"])
            manifest["skipped_count"] = len(manifest["packages"]) - manifest["built_count"]
            manifest["complete"] = manifest["built_count"] > 0 and manifest["skipped_count"] == 0
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            (stage / "checksums.txt").write_text("".join(sorted(f"{p['sha256']}  {p['asset']}\n" for p in manifest["packages"] if p["status"] == "built")))
            gh("release", "delete-asset", version, "manifest.json", "-R", repo, "--yes")
    # Manifest is uploaded last: a failed upload never marks the build complete.
    gh("release", "upload", version, *map(str, assets), "-R", repo, "--clobber")
    gh("release", "upload", version, str(stage / "checksums.txt"), "-R", repo, "--clobber")
    metadata = release_metadata(repo, version)
    if not metadata:
        raise RuntimeError("Cannot verify uploaded release assets; leaving publication incomplete for retry")
    uploaded = {a["name"]: a for a in metadata.get("assets", [])}
    expected_hashes = {p["asset"]: p["sha256"] for p in manifest["packages"] if p["status"] == "built"}
    expected_hashes["checksums.txt"] = sha256_file(stage / "checksums.txt")
    for asset in [*assets, stage / "checksums.txt"]:
        if uploaded.get(asset.name, {}).get("size") != asset.stat().st_size:
            raise RuntimeError(f"Upload not verified: {asset.name}")
        digest = uploaded[asset.name].get("digest")
        if digest and digest != "sha256:" + expected_hashes[asset.name]:
            raise RuntimeError(f"Upload digest differs: {asset.name}")
    # Remove only obsolete archives owned by this packager, after verification.
    current = {p["asset"] for p in manifest["packages"] if p["status"] == "built"}
    for asset in metadata.get("assets", []):
        name = asset["name"]
        if name.startswith(f"1panel-{version}-") and "-offline-linux-" in name and name.endswith(".tar.gz") and name not in current:
            gh("release", "delete-asset", version, name, "-R", repo, "--yes")
    gh("release", "upload", version, str(stage / "manifest.json"), "-R", repo, "--clobber")
    with tempfile.TemporaryDirectory() as temp:
        downloaded = Path(temp) / "manifest.json"
        gh("release", "download", version, "-R", repo, "-p", "manifest.json", "-O", str(downloaded), "--clobber")
        if json.loads(downloaded.read_text()) != manifest:
            raise RuntimeError("Uploaded manifest read-back failed")
    # Rebuilding an old version must not replace the latest stable release.
    latest = fetch(f"https://api.github.com/repos/{repo}/releases/latest", True)
    def version_order(value):
        return tuple(map(int, re.findall(r"\d+", value)[:3]))
    flags = ["--prerelease=false"] if plan["mode"] == "stable" else ["--prerelease", "--latest=false"]
    if plan["mode"] == "stable" and latest:
        flags += ["--latest" if version_order(version) >= version_order(latest.get("tag_name", "")) else "--latest=false"]
    gh("release", "edit", version, "-R", repo, "--draft=false", "--notes", notes, *flags)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["plan", "stage", "publish"])
    parser.add_argument("--provider", choices=["github", "cnb"], default="github")
    args = parser.parse_args()
    if args.command == "plan": plan_release(args.provider)
    elif args.command == "stage": stage_release(args.provider)
    else: publish_release()


if __name__ == "__main__":
    main()
