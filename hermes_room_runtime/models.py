"""Small, dependency-free contracts shared by the runtime and its callers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PROMPT_BYTES = 256 * 1024
_MAX_EVIDENCE_BYTES = 1024 * 1024
_MAX_HUB_RESULT_BYTES = 256 * 1024
_MAX_HUB_RESULT_DEPTH = 12
_MAX_HUB_RESULT_FIELDS = 128
_MAX_HUB_RESULT_ITEMS = 256
_MAX_HUB_RESULT_STRING = 8192
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET_TEXT = re.compile(
    r"\b(?:Bearer\s+|eyJ[A-Za-z0-9_-]{8,}\.)[A-Za-z0-9._~+/=-]{12,}",
    re.IGNORECASE,
)
_FORBIDDEN_RESULT_KEYS = {
    "analysis_id",
    "api_key",
    "authorization",
    "chain_of_thought",
    "chainofthought",
    "cookie",
    "customer_id",
    "customer_name",
    "email",
    "name",
    "original_log",
    "original_logs",
    "password",
    "prompt",
    "raw_payload",
    "raw_log",
    "raw_logs",
    "secret",
    "session",
    "stderr",
    "stdout",
    "token",
    "tool_trace",
    "tool_traces",
    "video_id",
    "video_url",
}


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


def _validate_hub_result_value(value: Any, *, depth: int = 0) -> None:
    """Mirror the Evidence Hub Agent Jobs v1 structured-result boundary."""

    if depth > _MAX_HUB_RESULT_DEPTH:
        raise ValueError("result nesting exceeds the safe limit")
    if isinstance(value, dict):
        if len(value) > _MAX_HUB_RESULT_FIELDS:
            raise ValueError("result object has too many fields")
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_RESULT_KEYS or normalized.endswith(
                ("_email", "_token", "_url")
            ):
                raise ValueError("result contains a forbidden raw or identifying field")
            _validate_hub_result_value(nested, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > _MAX_HUB_RESULT_ITEMS:
            raise ValueError("result list has too many items")
        for item in value:
            _validate_hub_result_value(item, depth=depth + 1)
    elif isinstance(value, str):
        if len(value) > _MAX_HUB_RESULT_STRING:
            raise ValueError("result string is too long")
        if _EMAIL.search(value) or _SECRET_TEXT.search(value):
            raise ValueError("result contains customer or credential text")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("result numbers must be finite")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("result must contain JSON values only")


def validate_hub_result(value: dict[str, Any]) -> None:
    """Raise when a completion result would be rejected by Evidence Hub v1."""

    if not isinstance(value, dict):
        raise ValueError("result must be a JSON object")
    _validate_hub_result_value(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("result must be finite JSON") from exc
    if len(encoded) > _MAX_HUB_RESULT_BYTES:
        raise ValueError("result exceeds 256 KiB")


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
    evidence_sha256_override: str | None = None

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
        if (
            self.evidence_sha256_override is not None
            and _SHA256.fullmatch(self.evidence_sha256_override) is None
        ):
            raise ValueError("evidence_sha256_override must be a lowercase SHA-256")

    @property
    def evidence_bytes(self) -> bytes:
        return canonical_json(self.evidence)

    @property
    def evidence_sha256(self) -> str:
        return self.evidence_sha256_override or hashlib.sha256(self.evidence_bytes).hexdigest()


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
