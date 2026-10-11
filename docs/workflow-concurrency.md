# Independent runs and serialized release writers

Every workflow run/attempt has its own admission group. Push, pull request, schedule, normal manual builds and repair validation can prepare independently without replacing another pending run.

Only `build` and `publication_repair` can write release assets. Both hold `offline-release-<resolved release tag>` for the entire writer job, with `queue: max` and `cancel-in-progress: false`. The key is based on planner output and has no branch or operation component, so normal publication and repair of the same tag exclude each other across refs. Different tags remain independent.

Package jobs have `max-parallel: 3`; native jobs have `max-parallel: 2`, both per workflow run. These are not global account-wide resource reservations. Other runs and repositories can consume additional runner capacity. Observe actual queue times before increasing admission.

Each writer reauthenticates exact plan, receipt, accepted assets and native evidence after acquiring the lock. Normal publication uses a verified draft and explicit `make_latest: legacy`. Repairs retain backups and journaled rollback; their checksum switch is last. Locks do not make the GitHub multi-asset transition atomic. Review the journal and remote state after an interruption.

Every conditional pipeline job starts with `!cancelled()` and checks its required prerequisites. A failed independent branch may reach aggregation and acceptance for explicit accounting, while cancellation cannot authorize a writer. Only journal-upload steps retain `always()` so failure evidence can survive when runner shutdown permits.

When adopting the workflow, ensure writer-capable runs using older workflow revisions have finished before overlapping writers on the new policy. New locks cannot constrain old queued/running workflow bytes or legacy writers. Do not rerun retired CNB or receipt-only workflows as a substitute for current admission.

- [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [GitHub workflow cancellation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation)
