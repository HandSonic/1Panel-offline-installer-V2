# Build and publication runbook

The supported entrypoint is `.github/workflows/build-offline-v2.yml`. Select the intended immutable workflow revision and use GitHub Actions **Run workflow**:

1. Choose `build` for a new release, `validate-repair` to inspect candidates without writing release assets, or `repair-existing` for the existing release being repaired.
2. Set `mode` to `stable`, `beta` or `dev`. Set the exact application `version` for repair operations. A normal build may leave it empty to resolve the channel's latest version.
3. Set `release_tag` when needed; an empty tag uses the resolved version.
4. Choose upstream `release`, or `verified-ci` with the exact upstream run ID, artifact ID, ZIP SHA-256 and build repository commit. CI input authentication binds the archive bytes to a successful producer job; an overall failed producer run is usable only with authenticated explicit branch outcomes.
5. Review each failed branch and the final accepted inventory. A passing unrelated product cannot substitute for a failed product's native or upgrade evidence.

## Public upstream packages

New upstream releases contain architecture packages and their `.sha256` files. A
`package-only-v1` manifest binds `build-metadata/resolved-source.json` and any
required frontend lock as non-executable, hash-covered archive members. The
reader trusts publication permissions in `HandSonic/1Panel-Build-v2`; it verifies
release/tag identity, GitHub asset IDs and full SHA-256, sidecars, archive member
hashes, ELF architecture, and immutable official installer/configuration bytes.
It does not query historical Actions logs for this format. A publisher able to
replace both package and checksum remains within this trust boundary.

One available architecture supplies the shared contract; each package job checks
its own pinned archive. Missing or changed sibling payloads fail only their own
products. Invalid shared contracts fail the custom origin. Existing releases
with canonical controls retain the older receipt validation path; migration
backs those controls up before activating new-format packages. Retained original
packages and controls remain necessary for historical predecessor recovery.

## Public offline packages

New lower releases expose only the accepted architecture archives and each
archive’s corresponding `.sha256` file. Repacked archives carry a small `publication` marker in their
existing `offline-manifest.json`: format, fixed publisher, release tag and the
current source/dependency/generator/validation policy fingerprint. Enterprise
original archives remain byte-for-byte vendor passthrough and use independently
known vendor hashes instead of an added marker.

Public readers trust publication permissions in
`HandSonic/1Panel-offline-installer-V2`, complete stable GitHub release/asset
identities, downloaded SHA-256 and package manifests. They do not authenticate
old Actions logs for this format. A publisher able to replace both the packages
and checksums is within this trust boundary. No-op requires the complete current
requested product inventory and current policy; a partial release remains
eligible for rebuilding failed products.

New-format predecessor selection verifies the selected package and records any
nearer product absences from the stable public inventory. Missing selected bytes
fail that product; an unrelated missing sibling cannot replace or block its
selected bytes. Original application bytes still have to match the package's
source pins, using exact vendor URLs or hash-matching retained upper assets.
Dependencies use the package's own canonical Docker/Compose pins and are checked
against bundled bytes. Missing original source bytes fail closed; neither current
locks nor a different upper generation are substitutes. The current native job
still installs that predecessor and runs actual target upgrade and rollback.

Canonical old JSON controls always select the strict legacy reader, including its
run/log checks. Canonical old JSON controls and `checksums.txt` are replacement conflicts:
the writer lists their exact identities and stops until they are separately
confirmed and removed. Historical backup readers remain supported. Internal final receipts, native acceptance,
exact acceptance-job authentication and writer admission remain unchanged in
Actions artifacts. Public package metadata is not a historical native-test claim.

## Shared pipeline

The planner resolves immutable source inputs, configuration and tools, verifies the selected upstream trust boundary, and discovers vendor availability. Its exact `plan.json` digest is passed through workflow outputs. Every subsequent consumer checks those exact bytes and rederives the inventory. A repository JSON file or historical version name never activates a fallback.

`package_matrix.py plan`, `shard` and `aggregate` implement the internal preparation phases. Package jobs are read-only toward releases. Each product has its own branch and immutable upload receipt. Aggregation authenticates run/attempt/commit, producer logs, ZIP bytes, payload bytes and companion original enterprise bytes, then records both success and failure outcomes.

`runtime_publication.py status` verifies an existing release against the current source, generator, validation and native-gate policy. New-format packages use their SHA-covered policy marker; canonical legacy controls still require their original receipt authentication.

The daily schedule repeats this check even after a partial release. Product jobs reuse individually validated current archives and rebuild missing or invalid ones, so an earlier successful package keeps its exact compressed bytes. A missing `.sha256` is filled from the validated archive. Existing conflicting bytes still require the separate review described below. Draft discovery is explicit to publication/retry clients: they locate the unique exact tag and bind its numeric release ID. Newly created drafts are bound to the POST response ID immediately; unpublished drafts are never read through the published-tag endpoint or accepted as public predecessor sources.

