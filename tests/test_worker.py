from __future__ import annotations

import asyncio
from dataclasses import dataclass

from hermes_room_runtime import HubJobWorker, JobResult, JobStatus, LeasedHubJob


def _leased_job() -> LeasedHubJob:
    return LeasedHubJob(
        job_id="018f7bc8-6bbd-7a00-8000-000000000099",
        task_kind="incident-diagnosis",
        environment="prod",
        subject_ref="status:prod:video-analysis",
        options={"locale": "ko", "force": False},
        lease_token="lease-token-that-is-long-enough-for-the-contract",  # noqa: S106
    )


class Client:
    def __init__(self, job: LeasedHubJob | None) -> None:
        self.job = job
        self.completed: dict | None = None
        self.heartbeat_count = 0

    def lease(self, **kwargs):
        return self.job

    def status_bundle(self, environment):
        assert environment == "prod"
        return {
            "bundle_version": "actverse-hermes-evidence/1.0",
            "status": {"overall": {"state": "unknown"}},
        }

    def heartbeat(self, *args, **kwargs):
        self.heartbeat_count += 1

    def complete(self, job, **kwargs):
        assert job.lease_token not in str(kwargs)
        self.completed = kwargs
        return {"job": {"status": kwargs["status"]}}


@dataclass
class Runtime:
    seen_request: object | None = None

    async def run(self, request):
        self.seen_request = request
        return JobResult(
            job_id=request.job_id,
            task_kind=request.task_kind,
            status=JobStatus.SUCCEEDED,
            result={"summary": "bounded diagnosis"},
            evidence_sha256=request.evidence_sha256,
            slot=0,
            duration_ms=25,
        )


def test_worker_leases_hub_job_runs_stateless_runtime_and_completes_hub_record() -> None:
    client = Client(_leased_job())
    runtime = Runtime()
    worker = HubJobWorker(client=client, runtime=runtime, worker_id="oracle-room-01")

    assert asyncio.run(worker.run_once()) is True

    assert runtime.seen_request is not None
    assert runtime.seen_request.job_id == _leased_job().job_id
    assert _leased_job().lease_token not in runtime.seen_request.prompt
    assert _leased_job().lease_token not in str(runtime.seen_request.evidence)
    assert client.completed is not None
    assert client.completed["status"] == "succeeded"
    assert client.completed["result"]["result"] == {"summary": "bounded diagnosis"}
    assert "stdout_tail" not in client.completed["result"]
    assert "stderr_tail" not in client.completed["result"]


def test_worker_returns_false_without_creating_runtime_work_when_queue_is_empty() -> None:
    client = Client(None)
    runtime = Runtime()
    worker = HubJobWorker(client=client, runtime=runtime, worker_id="oracle-room-01")

    assert asyncio.run(worker.run_once()) is False
    assert runtime.seen_request is None
    assert client.completed is None
