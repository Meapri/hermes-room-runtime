"""Evidence Hub Agent Jobs v1 worker for the isolated Hermes runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from .hub import HubAgentJobClient, HubApiError, LeasedHubJob
from .models import JobRequest, JobResult, JobStatus
from .runtime import HermesJobRuntime

log = logging.getLogger(__name__)

SUPPORTED_TASK_KINDS = (
    "incident-diagnosis",
    "finding-triage",
    "proposal-draft",
    "scenario-authoring",
)

_TASK_PROMPTS = {
    "incident-diagnosis": (
        "Diagnose the referenced status subject using only evidence.json. Return a concise JSON "
        "object with summary, confidence, reason_codes, findings, and recommended_actions."
    ),
    "finding-triage": (
        "Triage the referenced finding using only evidence.json. Return summary, severity, "
        "confidence, reason_codes, and recommended_actions as JSON."
    ),
    "proposal-draft": (
        "Draft a non-executing remediation proposal from evidence.json. Return summary, risks, "
        "steps, validation, and rollback as JSON. Do not modify repositories or deploy."
    ),
    "scenario-authoring": (
        "Draft a validation scenario from evidence.json. Return objective, preconditions, steps, "
        "expected_results, and safety_constraints as JSON. Do not execute the scenario."
    ),
}


@dataclass(frozen=True)
class HubJobWorker:
    client: HubAgentJobClient
    runtime: HermesJobRuntime
    worker_id: str
    task_kinds: tuple[str, ...] = SUPPORTED_TASK_KINDS
    lease_seconds: int = 120
    job_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not 3 <= len(self.worker_id) <= 128:
            raise ValueError("worker_id must contain between 3 and 128 characters")
        if not self.task_kinds or not set(self.task_kinds).issubset(SUPPORTED_TASK_KINDS):
            raise ValueError("task_kinds contains an unsupported job type")
        if not 30 <= self.lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 30 and 900")
        if not 1 <= self.job_timeout_seconds <= 1800:
            raise ValueError("job_timeout_seconds must be between 1 and 1800")

    async def _heartbeat_loop(self, job: LeasedHubJob, stopped: asyncio.Event) -> None:
        interval = max(10, self.lease_seconds // 3)
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                await asyncio.to_thread(
                    self.client.heartbeat,
                    job,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )

    @staticmethod
    def _completion_status(result: JobResult) -> str:
        if result.status is JobStatus.UNAVAILABLE:
            return "failed"
        return result.status.value

    async def _complete_failure(
        self,
        job: LeasedHubJob,
        *,
        error_code: str,
        summary: str,
    ) -> None:
        await asyncio.to_thread(
            self.client.complete,
            job,
            worker_id=self.worker_id,
            status="failed",
            result={
                "contract_version": "actverse-hermes-job-result/1.0",
                "summary": summary,
            },
            error_code=error_code,
        )

    async def run_once(self) -> bool:
        job = await asyncio.to_thread(
            self.client.lease,
            worker_id=self.worker_id,
            task_kinds=list(self.task_kinds),
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False

        stopped = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(job, stopped))
        try:
            try:
                evidence = await asyncio.to_thread(
                    self.client.status_bundle,
                    job.environment,
                )
            except HubApiError:
                await self._complete_failure(
                    job,
                    error_code="hub-evidence-unavailable",
                    summary="The bounded Evidence Hub status bundle was unavailable.",
                )
                return True

            prompt = (
                f"subject_ref={job.subject_ref}\n"
                f"locale={job.options.get('locale', 'ko')}\n"
                f"{_TASK_PROMPTS[job.task_kind]}"
            )
            runtime_result = await self.runtime.run(
                JobRequest(
                    job_id=job.job_id,
                    task_kind=job.task_kind,
                    prompt=prompt,
                    evidence=evidence,
                    timeout_seconds=self.job_timeout_seconds,
                )
            )
            completion_status = self._completion_status(runtime_result)
            await asyncio.to_thread(
                self.client.complete,
                job,
                worker_id=self.worker_id,
                status=completion_status,
                result=runtime_result.as_record(),
                evidence_sha256=runtime_result.evidence_sha256,
                error_code=(
                    None if completion_status == "succeeded" else runtime_result.error_code
                ),
            )
            return True
        finally:
            stopped.set()
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, HubApiError):
                await heartbeat

    async def serve(self, *, poll_seconds: float = 2.0) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        while True:
            try:
                handled = await self.run_once()
            except HubApiError as exc:
                log.warning(
                    "Hub worker request failed status=%s code=%s",
                    exc.status_code,
                    exc.code,
                )
                handled = False
            except Exception:
                # Do not include the exception text: provider and tool failures
                # can contain material that must not reach durable host logs.
                log.exception("Hub worker job failed", exc_info=False)
                handled = False
            if not handled:
                await asyncio.sleep(poll_seconds)