The shared native jobs install accepted installable products on amd64/arm64 with both existing and fresh Docker. Community products also run predecessor-to-candidate upgrades. `runtime_publication.py accept` binds those results to the exact plan and producer artifacts. Enterprise originals qualify by authenticated vendor-byte identity; unsupported native architectures retain an explicit static-only coverage category.

The final workflow writer invokes `runtime_publication.py publish` with the accepted artifact ID, receipt SHA-256 and native-acceptance SHA-256. It reauthenticates all inputs after acquiring the resolved-tag lock. These internal commands require the workflow-bound environment and immutable evidence; the workflow supplies the complete arguments.

## Safe re-publication

`repair-existing` uses the same preparation and native admission path as normal builds. Before uploading anything, the writer compares the complete canonical asset inventory with the admitted files. Identical names and bytes are a no-op. A differing same-name asset or any canonical item to retire stops the entire write with journal phase `confirmation_required`; the journal lists each exact asset ID, name, size and SHA-256. It does not rename, back up, clobber or delete assets, and it does not change release notes.

Obtain action-time confirmation before permanently deleting the exact conflicting assets through a separate supported flow. The journal itself is not deletion approval. After that deletion, rerun the same workflow: missing canonical packages upload normally, followed by their SHA sidecars, with server-digest and downloaded-byte verification. New versions use this same add-only path. An interrupted or uncertain upload leaves any uploaded canonical bytes in place; inspect the journal and remote state, then rerun to verify existing bytes and fill only missing names. GitHub does not provide an atomic multi-asset upload.

`validate-repair` has no release writer. A validated candidate alone is not permission to publish. Publication authorization and workflow permissions remain separate from byte validation.

Normal `build` publication isolates canonical conflicts by product (archive plus
its SHA sidecar). It retains conflicting remote bytes, uploads other independently
accepted products, and records `partial`, exact conflict identities and completed
products in the journal. Actions still reports the conflict. A draft containing
an unverified conflict stays private after safe uploads; it cannot expose those
conflicting files by becoming public. Shared legacy controls still block a partial
protocol migration, and explicit `repair-existing` retains its strict preflight.
All writes use the bound numeric Release ID and upload readback uses the returned
asset ID. Failures report a fixed stage/error code without printing credentials,
signed URLs or raw subprocess stderr.

### Explicit predecessor repair

When canonical public predecessor authentication is blocked, a manual `validate-repair` or `repair-existing` invocation may explicitly set `repair_predecessor`. Supply the same bounded JSON selector shape documented for `candidate_predecessor`: `version`, `run_id`, `run_attempt`, `head_sha`, `controls_artifact_id`, `receipt_sha256`, and `plan_sha256`. Both inputs are mutually exclusive. The predecessor must be numerically earlier in the same channel and independently built at the exact reviewed target workflow head. This is never an automatic fallback from an invalid public receipt.

The new target run builds and validates its own packages, authenticates the predecessor controls/shard/source/dependency pins and exact successful fresh-install evidence, then actually installs that predecessor and runs the unchanged target's international-edition upgrade and real rollback harness. A read-only candidate may supply its independently authenticated package and fresh-install evidence; its recovery report cannot supply the new target's admission.

The plan, preparation receipt, native upgrade input/result, and final admission subject bind `repair_predecessor` together with `repair_operation`. Finalization recomputes admission using the authenticated current manual identity. `validate-repair` can retain a final candidate artifact, but its operation cannot be relabeled to authorize a writer. Only the current `repair-existing` invocation can send its own successfully admitted final artifact through the existing locked, add-only writer. Ordinary build, push and schedule paths reject the repair input. Native failures suppress the affected product; shared identity, cancellation or hash failures block repair admission. Enterprise-original byte-identity passthrough is unchanged.

Existing public repaired receipts remain valid legacy predecessor inputs when their exact successful acceptance-job markers and non-cancelled producer identity authenticate their bytes. Newly repaired releases use the package-only public reader above, while writer admission still requires the exact internal acceptance controls. Raw report claims never substitute for writer authentication. The permanently read-only `candidate_predecessor` path still produces no publication admission.

The old receipt-only revalidation/refresh modes and standalone `repair_release.py`, `create_validation_receipt.py` and `check_release_state.py` entrypoints are retired. `manual_publication.py` now contains only shared CI authentication and isolated validation helpers. Old schema-1 receipts may be read through explicitly historical verification APIs for authenticated evidence/bootstrap; they cannot authorize a current publication or no-op.

The former CNB recipes and separate published-only native smoke workflow are retired. The main GitHub workflow owns candidate testing and publication. Historical implementations remain in Git history. No CNB account, settings, tags or release assets are changed by retiring those repository files.

## Verification boundaries

Local regression tests inspect synthetic archives/contracts and historical installer text, plus controlled shell flows. They do not prove a live release, real downloaded application execution, CNB behavior or full historical native coverage. Only exact successful native results from the workflow provide native evidence for their own inputs and scenarios.
