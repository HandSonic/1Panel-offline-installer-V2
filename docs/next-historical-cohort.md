# Prepared v2.2.4 through v2.2.1 contracts

These version contracts are source preparation, not native runtime receipts.
Each candidate still needs its complete hosted package and native gates before
the existing repair writer can publish it. No package download error authorizes
removing an expected source or architecture.

| Version | Archives | Package shards | Native rows | Enterprise |
| --- | ---: | ---: | ---: | --- |
| v2.2.4 | 13 | 13 | 8 | Explicitly unavailable at reviewed canonical endpoints |
| v2.2.3 | 17 | 15 | 12 | amd64 and arm64, original and Docker-enhanced |
| v2.2.2 | 17 | 15 | 12 | amd64 and arm64, original and Docker-enhanced |
| v2.2.1 | 17 | 15 | 12 | amd64 and arm64, original and Docker-enhanced |

Official sources have six architectures: amd64, arm64, armv7, ppc64le, s390x and
riscv64. Custom sources add loong64. The 2026-10-09 UTC official checksum GETs
and archive HEADs establish the advertised pins and byte lengths; this local
preparation did not download or fully inspect the 24 official archive bodies.
The package source gate must verify each archive's complete SHA-256 and length
before repacking, then validate the actual payload. Official checksum endpoints
are `https://resource.fit2cloud.com/1panel/package/v2/stable/VERSION/release/checksums.txt`.

`config/source-availability/v2.2.4.json` records exact UTC observations of HTTP
404 from the enterprise checksum GET and both amd64/arm64 archive HEADs at
`https://resource.fit2cloud.com/1panel/package/enterprise/stable/v2.2.4/release/`.
Its enterprise source lock is explicitly empty; there is no enterprise layout
contract or enterprise native row for that version. This means unavailable at
those reviewed endpoints, not proof that an enterprise release never existed.
The evidence and absence validator are included in that version's policy hash.

All six enterprise archives for v2.2.3, v2.2.2 and v2.2.1 were separately streamed
to compressed EOF. Their exact source hashes/lengths, gzip integrity, safe member
inventories, Core/Agent ELF/hash, and original installer/control resources were
verified without retaining large archives. None has an AppStore archive or
installation function. v2.2.3 enterprise uses installer
`9a3f6c8ccbbab2ba48d2aba3759ee999efa8b1f9`, while v2.2.3 custom uses
`4441fc3e546cf155ae1145e7d092909cbfc9476f`; their hashes must not be conflated.
v2.2.2 and v2.2.1 enterprise use the interactive
`2ac4eedaa295375bb5c3360d5b44ae0d50eb1e6c` cohort. Enhanced enterprise packages
preserve the original upgrader and all other original members, including the
root service aliases required by the upgrader. Original archives remain exact
vendor bytes. Static stream inspection is not an enterprise runtime pass.

## Immutable custom producer selection

- v2.2.4/v2.2.3 select `vendor/upstream-validation-historical-22-cli`, producer
  `6c6086a1722e0148fa9b74fc0a78963b4a70cdee`, tree
  `3c6189e9b8ccfbbfec111d78c4d91081162a6e94`. Go is 1.26.1.
- v2.2.2/v2.2.1 select `vendor/upstream-validation-historical-22-interactive`,
  producer `62214b921129d748dd42ed8224c6b73c380038e3`, tree
  `0ba81649aef2626170722a0e7e5701b37a0faec6`. Go is 1.25.10.
- Both use Node 22.22.1/npm 10.9.4 and each selected exact version's dependency,
  source, installer-resource, GeoIP and embedded-configuration contracts.
- Existing routes, vendor files and all four existing semantic policy hashes
  remain unchanged. v2.3.2 retains its frozen Node 22.14.0 producer expectation.
- v2.2.1 now has its own exact Core/Agent YAML input and registry entry. Historical
  source-provenance qualifications are preserved rather than upgraded to claims
  about unavailable original build logs.

The new snapshots were extracted from cached local git commits whose full trees
were previously verified against published producer trees. Local tree IDs and
every selected blob were recomputed. The original remote create/readback tool
responses were not persisted; their bindings are retained in the upstream task's
verified completion record, not represented as a fresh remote read. Every copied
file has a SHA-256 in `SOURCE.json`; each route pins that exact manifest. The full
referenced YAML inventory is preserved. Existing immutable snapshots were not
edited or repointed.

The native harness is unchanged. It already selects the real installer's CLI or
bounded PTY interface. v2.2.4 shares v2.2.5's CLI cohort; v2.2.3 has a different
CLI cohort; v2.2.2/v2.2.1 are interactive. Local shell/configuration and synthetic
package tests check those boundaries and preservation. Only successful hosted
installation of the exact candidate bytes establishes native coverage.
