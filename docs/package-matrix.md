# Runtime package preparation

The planner derives product membership from authenticated source contracts and current vendor observations. No per-application-version release matrix, source lock or validator registry exists in the repository. Docker and Compose pins are generic dependencies by architecture.

Official, custom, enterprise-original and enterprise-docker products use independent source/architecture jobs, with at most three package jobs per workflow run. Each enterprise-docker job supplies its authenticated original archive as a verification companion; this companion does not implicitly publish the enterprise-original product. Explicit successful/absent discovery and unresolved failures are distinct. A failed checksum request or archive read cannot silently remove a requested branch.

Each shard records exact bytes, workflow commit/run/attempt, product row and shared plan digest. Aggregation checks successful producer identity, exact artifact ZIP hashes, archive members, shard identity and hashes. Missing, extra, duplicate, corrupted or mismatched evidence fails closed. An authenticated failed branch is retained as failed, and successful branches continue independently. Zero accepted package branches cannot publish.

The plan also derives install rows for installable amd64/arm64 products in existing/fresh Docker scenarios, and upgrade rows for native community products. Native concurrency is limited to two per workflow run. Acceptance maps exact native/upgrade results to products; a failure blocks only products whose required coverage is missing or failed. Final receipts include all requested products, accepted products and failure accounting.

Pinned Docker/Compose caches are keyed by architecture and dependency hashes. Cache hits still pass byte-count and SHA-256 checks. Verified upstream CI input is authenticated once and shared with custom jobs. Neither a cache hit nor an earlier unrelated workflow is publication evidence.

Partial reruns may reuse an authenticated successful package artifact from an earlier attempt in the same run. They still require the same plan, commit and exact successful producer evidence. Native/admission records bind the preparation attempt and candidate bytes. Unexpected future attempts, changed controls or duplicate uploads are rejected.

Each scheduled run also checks the current release, including an unfinished draft. A missing or invalid package/checksum keeps the release eligible for retry. Each product job first attempts to reuse its exact existing archive bytes, bound to immutable GitHub asset IDs, size, SHA-256 and its own current source/dependency/implementation policy. A missing checksum can be regenerated from a validated archive. Reused archives still pass current static checks and the normal native acceptance gates; they are never treated as an earlier run's native-test evidence. Only absent or invalid candidates are rebuilt, avoiding timestamp-only conflicts with successful published archives. Recovery of another source or architecture does not invalidate an unrelated product's policy.

Normal builds and manual repair validation share this pipeline. Only the final normal-build or explicit repair job can write releases, under the resolved-tag lock described in [workflow concurrency](workflow-concurrency.md).
