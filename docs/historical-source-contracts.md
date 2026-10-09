# Historical downstream source contracts

The first resolved historical batch is v2.3.1, v2.3.0 and v2.2.5. Each version
has its own 17-product matrix: seven custom architectures, six official
architectures, and original plus Docker-enhanced enterprise archives for amd64
and arm64. Official loong64 and the other enterprise architectures are not
invented or replaced with custom binaries. The exact vendor checksum manifests
omit those canonical names; canonical HEAD probes returned 404. Most omissions
also have GET 404 corroboration. A missing secondary probe is not a claim of
permanent or universal architecture non-support.

## Measured vendor inputs

All 18 official and six enterprise archives for this batch were completely
downloaded as streams and compared with vendor-published checksums, byte counts,
gzip integrity, expected member roots and Core/Agent ELF targets. The versioned
`official-sources-*.json` and `enterprise-sources-*.json` files contain the exact
URL, version, bytes and SHA-256. The official builder now consumes its locks
before repacking, including cache invalidation and version/channel checks; the
release validator independently checks the recorded input against the lock.

Official manifests are at
`https://resource.fit2cloud.com/1panel/package/v2/stable/VERSION/release/checksums.txt`.
Enterprise manifests use
`https://resource.fit2cloud.com/1panel/package/enterprise/stable/VERSION/release/checksums.txt`.
These establish vendor archive identity, not a custom-build source commit.
The added v2.3.2 official pins preserve the measured original-input identities
already recorded in its audited and published offline manifests.

## Enterprise history is a separate contract

`config/enterprise-contracts.json` pins the original installer hash and AppStore
capability for each reviewed version. v2.3.0, v2.3.1 and v2.3.2 require
`appstore.tar.gz` and the corresponding installer function. Authentic v2.2.5
enterprise archives contain neither. Their installer hash is
`6ca9e09b597d02bf48c26ad4fe8a886dcefbdcc0e1194aafeac2321f73e8e901`, matching
the historical `9a3f6c8c…` cohort. The builder preserves that absence instead of
injecting current AppStore behavior.

Both enterprise products retain the original inner directory and official
`upgrade.sh`. Original archives remain byte-identical. Enhanced archives retain
every original file except the reviewed Docker/IP installer patch, then add
the pinned Docker/Compose payloads. The community upgrade implementation is
never substituted for the enterprise upgrader. This contract does not establish
enterprise rollback support or a successful real upgrade.

## Corrected custom inputs

`config/upstream-validation.json` selects an immutable validation snapshot by
version. Every file in the selected snapshot is checked against its pinned
source manifest; unexpected files, symlinks, changed provenance and unreviewed
versions fail closed.

- v2.3.2 retains the `6e31c54e54fc0edf4450c78bad9868c2adbd9563` producer contract
  (including Node 22.14.0) that validated its existing corrected package bytes.
- v2.3.1, v2.3.0 and v2.2.5 use the reviewed
  `b6502c789507eaa6759c70dd853ace23a36a9012` upstream validator snapshot, including
  dependency-input locks, Node 22.22.1 and exact semantic configuration checks.
  Its Python YAML dependency is pinned to PyYAML 6.0.3 in an isolated CI venv.

The historical snapshot is copied from
`HandSonic/1Panel-Build-v2` at that exact commit. `SOURCE.json` records each copied
file SHA-256. The snapshot's broader configuration inventory does not enable
other versions: routing and release matrices must be reviewed separately.

Use the selected corrected upstream CI artifact with its exact run, artifact,
ZIP digest and build commit, or the canonical release only after upstream
promotion and byte verification. An old historical release passing gzip and ELF
checks is not a valid replacement for corrected binaries. Default release
downloads refresh the authoritative custom checksum before considering cache
reuse. Do not label locally downloaded public-release bytes as CI downloads.
Every custom archive, whether downloaded from a canonical release or imported
from verified CI, then passes its selected vendored package validator before
extraction or repacking. The downstream release gate rechecks pinned producer
metadata, GeoIP and installer-resource identities and the actual preserved
version-normalized control script. Self-consistent sidecars/manifests cannot
substitute an unreviewed Node/npm/Go toolchain or another installer cohort.

## Policy migration and publication order

These stronger shared validators intentionally change validation policy
fingerprints, including v2.3.2. Do not retain or fake the old fingerprint to make
the public no-op gate pass. Its old receipt must be backed up recoverably after
the exact existing package and checksum bytes pass fresh read-only validation.
Refreshing a receipt must not rebuild, rename, upload or replace current package
or checksum bytes, and must reject concurrent asset identity or digest changes.
Unrelated entries in the enterprise registry are excluded from a version's
fingerprint; changes to that version's contract are included.

Sequence each historical version independently:

1. Verify and promote the corrected upstream seven-architecture set.
2. Prepare and validate the complete downstream matrix from those exact inputs,
   with separate official and enterprise source locks.
3. Run applicable installation and upgrade/recovery gates against the exact
   prepared bytes. Published-package smoke tests do not validate unpublished
   package candidates.
4. Publish only through the explicitly selected, reviewed correction/repair
   process; retain recoverable originals and recheck the complete remote matrix.

Current v2.3.2 installation receipts cover twelve native combinations of custom,
official and enterprise-docker, amd64/arm64, and existing/fresh Docker. They do
not establish historical runtime passes, other native architectures, enforced
network isolation, upgrades or rollback. Enterprise-original needs its own
existing-Docker-only installation gate and must not be treated as containing
the enhanced Docker bundle.
