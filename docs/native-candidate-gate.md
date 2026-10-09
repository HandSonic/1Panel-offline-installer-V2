# Native candidate admission

Normal builds, validation-only runs and repairs use the same candidate gate. The gate consumes the exact aggregate/shard artifacts of the selected workflow, rather than downloading an unrelated previously published package.

The authenticated runtime plan defines requested products and native/upgrade expectations. Candidate preparation rechecks the workflow run, commit, attempt, successful producer job and upload log; exact artifact ID and ZIP digest; receipt and plan digests; bounded safe archive members; complete file inventories; and package byte hashes. The candidate schema requires the runtime plan and explicit branch accounting. Legacy schema-1 preparation has no production fallback.

For enterprise-docker, the candidate includes a nonpublishing original-archive companion. Its bytes must equal authenticated vendor discovery, and enhanced members must retain original enterprise bytes except for the declared installer change and added Docker resources.

Each successful native install result records its exact candidate archive, architecture and existing/fresh Docker scenario. Native community products also require the exact authenticated predecessor upgrade result. Acceptance rejects missing, duplicate, malformed, failed, cancelled or wrongly attributed evidence. Earlier package attempts may be reused only when tied to the same run/commit/plan; native results must satisfy the selected preparation and attempt rules.

Release writers receive the acceptance artifact and its receipt/native-control hashes through workflow outputs. They authenticate those controls again after obtaining the per-tag writer lock. Read-only validation has no release mutation step. The separate published-only smoke workflow and receipt refresh graph have been retired.

Content-addressed historical installer samples test parsing, interface recognition, patch preservation and safe stubbed shell behavior. They are not a production version whitelist or a claim that all historical binaries have passed native tests.
