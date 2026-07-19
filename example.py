"""Minimal Actverse job example.

Build first:
    docker build -t hermes-room-runtime:0.3.0 .
"""

import asyncio
import os
from pathlib import Path

from hermes_room_runtime import HermesJobRuntime, JobRequest, RuntimeConfig


async def main() -> None:
    runtime = HermesJobRuntime(
        RuntimeConfig(
            image="hermes-room-runtime:0.3.0",
            state_root=Path("./runtime-data"),
            slots=2,
            config_path=Path("config.example.yaml"),
            provider_env={"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]},
            # Production should use a named network containing only the model proxy.
            network_mode="actverse-hermes-egress",
            read_only_mounts={
                str(Path("../actverse-api").resolve()): "/repos/actverse-api",
            },
        )
    )
    request = JobRequest(
        job_id="incident-42",
        task_kind="incident-diagnosis",
        prompt=(
            "Inspect the evidence bundle and mounted code. Write "
            '{"category":"...","confidence":0.0,"summary":"..."} to result.json.'
        ),
        evidence={
            "bundle_version": "actverse-hermes-evidence/1.0",
            "source_contract": "actverse-evidence-hub/status-v1",
            "status": {"overall": {"customer_state": "unknown"}},
        },
    )
    try:
        print(await runtime.run(request))
    finally:
        await runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
