# Native candidate admission

Normal builds, validation-only runs and repairs use the same candidate gate. The gate consumes the exact aggregate/shard artifacts of the selected workflow, rather than downloading an unrelated previously published package.

The authenticated runtime plan defines requested products and native/upgrade expectations. Candidate preparation rechecks the workflow run, commit, attempt, successful producer job and upload log; exact artifact ID and ZIP digest; receipt and plan digests; bounded safe archive members; complete file inventories; and package byte hashes. The candidate schema requires the runtime plan and explicit branch accounting. Legacy schema-1 preparation has no production fallback.

For enterprise-docker, the candidate includes a nonpublishing original-archive companion. Its bytes must equal authenticated vendor discovery, and enhanced members must retain original enterprise bytes except for the declared installer change and added Docker resources.

Each successful native install result records its exact candidate archive, architecture and existing/fresh Docker scenario. Native community products also require the exact authenticated predecessor upgrade result. Acceptance rejects missing, duplicate, malformed, failed, cancelled or wrongly attributed evidence. Earlier package attempts may be reused only when tied to the same run/commit/plan; native results must satisfy the selected preparation and attempt rules.

Release writers receive the acceptance artifact and its receipt/native-control hashes through workflow outputs. They authenticate those controls again after obtaining the per-tag writer lock. Read-only validation has no release mutation step. The separate published-only smoke workflow and receipt refresh graph have been retired.

Content-addressed historical installer samples test parsing, interface recognition, patch preservation and safe stubbed shell behavior. They are not a production version whitelist or a claim that all historical binaries have passed native tests.

## Explicit read-only candidate predecessors

`workflow_dispatch` with operation `validate-repair` may supply the optional
`candidate_predecessor` input as one JSON object. Its exact fields are `version`,
`run_id`, `run_attempt`, `head_sha`, `controls_artifact_id`, `receipt_sha256`, and
`plan_sha256`. IDs and attempts are positive JSON integers. The commit is the
40-character lowercase Git SHA; both digests are lowercase SHA-256 values.
All pins describe one independently built downstream candidate, with canonical
release tag equal to its version. The selected predecessor must be numerically
strictly earlier in the same stable/beta/dev channel. There is no version map.

This mode is explicit. Omitting the input preserves the canonical public-release
path, including its hard failure for a missing, invalid, or cancelled receipt.
A candidate input failure never falls back to a public release or another run.
The candidate and target must use the same reviewed workflow commit. Cross-head
compatibility is deliberately unsupported: an old receipt's policy claim alone
cannot establish what code its workflow actually ran.

The candidate run must be a completed workflow-dispatch run. Its overall
conclusion may be failure because a seed has no authenticated predecessor of its
own, but cancellation, incomplete runs, and a superseding attempt are rejected.
For each requested official/custom amd64/arm64 row, the exact successful package
job, preparation job, small controls ZIP, selected shard ZIP, receipt, plan,
archive bytes, and successful hosted native *fresh* installation evidence must
all agree. Native evidence requires its exact successful job, hosted runner,
upload log, artifact digest, and result digest; a success claim in JSON is not
a trust root. Missing or expired evidence fails closed. No candidate's own
upgrade is inferred from its fresh installation.

The predecessor is independently checked against authenticated vendor/upstream
source bytes before installation. Those bytes must match that candidate's own
plan; its Docker/Compose dependencies also come from its own authenticated plan.
They are not replaced with the target's dependency pins. Official bytes are
fetched from the candidate's exact vendor pin without rediscovery. Custom CI
inputs are fetched using the candidate plan's exact upstream run, artifact ID,
ZIP hash and producer commit; their controls, raw archive records, immutable
configuration sources and package contents must match that candidate's contract.
For public custom input, only an immutable asset ID matching the candidate's raw
archive hash/size is used and rechecked. Neither route resolves a current tag's
contract. No historical repository scripts are downloaded or executed to perform
validation. Source-lock/schema consumption remains a separate prerequisite;
this mode does not relax source contract checks or make a missing source available.

The current native upgrade harness then actually installs those predecessor
bytes, observes the real service-start failure and rollback, and runs the
unchanged target upgrader with the existing persistence checks. Each upgrade
result retains its read-only candidate binding. A successfully rebuilt target
may provide its own separately authenticated fresh-install evidence to the next
candidate link. Each link independently installs and tests its predecessor.

The `read_only_recovery` selector is bound into the target plan and preparation
receipt. Acceptance preserves full authenticated native results and all failed
or missing rows in `read-only-native-recovery.json`, even if every native row
fails. This report contains no publishable final receipt or package admission.
The workflow retains it separately from `native-admitted-*` artifacts. Both
writer job conditions and Python writer/admission gates reject recovery mode,
including candidate upgrade evidence relabelled as ordinary admission. Any
publication migration requires a separate review.

This establishes only the tested rebuilt candidate bytes. It does not establish
that bytes in any old public release were safe, and it does not grant publication
eligibility. Synthetic local regression fixtures are not native acceptance.
