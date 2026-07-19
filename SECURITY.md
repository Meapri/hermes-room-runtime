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
setgid job directories so that the host worker and image user can exchange bounded inputs and results without
running Hermes as root or making the state tree world-readable.

## Required production controls

1. Pin the runtime image by digest in `RuntimeConfig.image`.
2. Use a dedicated non-root Docker host or VM.
3. Put the model proxy in a dedicated Docker network; deny arbitrary internet and host egress.
4. Use a Hub consumer token containing only `agent-jobs:work` plus the exact read scopes required by the
   host-side evidence loader. Never grant `agent-jobs:submit`, `agent-jobs:read`, or `sources:manage` to a worker.
5. Pass explicit read-only repository mounts. Never mount a repository parent wholesale.
6. Keep `retain_job_dirs=False`; run `prune_stale_jobs` after crash recovery.
7. Apply a host concurrency ceiling no larger than the configured slot count.
8. Treat `unknown`, missing evidence and stale evidence as uncertainty, never as an outage assertion.
9. Let Hub enforce the 256 KiB structured-result hygiene contract before a job becomes terminal. Space should
   still render result fields defensively.

## Remaining risks

- A container with network access can exfiltrate provider credentials available during its turn.
- Read-only source code can still contain embedded secrets; repositories should be scanned before mounting.
- Docker Engine and the Linux kernel remain shared infrastructure across slots.
- The host copies optional Hermes OAuth state into a job when `auth_path` is configured. Prefer short-lived provider
  credentials and omit `auth_path` for service deployments.
- Agent output can be wrong even when the runtime and schema are correct. Evidence provenance and human review are
  separate controls.
