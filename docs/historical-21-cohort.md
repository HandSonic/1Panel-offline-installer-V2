# Prepared v2.1.13, v2.1.12 and v2.1.11 contracts

These contracts activate source selection and validation planning for three
historical versions. They are not native installation receipts. Each version
has 13 archives and 13 package shards: six official architectures (amd64, arm64,
armv7, ppc64le, s390x and riscv64) and seven corrected custom architectures (the
same six plus loong64). Its eight native cases are official/custom × amd64/arm64
× existing/fresh Docker. The existing package and native candidate workflows
consume these inventories without a workflow or harness change.

The official source locks contain the exact vendor checksum, URL and length.
The 2026-10-09 UTC source audit completely streamed the official and custom
amd64/arm64 inputs, matching complete archive SHA-256 and length, gzip integrity,
member safety, executable Core/Agent ELF targets and actual binary hashes.
Official armv7, ppc64le, s390x and riscv64 pins were established from vendor
checksums and HTTP metadata; their complete bytes still require the package
source gate. Custom releases have the corrected seven-architecture producer
receipts. Static input inspection does not establish an offline installation.

Each enterprise source lock is explicitly empty. The accompanying
`config/source-availability/VERSION.json` preserves the exact checksum GET and
amd64/arm64 archive HEAD 404 observations accepted by the existing absence
validator. The source audit additionally observed HEAD and GET 404 for all seven
canonical enterprise architectures and both HEAD/GET 404 for official loong64.
This means unavailable at those reviewed canonical endpoints, not proof that an
enterprise edition never existed. Empty enterprise matrix keys are not added.
No download failure changes expected package membership.

## Immutable producer and historical configuration

All three versions select `vendor/upstream-validation-historical-21-interactive`
at producer `8e8ffd07a00c2660b559e999846b6bf6d82fc152`, tree
`117123fb751dba5130b0d6079fd5388641772bc8`. Its toolchain is Go 1.25.7,
Node 22.22.1 and npm 10.9.4. The selected source commits are:

- v2.1.13: `7159aca226c172cf9360a93d37e10413d1dc4276`
- v2.1.12: `c392b72470a268f4720d7b68544720bf8c64fae8`
- v2.1.11: `79703e3c71b47040fa3d89abed475d94ecdeb367`

v2.1.13/v2.1.12 use installer `81a7964ac739d5be7b7099505666dd6f6c9a0544`;
v2.1.11 uses `1da5a4178bdaac17037536b732970d16df0ceec0`. The producer used
reviewed deterministic frontend lock repairs. Its source contracts pin original
dependency inputs; this does not claim access to original historical build logs.
The embedded configuration entries retain their source-provenance qualification.
The six older Core/Agent YAML templates do not contain `is_enterprise`; no newer
field is injected during normalization.

The snapshot was extracted from cached local commit
`eb56c91331eef8b9971cbf55723b229a24599f54`, which resolves to the producer's exact
full tree. The prior publication binding records the matching remote tree,
remote commit creation and branch readback on 2026-10-09T05:32:10Z. This patch
recomputes the local commit/tree objects and every selected blob, and records
each copied file's SHA-256 in `SOURCE.json`. It does not present that retained
binding as a fresh remote fetch. No existing immutable snapshot or route is
changed. Background v2.1.10 YAML entries in the immutable snapshot do not activate
that version; it still has no downstream route, matrix or source locks.

## Preserve the actual installer and source differences

All 12 audited native inputs contain the same interactive `install.sh`, SHA-256
`6c460520dff0fe895fec037bd7a6e332cbbe78c56e0edbbd147c552881c5bd8b`. It reads
`.selected_edition` with default `cn`, has five configuration prompts, no extra
edition prompt, no AppStore installation and no builtin Docker-tree helper. The
existing whole-function Docker patch handles it unchanged and idempotently.

v2.1.12 official retains the older 13,142-byte `1pctl` and all five older language
files. Custom retains its 13,198-byte control and newer language files. Only
custom inputs are bound to the selected producer's normalized control/resource
contract; official inputs use their own whole-archive locks. The packager patches
`install.sh` and preserves each source's control and language bytes. The test
fixtures retain both actual v2.1.12 controls, which are read but never executed.
v2.1.13 and v2.1.11 resources match across sources after control-version
normalization. The production patcher, native harness and validators are
unchanged, as are all eight previous version policy fingerprints.

Before publication, every version still needs all 13 exact offline candidates
to pass the complete static package gate and all eight native installation rows
against those exact candidate bytes. No native installation, CI dispatch or
publication is performed by this patch. Network isolation and real
upgrade/rollback remain pending separate work; static tests do not establish
them as passed or add them as publication prerequisites.
