# Parallel versions and serialized release writers

Every workflow run/attempt has a unique admission group. Push, pull request,
schedule, normal manual build and both read-only modes can therefore start on
the same ref without replacing a different pending run. No temporary branch per
version is needed. The existing trigger and publication conditions are retained.

Only `build`, `publication_repair` and `publication_receipt_refresh` can write
releases. Each holds `offline-release-<resolved release tag>` for its whole job.
The normal writer takes the tag from `build_plan`, repair from `publication_plan`,
and receipt refresh from the successfully validated `publication_revalidate`
outputs. The key has no branch, operation, version-input or run component:
normal publishing, repair and refresh of the same canonical tag exclude each
other even across refs. Different tags can publish independently. GitHub group
names are case insensitive, so differently cased tag strings conservatively
share a lock too.

All writers use `queue: max` and `cancel-in-progress: false`. GitHub permits one
running and up to 100 pending jobs per group; a full queue rejects additional
jobs. The default `queue: single` would replace an older pending job even with
`cancel-in-progress: false`. We do not use that default for writers. Queuing is
FIFO by the time a job starts waiting on its group, not by workflow dispatch
order, so preparation speed can change writer order. This is mutual exclusion,
not a transaction or a guarantee that an older prepared candidate is newer.

Each writer still revalidates its exact input after obtaining the lock. A normal
writer refuses an already public release; a concurrent duplicate can therefore
finish with a safe failure. Receipt refresh verifies the unchanged public asset
snapshot and fails if a preceding repair changed it. Repair retains all package,
same-run receipt/candidate/native checks, recoverable backups, readback, rollback
and checksum-last switching. Read-only jobs do not hold the writer lock, and an
overlapping release change can invalidate their snapshot and require a fresh
read. Reuse already-passed read-only cohort evidence when deciding to dispatch;
the existing repair run's own mandatory gates still execute.

## Repository-wide state

Release assets and notes are scoped to the tag. Normal publishing additionally
affects the repository's latest-release selection, which tag locks cannot
serialize. Its final draft-to-public PATCH explicitly sets `make_latest: legacy`:
GitHub selects latest based on release creation date and higher semantic version,
instead of the newly published release's default `true`. This is GitHub's server
policy, not a claim that the highest numeric tag always wins. The workflow does
not implement a client-side read/compare/write of the shared latest pointer.
The verified draft ID/tag is used for that PATCH. Repair edits assets and notes
only, while receipt refresh edits assets only; neither explicitly changes latest.
Empty normal-build version inputs still resolve through the selected channel's
existing `/latest` endpoint before the tag is exported and used as a lock key.

Artifacts remain scoped to their run and attempt and are consumed through the
existing exact artifact/producer evidence checks. Docker and Compose cache keys
include OS, architecture and source-lock hashes. Concurrent cache misses may
duplicate downloads or contend while saving the same key; restored payloads
still undergo pinned size/SHA checks, and caches are never publication evidence.
The workflow does not share mutable workspace files across hosted runner jobs.

## Initial admission and capacity

Admit at most **three active version runs** in the repair cohort, with the existing
native matrix `max-parallel: 2` unchanged. That cohort can run at most **six native
jobs** simultaneously, and at most nine package-shard jobs (three per version).
This is an operator/caller admission rule, **not a repository-global six-job cap**.
Each job's matrix limit applies only to its own workflow run. Another manual
validation, the separate native smoke workflow or other repositories can consume
additional runner capacity. GitHub concurrency groups are single-writer locks,
not an arbitrary six-slot semaphore.

Before admitting/replacing a cohort run, inspect active and queued workflows,
count every nonterminal cohort version once and account for other native jobs.
Add a new version only after one cohort run is terminal; do not bypass the budget
with duplicate refs or attempts. Start with three when actual available hosted
capacity permits; use two versions/four native jobs if competing work or observed
runner queues warrant it. This operational bound requires all cohort dispatchers
to cooperate; the workflow alone cannot enforce a global limit across arbitrary
triggers. A strict global semaphore would require a separate coordinated design.

GitHub documents standard hosted account limits of 20 concurrent jobs on Free,
40 on Pro, 60 on Team and 500 on Enterprise. These are account-wide ceilings,
not a reservation for this repository or proof of immediately available arm64
capacity. Observe actual queue/start times and account workload before claiming
a measured speedup. This change adds no permissions or external locking service.

## Migrating the lock

The old workflow-level `offline-release` lock and new per-tag writer locks do not
exclude one another. Before first overlapping writers on this policy, verify
that **all writer-capable runs on the old workflow have terminated**, including
pending, queued, in-progress and waiting runs, on every ref and trigger type.
Do not dispatch or rerun old workflow revisions during the new-policy cohort.
An old run's successful earlier job is not evidence that its writer has drained.
Read-only old runs cannot mutate releases but count toward resource admission.

Even older workflows may have no lock at all; neither the old global group nor
the new tag groups protects against those writers. An explicitly targeted
historical repair can overlap a legacy writer only after its actual resolved
target is known and disjoint, with no shared latest mutation by the repair. A
queued legacy workflow that resolves `/latest` later does not meet that condition:
reading the endpoint now does not freeze what that run will resolve. Keep that
writer risk open until it terminates or its target is established. Deploying this
workflow cannot retroactively constrain runs using old workflow bytes.

Review and merge the workflow once, verify GitHub accepts the `queue: max` syntax
on the selected revision, then dispatch distinct versions on that same immutable
reviewed revision. If GitHub rejects the queue key, stop and correct the workflow;
do not silently fall back to lossy single-pending semantics. Preserve the existing
independent release-journal and canonical-byte readback before closing a repair.

Sources checked 2026-10-09:

- [GitHub concurrency and queuing](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [GitHub Actions limits](https://docs.github.com/en/actions/reference/limits)
- [Release API: make_latest](https://docs.github.com/en/rest/releases/releases#update-a-release)
- [Dependency cache behavior](https://docs.github.com/en/actions/concepts/workflows-and-actions/dependency-caching)
