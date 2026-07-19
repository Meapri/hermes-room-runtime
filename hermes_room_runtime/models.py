"""Small, dependency-free contracts shared by the runtime and its callers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_MAX_PROMPT_BYTES = 256 * 1024
_MAX_EVIDENCE_BYTES = 1024 * 1024


class JobStatus(StrEnum):
    SUCCEEDED = "succeeded"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"


def canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must be JSON serializable") from exc


@dataclass(frozen=True)
class JobRequest:
    """One stateless Hermes job.

    `evidence` is already-bounded Hub output supplied by the trusted host. Provider
    credentials are deliberately not part of this serializable contract.
    """

    job_id: str
    task_kind: str
    prompt: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        for label, value in (("job_id", self.job_id), ("task_kind", self.task_kind)):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"{label} must match {_SAFE_ID.pattern}")
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if len(self.prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise ValueError("prompt exceeds the 256 KiB runtime limit")
        if not 1 <= self.timeout_seconds <= 1800:
            raise ValueError("timeout_seconds must be between 1 and 1800")
        if len(self.evidence_bytes) > _MAX_EVIDENCE_BYTES:
            raise ValueError("evidence exceeds the 1 MiB runtime limit")

    @property
    def evidence_bytes(self) -> bytes:
        return canonical_json(self.evidence)

    @property
    def evidence_sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes).hexdigest()


@dataclass(frozen=True)
class JobResult:
    job_id: str
    task_kind: str
    status: JobStatus
    result: dict[str, Any] | None
    evidence_sha256: str
    slot: int
    duration_ms: int
    returncode: int | None = None
    error_code: str | None = None
    timed_out: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""

    def as_record(self) -> dict[str, Any]:
        """Return the bounded record suitable for a future Hub-owned job API.

        Process output tails are intentionally excluded because they may contain
        provider or tool diagnostics that do not belong in durable storage.
        """

        return {
            "contract_version": "actverse-hermes-job-result/1.0",
            "job_id": self.job_id,
            "task_kind": self.task_kind,
            "status": self.status.value,
            "result": self.result,
            "evidence_sha256": self.evidence_sha256,
            "runtime": {
                "slot": self.slot,
                "duration_ms": self.duration_ms,
                "returncode": self.returncode,
                "error_code": self.error_code,
                "timed_out": self.timed_out,
            },
        }


@dataclass(frozen=True)
class RuntimeHealth:
    ready: bool
    configured_slots: int
    running_slots: int
    reason: str | None = None
