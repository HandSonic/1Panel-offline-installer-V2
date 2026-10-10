# Native runtime validation

The main build workflow runs candidate native installation on disposable GitHub-hosted Linux amd64/arm64 runners. It authenticates the same-run plan, package producer and candidate bytes before safely unpacking. Local machines and self-hosted runners are rejected by the privileged harness guard.

Every installable native product requires existing and fresh Docker scenarios. Existing-Docker tests verify that a healthy daemon's version, process identity and executable hash are preserved. Fresh-Docker tests force use of bundled binaries and bind the running daemon and installed files to those bytes. Both execute bundled Compose, verify application files and live service executables, check the core version and probe local HTTP readiness without proxies or redirects.

The installer adapter is selected from the actual authenticated script interface. CLI option/password conventions must match supported semantics; older interactive installers use their own translation-key prompt meanings. Regional edition is preserved and recorded separately from package source. Synthetic credentials stay out of command arguments and artifacts. Installer logs are removed after execution.

Native community products also require predecessor-to-candidate upgrade checks. The predecessor has independently authenticated published evidence or supported source/bootstrap evidence. Historical source reading verifies exact immutable bytes through generic parsers and cannot register a version for production. Upgrade tests verify preserved configuration and running candidate files.

Manual repair runs may select `repair_predecessor` with an exact independent candidate identity at the same reviewed workflow commit. The authenticated `repair_predecessor` and `repair_operation` context must agree across the plan, preparation, input, actual native result and admission subject. The current target still requires both fresh/existing installation results, an actual predecessor installation, and real international-edition upgrade/rollback. Read-only recovery results cannot be relabeled into this context. A `validate-repair` artifact cannot authorize a `repair-existing` writer. See [the publication runbook](publication-runbook.md#explicit-predecessor-repair).

A native result proves only its exact package, architecture and scenario. Non-native architectures receive static coverage, enterprise originals receive vendor-byte identity coverage, and enterprise-docker retains its original enterprise upgrader. Native installation results do not establish every distribution, language or historical version. Network isolation is not enforced, and that limitation is recorded in results.

`test_historical_shell_flows.py` additionally exercises immutable content-addressed installer samples under command stubs and temporary filesystem roots. It covers prompts, terminal and fallback password input, invalid-input retries, CLI defaults, edition-dependent language, invalid options/values, call order and AppStore copying. Those controlled tests are separate from actual native service evidence.

The old separate published-package smoke workflow is retired because it could not validate the unpublished candidates produced by the main workflow.
