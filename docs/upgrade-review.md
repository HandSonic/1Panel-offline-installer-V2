# Offline upgrade review: community and enterprise

## Scope and source

The comparison uses the checksum-verified enterprise v2.3.2 archive's `upgrade.sh`. That script remains byte-for-byte upstream in the enterprise package. The repository's `upgrade_offline.sh` is the community script; it is not an enterprise replacement.

## Findings and decisions

| Area | Enterprise reference | Community correction |
| --- | --- | --- |
| Errors | `set -e`, explicit stop/start aggregation | Explicit stage results and a failure trap; never let the last successful service mask the first failure |
| Version | Installed `1pctl version`, target package assignment; channel/order guards | Read existing core and agent `SystemVersion` rows with SQLite read-only connections, never execute package binaries or assume enterprise CLI syntax; refuse inconsistent/missing rows, unsupported versions, stable/alpha/nightly switches, equal stable/alpha releases, and downgrades |
| Architecture | Host and package architecture validation | Inspect both ELF headers directly and compare class, endianness and machine to host architecture |
| Backups | Stop, then copy binaries, language, units, DB directory and GeoIP | Stop and verify both services inactive; copy every replacement target and the entire DB directory (including sidecars) into a unique protected backup; track absent paths too |
| Recovery | No complete automatic rollback transaction | Restore binaries, configuration, language, GeoIP, units and the complete DB directory before restarting old services; if stop or restore cannot be trusted, fail and require manual recovery |
| Configuration | Enterprise BASE_DIR/LANGUAGE replacement | Preserve community BASE_DIR, ORIGINAL_PORT, ORIGINAL_USERNAME, ORIGINAL_PASSWORD, ORIGINAL_ENTRANCE, LANGUAGE, PANEL_EDITION and CHANGE_USER_INFO as raw assignment text; callable regex replacement avoids backslash/backreference corruption; never source configuration; validate staged shell syntax |
| Database update | Enterprise `1panel update version ...` | Community parameterized SQLite updates, exactly one version row in each DB, no partial-success fallback; restore both databases after any failure and verify again after startup |
| Service managers | systemd only | Keep systemd, OpenRC and SysV adapters, with package `initscript/` then root fallback; retain installed units if package supplies none; no enable/boot-policy changes |
| Health | Both services, repeated bounded checks | Both services must pass two consecutive checks within 30 attempts; any start, reload, health or version-verification failure is fatal |
| App store | Optional enterprise offline app-store copy | No invented community app-store dependency or enterprise command port |
| Secrets | Summary fields only | No username/password/entrance output; configuration and DB exceptions are redacted; backup/stage directories are owner-only |

## Operational contract

Run as root on an existing community v2 installation. `python3` with the standard `sqlite3` module, Bash and `flock` are required before stopping services. Database version rows are the current-version authority because a control script's original installation version can be stale. The conservative requirement for matching core/agent version rows intentionally blocks damaged or unfamiliar database layouts rather than guessing. Alpha upgrades stay within alpha; nightly-to-nightly is allowed because its label has no ordering information. Signature/checksum provenance belongs to package acquisition; ELF validation is not a signature or proof that a binary can start.

A filesystem lock prevents concurrent runs of this community script. Staging and preflight precede downtime. SQLite databases are copied only after both services stop. Backups live in a uniquely named `upgrade-backup.*` directory beside the package and are retained for recovery; they contain sensitive data and must not be published. The script does not source configuration, run downloaded binaries during preflight, mutate application stores, or use enterprise-only CLI commands.

Rollback covers files replaced by this script and all files in the application's DB directory, including startup migrations there. It cannot undo external-system side effects or migrations outside that directory. A power failure/SIGKILL cannot trigger the shell's recovery trap. If rollback cannot stop services or restore a file, automatic restart is withheld and the protected backup path is reported. This is a guarded upgrade procedure, not a substitute for an independently verified system backup.

## Bounded regression method

`python3 -m unittest discover -s tests -p test_upgrade_flow.py -v` copies the script into a temporary fixture, rewrites all host filesystem destinations into that fixture, and replaces service management commands with stateful mocks. The root check is bypassed only in the temporary test copy. Minimal inert ELF fixtures are never executed. No installed or downloaded 1Panel binary, real service manager, or real host installer is run.

Cases cover success, both independent service-start failures, stop failure, daemon reload failure, language/GeoIP/config/unit copy failures, partial replacement, backup failure, one-database-only update failure, post-start version failure, failed health after a simulated SQLite migration, restore failure with services left stopped, missing/invalid current version, downgrade/equal/cross-channel refusal, malformed ELF rejection, preservation of secret special characters and raw geographic PANEL_EDITION assignments (cn/intl, quoted, unknown, absent old key, rollback and no evaluation), removal of stale language resources, absent-file rollback, protected backups, and OpenRC/SysV success. These are controlled flow regressions, not a claim of live-production service or ABI validation.

## Source verification and edition boundary

The community v2.3.2 source defines `SystemVersion` in both databases: [core initial settings migration](https://github.com/1Panel-dev/1Panel/blob/v2.3.2/core/init/migration/migrations/init.go) initializes it from `global.CONF.Base.Version`; [agent initial settings migration](https://github.com/1Panel-dev/1Panel/blob/v2.3.2/agent/init/migration/migrations/init.go) initializes it from `nodeInfo.Version`. These are community-source facts, not assumptions transferred from the enterprise upgrade command.

`PANEL_EDITION=cn` occurs in both enterprise and community control scripts; upstream's Edition setting is geographic and cannot identify the installed licensing/build variant. This updater is community-only and is bundled only by the community packaging branch. It does not claim a reliable automatic enterprise-installation detector: operators must choose the correct edition-specific package. Enterprise installations must use their preserved official `upgrade.sh`.

Architecture coverage includes amd64, arm64, armv7, ppc64le, s390x, riscv64 and loong64 (both `loongarch64` and `loong64` host aliases), with per-architecture matching and mismatched-endianness fixture tests. ARMv6 is rejected; an ARMv7 payload is not assumed compatible.

## Geographic edition preservation and package migration

Community upgrades preserve an installed `PANEL_EDITION` assignment verbatim,
including `cn`, `intl`, quotes or comments. It is a geographic setting, independent
of licensing and language. No region is inferred from `LANGUAGE`, package origin,
or a command execution. An older installation without that assignment retains
the package default; an unfamiliar existing assignment is preserved rather than
silently rewritten. The existing backup/rollback transaction covers this setting.

Earlier community updaters omitted this assignment and could report success after
replacing an installed `intl` control script with the packaged `cn` default.
The correction changes the bundled community `upgrade.sh` and the downstream
policy fingerprint for each selected version. The release validator now requires
the bundled community updater to equal the reviewed source, even when its own
manifest and archive checksum agree. Old affected community packages require
rebuilding and complete candidate validation/publication; a receipt-only refresh
cannot repair or certify their old bytes. Existing installation-only evidence
remains limited to its original scope and does not prove upgrade preservation.

Enterprise-original archives and enterprise-docker preserved upstream files stay
under their separate vendor-byte contracts. This community correction does not
replace either enterprise updater or change historical installer/region behavior.
