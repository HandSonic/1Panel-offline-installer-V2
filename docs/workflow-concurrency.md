# Independent runs and serialized release writers

Workflow runs prepare independently under GitHub's default concurrency behavior. Push and pull request runs execute regression and syntax checks. Scheduled builds, manual builds and manual repair validation must pass that same suite before planning, then pass their input, payload and native admission gates. They can prepare independently without replacing another pending run. Historical artifact transport uses the separate, manually triggered `recover-admitted-artifacts.yml` workflow with its fixed approved pins and read-only permissions. Markdown documentation may skip CI; JSON recovery inputs under `docs/` still require regression checks.

Only `publication_publish` can write release assets. Normal builds and explicit repairs share this writer, which holds `offline-release-<resolved release tag>` for the entire writer job, with `queue: max` and `cancel-in-progress: false`. The single `publication_plan` job resolves version/tag/mode and produces the authenticated plan; repair operations require an explicit version, while normal builds may resolve latest. The lock key is based on that planner output and has no branch or operation component, so normal publication and repair of the same tag exclude each other across refs. Different tags remain independent.

Package jobs have `max-parallel: 3`; native jobs have `max-parallel: 2`, both per workflow run. These are not global account-wide resource reservations. Other runs and repositories can consume additional runner capacity. Observe actual queue times before increasing admission.

The writer reauthenticates exact plan, receipt, accepted assets and native evidence after acquiring the lock. Normal publication uses a verified draft and explicit `make_latest: legacy`. Re-publication preflights all conflicts before uploading; differing or retired canonical assets require separate action-time deletion confirmation. Missing canonical files upload without renames or backups, with checksum sidecars last. Locks do not make the GitHub multi-asset transition atomic. Review the journal and remote state after an interruption.

Every conditional pipeline job starts with `!cancelled()` and checks its required prerequisites. A failed independent branch may reach aggregation and acceptance for explicit accounting, while cancellation cannot authorize a writer. Only journal-upload steps retain `always()` so failure evidence can survive when runner shutdown permits.

When adopting the workflow, ensure writer-capable runs using older workflow revisions have finished before overlapping writers on the new policy. New locks cannot constrain old queued/running workflow bytes or legacy writers. Do not rerun retired CNB or receipt-only workflows as a substitute for current admission.

- [GitHub concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [GitHub workflow cancellation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation)
