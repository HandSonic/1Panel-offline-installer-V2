# Bounded package preparation

Normal build and manual validate-repair/repair-existing paths prepare one source/architecture
per matrix job, with at most three concurrent runners. Enterprise original and
enhanced products share one architecture job so the original is downloaded once.
The v2.3.2 matrix therefore has 15 jobs producing 17 archives.

Every shard validates its package before upload and records its exact bytes,
workflow commit/run, requested row and shared plan hash. Aggregation rejects
missing, extra, duplicate or changed shards, regenerates flat checksums, and runs
the full application/Docker/Compose/installer/enterprise validator over all 17.
Only then is the publication receipt generated. One separate manual-only writer
retains the existing staged readback, backup and rollback behavior.

Pinned Docker/Compose inputs use an architecture/lock-hash cache. Cache hits still
pass source SHA/size checks; a cache is never publication evidence. Verified CI
upstream input is fetched once and shared only with custom package jobs. Public
release inputs retain their authoritative checksum checks.

Three concurrent jobs are a conservative starting point, not a measured speedup
guarantee. Available account runners, download throttling, cache misses and
artifact transfer costs determine actual wall time. Record plan/package/aggregate
job durations before increasing concurrency. Aggregate validation and publication
remain serial deliberately. Failed-job reruns reuse successful producer artifact IDs. Aggregation selects the
latest available attempt for each row within this workflow run and still requires
every shard to match the same plan hash, source identity and workflow commit. No
existing artifact is overwritten or deleted.

Normal version resolution/no-op checks run first. A required new build shares the
same plan, package matrix and aggregate gate, then a single draft publication job
revalidates the downloaded output before publishing. Manual repair retains its
separate same-release writer. PR checks and read-only validation use separate
concurrency groups; all public writers still share one release group. Historical
source and installer compatibility gates remain prerequisites; parallel execution
does not turn unreviewed versions into supported versions.
