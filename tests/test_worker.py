from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hermes_room_runtime import HubApiError, HubJobWorker, JobResult, JobStatus, LeasedHubJob
from hermes_room_runtime.worker import _idle_poll_delay, _safe_exception_location


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
        self.runtime_heartbeat: dict | None = None

    def lease(self, **kwargs):
        return self.job

    def evidence_bundle(self, job, *, worker_id):
        assert job.environment == "prod"
        assert worker_id == "oracle-room-01"
        return {
            "api_version": "agent-evidence-bundle-v1",
            "bundle_sha256": "b" * 64,
            "policy": {
                "allowed_result_fields": [
                    "summary",
                    "confidence",
                    "reason_codes",
                    "findings",
                    "recommended_actions",
                    "missing_evidence",
                ],
                "must_report_gaps": True,
                "must_be_inconclusive_with_gaps": True,
                "must_be_inconclusive_without_facts": True,
            },
            "facts": [{"kind": "journey-status"}],
            "gaps": [],
            "reason_codes": [],
        }

    def heartbeat(self, *args, **kwargs):
        self.heartbeat_count += 1

    def complete(self, job, **kwargs):
        assert job.lease_token not in str(kwargs)
        self.completed = kwargs
        return {"job": {"status": kwargs["status"]}}

    def worker_heartbeat(self, **kwargs):
        self.runtime_heartbeat = kwargs


@dataclass
class Runtime:
    seen_request: object | None = None
    running_slots: int = 2
    shutdown_called: bool = False

    def __post_init__(self):
        self.config = SimpleNamespace(slots=2)

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

    async def ensure(self):
        return True

    async def health(self):
        return SimpleNamespace(
            ready=self.running_slots == 2,
            configured_slots=2,
            running_slots=self.running_slots,
            reason=None if self.running_slots == 2 else "slots-not-ready",
        )

    async def shutdown(self):
        self.shutdown_called = True


class FailingRuntime(Runtime):
    async def run(self, request):
        self.seen_request = request
        raise RuntimeError("customer-looking-sensitive-runtime-detail")


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
    assert client.completed["evidence_sha256"] == "b" * 64
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


def test_worker_does_not_lease_when_runtime_preflight_fails() -> None:
    class NotReadyRuntime(Runtime):
        async def ensure(self):
            return False

    class MustNotLease(Client):
        def lease(self, **kwargs):
            raise AssertionError("a fail-closed runtime must not acquire a lease")

    worker = HubJobWorker(
        client=MustNotLease(_leased_job()),
        runtime=NotReadyRuntime(),
        worker_id="oracle-room-01",
    )
    assert asyncio.run(worker.run_once()) is False


def test_missing_lease_bound_bundle_closes_job_as_inconclusive_without_running() -> None:
    class OldHubClient(Client):
        def evidence_bundle(self, job, *, worker_id):
            raise HubApiError(404, "request-failed")

    client = OldHubClient(_leased_job())
    runtime = Runtime()
    worker = HubJobWorker(client=client, runtime=runtime, worker_id="oracle-room-01")

    assert asyncio.run(worker.run_once()) is True
    assert runtime.seen_request is None
    assert client.completed is not None
    assert client.completed["status"] == "inconclusive"
    assert client.completed["error_code"] == "hub-evidence-unavailable"
    assert client.completed["result"]["missing_evidence"] == ["hub-evidence-unavailable"]


@pytest.mark.parametrize(
    "facts,gaps,reason_codes",
    [
        ([], ["evidence-bundle-empty"], ["evidence-bundle-empty"]),
        ([{"kind": "journey-status"}], ["recent-evidence-missing"], []),
        ([{"kind": "journey-status"}], [], ["evidence-truncated"]),
    ],
)
def test_bundle_policy_closes_gaps_as_inconclusive_without_running_hermes(
    facts: list,
    gaps: list[str],
    reason_codes: list[str],
) -> None:
    class GapClient(Client):
        def evidence_bundle(self, job, *, worker_id):
            bundle = super().evidence_bundle(job, worker_id=worker_id)
            return {**bundle, "facts": facts, "gaps": gaps, "reason_codes": reason_codes}

    client = GapClient(_leased_job())
    runtime = Runtime()
    worker = HubJobWorker(client=client, runtime=runtime, worker_id="oracle-room-01")

    assert asyncio.run(worker.run_once()) is True
    assert runtime.seen_request is None
    assert client.completed is not None
    assert client.completed["status"] == "inconclusive"
    assert client.completed["error_code"] == "hub-evidence-incomplete"
    assert client.completed["evidence_sha256"] == "b" * 64
    assert client.completed["result"]["missing_evidence"] == list(
        dict.fromkeys(reason_codes or gaps)
    )


