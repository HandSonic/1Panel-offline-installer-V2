# Native installer validation

`native-install-smoke.yml` is a read-only publication consumer. It never creates,
replaces or deletes release assets. A same-repository PR changing the harness
runs the pinned v2.3.2 custom/amd64 package with the runner's existing Docker.
Manual runs select an exact application version, published release tag, source,
native architecture, Docker scenario and expected archive SHA-256. Release tags
may identify reviewed correction releases while filenames keep the application
version. The server digest and downloaded
bytes must both match before complete static package validation and extraction.
The result records that verified archive SHA-256 as well as the manifest hash.
This consumes the published package bytes, not a new package built from the PR;
it cannot establish runtime correctness of unpublished packager changes.

The real installer runs only as root on a disposable GitHub-hosted Linux VM;
local computers and self-hosted runners are rejected. The fresh-Docker scenario
stops the disposable runner's original Docker and moves its CLI entrypoints into
a temporary recovery directory so the installer must use the bundled engine.
The existing-Docker scenario asserts the healthy existing systemd daemon's
version, process identity and executable hash are preserved. The fresh scenario
accepts a runner without existing Docker units, verifies every installed Docker
binary hash and binds the running daemon to the bundled executable.
Both execute the bundled Compose plugin and verify installed application bytes,
both services' live process executable hashes, the core CLI version command and
a local HTTP response. A bounded readiness wait accommodates historical
installers' final service restart. The HTTP probe bypasses proxies and refuses
redirects. Version matching rejects unrequested prerelease/build suffixes.
Historical installers use their own translation-key prompt meanings; modern CLI
is selected only when the actual script declares the capability. The package's
regional edition (`cn`, `intl`, or legacy without that setting) is preserved,
checked in the installed control script and recorded separately from the package
source (`custom`, `official`, or `enterprise-docker`). Native runs use English;
they do not establish the other language defaults. Credentials
are synthetic, kept out of command arguments/artifacts and installer logs are
removed after execution.

A successful JSON result proves native installation only for that exact input,
architecture and Docker scenario. It does not prove upgrades, migrations,
rollback, an independent agent version command, other versions or other
architectures. Only amd64 and arm64 have runner mappings here. Version-specific
custom configuration locks and enterprise-original source locks are still
required by static validation; missing historical locks fail closed.
Network isolation is not yet
enforced, and that limitation is recorded in every result. Enterprise enhanced
packages are validated against their unchanged original archive before running;
this test does not substitute the community upgrader for enterprise upgrade.
No runtime result is a publication approval or an all-history completion claim.

`test_historical_shell_flows.py` complements this with safe, temporary-root
configuration-flow execution of all twelve immutable installer fixtures. It
covers prompt-driven configuration, actual `/dev/tty` and fallback password
input, invalid-input retries, modern CLI defaults, edition-dependent language,
invalid CLI options/values, main call order, and AppStore resource copying.
Those controlled tests remain separate from actual native service evidence.
