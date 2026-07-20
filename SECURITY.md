# Security model

## Trust boundaries

- Space and the runtime host are trusted orchestration code.
- Evidence Hub responses, product repositories and prompts are untrusted agent input.
- Hermes and every command it produces are untrusted execution.
- Docker is a containment boundary, not a substitute for a dedicated VM where stronger tenant isolation is required.

The runtime never mounts the Docker socket, host home, SSH directory, GitHub credentials, cloud CLI state or Hub
token into a slot. Provider credentials exist only in the `docker exec` process environment and must be scoped to
the model proxy when possible.

The image keeps its non-root user. The runtime adds only the host worker's primary group to the container and uses
group-private job directories. A fixed post-run command assigns only `result.json` to that group with mode `0640`,
so the host and image user can exchange bounded data without running Hermes as root or making it world-readable.
After every acquired job, the runtime force-removes the entire slot container before deleting job state and creating
a fresh execution shell. A slot whose removal cannot be verified is quarantined and is not returned to the pool.

## Required production controls

1. Pin the runtime image by digest in `RuntimeConfig.image`.
2. Use a dedicated non-root Docker host or VM.
3. Put the model proxy in a dedicated internal Docker network, label it
   `com.actverse.hermes-egress=restricted-v1`, and set `HERMES_REQUIRE_RESTRICTED_NETWORK=true`. Deny arbitrary
   internet and host egress independently with the host firewall. The runtime verifies the network's internal flag,
   label and identity before creating a slot.
4. Use a Hub consumer token containing only `agent-jobs:work` plus the exact read scopes required by the
   host-side evidence loader. Never grant `agent-jobs:submit`, `agent-jobs:read`, or `sources:manage` to a worker.
5. Pass explicit read-only repository mounts. Never mount a repository parent wholesale.
6. Keep `retain_job_dirs=False`; run `prune_stale_jobs` after crash recovery.
7. Apply a host concurrency ceiling no larger than the configured slot count.
8. Treat `unknown`, missing evidence and stale evidence as uncertainty, never as an outage assertion.
9. Keep the runtime's local 256 KiB structured-result validator aligned with Hub. Invalid output is discarded and
   completed as a bounded `inconclusive` record; Hub remains the final enforcement boundary. Space should still
   render result fields defensively.
10. Require the lease-bound `agent-evidence-bundle-v1` contract. Missing, oversized, mismatched or invalid bundles
    become `inconclusive`; do not fall back to a broader status response. Honor the bundle policy by refusing to
    infer success whenever facts are empty or gaps/reason codes are present.

## Remaining risks

- A container with network access can exfiltrate provider credentials available during its turn.
- Read-only source code can still contain embedded secrets; repositories should be scanned before mounting.
- Docker Engine and the Linux kernel remain shared infrastructure across slots.
- The host copies optional Hermes OAuth state into a job when `auth_path` is configured. Prefer short-lived provider
  credentials and omit `auth_path` for service deployments.
- Agent output can be wrong even when the runtime and schema are correct. Evidence provenance and human review are
  separate controls.
