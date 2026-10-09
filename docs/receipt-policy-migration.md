# Revalidate unchanged published packages after a policy migration

Use the existing `Build 1Panel v2 Offline` workflow (`build-offline-v2.yml`).
These modes are specifically for a complete public release whose packages and
checksum bytes are already correct, but whose receipt has an older validator
policy fingerprint. They never rebuild or repackage archives.

A community `upgrade.sh` change is a payload correction: old bundled updater bytes
fail the reviewed-source check even with a self-consistent manifest. Rebuild those
community packages through the full candidate gate; do not use receipt refresh
to claim the correction is present. Enterprise passthrough bytes remain governed
by their separate vendor locks.

## Read-only validation

Dispatch `operation=validate-receipt`, an explicit `version` (for example
`v2.3.2`), and the same `release_tag`. No upstream CI inputs are used.

The separate `publication_revalidate` job has only `contents: read` and
`actions: read`. It downloads the complete canonical package set, the exact
published `checksums.txt`, and the previous receipt. It verifies:

- The release ID/tag, every canonical asset ID, size and server SHA-256.
- The prior receipt's identity, complete matrix, checksum bytes, successful
  original workflow/commit, and exact receipt SHA-256 from its validation log.
  Only a stale policy fingerprint is allowed; all other proof checks remain.
- All existing archive contents under the current complete validator contract.
- Unchanged remote identities and hashes again after validation.

The artifact contains the original package/checksum bytes and a new candidate
receipt bound to this run, commit and attempt. The original receipt bytes are
preserved as `control/previous-release-validation.json`. No release write job is
eligible in this mode, and no package-builder graph is eligible either.

## Explicit receipt refresh

After reviewing read-only results, dispatch `operation=refresh-receipt` for the
same version/tag. This starts a **fresh same-run read-only validation**, followed
by the separately gated `publication_receipt_refresh` job. It does not consume a
receipt or artifact from an earlier validation-only run. A retry with a new run
attempt must rerun read-only validation too; a previous-attempt proof is rejected.

The write job checks the exact candidate receipt hash from the read-only job,
its run/commit/attempt, prior receipt and complete input bytes, and current policy.
Any missing package, package/checksum byte change, asset ID replacement, remote
hash change, unexpected asset or stale proof rejects refresh before remote writes.

The existing backed-up repair writer receives the **full verified canonical
set**. Matching packages/checksums are skipped; a receipt-only guard forbids their
upload or rename. Only `release-validation.json` is staged, read back, backed up
and switched. The old receipt remains as `release-validation.json.backup-*`.
Package/checksum bytes and IDs, release notes, tags and upstream assets stay
unchanged. No assets are deleted, and `--clobber` is not used.

The workflow exports the receipt-refresh journal on success and failure. A
post-write journal-export failure is reported without failing an otherwise
successful publication run, so the published receipt remains verifiable. The
receipt writer itself must still succeed. If the journal
already exists, review it before retrying. Uncertain receipt rename results use
the existing observed-state rollback and preserve old/new bytes for recovery.

## Limits and failure handling

- This is a policy-only migration, not a substitute for correcting invalid
  archives. Validation failure must be investigated; do not rebuild merely to
  change the receipt. Existing `build`, `validate-repair` and `repair-existing`
  modes retain their separate meanings and write gates.
- Missing server digests or unavailable original validation logs fail closed.
  This mode cannot invent prior provenance or bless a missing/stale source.
- Package byte comparisons are exact, including original checksum line endings
  and ordering. A semantically equivalent rewritten checksum file is rejected.
- GitHub has no multi-asset compare-and-swap. Reads before every mutation catch
  observed concurrent drift, but cannot make the naming transition atomic.
  External changes during the transaction can require manual journal review;
  the guard will never repair package drift as part of a receipt refresh.
- A refreshed receipt is considered verified by the normal release checker only
  after its full workflow completes successfully. The read-only job emits the
  same exact receipt marker as other validation jobs and is an allowed receipt
  log producer (`publication_revalidate`).
- Local fake-release tests prove gates, byte preservation, drift rejection and
  rollback behavior. They do not claim live GitHub execution or real package
  revalidation. Those remain explicit execution steps for the release owner.
