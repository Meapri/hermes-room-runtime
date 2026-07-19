"""Bounded, read-only Evidence Hub loader.

Hub credentials stay in the trusted host process. Only the returned, size-limited
JSON bundle is staged for Hermes; the token and original request body are never
written into a room.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class HubApiError(RuntimeError):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(f"Evidence Hub request failed: status={status_code} code={code}")


def _validated_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    allowed = {"http", "https"} if loopback else {"https"}
    if (
        parsed.scheme not in allowed
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Hub URL must use HTTPS, except for explicit loopback addresses")
    return value.rstrip("/")


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError("Hub token file is unavailable") from exc
    if not 32 <= len(token) <= 256 or any(ch.isspace() for ch in token):
        raise ValueError("Hub token file does not satisfy the credential contract")
    return token


@dataclass(frozen=True)
class HubEvidenceLoader:
    base_url: str
    token_file: Path
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validated_base_url(self.base_url))
        if not 1 <= self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {_read_token(self.token_file)}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "hermes-room-runtime/0.3.0",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            ) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = "request-failed"
            try:
                problem = json.loads(exc.read(_MAX_RESPONSE_BYTES).decode("utf-8"))
                if isinstance(problem, dict) and isinstance(problem.get("code"), str):
                    code = problem["code"]
            except (UnicodeDecodeError, ValueError):
                pass
            raise HubApiError(exc.code, code) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HubApiError(0, "transport-failed") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise HubApiError(0, "response-too-large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HubApiError(0, "response-invalid") from exc
        if not isinstance(value, dict):
            raise HubApiError(0, "response-invalid")
        return value

    def status_bundle(
        self, environment: Literal["dev", "prod", "unknown"] = "prod"
    ) -> dict[str, Any]:
        status = self._request(
            "GET",
            "/api/status/v1/overview",
            query={"environment": environment},
        )
        return {
            "bundle_version": "actverse-hermes-evidence/1.0",
            "source_contract": "actverse-evidence-hub/status-v1",
            "environment": environment,
            "status": status,
        }

    def journey_bundle(
        self,
        *,
        environment: Literal["dev", "prod"],
        analysis_id: str,
        window_hours: int = 24,
        max_items: int = 200,
    ) -> dict[str, Any]:
        if not 1 <= window_hours <= 168:
            raise ValueError("window_hours must be between 1 and 168")
        if not 1 <= max_items <= 500:
            raise ValueError("max_items must be between 1 and 500")
        timeline = self._request(
            "POST",
            "/api/evidence/v1/journeys/timeline",
            payload={
                "environment": environment,
                "correlation_type": "analysis_id",
                "value": analysis_id,
                "window_hours": window_hours,
                "max_items": max_items,
            },
        )
        return {
            "bundle_version": "actverse-hermes-evidence/1.0",
            "source_contract": "actverse-evidence-hub/evidence-v1-journey",
            "environment": environment,
            "timeline": timeline,
        }


_TASK_KINDS = {
    "incident-diagnosis",
    "finding-triage",
    "proposal-draft",
    "scenario-authoring",
}
_ENVIRONMENTS = {"dev", "prod", "unknown"}


@dataclass(frozen=True)
class LeasedHubJob:
    job_id: str
    task_kind: str
    environment: Literal["dev", "prod", "unknown"]
    subject_ref: str
    options: dict[str, Any]
    lease_token: str

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> LeasedHubJob | None:
        job = payload.get("job")
        token = payload.get("lease_token")
        if job is None and token is None:
            return None
        if not isinstance(job, dict) or not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise HubApiError(0, "job-contract-invalid")
        try:
            job_id = str(UUID(str(job["job_id"])))
            task_kind = str(job["task_kind"])
            environment = str(job["environment"])
            subject_ref = str(job["subject_ref"])
            options = job["options"]
        except (KeyError, TypeError, ValueError) as exc:
            raise HubApiError(0, "job-contract-invalid") from exc
        if (
            task_kind not in _TASK_KINDS
            or environment not in _ENVIRONMENTS
            or not isinstance(options, dict)
            or not 3 <= len(subject_ref) <= 128
        ):
            raise HubApiError(0, "job-contract-invalid")
        return cls(
            job_id=job_id,
            task_kind=task_kind,
            environment=environment,  # type: ignore[arg-type]
            subject_ref=subject_ref,
            options=options,
            lease_token=token,
        )


@dataclass(frozen=True)
class HubAgentJobClient(HubEvidenceLoader):
    """Worker-only Agent Jobs v1 client.

    The consumer token and per-lease token remain in this trusted host object;
    neither one is included in the bounded evidence bundle staged for Hermes.
    """

    def lease(
        self,
        *,
        worker_id: str,
        task_kinds: list[str],
        lease_seconds: int = 120,
    ) -> LeasedHubJob | None:
        payload = self._request(
            "POST",
            "/api/agent/v1/jobs/lease",
            payload={
                "worker_id": worker_id,
                "task_kinds": task_kinds,
                "lease_seconds": lease_seconds,
            },
        )
        return LeasedHubJob.from_response(payload)

    def worker_heartbeat(
        self,
        *,
        worker_id: str,
        task_kinds: list[str],
        slots: int,
        runtime_version: str,
        heartbeat_ttl_seconds: int = 60,
    ) -> None:
        response = self._request(
            "POST",
            "/api/agent/v1/workers/heartbeat",
            payload={
                "worker_id": worker_id,
                "task_kinds": task_kinds,
                "slots": slots,
                "runtime_version": runtime_version,
                "heartbeat_ttl_seconds": heartbeat_ttl_seconds,
            },
        )
        worker = response.get("worker")
        if (
            response.get("api_version") != "agent-runtime-v1"
            or not isinstance(worker, dict)
            or worker.get("worker_id") != worker_id
        ):
            raise HubApiError(0, "runtime-heartbeat-contract-invalid")

    def heartbeat(
        self,
        job: LeasedHubJob,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> None:
        self._request(
            "POST",
            f"/api/agent/v1/jobs/{job.job_id}/heartbeat",
            headers={"X-Agent-Lease-Token": job.lease_token},
            payload={"worker_id": worker_id, "lease_seconds": lease_seconds},
        )

    def complete(
        self,
        job: LeasedHubJob,
        *,
        worker_id: str,
        status: Literal["succeeded", "inconclusive", "failed", "timed_out"],
        result: dict[str, Any],
        evidence_sha256: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/agent/v1/jobs/{job.job_id}/complete",
            headers={"X-Agent-Lease-Token": job.lease_token},
            payload={
                "worker_id": worker_id,
                "status": status,
                "result": result,
                "evidence_sha256": evidence_sha256,
                "error_code": error_code,
            },
        )
