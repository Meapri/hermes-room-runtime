"""Evidence Hub Agent Jobs v1 worker for the isolated Hermes runtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from dataclasses import dataclass

from .hub import HubAgentJobClient, HubApiError, LeasedHubJob
from .models import JobRequest, JobResult, JobStatus, validate_hub_result
from .runtime import HermesJobRuntime

log = logging.getLogger(__name__)
RUNTIME_VERSION = "0.4.0"


def _safe_exception_location(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    return " > ".join(f"{frame.name}:{frame.lineno}" for frame in frames[-4:]) or "unavailable"


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

_TASK_RESULT_FIELDS = {
    "incident-diagnosis": {
        "summary",
        "confidence",
        "reason_codes",
        "findings",
        "recommended_actions",
        "missing_evidence",
    },
    "finding-triage": {
        "summary",
        "severity",
        "confidence",
        "reason_codes",
        "recommended_actions",
        "missing_evidence",
    },
    "proposal-draft": {
        "summary",
        "risks",
        "steps",
        "validation",
        "rollback",
        "missing_evidence",
    },
    "scenario-authoring": {
        "objective",
        "preconditions",
        "steps",
        "expected_results",
        "safety_constraints",
        "missing_evidence",
    },
}


@dataclass(frozen=True)
class HubJobWorker:
    client: HubAgentJobClient
    runtime: HermesJobRuntime
    worker_id: str
    task_kinds: tuple[str, ...] = SUPPORTED_TASK_KINDS
    lease_seconds: int = 120
    job_timeout_seconds: float = 300.0
    runtime_heartbeat_seconds: float = 20.0
    runtime_heartbeat_ttl_seconds: int = 60

    def __post_init__(self) -> None:
        if not 3 <= len(self.worker_id) <= 128:
            raise ValueError("worker_id must contain between 3 and 128 characters")
        if not self.task_kinds or not set(self.task_kinds).issubset(SUPPORTED_TASK_KINDS):
            raise ValueError("task_kinds contains an unsupported job type")
        if not 30 <= self.lease_seconds <= 900:
            raise ValueError("lease_seconds must be between 30 and 900")
        if not 1 <= self.job_timeout_seconds <= 1800:
            raise ValueError("job_timeout_seconds must be between 1 and 1800")
        if not 5 <= self.runtime_heartbeat_seconds <= 120:
            raise ValueError("runtime_heartbeat_seconds must be between 5 and 120")
        if not 30 <= self.runtime_heartbeat_ttl_seconds <= 300:
            raise ValueError("runtime_heartbeat_ttl_seconds must be between 30 and 300")
        if self.runtime_heartbeat_ttl_seconds < self.runtime_heartbeat_seconds * 2:
            raise ValueError("runtime heartbeat TTL must cover at least two heartbeat intervals")

    async def _heartbeat_loop(self, job: LeasedHubJob, stopped: asyncio.Event) -> None:
        interval = max(10, self.lease_seconds // 3)
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    await asyncio.to_thread(
                        self.client.heartbeat,
                        job,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                except HubApiError as exc:
                    log.warning(
                        "Hub job heartbeat failed status=%s code=%s",
                        exc.status_code,
                        exc.code,
                    )

    async def _runtime_heartbeat_loop(self, stopped: asyncio.Event) -> None:
        while not stopped.is_set():
            try:
                announced = await self._announce_runtime()
                if not announced:
                    log.warning("Hermes runtime is not ready; availability heartbeat skipped")
            except HubApiError as exc:
                log.warning(
                    "Hub runtime heartbeat failed status=%s code=%s",
                    exc.status_code,
                    exc.code,
                )
            except Exception as exc:
                log.error(
                    "Hermes runtime heartbeat failed error_type=%s location=%s",
                    type(exc).__name__,
                    _safe_exception_location(exc),
                )
            try:
                await asyncio.wait_for(
                    stopped.wait(),
                    timeout=self.runtime_heartbeat_seconds,
                )
            except TimeoutError:
                continue

    async def _announce_runtime(self) -> bool:
        if not await self.runtime.ensure():
            return False
        health = await self.runtime.health()
        if health.running_slots < 1:
            return False
        await asyncio.to_thread(
            self.client.worker_heartbeat,
            worker_id=self.worker_id,
            task_kinds=list(self.task_kinds),
            slots=health.running_slots,
            runtime_version=RUNTIME_VERSION,
            heartbeat_ttl_seconds=self.runtime_heartbeat_ttl_seconds,
        )
        return True

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

    async def _complete_inconclusive(
        self,
        job: LeasedHubJob,
        *,
        error_code: str,
        summary: str,
        missing_evidence: list[str] | None = None,
        evidence_sha256: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self.client.complete,
            job,
            worker_id=self.worker_id,
            status="inconclusive",
            result={
                "contract_version": "actverse-hermes-job-result/1.0",
                "summary": summary,
                "missing_evidence": missing_evidence or [error_code],
            },
            evidence_sha256=evidence_sha256,
            error_code=error_code,
        )

    @staticmethod
    def _safe_completion(
        result: JobResult,
        *,
        allowed_result_fields: set[str],
    ) -> tuple[str, dict, str | None]:
        status = HubJobWorker._completion_status(result)
        error_code = None if status == "succeeded" else result.error_code
        record = result.as_record()
        try:
            if result.result is not None and not set(result.result).issubset(allowed_result_fields):
                raise ValueError("result contains task-incompatible fields")
            validate_hub_result(record)
        except ValueError:
            status = "inconclusive"
            error_code = "result-policy-rejected"
            record = {
                "contract_version": "actverse-hermes-job-result/1.0",
                "job_id": result.job_id,
                "task_kind": result.task_kind,
                "status": status,
                "result": None,
                "evidence_sha256": result.evidence_sha256,
                "runtime": {
                    "slot": result.slot,
                    "duration_ms": result.duration_ms,
                    "returncode": result.returncode,
                    "error_code": error_code,
                    "timed_out": result.timed_out,
                },
            }
            validate_hub_result(record)
        return status, record, error_code

    async def run_once(self) -> bool:
        # Do not acquire a durable lease unless Docker and the restricted network
        # contract are ready. This keeps a bad host configuration fail-closed.
        if not await self.runtime.ensure():
            return False
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
                    self.client.evidence_bundle,
                    job,
                    worker_id=self.worker_id,
                )
            except HubApiError:
                # A missing or incompatible lease-bound endpoint must not silently
                # broaden the evidence scope by falling back to the status overview.
                await self._complete_inconclusive(
                    job,
                    error_code="hub-evidence-unavailable",
                    summary="The lease-bound Evidence Hub bundle was unavailable.",
                )
                return True

            policy = evidence["policy"]
            gaps = evidence["gaps"]
            reason_codes = evidence["reason_codes"]
            facts = evidence["facts"]
            allowed_result_fields = set(policy["allowed_result_fields"])
            if allowed_result_fields != _TASK_RESULT_FIELDS[job.task_kind]:
                await self._complete_inconclusive(
                    job,
                    error_code="hub-evidence-contract-invalid",
                    summary="The evidence bundle result policy did not match the task contract.",
                    evidence_sha256=evidence["bundle_sha256"],
                )
                return True
            has_required_gap = policy["must_be_inconclusive_with_gaps"] and bool(
                gaps or reason_codes
            )
            has_no_facts = policy["must_be_inconclusive_without_facts"] and not facts
            if has_required_gap or has_no_facts:
                await self._complete_inconclusive(
                    job,
                    error_code="hub-evidence-incomplete",
                    summary="The lease-bound evidence requires an inconclusive result.",
                    missing_evidence=list(dict.fromkeys(reason_codes or gaps))
                    or ["evidence-bundle-empty"],
                    evidence_sha256=evidence["bundle_sha256"],
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
                    evidence_sha256_override=evidence["bundle_sha256"],
                )
            )
            completion_status, completion_record, completion_error = self._safe_completion(
                runtime_result,
                allowed_result_fields=allowed_result_fields,
            )
            await asyncio.to_thread(
                self.client.complete,
                job,
                worker_id=self.worker_id,
                status=completion_status,
                result=completion_record,
                evidence_sha256=runtime_result.evidence_sha256,
                error_code=completion_error,
            )
            return True
        except HubApiError:
            raise
        except Exception as exc:
            # Provider/tool exception text can contain customer material. Record
            # only bounded operational metadata in host logs and Hub state.
            log.error(
                "Hub worker leased job failed error_type=%s location=%s",
                type(exc).__name__,
                _safe_exception_location(exc),
            )
            await self._complete_failure(
                job,
                error_code="worker-internal-error",
                summary="The isolated worker could not complete the job.",
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
        runtime_stopped = asyncio.Event()
        runtime_heartbeat = asyncio.create_task(
            self._runtime_heartbeat_loop(runtime_stopped),
            name="hub-runtime-heartbeat",
        )
        concurrency = max(1, self.runtime.config.slots)
        active: set[asyncio.Task[bool]] = set()
        try:
            while True:
                while len(active) < concurrency:
                    active.add(asyncio.create_task(self._run_once_guarded()))
                done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                handled = any(task.result() for task in done)
                if not handled:
                    await asyncio.sleep(poll_seconds)
        finally:
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            runtime_stopped.set()
            runtime_heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime_heartbeat
            await self.runtime.shutdown()

    async def _run_once_guarded(self) -> bool:
        try:
            return await self.run_once()
        except HubApiError as exc:
            log.warning(
                "Hub worker request failed status=%s code=%s",
                exc.status_code,
                exc.code,
            )
        except Exception as exc:
            # Provider/tool failures can contain material that must not reach host logs.
            log.error(
                "Hub worker job failed error_type=%s location=%s",
                type(exc).__name__,
                _safe_exception_location(exc),
            )
        return False
