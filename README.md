# Adaptive 1Panel v2 offline packages

[中文](README_zh.md)

Combine official or HandSonic/1Panel-Build-v2 releases with Docker and Compose for offline installation.

## Build

Requires Bash and Python 3.9+ on a connected Linux/macOS/WSL build host. Downloaded executables are inspected, never run.

```bash
bash prepare_offline.sh --source both --arch amd64,arm64 --allow-missing
bash prepare_offline.sh --app_version v2.2.5 --docker_version 27.5.1 --compose_version v2.30.3 --allow-missing
```

Defaults: latest stable 1Panel, both sources, all seven supported architectures, latest Docker/Compose. Existing flags remain available with --help.

The application version stays exact. Docker/Compose versions are preferences: mirrors, discovered release assets, official directory listings, valid caches and compatible historical versions provide fallbacks. Failed source/architecture pairs are isolated with --allow-missing. Invalid caches are repaired; URL-based keys separate repositories/channels. Validation checks ELF architecture/load segments, archive paths and completeness instead of an arbitrary minimum file size.

## Install and upgrade

```bash
sha256sum -c checksums.txt
tar -xzf 1panel-v2.2.5-official-offline-linux-amd64.tar.gz
cd 1panel-v2.2.5-official-offline-linux-amd64
sudo bash install.sh
# For an existing installation:
sudo bash upgrade.sh
```

install.sh is an independent launcher. It prepares local Docker/Compose and invokes the original install-upstream.sh with arguments unchanged; upstream prompts, whitespace and function names are never patched. Working engines/plugins are preserved. New engines support systemd/OpenRC/SysV. Ordinary curl/wget calls in the upstream Bash installer fail promptly in the offline environment.

The host still needs Docker's kernel/cgroups/iptables requirements. Application-store images are not included. quick_start.sh remains a checksum-verified **online** installer.

upgrade.sh stops services, snapshots databases including WAL, configuration, units, binaries and resources, then updates transactionally. Both services must remain healthy; substantive failures restore the full snapshot. Transient starts can recover, existing units/configuration are preserved, and absent optional language/GeoIP resources retain installed copies. Docker/Compose are not upgraded. Backups remain next to the package.

PANEL_HEALTH_TIMEOUT defaults to 60 seconds. --force permits an intentional downgrade with a compatible backup. PANEL_BASE_DIR_OVERRIDE corrects directory detection while retaining the upstream 1pctl format and its shell-path limitations. Keep upgrade packages outside the db/conf/config/geo directories being replaced.

## Release behavior

build/<version>/manifest.json records each built/skipped pair, reason, actual dependency versions, URLs, hashes and fingerprint. Each package also carries a manifest; checksums.txt uses flat release attachment filenames.

GitHub and CNB share resolution, packaging and staging. GitHub skips only when the complete manifest, fingerprint and attachments match. Dependency/script/custom-asset changes rebuild; partial results may publish and missing pairs retry later. Zero packages never create an empty release. New GitHub releases remain drafts until upload verification. The manifest is written last; replacing an existing release first invalidates its old completion marker. Rebuilding historical versions does not intentionally move latest backwards.

CNB always rechecks available packages rather than treating an existing tag as completion, then uses native attachment upload. Interrupted uploads retry on subsequent runs. Updating existing releases is not a platform-level atomic transaction: consumers should use the current manifest and checksums.

Branches and PRs run tests without publishing. GitHub release publication is restricted to master; manual branch builds retain reviewable Actions artifacts.

```bash
python3 -m unittest discover -s tests -v
```

Offline fixtures cover layout changes, 404/truncated/wrong-architecture downloads, fallback/cache repair, partial releases, upgrade rollback and online checksum verification. Routine formatting/version changes adapt automatically; breaking upstream installation protocols or runtime requirements can still require compatibility work.
