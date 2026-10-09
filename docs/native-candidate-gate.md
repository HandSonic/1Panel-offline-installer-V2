# Native acceptance of exact repair candidates

`validate-repair` and `repair-existing` share the same package construction,
17-archive static validation, and native candidate gate. After
`publication_prepare`, twelve disposable GitHub-hosted VMs cover custom,
official, and enterprise-with-Docker packages on amd64 and arm64, each with
existing and fresh Docker. At most two native jobs run concurrently.

Each native job downloads only its package shard and the small aggregate
controls. The candidate verifier binds artifact IDs, server and downloaded ZIP
hashes, successful producer logs, workflow run/commit/attempt, receipt hash,
matrix plan, and each selected archive's size/hash. Earlier successful shards
from the same run can be used only under the aggregation's latest-attempt rule;
their actual bytes still must match the current complete receipt. Inputs fail
closed before installation if any identity, required row, or digest differs.
For a native-only failed-job retry, the exact latest successful preparation may
come from an earlier attempt. Its supplied controls ID and receipt stay fixed;
shards are selected as of that preparation attempt, while the runtime receipt
records the new execution attempt separately. A newer controls artifact or
successful preparation makes the older candidate ineligible.

The existing strict package validator runs again before safe extraction. The
unchanged native harness then checks installed and live service binary hashes,
exact panel version, native architecture, bundled Docker/Compose integration,
and preservation of an existing Docker daemon. Every result is saved beside
its exact candidate provenance. Credentials and raw installer logs are excluded.

The recoverable repair writer requires the entire twelve-row matrix to succeed
and downloads the same aggregate artifact bound by that receipt. It does not
publish a later rebuild based only on a previous candidate's test result. A
failed, cancelled, skipped, or incomplete native matrix blocks that writer.
PR runs do not install candidates or write releases. Normal new-release build
and receipt-only migration graphs are unchanged by this repair-specific gate.

## Explicit coverage boundaries

- All 17 package archives retain static source, ELF architecture, Docker,
  enterprise-preservation, embedded configuration, and checksum validation.
- The other five custom CPU architectures have no native runtime result here.
- Enterprise-original archives stay byte-identical to the pinned vendor files.
  The enhanced-package harness cannot run them: they intentionally lack its
  offline manifest and bundled Docker/Compose. Separate amd64/arm64 tests with
  existing Docker must derive assertions from each vendor installer cohort;
  they must not patch the original archive or expect fresh offline Docker.
- The harness preserves historical interactive/noninteractive and regional
  installer differences; a current-version pass is not evidence for an older
  cohort until that cohort's exact candidate runs successfully.
- Network isolation, real upgrade, and rollback are not enforced or verified
  by this smoke gate and remain separate work.

## Separate original-enterprise acceptance plan

Use two additional disposable native VMs, one per architecture, with an already
running Docker daemon and Compose. Reuse the same shard and aggregate receipt
bindings but select the vendor-original archive; also verify its versioned
enterprise source lock and safe member inventory. Do not insert a manifest or
offline helpers into that archive. A separate thin harness may reuse the guarded
prompt/configuration, readiness, exact-version and live-process checks, deriving
expected core/agent hashes directly from the validated original inventory.

Record before/after Docker process identity and version, plus Compose version
and binary identity. The vendor script's existing-Docker path is the subject of
the test, so do not apply enhanced-package assertions that require installation
of our bundled Compose. Verify the expected AppStore state only when that
version's vendor contract includes it (v2.2.5 intentionally does not). Preserve
each cohort's original installer and upgrader bytes. No successful original
runtime result is implied until these separate tests actually execute.

## Extending historical cohorts without unnecessary receipt churn

Source locks and matrices are version-specific; embedded configuration and
enterprise capability facts select only the requested version. Adding unrelated
version entries leaves existing version fingerprints unchanged. Keep each
vendored producer snapshot immutable: add a new snapshot and routes for later
cohorts instead of replacing an old cohort's selected snapshot. Changes to shared
validator code intentionally change affected fingerprints and require genuine
revalidation of unchanged published bytes before any receipt-only refresh.
