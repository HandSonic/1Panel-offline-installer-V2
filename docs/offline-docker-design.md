# Offline Docker integration decision

## What upstream already supports

The reviewed v2.3.2 official enterprise install.sh includes
`Install_Docker_Offline`. It expects `docker/bin`,
`docker/service/docker.service`, and `docker/conf/daemon.json` inside the package.
The inspected original enterprise archives contain an offline application store,
but not this Docker directory or an iptables-deb directory. Their bytes remain
unchanged in the enterprise-original assets.

Official original source:
https://resource.fit2cloud.com/1panel/package/enterprise/stable/v2.3.2/release/
This historical observation remains source history. Current builds authenticate exact URLs, hashes and archive capabilities in the per-run plan; they do not consult a version-specific source file.

## Why use the isolated helper for this release

Simply adapting directory names would not provide the reviewed contract:

- The official built-in helper copies binaries to /usr/bin and overwrites
  /etc/docker/daemon.json. Our helper installs new binaries to /usr/local/bin,
  leaves daemon.json intact, and preserves a healthy existing engine/service.
- The official helper does not provide our Compose plugin installation step.
- Official selection logic still includes online Docker fallback and location
  lookups. The reviewed helper has no curl, wget, apt or other online dependency
  installation path; its public-IP lookup is also disabled.
- Official Debian 11/12/13 dependency handling runs `apt-get -f install` only when
  iptables is absent and a matching iptables-deb directory exists. Without that
  directory it returns without supplying the prerequisite. This conditional path
  must not be described as an unconditional network request in the original
  enterprise package.
- Our helper checks a running systemd manager before fresh installation writes.
  An unhealthy existing Docker installation requires a known docker.service and
  a successful startup before any Compose installation writes. Client-only or
  unsupported service configurations fail cleanly instead of being overwritten.

A future official-layout adapter is possible, but configuration preservation,
offline-only routing, Compose handling and failure checks would still need
reviewed changes and the same regressions. Switching layouts mid-release adds
review work without removing these requirements.

## Explicit prerequisites and boundaries

A **fresh default-network Docker installation requires iptables already present**.
If it is missing, this installer stops before copying Docker or Compose files and
asks for the prerequisite to be installed offline. It never runs apt as fallback.
It does not bundle or promise validation of every distribution's firewall/kernel
requirements. No untested dependency bundles are added.

Healthy existing Docker is not subject to this fresh-install iptables check;
its networking configuration is preserved rather than inferred or replaced.
Fresh installations with specialized networking need an explicit configuration
review instead of an assumption that the default-network prerequisite applies.

Every engine health probe explicitly targets `unix:///var/run/docker.sock`.
Conflicting DOCKER_HOST/DOCKER_CONTEXT values and an existing non-default Docker
context are rejected before installation copies. Custom sockets or contexts need
manual preparation; the installer does not change contexts, environment
credentials or daemon.json. Healthy default-local engines remain supported.
Tests prove no-network behavior for the patched installation path, not that an
installed application can never initiate network traffic.

Regression tests use command stubs and a temporary fake filesystem. They do not
start a real Docker daemon, execute downloaded binaries, or install on all seven
native architectures. Archive, hash and ELF checks are reported separately from
those runtime claims.
