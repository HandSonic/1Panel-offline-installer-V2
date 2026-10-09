# Build and publication runbook

The supported entrypoint is `.github/workflows/build-offline-v2.yml`. Select the intended immutable workflow revision and use GitHub Actions **Run workflow**:

1. Choose `build` for a new release, `validate-repair` to inspect candidates without writing release assets, or `repair-existing` for the existing release being repaired.
2. Set `mode` to `stable`, `beta` or `dev`. Set the exact application `version` for repair operations. A normal build may leave it empty to resolve the channel's latest version.
3. Set `release_tag` when needed; an empty tag uses the resolved version.
4. Choose upstream `release`, or `verified-ci` with the exact upstream run ID, artifact ID, ZIP SHA-256 and build repository commit. CI input authentication binds the archive bytes to a successful producer job; an overall failed producer run is usable only with authenticated explicit branch outcomes.
5. Review each failed branch and the final accepted inventory. A passing unrelated product cannot substitute for a failed product's native or upgrade evidence.

## Shared pipeline

The planner resolves immutable source inputs, configuration and tools, authenticates producer evidence, and discovers vendor availability. Its exact `plan.json` digest is passed through workflow outputs. Every subsequent consumer checks those exact bytes and rederives the inventory. A repository JSON file or historical version name never activates a fallback.

`package_matrix.py plan`, `shard` and `aggregate` implement the internal preparation phases. Package jobs are read-only toward releases. Each product has its own branch and immutable upload receipt. Aggregation authenticates run/attempt/commit, producer logs, ZIP bytes, payload bytes and companion original enterprise bytes, then records both success and failure outcomes.

`runtime_publication.py status` verifies an existing release against the current source, generator, validation and native-gate policy. A stale or unverifiable receipt cannot become a current no-op by changing its fingerprint.

The shared native jobs install accepted installable products on amd64/arm64 with both existing and fresh Docker. Community products also run predecessor-to-candidate upgrades. `runtime_publication.py accept` binds those results to the exact plan and producer artifacts. Enterprise originals qualify by authenticated vendor-byte identity; unsupported native architectures retain an explicit static-only coverage category.

The final workflow writer invokes `runtime_publication.py publish` with the accepted artifact ID, receipt SHA-256 and native-acceptance SHA-256. It reauthenticates all inputs after acquiring the resolved-tag lock. These internal commands require the workflow-bound environment and immutable evidence; the workflow supplies the complete arguments.

## Recoverable repair

`repair-existing` uses the same preparation and admission path as normal builds. The writer stages admitted replacements, verifies server digests and downloaded bytes, retains old assets under recoverable backup names, and switches checksums last. No release asset is deleted. The journal records each transition. Inspect the journal and actual remote assets before retrying an interrupted repair; GitHub does not provide an atomic multi-asset transaction.

`validate-repair` has no release writer. A validated candidate alone is not permission to publish. Publication authorization and workflow permissions remain separate from byte validation.

The old receipt-only revalidation/refresh modes and standalone `repair_release.py`, `create_validation_receipt.py` and `check_release_state.py` entrypoints are retired. `manual_publication.py` now contains only shared CI authentication and isolated validation helpers. Old schema-1 receipts may be read through explicitly historical verification APIs for authenticated evidence/bootstrap; they cannot authorize a current publication or no-op.

The former CNB recipes and separate published-only native smoke workflow are retired. The main GitHub workflow owns candidate testing and publication. Historical implementations remain in Git history. No CNB account, settings, tags or release assets are changed by retiring those repository files.

## Verification boundaries

Local regression tests inspect synthetic archives/contracts and historical installer text, plus controlled shell flows. They do not prove a live release, real downloaded application execution, CNB behavior or full historical native coverage. Only exact successful native results from the workflow provide native evidence for their own inputs and scenarios.
