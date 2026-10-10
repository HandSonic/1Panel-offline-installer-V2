# 1Panel v2 Offline Package Generator

[中文](README_zh.md)

Build, verify and install offline 1Panel v2 packages. The generator bundles pinned Docker and Compose binaries, patches supported installer interfaces and preserves upstream application resources.

## Build packages

Use the repository's **Build 1Panel v2 Offline** GitHub Actions workflow (`.github/workflows/build-offline-v2.yml`). Select a channel and an application version; an empty version resolves the channel's latest version for normal builds.

- `build`: resolve inputs, prepare independent package branches, perform native acceptance and publish admitted products.
- `validate-repair`: prepare and test an explicit version/tag without changing its release.
- `repair-existing`: run the same gates and recoverably repair the specified existing public release.

The workflow resolves source commits, source configuration, producer provenance, official/enterprise availability and architecture membership for each run. Every consumer requires that run's exact authenticated plan. No application-version whitelist, release matrix, embedded configuration registry or historical validator snapshot is maintained in this repository. A new version does not require a repository data change.

Official, custom, enterprise-original and enterprise-docker products are independent branches. Discovery errors remain explicit failures. A checksum or download failure never becomes an absence claim. Successful products are published only after their own required checks pass; failed branches remain visible in Actions and the receipt. If no product qualifies, publication fails.

`docker-sources.json` and `compose-sources.json` contain generic architecture-specific dependency pins. Their URL, version, byte count and SHA-256 must match the bundled files. Updating those dependencies requires validating their actual bytes and ELF architectures.

The former CNB recipes, separate published-package smoke workflow, receipt refresh modes and standalone release mutation scripts have been retired. The GitHub workflow is the supported build/publication route. Historical source remains in Git history. This cleanup does not claim that any CNB runtime was tested.

## Package contents

Community assets are named `1panel-<version>-<source>-offline-linux-<arch>.tar.gz`. Each contains the application, Docker's required binaries, Compose, a service definition, installation and upgrade scripts, and an `offline-manifest.json` with exact provenance and payload hashes.

Enterprise-original assets preserve the authenticated upstream archive byte-for-byte. Enterprise-docker assets retain the original inner directory and upstream upgrade script, add Docker/Compose, and patch the installer. AppStore membership is determined from authenticated archive contents and the installer interface. Original enterprise files are compared byte-for-byte during validation.

Checksums use flat asset filenames. The release receipt records requested products, failures, accepted products and exact workflow/native evidence.

## Install or upgrade offline

Download an admitted package and its published checksum, verify the checksum, transfer the files to the target machine, and extract the archive. From its extracted directory:

```bash
sudo ./install.sh
```

To upgrade an existing installation, use the included `upgrade.sh`. Community upgrades use the repository's guarded offline updater; enterprise packages retain their own upstream updater. Back up the installation and use a package matching the target architecture.

Fresh Docker installation requires systemd and preinstalled iptables for default networking. A healthy existing local Docker engine is preserved. Conflicting `DOCKER_HOST`, `DOCKER_CONTEXT`, non-default contexts and custom sockets require manual preparation. The installer does not rewrite `daemon.json` or fetch missing operating-system dependencies.

## Development and validation

The package scripts consume `ONEPANEL_RESOLVED_PLAN` and `ONEPANEL_RESOLVED_PLAN_SHA256` from the authenticated workflow planner. Setting a local JSON file alone does not enable a fallback build. For pipeline details and repair commands, see the [publication runbook](docs/publication-runbook.md).

```bash
python3 -m pip install -r requirements-validation.txt
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
bash -n prepare_offline.sh upgrade_offline.sh quick_start.sh
```

Tests use synthetic authenticated contracts and content-addressed historical installer text. They cover malformed input, archive/path/ELF checks, exact normalized configuration, producer and inventory tampering, offline shell behavior and publication admission. They do not execute downloaded application binaries or establish native compatibility by themselves.

Native installation runs only on disposable GitHub-hosted amd64/arm64 runners, for existing and fresh Docker. Community upgrades also require an authenticated predecessor test. Other architectures receive static validation without an implied native runtime claim. See [native validation](docs/native-runtime-validation.md), [package preparation](docs/package-matrix.md) and [writer concurrency](docs/workflow-concurrency.md).
