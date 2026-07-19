"""Compatibility import for the pre-0.2 single-file package.

The Kakao-style persistent `HermesRoomManager` API was intentionally retired.
Use `HermesJobRuntime`: Actverse jobs must not resume another room's history.
"""

from hermes_room_runtime import (  # noqa: F401
    HermesJobRuntime,
    HubEvidenceLoader,
    JobRequest,
    JobResult,
    JobStatus,
    RuntimeConfig,
    RuntimeHealth,
)

__all__ = [
    "HermesJobRuntime",
    "HubEvidenceLoader",
    "JobRequest",
    "JobResult",
    "JobStatus",
    "RuntimeConfig",
    "RuntimeHealth",
]
