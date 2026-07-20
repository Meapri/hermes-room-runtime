"""Opt-in Docker boundary smoke test.

Run with:
    HERMES_RUNTIME_LIVE_DOCKER=1 pytest -q tests/test_docker_live.py
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from hermes_room_runtime import HermesJobRuntime, JobRequest, JobStatus, RuntimeConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_RUNTIME_LIVE_DOCKER") != "1",
    reason="live Docker smoke is opt-in",
)


def test_live_docker_job_is_stateless_and_cleaned() -> None:
    shared_root = Path.cwd() / "runtime-data"
    shared_root.mkdir(exist_ok=True)
    test_root = Path(tempfile.mkdtemp(prefix="live-test-", dir=shared_root))
    config = test_root / "config.yaml"
    config.write_text("model: {}\n", encoding="utf-8")
    runtime = HermesJobRuntime(
        RuntimeConfig(
            image="hermes-room-runtime-fake:test",
            state_root=test_root / "state",
            slots=2,
            config_path=config,
            provider_env={"OPENAI_API_KEY": "live-smoke-secret"},
            network_mode="none",
            include_output_tails=True,
        )
    )

    async def exercise():
        requests = [
            JobRequest(
                f"job-{index}",
                "live-smoke",
                "write the requested result",
                {"status": index},
            )
            for index in range(2)
        ]
        try:
            assert await runtime.ensure() is True
            before = []
            for slot in range(2):
                code, stdout, _ = await runtime._docker(
                    "inspect",
                    "-f",
                    "{{.Id}}",
                    runtime._slot_name(slot),
                )
                assert code == 0
                before.append(stdout.strip())
            results = await asyncio.gather(*(runtime.run(request) for request in requests))
            after = []
            for slot in range(2):
                code, stdout, _ = await runtime._docker(
                    "inspect",
                    "-f",
                    "{{.Id}}",
                    runtime._slot_name(slot),
                )
                assert code == 0
                after.append(stdout.strip())
            return results, before, after
        finally:
            await runtime.shutdown()

    try:
        results, before, after = asyncio.run(exercise())
        assert [result.status for result in results] == [
            JobStatus.SUCCEEDED,
            JobStatus.SUCCEEDED,
        ], results
        assert {result.slot for result in results} == {0, 1}
        assert all(old != new for old, new in zip(before, after, strict=True))
        assert [result.result["evidence_state"] for result in results] == [0, 1]
        assert not list((test_root / "state" / ".exec-env").iterdir())
        jobs = (test_root / "state" / "slots").glob("*/jobs")
        assert all(not list(path.iterdir()) for path in jobs)
    finally:
        shutil.rmtree(test_root, ignore_errors=True)