@pytest.mark.parametrize(
    "unsafe_result",
    [
        {"email": "person@example.com"},
        {"findings": ["x" * 8000 for _ in range(40)]},
        {"summary": "bounded", "debug": "not allowed for this task"},
    ],
)
def test_worker_replaces_hub_incompatible_result_with_bounded_inconclusive_record(
    unsafe_result: dict,
) -> None:
    class UnsafeRuntime(Runtime):
        async def run(self, request):
            return JobResult(
                job_id=request.job_id,
                task_kind=request.task_kind,
                status=JobStatus.SUCCEEDED,
                result=unsafe_result,
                evidence_sha256=request.evidence_sha256,
                slot=0,
                duration_ms=25,
            )

    client = Client(_leased_job())
    worker = HubJobWorker(client=client, runtime=UnsafeRuntime(), worker_id="oracle-room-01")

    assert asyncio.run(worker.run_once()) is True
    assert client.completed is not None
    assert client.completed["status"] == "inconclusive"
    assert client.completed["error_code"] == "result-policy-rejected"
    assert client.completed["result"]["result"] is None
    assert str(unsafe_result) not in str(client.completed)


def test_worker_marks_unexpected_runtime_failure_terminal_without_waiting_for_lease_expiry(
    caplog,
) -> None:
    client = Client(_leased_job())
    runtime = FailingRuntime()
    worker = HubJobWorker(client=client, runtime=runtime, worker_id="oracle-room-01")

    assert asyncio.run(worker.run_once()) is True

    assert client.completed is not None
    assert client.completed["status"] == "failed"
    assert client.completed["error_code"] == "worker-internal-error"
    assert client.completed["result"] == {
        "contract_version": "actverse-hermes-job-result/1.0",
        "summary": "The isolated worker could not complete the job.",
    }
    assert "customer-looking-sensitive-runtime-detail" not in caplog.text
    assert _leased_job().lease_token not in caplog.text


def test_worker_advertises_only_slots_that_are_actually_running() -> None:
    client = Client(None)
    runtime = Runtime(running_slots=1)
    worker = HubJobWorker(client=client, runtime=runtime, worker_id="oracle-room-01")

    assert asyncio.run(worker._announce_runtime()) is True
    assert client.runtime_heartbeat is not None
    assert client.runtime_heartbeat["slots"] == 1
    assert client.runtime_heartbeat["runtime_version"] == "0.5.0"


def test_serve_runs_up_to_configured_slot_count_concurrently() -> None:
    reached = asyncio.Event()
    release = asyncio.Event()
    state = {"active": 0, "maximum": 0}

    class ConcurrentWorker(HubJobWorker):
        async def run_once(self) -> bool:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            if state["active"] == 5:
                reached.set()
            try:
                await release.wait()
                return True
            finally:
                state["active"] -= 1

    async def scenario() -> None:
        runtime = Runtime()
        runtime.config.slots = 5
        worker = ConcurrentWorker(client=Client(None), runtime=runtime, worker_id="oracle-room-01")
        serving = asyncio.create_task(worker.serve(poll_seconds=0.1))
        await asyncio.wait_for(reached.wait(), timeout=2)
        assert state["maximum"] == 5
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
        assert runtime.shutdown_called is True

    asyncio.run(scenario())


def test_safe_exception_location_never_includes_exception_message() -> None:
    try:
        raise RuntimeError("credential-looking-sensitive-value")
    except RuntimeError as exc:
        location = _safe_exception_location(exc)

    assert "test_safe_exception_location" in location
    assert "credential-looking-sensitive-value" not in location


def test_idle_poll_delay_keeps_empty_queue_request_rate_stable_with_more_slots() -> None:
    assert _idle_poll_delay(4, 1) == 4
    assert _idle_poll_delay(4, 5) == 20
    assert _idle_poll_delay(4, 32) == 60
